# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only controls for an engine-level power-gating request."""

import dearpygui.dearpygui as dpg

from . import lowpower_profiles


class GrGatingLivePanel:
    """Render status and controls for this instance's request."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.stale = False
        self.release_pending = False

    def build(self):
        dpg.add_text(
            "Hold requests that the driver disable engine-level power gating (ELPG) for this session. "
            "The driver selects associated controllers, which can include graphics and media engines. "
            "Individual engines cannot be selected. Driver readback is a partial check, not physical residency. "
            "The request is not automatically reapplied after a driver restart.",
            tag="gr_gating_live_explainer", wrap=self.app.s(900),
        )
        dpg.add_text("A saved profile describes only this instance's request. Load it manually to apply; "
                     "requests are not replayed after a driver restart.", wrap=self.app.s(900))
        with dpg.group(horizontal=True):
            dpg.add_button(label="Refresh", tag="gr_gating_live_read", callback=self.read)
            dpg.add_button(label="Save request profile", tag="gr_gating_live_save_profile",
                           callback=self.app.open_save_profile, user_data=("lowpower_requests", "elpg", None))
            dpg.add_button(label="Hold disabled", tag="gr_gating_live_hold",
                           callback=self.hold, enabled=False)
            dpg.add_button(label="Release", tag="gr_gating_live_release",
                           callback=self.release, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="gr_gating_live_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="gr_gating_live_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(100))

    def _controller(self):
        return lowpower_profiles.controller(self.app, "elpg")

    def reset_view(self):
        """Forget the previous adapter's display after a GPU switch."""
        self.snapshot = None
        self.stale = False
        self.release_pending = False
        dpg.set_value("gr_gating_live_readback", "")
        dpg.set_value("gr_gating_live_status", "Refresh status before requesting a change.")
        self._configure_actions()

    @staticmethod
    def _format(snapshot):
        lines = ["Request readback: " + ("available" if snapshot["support"] else "unavailable")]
        if snapshot.get("uncertain_outcome", False):
            lines.append("This instance's request outcome is uncertain; use Release.")
        else:
            lines.append("This instance's disable request: "
                         + ("held" if snapshot["owned_request"] else "not held"))
        observed = {str(index) for index in snapshot.get("observed_controllers", [])}
        if observed:
            lines.append("Observed request changes on LPWR controllers: "
                         + ", ".join(map(str, snapshot["observed_controllers"])) + " (partial readback)")
        if snapshot.get("last_verification_error"):
            lines.append("Last verification error: " + str(snapshot["last_verification_error"]))
        for index, controller in snapshot["controllers"].items():
            if not controller.get("available"):
                lines.append(f"LPWR controller {index}: support readback unavailable")
                continue
            if not controller.get("supported"):
                lines.append(f"LPWR controller {index}: unsupported")
                continue
            enabled = controller.get("enabled_predicate")
            state = "unknown" if enabled is None else ("enabled" if enabled else "disabled")
            line = f"LPWR controller {index}, latest enabled predicate: {state}"
            if snapshot["owned_request"] and not snapshot.get("uncertain_outcome", False):
                transition = "request transition observed" if str(index) in observed else "no new request transition observed"
                line += f"; {transition}"
            lines.append(line)
        return "\n".join(lines)

    def _configure_actions(self):
        snapshot = self.snapshot
        safe = snapshot is not None and not snapshot.get("uncertain_outcome", False)
        owned = safe and snapshot["owned_request"]
        dpg.configure_item("gr_gating_live_hold", enabled=bool(
            safe and not self.stale and snapshot["support"] and not owned))
        dpg.configure_item("gr_gating_live_release", enabled=bool(
            owned or self.release_pending or (snapshot and snapshot.get("uncertain_outcome", False))))

    def _failed(self, operation, exc):
        # A failed write may have changed the request. Read again before acting.
        if operation != "refresh":
            self.snapshot = None
            self.release_pending = True
            dpg.set_value("gr_gating_live_readback", "")
        self.stale = True
        self._configure_actions()
        action = "Refresh status." if operation == "refresh" else "Use Release or refresh status."
        message = f"Engine-level power gating (ELPG) {operation} failed: {exc}. {action}"
        dpg.set_value("gr_gating_live_status", message)
        self.app.log(message, False)

    def _show(self, snapshot, message):
        self.snapshot = snapshot
        self.stale = False
        self.release_pending = bool(snapshot.get("uncertain_outcome", False))
        dpg.set_value("gr_gating_live_readback", self._format(snapshot))
        dpg.set_value("gr_gating_live_status", message)
        self._configure_actions()

    def read(self, *unused):
        try:
            self._show(self._controller().read(), "Engine-level power-gating (ELPG) status refreshed.")
        except Exception as exc:  # noqa: BLE001
            self._failed("refresh", exc)

    def hold(self, *unused):
        if not self.app.guard():
            return
        try:
            self._show(lowpower_profiles.change(self.app, "elpg", "disabled"), "Disable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold", exc)

    def release(self, *unused):
        try:
            self._show(lowpower_profiles.change(self.app, "elpg", "released"), "This instance's disable request was released.")
        except Exception as exc:  # noqa: BLE001
            self._failed("release", exc)
