# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in framebuffer walkthrough with live reads and display-only staging."""

import hashlib
import json
import math
import struct
from pathlib import Path


class UiCapture:
    """Run on the DPG thread; invalid Apply inputs stop at the parser before any writer."""

    def __init__(self, dpg, app, directory):
        self.dpg, self.app = dpg, app
        self.directory = Path(directory)
        self.directory.mkdir(exist_ok=False)
        self.active = True
        self.pending = None
        self.wait = 3
        self.frames = 0
        self.snapshot = None
        self.panel = None
        self.live_scroll = None
        self.live_original = {}
        self.manifest = {"schema": 1, "state": "capturing", "captures": [], "skipped": [], "errors": [],
                         "bounds_provenance": getattr(app, "_power_slider_baseline", None)}
        self._save()
        try:
            self.snapshot = {"tab": dpg.get_value("tabs"), "scroll": dpg.get_y_scroll("page_control"),
                             "clocks": dpg.get_value("ctl_clocks")}
            if all(dpg.does_item_exist(tag) for tag in ("sl_pl", "in_pl")):
                config = dpg.get_item_configuration("sl_pl")
                self.snapshot.update(slider=dpg.get_value("sl_pl"), input=dpg.get_value("in_pl"),
                                     minimum=config["min_value"], maximum=config["max_value"])
            dpg.set_value("tabs", "tab_control")
            dpg.set_y_scroll("page_control", 0)
            self.stages = ["control-actual"]
            style = getattr(app, "_power_slider_style", None)
            bounds = getattr(style, "original_bounds", None)
            provenance = self.manifest["bounds_provenance"] or {}
            if ("slider" in self.snapshot and bounds is not None and len(bounds) == 2 and
                    all(math.isfinite(v) for v in bounds) and bounds[0] < bounds[1] and
                    provenance.get("source") in ("live-bios", "receipt-original") and
                    type(provenance.get("minimum_mw")) is int and type(provenance.get("maximum_mw")) is int and
                    bounds == (provenance["minimum_mw"] / 1000, provenance["maximum_mw"] / 1000)):
                self.bounds = bounds
                self.stages += ["original-upper", "extended-in-range", "extended-above"]
            else:
                self.bounds = None
                self.manifest["skipped"].append("Power range stages: original BIOS bounds or slider unavailable.")
            panel = getattr(app, "_registry_panel", None)
            headers = ("registry_header", "reg_rel_live_header", "reg_constants_live_header")
            self.panel = panel if panel and all(dpg.does_item_exist(tag) for tag in headers) else None
            if self.panel:
                self.snapshot["headers"] = {tag: dpg.get_value(tag) for tag in headers}
                self.live_original = {kind: dpg.get_value("reg_" + kind + "_live_select")
                                      for kind in ("rel", "constants")}
                self.stages.append("live-prepare")
                self.manifest["read_cache_note"] = "Live reads retain their normal panel caches and initial values."
            else:
                self.manifest["skipped"].append("Live panels: registry panel or headers unavailable.")
            self.stages.append("restored-display")
            self.stage = 0
            self._save()
        except Exception as exc:
            self._finish(exc)

    def _save(self):
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")

    def _restore_widgets(self):
        if self.snapshot is None:
            return
        dpg, saved = self.dpg, self.snapshot
        dpg.set_value("ctl_clocks", saved["clocks"])
        if self.panel:
            for kind in ("rel", "constants"):
                choices = getattr(self.panel, kind + "_live_choices")
                if choices:
                    selected = self.live_original.get(kind)
                    dpg.set_value("reg_" + kind + "_live_select", selected if selected in choices else next(iter(choices)))
                    getattr(self.panel, "select_" + kind + "_live")()
            for tag, value in saved.get("headers", {}).items():
                dpg.set_value(tag, value)
        self._restore_power()

    def _restore_power(self):
        saved, dpg = self.snapshot, self.dpg
        if saved and "slider" in saved and dpg.does_item_exist("sl_pl") and dpg.does_item_exist("in_pl"):
            dpg.configure_item("sl_pl", min_value=saved["minimum"], max_value=saved["maximum"])
            dpg.set_value("sl_pl", saved["slider"])
            dpg.set_value("in_pl", saved["input"])
            self.app.refresh_power_slider_style()

    def _prepare_live(self):
        self._restore_power()
        dpg = self.dpg
        dpg.set_value("registry_header", True)
        cases = []
        for kind in ("rel", "constants"):
            try:
                getattr(self.panel, "refresh_" + kind + "_live")()
                choices = getattr(self.panel, kind + "_live_choices")
                if getattr(self.panel, kind + "_live_stale"):
                    raise RuntimeError("read is stale")
                if kind == "rel":
                    cases.extend(("rel", label, row) for label, row in choices.items())
                else:
                    for role in ("OV", "ALT", "VMIN"):
                        matches = [(kind, label, row) for label, row in choices.items() if row.get("role") == role]
                        cases.extend(matches)
                        if not matches:
                            self.manifest["skipped"].append("Live " + role + ": no supported rows.")
                        if role == "VMIN":
                            for rail in (0, 1):
                                if not any(row.get("rail") == rail for _, _, row in matches):
                                    self.manifest["skipped"].append("Live VMIN rail " + str(rail) + ": unavailable.")
                if not choices:
                    self.manifest["skipped"].append("Live " + kind + ": " + dpg.get_value("reg_" + kind + "_live_status"))
            except Exception as exc:
                self.manifest["skipped"].append("Live " + kind + " read failed: " + str(exc))
        self.live_cases = {}
        new_stages = []
        for kind, label, row in cases:
            name = "live-" + kind + "-" + str(row["rail"]) + ("-" + row["role"].lower() if kind == "constants" else "")
            self.live_cases[name] = (kind, label, False)
            new_stages.append(name)
        for kind in ("rel", "constants"):
            match = next(((label, row) for k, label, row in cases if k == kind), None)
            if match:
                name = "live-" + kind + "-invalid"
                self.live_cases[name] = (kind, match[0], True)
                new_stages.append(name)
        self.stages[self.stage:self.stage + 1] = new_stages
        return bool(new_stages)

    def _stage_live(self, name):
        kind, label, invalid = self.live_cases[name]
        dpg = self.dpg
        for header in ("reg_rel_live_header", "reg_constants_live_header"):
            dpg.set_value(header, header == "reg_" + kind + "_live_header")
        dpg.set_value("reg_" + kind + "_live_select", label)
        getattr(self.panel, "select_" + kind + "_live")()
        if invalid:
            dpg.set_value("reg_" + kind + "_live_value", "not a number")
            getattr(self.panel, "apply_" + kind + "_live")()
        dpg.set_value("ctl_clocks", "Developer capture: " + name)
        self.live_scroll = "reg_" + kind + "_live_header"

    def _stage(self, name):
        dpg = self.dpg
        if name == "live-prepare":
            if not self._prepare_live():
                return self._stage(self.stages[self.stage])
            name = self.stages[self.stage]
        if name.startswith("live-"):
            self._stage_live(name)
            return True
        if name == "restored-display":
            self._restore_widgets()
            dpg.set_value("tabs", self.snapshot["tab"])
            dpg.set_y_scroll("page_control", self.snapshot["scroll"])
        else:
            lo, hi = self.bounds
            step = max(1.0, (hi - lo) * 0.05)
            maximum = max(self.snapshot["maximum"], hi + step)
            minimum = min(self.snapshot["minimum"], lo)
            if name == "original-upper":
                minimum, maximum, value = self.snapshot["minimum"], self.snapshot["maximum"], hi
                if not minimum <= value <= maximum:
                    self.manifest["skipped"].append("Original upper endpoint is outside the displayed range.")
                    return False
            elif name == "extended-in-range":
                value = (lo + hi) / 2
            else:
                value = hi + step / 2
            dpg.configure_item("sl_pl", min_value=minimum, max_value=maximum)
            dpg.set_value("sl_pl", value)
            dpg.set_value("in_pl", value)
            self.app.refresh_power_slider_style()
        if name != "restored-display":
            dpg.set_value("ctl_clocks", "Developer capture: display-only " + name)
        return True

    def tick(self):
        """Advance once after a rendered frame; return while capture is active."""
        if not self.active:
            return
        try:
            self.frames += 1
            if self.live_scroll is not None:
                scroll = self.dpg.get_item_pos(self.live_scroll)[1]
                self.dpg.set_y_scroll("page_control", max(0, min(scroll, self.dpg.get_y_scroll_max("page_control"))))
                self.live_scroll = None
            if self.pending is not None:
                path, name, requested_frame, request_tick = self.pending
                if path.is_file() and self.frames - request_tick >= 2:
                    raw = path.read_bytes()
                    if (len(raw) < 45 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR"
                            or raw[-12:-4] != b"\0\0\0\0IEND"):
                        raise RuntimeError("Framebuffer output is not a complete PNG")
                    width, height = struct.unpack_from(">II", raw, 16)
                    if not width or not height:
                        raise RuntimeError("Framebuffer output has empty dimensions")
                    item = {"stage": name, "synthetic": name in ("original-upper", "extended-in-range", "extended-above") or name.endswith("-invalid"),
                            "path": path.name, "requested_frame": requested_frame,
                            "width": width, "height": height, "sha256": hashlib.sha256(raw).hexdigest()}
                    if name in getattr(self, "live_cases", {}):
                        kind, _, _ = self.live_cases[name]
                        prefix = "reg_" + kind + "_live_"
                        item["live"] = {key: self.dpg.get_value(prefix + tag) for key, tag in
                                        (("selected", "select"), ("value", "value"), ("status", "status"))}
                    if "slider" in self.snapshot:
                        cfg = self.dpg.get_item_configuration("sl_pl")
                        item["widget"] = {"slider": self.dpg.get_value("sl_pl"), "input": self.dpg.get_value("in_pl"),
                                          "min": cfg["min_value"], "max": cfg["max_value"]}
                    self.manifest["captures"].append(item)
                    self.pending = None
                    self.stage += 1
                    if self.stage == len(self.stages):
                        self._finish()
                        return
                    if not self._stage(self.stages[self.stage]):
                        self.stage += 1
                        if self.stage == len(self.stages):
                            self._finish()
                            return
                        self._stage(self.stages[self.stage])
                    self.wait = 3
                    self._save()
                elif self.frames - request_tick > 240:
                    raise RuntimeError("Framebuffer output was not produced")
                return
            self.wait -= 1
            if self.wait <= 0:
                name = self.stages[self.stage]
                path = self.directory / (name + ".png")
                self.pending = (path, name, self.dpg.get_frame_count(), self.frames)
                self.dpg.output_frame_buffer(file=str(path))
        except Exception as exc:
            self._finish(exc)

    def _finish(self, error=None):
        actions = [self._restore_widgets]
        if self.snapshot is not None:
            actions.extend((lambda: self.dpg.set_y_scroll("page_control", self.snapshot["scroll"]),
                            lambda: self.dpg.set_value("tabs", self.snapshot["tab"])))
        for action in actions:
            try:
                action()
            except Exception as exc:
                self.manifest["errors"].append("UI restoration failed: " + str(exc))
        if error is not None:
            self.manifest["errors"].append(str(error))
        self.active = False
        self.manifest["state"] = "failed" if self.manifest["errors"] else "complete"
        self._save()

