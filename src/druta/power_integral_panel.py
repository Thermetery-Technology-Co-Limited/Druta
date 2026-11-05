# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual, session-only power-policy feedback adjustment."""

import dearpygui.dearpygui as dpg


class PowerIntegralPanel:
    def __init__(self, app):
        self.app = app
        self.controller = None
        self.controller_gpu = None
        self.snapshot = None
        self.snapshot_gpu = None
        self.stale = True
        self.labels = {}
        self.initial_input_checked = False

    def build(self):
        dpg.add_separator()
        dpg.add_text("Power-policy feedback adjustment")
        dpg.add_text(
            "100% is a 1x bound relative to the policy limit. This adjustment does not "
            "change the board request. Values shown are software effective limits, not physical power.",
            wrap=self.app.s(900))
        with dpg.group(horizontal=True):
            dpg.add_button(label="Read", tag="integral_read", callback=self.read)
            dpg.add_combo([], tag="integral_policy", width=self.app.s(220), callback=self._selection_changed)
        with dpg.group(horizontal=True):
            dpg.add_input_text(label="Upper adjustment (%)", tag="integral_input", width=self.app.s(130))
            dpg.add_button(label="Apply now", tag="integral_apply", callback=self.apply, enabled=False)
            dpg.add_button(label="Use reported default", tag="integral_default", callback=self.use_default, enabled=False)
        with dpg.group(horizontal=True):
            dpg.add_input_text(label="Lower adjustment (%)", tag="integral_lower_input", width=self.app.s(130))
            dpg.add_button(label="Apply now", tag="integral_lower_apply", callback=self.apply_lower, enabled=False)
            dpg.add_button(label="Use reported default", tag="integral_lower_default", callback=self.use_lower_default, enabled=False)
        dpg.add_text("Read to select an available policy.", tag="integral_status", wrap=self.app.s(900))
        dpg.add_input_text(tag="integral_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(100))

    def _controller(self):
        if self.controller_gpu is not self.app.gpu:
            from .power_integral_runtime import PowerIntegralController
            self.controller_gpu = self.app.gpu
            self.controller = PowerIntegralController(self.app.gpu)
        return self.controller

    def _row(self, snapshot=None):
        snapshot = self.snapshot if snapshot is None else snapshot
        policy = self.labels.get(dpg.get_value("integral_policy"))
        return next((row for row in (snapshot or {}).get("policies", [])
                     if row.get("policy") == policy and row.get("available") is True), None)

    def _eligible(self):
        return bool(not self.stale and self.snapshot_gpu is self.app.gpu and self.snapshot
                    and self.snapshot.get("identity") and self._row())

    def _actions(self):
        enabled = self._eligible()
        dpg.configure_item("integral_apply", enabled=enabled)
        dpg.configure_item("integral_default", enabled=enabled)
        dpg.configure_item("integral_lower_apply", enabled=enabled)
        dpg.configure_item("integral_lower_default", enabled=enabled)

    @staticmethod
    def _format(row):
        from .power_integral_runtime import raw_to_percent
        def amount(value):
            if value is None:
                return "--"
            unit = {"mW": "W", "mA": "A"}.get(row.get("unit"), row.get("unit", ""))
            scaled = f"{value / 1000:.3f}".rstrip("0").rstrip(".")
            return f"{scaled} {unit}"
        return (f"Current upper: {raw_to_percent(row['upper_raw'])}%  |  "
                f"Reported default upper: {raw_to_percent(row['default_upper_raw'])}%  |  "
                f"Current lower: {raw_to_percent(row['lower_raw'])}%  |  "
                f"Reported default lower: {raw_to_percent(row['default_lower_raw'])}%\n"
                f"Requested: {amount(row.get('requested'))}  |  "
                f"PMU output: {amount(row.get('pmu_output'))}  |  "
                f"Effective limit: {amount(row.get('effective'))}\n"
                f"Past samples: {row.get('past_samples', '--')} (default {row.get('default_past_samples', '--')})  |  "
                f"Next samples: {row.get('next_samples', '--')} (default {row.get('default_next_samples', '--')})")

    def _render(self):
        row = self._row()
        dpg.set_value("integral_readback", self._format(row) if row else "")
        self._actions()

    def _selection_changed(self, sender=None, app_data=None, user_data=None):
        self._render()
        row = self._row()
        if row and self._eligible():
            from .power_integral_runtime import raw_to_percent
            dpg.set_value("integral_input", raw_to_percent(row["upper_raw"]))
            dpg.set_value("integral_lower_input", raw_to_percent(row["lower_raw"]))

    def _show(self, snapshot, gpu, message):
        if gpu is not self.app.gpu or not snapshot.get("identity"):
            raise ValueError("adapter identity is unavailable or selection changed")
        rows = [r for r in snapshot.get("policies", []) if r.get("available") is True]
        labels = {f"Policy {r['policy']} (type {r['type']}, channel {r['channel']})": r['policy'] for r in rows}
        previous = dpg.get_value("integral_policy")
        self.snapshot, self.snapshot_gpu, self.stale, self.labels = snapshot, gpu, False, labels
        dpg.configure_item("integral_policy", items=list(labels))
        dpg.set_value("integral_policy", previous if previous in labels else next(iter(labels), ""))
        dpg.set_value("integral_status", message if rows else "No feedback adjustments are available on this adapter.")
        self._render()

    def _failed(self, operation, exc):
        if self.snapshot_gpu is not self.app.gpu:
            self.snapshot, self.snapshot_gpu, self.labels = None, None, {}
            dpg.configure_item("integral_policy", items=[])
            dpg.set_value("integral_policy", "")
            dpg.set_value("integral_readback", "")
        self.stale = True
        self._actions()
        message = f"Feedback adjustment {operation} failed: {exc}. Read again before applying."
        if self.snapshot:
            message += " Displayed values are stale."
        dpg.set_value("integral_status", message)
        self.app.log(message, False)

    def read(self, sender=None, app_data=None, user_data=None):
        if not self.app.guard():
            return
        gpu = self.app.gpu
        try:
            self._show(self._controller().read(), gpu, "Read complete.")
            if not self.initial_input_checked and self._row():
                if not dpg.get_value("integral_input"):
                    from .power_integral_runtime import raw_to_percent
                    dpg.set_value("integral_input", raw_to_percent(self._row()["upper_raw"]))
                if not dpg.get_value("integral_lower_input"):
                    from .power_integral_runtime import raw_to_percent
                    dpg.set_value("integral_lower_input", raw_to_percent(self._row()["lower_raw"]))
                self.initial_input_checked = True
        except Exception as exc:  # noqa: BLE001
            self._failed("read", exc)

    def _request(self, use_default, lower=False):
        if not self._eligible():
            if self.snapshot_gpu is not self.app.gpu:
                self._failed("selection", ValueError("selected adapter changed"))
            else:
                self._actions()
            return
        if not self.app.guard():
            return
        gpu, identity = self.app.gpu, self.snapshot['identity']
        policy = self._row()['policy']
        try:
            from .power_integral_runtime import percent_to_raw
            input_tag = "integral_lower_input" if lower else "integral_input"
            raw = None if use_default else percent_to_raw(dpg.get_value(input_tag))
            controller = self._controller()
            fresh = controller.read()
            if gpu is not self.app.gpu or fresh.get('identity') != identity:
                raise ValueError("adapter identity changed; read again")
            row = self._row(fresh)
            if row is None or row.get('policy') != policy:
                raise ValueError("selected policy is no longer available")
            if use_default:
                raw = row['default_lower_raw' if lower else 'default_upper_raw']
            field = 'lower_raw' if lower else 'upper_raw'
            if raw == row[field]:
                self._show(fresh, gpu, "Adjustment already matches; nothing written.")
                return
            from . import profiles
            template = profiles.capture_power_integral(gpu, {policy: (field,)})
            if template['power_integral']['identity'] != identity or gpu is not self.app.gpu:
                raise ValueError("adapter identity changed while capturing Undo")
            name, _, missing = profiles.autosave_power_integral(gpu, "power-feedback", template)
            if missing:
                raise ValueError("could not capture a complete selected-field Undo point")
            self.app.log(f"Undo point saved as '{name}'", None)
            setter = controller.set_lower if lower else controller.set_upper
            result = setter(policy, raw, expected_identity=identity)
            self._show(result, gpu, "Adjustment applied and read back.")
            self.app.log("Power-policy feedback adjustment applied.", True)
        except Exception as exc:  # noqa: BLE001
            self._failed("apply", exc)

    def apply(self, sender=None, app_data=None, user_data=None):
        self._request(False)

    def use_default(self, sender=None, app_data=None, user_data=None):
        self._request(True)

    def apply_lower(self, sender=None, app_data=None, user_data=None):
        self._request(False, lower=True)

    def use_lower_default(self, sender=None, app_data=None, user_data=None):
        self._request(True, lower=True)
