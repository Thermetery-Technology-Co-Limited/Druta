# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit framebuffer trial of the real live fixed-REL panel callbacks."""

import copy
import json
import struct
import time

from . import vfe_equation_runtime as equations
from .registry_flags import _same_state
from .ui_capture import UiCapture


class LiveRelBoundsCapture(UiCapture):
    """Fix one ready rail, restore its ROM bounds, and close the capture app."""

    def __init__(self, dpg, app, directory, rail, wanted_uv):
        self.rail, self.wanted = rail, wanted_uv
        self.label = None
        self.identity = None
        self.original = None
        self.before_voltage = None
        self.change_attempted = False
        self.restore_attempted = False
        self.restored = False
        self.identity_changed = False
        self.before_deadline = None
        self.active_deadline = None
        super().__init__(dpg, app, directory)
        if not self.active:
            return
        try:
            if type(rail) is not int or rail not in (0, 1) or type(wanted_uv) is not int:
                raise RuntimeError("Select an understood rail and whole-microvolt value")
            if self.panel is None:
                raise RuntimeError("Live REL panel is unavailable")
            self.stages = ["rel-before", "rel-active", "rel-restored", "restored-display"]
            self.stage = 0
            self._stage(self.stages[0])
            self._save()
        except Exception as exc:
            self._finish(exc)

    def _read_state(self, name):
        control = self.panel.rel_live_control
        with control.gpu._lock:
            raw = control._capture()
            summary = control._summary(raw)
        (self.directory / (name + ".json")).write_text(
            json.dumps({"raw": raw, "summary": summary}, indent=2) + "\n", encoding="utf-8"
        )
        rows = [row for row in summary["rows"] if row.get("rail") == self.rail]
        if self.identity is not None:
            if (summary.get("identity"), summary.get("image_sha256")) != self.identity[:2]:
                self.identity_changed = True
                raise RuntimeError("Live REL target or image changed")
            if len(rows) == 1 and rows[0].get("equation") != self.identity[2]:
                self.identity_changed = True
                raise RuntimeError("Live REL equation changed")
        if len(rows) != 1 or rows[0].get("status") != "ready" or rows[0].get("fixed_status") != "ready":
            raise RuntimeError("Selected fixed REL readback is not ready")
        return raw, summary, rows[0]

    def _select(self):
        self.dpg.set_value("reg_rel_live_select", self.label)
        self.panel.select_rel_live()

    def _check_panel_identity(self):
        snapshot = self.panel.rel_live_snapshot
        row = self.panel.rel_live_choices.get(self.label)
        if (snapshot is not None and (snapshot.get("identity"), snapshot.get("image_sha256")) != self.identity[:2]):
            self.identity_changed = True
            raise RuntimeError("Live REL target or image changed after refresh")
        if row is not None and row.get("equation") != self.identity[2]:
            self.identity_changed = True
            raise RuntimeError("Live REL equation changed after refresh")
        if self.panel.rel_live_stale or snapshot is None or row is None:
            raise RuntimeError("Live REL refresh did not return the selected row")

    def _record_action(self, name):
        self.manifest.setdefault("actions", []).append({
            "action": name,
            "status": self.dpg.get_value("reg_rel_live_status"),
            "selected": self.dpg.get_value("reg_rel_live_select"),
            "value": self.dpg.get_value("reg_rel_live_value"),
        })
        self._save()

    def _check_projection(self, raw, lower, upper):
        before = self.before_voltage
        actual = raw["voltage"]
        expected = copy.deepcopy(before["equations"])
        payload = bytearray.fromhex(expected["control_hex"])
        offset = equations.HEADER + self.identity[2] * equations.CONTROL_STRIDE
        struct.pack_into("<ff", payload, offset + 4, lower, upper)
        expected["control_hex"] = payload.hex()
        if (not _same_state(actual["equations"], expected)
                or not _same_state(actual["rail_control"], before["rail_control"])
                or actual["boost_percent"] != before["boost_percent"]):
            raise RuntimeError("Live REL definition or retained requests differ from the exact projection")

    def _stage(self, name):
        if name == "restored-display":
            return super()._stage(name)
        self.dpg.set_value("registry_header", True)
        self.dpg.set_value("reg_rel_live_header", True)
        self.dpg.set_value("reg_constants_live_header", False)
        self.live_scroll = "reg_rel_live_header"
        if name == "rel-before":
            self.panel.refresh_rel_live()
            if self.panel.rel_live_stale:
                raise RuntimeError("Live REL read is stale")
            matches = [(label, row) for label, row in self.panel.rel_live_choices.items()
                       if row.get("rail") == self.rail and row.get("status") == "ready"
                       and row.get("fixed_status") == "ready"]
            if len(matches) != 1:
                raise RuntimeError("Selected fixed REL control is unavailable")
            self.label, selected = matches[0]
            self._select()
            raw, summary, row = self._read_state("before")
            bounds = (row.get("lower_uv"), row.get("upper_uv"))
            stored = (row.get("stored_lower_uv"), row.get("stored_upper_uv"))
            policy = row.get("policy_uv") or {}
            if (any(type(value) is not int for value in (*bounds, *stored, row.get("equation"),
                                                          policy.get("vmin"), policy.get("overvoltage")))
                    or bounds != stored or bounds == (self.wanted, self.wanted)
                    or not bounds[0] <= self.wanted <= bounds[1]
                    or not policy["vmin"] <= self.wanted <= policy["overvoltage"]
                    or selected.get("equation") != row["equation"]
                    or (selected.get("lower_uv"), selected.get("upper_uv")) != bounds
                    or self.panel.rel_live_snapshot["identity"] != summary["identity"]
                    or self.panel.rel_live_snapshot["image_sha256"] != summary["image_sha256"]):
                raise RuntimeError("Current REL bounds, identity or policy do not support this trial")
            self.original = bounds
            self.identity = (summary["identity"], summary["image_sha256"], row["equation"])
            self.before_voltage = copy.deepcopy(raw["voltage"])
            self.manifest.update(selected_row=dict(row), wanted_uv=self.wanted,
                                 restoration_verified=False,
                                 qualification="Loaded equations, rail control and boost are compared here; clock requests, final curve and broad state require independent audit.")
            self.live_cases = dict.fromkeys(("rel-before", "rel-active", "rel-restored"),
                                            ("rel", self.label, False))
            self.before_deadline = time.monotonic() + 27.0
            self.manifest["before_observation_window_seconds"] = 27.0
            self.manifest["before_deadline_monotonic"] = self.before_deadline
        elif name == "rel-active":
            self._select()
            self.dpg.set_value("reg_rel_live_value", self.panel._rel_mv(self.wanted))
            self.change_attempted = True
            self.manifest["change_attempted"] = True
            self._save()
            try:
                self.panel.fix_rel_live()
            finally:
                self._record_action("fixed")
            raw, _, row = self._read_state("active")
            if (self.panel.rel_live_stale or not self.dpg.get_value("reg_rel_live_status").startswith("Applied live.")
                    or (row.get("lower_uv"), row.get("upper_uv")) != (self.wanted, self.wanted)):
                raise RuntimeError("Live REL fixed callback did not produce exact requested bounds")
            self._check_projection(raw, self.wanted, self.wanted)
            self.active_deadline = time.monotonic() + 27.0
            self.manifest["active_observation_window_seconds"] = 27.0
            self.manifest["active_deadline_monotonic"] = self.active_deadline
            self._save()
        elif name == "rel-restored":
            self._restore_live()
        self.dpg.set_value("ctl_clocks", "Developer capture: " + name)
        return True

    def tick(self):
        if (self.active and self.before_deadline is not None
                and self.stages[self.stage] == "rel-before" and time.monotonic() < self.before_deadline):
            return
        if (self.active and self.active_deadline is not None
                and self.stages[self.stage] == "rel-active" and time.monotonic() < self.active_deadline):
            return
        super().tick()

    def _restore_live(self):
        if self.restored or not self.change_attempted:
            return
        if self.identity_changed:
            raise RuntimeError("Live REL identity changed; recovery requires attention")
        raw, _, row = self._read_state("before-restore")
        if (row.get("lower_uv"), row.get("upper_uv")) != self.original:
            if self.restore_attempted:
                raise RuntimeError("Live REL restoration did not verify")
            self.panel.refresh_rel_live()
            self._check_panel_identity()
            self._select()
            if not self.app.guard():
                raise RuntimeError("Live REL panel guard blocked restoration")
            self.restore_attempted = True
            self.manifest["restore_attempted"] = True
            self._save()
            try:
                self.panel.restore_rel_live_bounds()
            finally:
                self._record_action("restore")
            if not self.dpg.get_value("reg_rel_live_status").startswith("Applied live."):
                raise RuntimeError("Live REL restore callback failed")
            raw, _, row = self._read_state("restored")
        else:
            (self.directory / "restored.json").write_text(
                (self.directory / "before-restore.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
        if (row.get("lower_uv"), row.get("upper_uv")) != self.original:
            raise RuntimeError("Live REL original bounds were not restored")
        self._check_projection(raw, *self.original)
        self.restored = True
        self.manifest["restoration_verified"] = True

    def _finish(self, error=None):
        if self.change_attempted and not self.restored:
            try:
                self._restore_live()
            except Exception as exc:
                self.manifest["errors"].append("Live REL restoration failed: " + str(exc))
                self.manifest["restoration_verified"] = False
                if not self.restore_attempted and not self.identity_changed:
                    self.restore_attempted = True
                    self.manifest["restore_attempted"] = True
                    self.manifest["recovery_fallback_attempted"] = True
                    try:
                        self.panel.rel_live_control.restore_bounds(self.rail)
                        raw, _, row = self._read_state("restored")
                        if (row.get("lower_uv"), row.get("upper_uv")) != self.original:
                            raise RuntimeError("Live REL original bounds were not restored")
                        self._check_projection(raw, *self.original)
                        self.restored = True
                        self.manifest["restoration_verified"] = True
                    except Exception as restore_error:
                        self.manifest["errors"].append("Live REL recovery failed: " + str(restore_error))
        super()._finish(error)
        self.dpg.stop_dearpygui()