class LiveVminCapture(UiCapture):
    """Explicit, reversible panel walkthrough for one live VMIN value."""

    def __init__(self, dpg, app, directory):
        self.original = None
        self.label = None
        self.change_attempted = False
        self.restore_attempted = False
        self.restored = False
        self.identity_changed = False
        super().__init__(dpg, app, directory)
        if not self.active:
            return
        try:
            if self.panel is None:
                raise RuntimeError("Live voltage panel is unavailable")
            self.stages = ["vmin-before", "vmin-active", "vmin-restored", "restored-display"]
            self.stage = 0
            self._stage(self.stages[0])
            self._save()
        except Exception as exc:
            self._finish(exc)

    def _read_state(self, name):
        control = self.panel.constants_live_control
        with control.gpu._lock:
            raw = control._capture()
            summary = control._summary(raw)
        (self.directory / (name + ".json")).write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        rows = [row for row in summary["rows"] if row.get("rail") == 0 and row.get("role") == "VMIN"]
        identity = getattr(self, "identity", None)
        if identity is not None and (summary["identity"], summary["image_sha256"]) != identity[:2]:
            self.identity_changed = True
            raise RuntimeError("Live VMIN identity changed")
        if identity is not None and len(rows) == 1 and rows[0].get("equation") != identity[2]:
            self.identity_changed = True
            raise RuntimeError("Live VMIN equation changed")
        if len(rows) != 1 or rows[0].get("status") != "ready":
            raise RuntimeError("Rail 0 VMIN readback is not ready")
        return summary, rows[0]

    def _select(self):
        self.dpg.set_value("reg_constants_live_select", self.label)
        self.panel.select_constants_live()

    def _record_action(self, name):
        self.manifest.setdefault("actions", []).append({"action": name,
            "status": self.dpg.get_value("reg_constants_live_status"),
            "selected": self.dpg.get_value("reg_constants_live_select")})
        self._save()

    def _stage(self, name):
        if name == "restored-display":
            return super()._stage(name)
        self.dpg.set_value("registry_header", True)
        self.dpg.set_value("reg_rel_live_header", False)
        self.dpg.set_value("reg_constants_live_header", True)
        self.live_scroll = "reg_constants_live_header"
        if name == "vmin-before":
            self.panel.refresh_constants_live()
            if self.panel.constants_live_stale:
                raise RuntimeError("Live VMIN read is stale")
            matches = [(label, row) for label, row in self.panel.constants_live_choices.items()
                       if row.get("rail") == 0 and row.get("role") == "VMIN" and row.get("status") == "ready"]
            if len(matches) != 1:
                raise RuntimeError("Rail 0 VMIN is unavailable")
            self.label, selected = matches[0]
            self._select()
            summary, row = self._read_state("before")
            value, maximum = row.get("value_uv"), row.get("maximum_uv")
            if (type(value) is not int or type(maximum) is not int or value + 5000 > maximum
                    or type(row.get("reported_uv")) is not int or selected.get("value_uv") != value
                    or selected.get("equation") != row.get("equation")
                    or self.panel.constants_live_snapshot["identity"] != summary["identity"]
                    or self.panel.constants_live_snapshot["image_sha256"] != summary["image_sha256"]):
                raise RuntimeError("Rail 0 VMIN value or bounds do not support the exact trial")
            key = self.panel._constants_live_key(self.panel.constants_live_snapshot, selected)
            if self.app._constants_live_initial_states.get(key) != value:
                raise RuntimeError("Panel initial VMIN differs from the current value")
            self.original, self.wanted = value, value + 5000
            self.original_reported = row["reported_uv"]
            self.wanted_reported = self.original_reported + 5000
            self.live_cases = dict.fromkeys(("vmin-before", "vmin-active", "vmin-restored"),
                                            ("constants", self.label, False))
            self.identity = (summary["identity"], summary["image_sha256"], row["equation"])
            self.manifest["selected_row"] = dict(row)
            self.manifest["wanted_uv"] = self.wanted
        elif name == "vmin-active":
            self._select()
            self.dpg.set_value("reg_constants_live_value", self.panel._rel_mv(self.wanted))
            self.change_attempted = True
            try:
                self.panel.apply_constants_live()
            finally:
                self._record_action("apply")
            summary, row = self._read_state("active")
            if (summary["identity"], summary["image_sha256"], row["equation"]) != self.identity:
                self.identity_changed = True
                raise RuntimeError("Live VMIN identity changed after apply")
            if (self.panel.constants_live_stale or not self.dpg.get_value("reg_constants_live_status").startswith("Applied live.")
                    or row.get("value_uv") != self.wanted or row.get("reported_uv") != self.wanted_reported):
                raise RuntimeError("Live VMIN apply did not produce the exact requested readback")
        elif name == "vmin-restored":
            self._restore_live()
        self.dpg.set_value("ctl_clocks", "Developer capture: " + name)
        return True

    def _restore_live(self):
        if self.restored or not self.change_attempted:
            return
        if self.identity_changed:
            raise RuntimeError("Live VMIN identity changed; recovery requires attention")
        summary, row = self._read_state("before-restore")
        if (summary["identity"], summary["image_sha256"], row["equation"]) != self.identity:
            self.identity_changed = True
            raise RuntimeError("Live VMIN identity changed before restoration")
        if row.get("value_uv") != self.original or row.get("reported_uv") != self.original_reported:
            if self.restore_attempted:
                raise RuntimeError("Live VMIN restoration did not verify")
            self.restore_attempted = True
            self.panel.refresh_constants_live()
            self._select()
            try:
                self.panel.restore_constants_live()
            finally:
                self._record_action("restore")
            if not self.dpg.get_value("reg_constants_live_status").startswith("Applied live."):
                raise RuntimeError("Live VMIN restore callback failed")
        summary, row = self._read_state("restored")
        if (summary["identity"], summary["image_sha256"], row["equation"]) != self.identity:
            self.identity_changed = True
            raise RuntimeError("Live VMIN identity changed after restoration")
        if row.get("value_uv") != self.original or row.get("reported_uv") != self.original_reported:
            raise RuntimeError("Live VMIN original readback was not restored")
        self.restored = True
        self.manifest["restoration_verified"] = True

    def _finish(self, error=None):
        if self.change_attempted and not self.restored:
            try:
                self._restore_live()
            except Exception as exc:
                self.manifest["errors"].append("Live VMIN restoration failed: " + str(exc))
                self.manifest["restoration_verified"] = False
                if not self.restore_attempted and not self.identity_changed:
                    self.restore_attempted = True
                    try:
                        self.panel.constants_live_control.set(0, "VMIN", self.original)
                        summary, row = self._read_state("restored")
                        if ((summary["identity"], summary["image_sha256"], row["equation"]) != self.identity
                                or row.get("value_uv") != self.original or row.get("reported_uv") != self.original_reported):
                            raise RuntimeError("Live VMIN original readback was not restored")
                        self.restored = True
                        self.manifest["restoration_verified"] = True
                    except Exception as restore_error:
                        self.manifest["errors"].append("Live VMIN recovery failed: " + str(restore_error))
        super()._finish(error)
