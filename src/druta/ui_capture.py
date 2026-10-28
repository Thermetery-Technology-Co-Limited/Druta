# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in framebuffer walkthrough using display-only widget changes."""

import hashlib
import json
import math
import struct
from pathlib import Path


class UiCapture:
    """Run on the DPG render thread; never invoke an application callback."""

    def __init__(self, dpg, app, directory):
        self.dpg, self.app = dpg, app
        self.directory = Path(directory)
        self.directory.mkdir(exist_ok=False)
        self.active = True
        self.pending = None
        self.wait = 3
        self.frames = 0
        self.snapshot = None
        self.manifest = {"schema": 1, "state": "capturing", "captures": [], "skipped": [], "errors": [],
                         "bounds_provenance": getattr(app, "_power_slider_baseline", None)}
        self.manifest["skipped"].append(
            "Live REL and voltage constants panels require their own read and expansion; not captured in this walkthrough."
        )
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
        if "slider" in saved and dpg.does_item_exist("sl_pl") and dpg.does_item_exist("in_pl"):
            dpg.configure_item("sl_pl", min_value=saved["minimum"], max_value=saved["maximum"])
            dpg.set_value("sl_pl", saved["slider"])
            dpg.set_value("in_pl", saved["input"])
            self.app.refresh_power_slider_style()

    def _stage(self, name):
        dpg = self.dpg
        if name == "restored-display":
            self._restore_widgets()
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
                    item = {"stage": name, "synthetic": name not in ("control-actual", "restored-display"),
                            "path": path.name, "requested_frame": requested_frame,
                            "width": width, "height": height, "sha256": hashlib.sha256(raw).hexdigest()}
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
