# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared adaptive-power policy requests for selected controllers."""

import dearpygui.dearpygui as dpg

_CONTROLLERS = {"Graphics (GR)": 0, "Memory subsystem (MS)": 2,
                "NVDEC": 4, "NVENC": 5, "NVJPG": 6, "OFA": 7}


class AdaptivePowerPanel:
    """Present the selected controller's aggregate policy state."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.snapshot_gpu = None
        self.stale = True

    def build(self):
        dpg.add_text(
            "Allow or disable the selected controller's shared adaptive-power policy for this session. "
            "Other reasons may keep it disabled. Readback is the aggregate driver policy state, "
            "not a measurement of physical gating, residency or power. "
            "The driver may also change idle thresholds; Allow does not restore their previous numbers. "
            "Requests are not reapplied after a driver restart.",
            tag="adaptive_power_explainer", wrap=self.app.s(900),
        )
        with dpg.group(horizontal=True):
            dpg.add_combo(list(_CONTROLLERS), default_value="Graphics (GR)",
                          tag="adaptive_power_controller", callback=self._selection_changed,
                          width=self.app.s(180))
            dpg.add_button(label="Refresh", tag="adaptive_power_read", callback=self.read)
            dpg.add_button(label="Allow", tag="adaptive_power_allow", callback=self.allow, enabled=False)
            dpg.add_button(label="Disable", tag="adaptive_power_disable", callback=self.disable, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="adaptive_power_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="adaptive_power_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(65))

    @staticmethod
    def _selected():
        return _CONTROLLERS.get(dpg.get_value("adaptive_power_controller"))

    def _controller(self, index):
        from .adaptive_power_live import AdaptivePowerController

        return AdaptivePowerController(self.app.gpu, index)

    def _eligible(self):
        snap = self.snapshot
        return bool(not self.stale and self.snapshot_gpu is self.app.gpu and snap
                    and snap.get("index") == self._selected() and self._selected() is not None
                    and snap.get("available") is True and type(snap.get("enabled")) is bool)

    def _configure_actions(self):
        eligible = self._eligible()
        dpg.configure_item("adaptive_power_allow", enabled=eligible)
        dpg.configure_item("adaptive_power_disable", enabled=eligible)

    def reset_view(self):
        """Forget the selected controller's stale readback."""
        self.snapshot = None
        self.snapshot_gpu = None
        self.stale = True
        dpg.set_value("adaptive_power_readback", "")
        dpg.set_value("adaptive_power_status", "Refresh status before requesting a change.")
        self._configure_actions()

    def _selection_changed(self, sender=None, app_data=None, user_data=None):
        self.reset_view()

    @staticmethod
    def _format(snapshot):
        if snapshot.get("available") is not True:
            return "Unavailable: " + str(snapshot.get("status", "No verified readback."))
        enabled = snapshot.get("enabled")
        state = "enabled" if enabled is True else "disabled" if enabled is False else "unknown"
        return f"{snapshot.get('name', 'Controller')} aggregate adaptive-power policy: {state}."

    def _show(self, snapshot, gpu, message):
        self.snapshot = snapshot
        self.snapshot_gpu = gpu
        self.stale = False
        dpg.set_value("adaptive_power_readback", self._format(snapshot))
        dpg.set_value("adaptive_power_status", message)
        self._configure_actions()

    def _failed(self, operation, exc):
        self.stale = True
        self._configure_actions()
        message = f"Adaptive power {operation} failed: {exc}. Refresh status before requesting a change."
        dpg.set_value("adaptive_power_status", message)
        self.app.log(message, False)

    def read(self, sender=None, app_data=None, user_data=None):
        gpu, index = self.app.gpu, self._selected()
        if index is None:
            self.reset_view()
            dpg.set_value("adaptive_power_status", "Select a valid controller before refreshing.")
            return
        try:
            snapshot = self._controller(index).read()
            if snapshot.get("index") != index:
                raise ValueError("readback controller does not match selection")
            message = "Status refreshed." if snapshot.get("available") is True else str(
                snapshot.get("status", "Unavailable."))
            self._show(snapshot, gpu, message)
        except Exception as exc:  # noqa: BLE001
            self._failed("refresh", exc)

    def _request(self, allowed):
        if not self._eligible():
            self._configure_actions()
            return
        if not self.app.guard():
            return
        gpu, index = self.app.gpu, self._selected()
        try:
            snapshot = self._controller(index).set_allowed(allowed)
            if snapshot.get("index") != index or snapshot.get("requested_allowed") is not allowed:
                raise ValueError("request result does not match selection")
            verification = snapshot.get("verification")
            state = "enabled" if snapshot.get("enabled") is True else "disabled" if snapshot.get("enabled") is False else "unknown"
            if verification == "already_allowed":
                message = "Policy is already allowed; no request was needed."
            elif snapshot.get("request_accepted") is True and verification == "aggregate_transition":
                message = f"Request accepted; aggregate policy is {state}."
            elif snapshot.get("request_accepted") is True and verification == "aggregate_only":
                message = (f"Request accepted; aggregate policy is {state}. "
                           "Other reasons may affect the aggregate state; the request effect is not independently verified.")
            else:
                raise ValueError("request acceptance or verification is unavailable")
            self._show(snapshot, gpu, message)
        except Exception as exc:  # noqa: BLE001
            self._failed("request", exc)

    def allow(self, sender=None, app_data=None, user_data=None):
        self._request(True)

    def disable(self, sender=None, app_data=None, user_data=None):
        self._request(False)
