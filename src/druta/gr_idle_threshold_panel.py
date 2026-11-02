# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only graphics idle threshold control."""

import dearpygui.dearpygui as dpg


class GrIdleThresholdPanel:
    """Show the selected adapter's driver request and verified readback."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.snapshot_gpu = None
        self.stale = True

    def build(self):
        dpg.add_text(
            "Adjust the graphics idle delay before power gating for this session. "
            "Actual latency, power and residency are not measured. "
            "The driver request is not reapplied after a driver restart.",
            tag="gr_idle_threshold_explainer", wrap=self.app.s(900),
        )
        with dpg.group(horizontal=True):
            dpg.add_input_text(label="GR idle threshold (µs)", tag="gr_idle_threshold_value",
                               callback=self._input_changed, width=self.app.s(180))
            dpg.add_button(label="Refresh", tag="gr_idle_threshold_read", callback=self.read)
            dpg.add_button(label="Apply", tag="gr_idle_threshold_apply", callback=self.apply, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="gr_idle_threshold_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="gr_idle_threshold_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(70))

    def reset_view(self):
        """Forget readback from the previous adapter."""
        self.snapshot = None
        self.snapshot_gpu = None
        self.stale = True
        dpg.set_value("gr_idle_threshold_value", "")
        dpg.set_value("gr_idle_threshold_readback", "")
        dpg.set_value("gr_idle_threshold_status", "Refresh status before requesting a change.")
        self._configure_actions()

    def _controller(self):
        from .gr_idle_threshold_live import GrIdleThresholdController

        return GrIdleThresholdController(self.app.gpu)

    def _value(self):
        value = dpg.get_value("gr_idle_threshold_value").strip()
        if not value or len(value) > 20 or not value.isascii() or not value.isdecimal():
            return None
        return int(value)

    def _eligible(self):
        snap = self.snapshot
        if (self.stale or self.snapshot_gpu is not self.app.gpu or not snap
                or not snap.get("available")):
            return False
        minimum, maximum, factor = snap.get("minimum_us"), snap.get("maximum_us"), snap.get("scale_factor")
        value = self._value()
        return bool(type(minimum) is int and type(maximum) is int and type(factor) is int
                    and factor > 0 and value is not None and minimum <= value <= maximum)

    def _configure_actions(self):
        dpg.configure_item("gr_idle_threshold_apply", enabled=self._eligible())

    def _input_changed(self, sender=None, app_data=None, user_data=None):
        self._configure_actions()

    @staticmethod
    def _format(snapshot):
        if not snapshot.get("available"):
            return "Unavailable: " + str(snapshot.get("status", "No verified threshold readback."))
        scaled = snapshot.get("scaled_pair")
        raw = snapshot.get("raw_pair")
        return (f"Driver readback: {snapshot.get('applied_us')} µs; "
                f"scaled pair: {scaled}; raw pair: {raw}; "
                f"scale: {snapshot.get('scale_factor')}; "
                f"allowed: {snapshot.get('minimum_us')}–{snapshot.get('maximum_us')} µs")

    def _show(self, snapshot, gpu, message):
        self.snapshot = snapshot
        self.snapshot_gpu = gpu
        self.stale = False
        dpg.set_value("gr_idle_threshold_readback", self._format(snapshot))
        dpg.set_value("gr_idle_threshold_status", message)
        self._configure_actions()

    def _failed(self, operation, exc):
        self.stale = True
        self._configure_actions()
        message = f"GR idle threshold {operation} failed: {exc}. Refresh status before applying."
        dpg.set_value("gr_idle_threshold_status", message)
        self.app.log(message, False)

    def read(self, sender=None, app_data=None, user_data=None):
        gpu = self.app.gpu
        try:
            snapshot = self._controller().read()
            if snapshot.get("available") and not dpg.get_value("gr_idle_threshold_value").strip():
                value = snapshot.get("applied_us")
                if type(value) is int:
                    dpg.set_value("gr_idle_threshold_value", str(value))
            if not snapshot.get("available"):
                message = str(snapshot.get("status", "Unavailable."))
            elif snapshot.get("scale_factor") is None or snapshot.get("maximum_us") is None:
                message = "Current scale could not be determined; Apply is unavailable."
            else:
                message = "Status refreshed."
            self._show(snapshot, gpu, message)
        except Exception as exc:  # noqa: BLE001
            self._failed("refresh", exc)

    def apply(self, sender=None, app_data=None, user_data=None):
        if not self._eligible():
            self._configure_actions()
            return
        if not self.app.guard():
            return
        gpu = self.app.gpu
        value = self._value()
        try:
            snapshot = self._controller().apply(value)
            if not snapshot.get("available"):
                message = str(snapshot.get("status", "Threshold readback unavailable."))
            else:
                applied = snapshot.get("applied_us")
                message = f"Requested {value} µs; driver readback {applied} µs."
            self._show(snapshot, gpu, message)
        except Exception as exc:  # noqa: BLE001
            self._failed("apply", exc)
