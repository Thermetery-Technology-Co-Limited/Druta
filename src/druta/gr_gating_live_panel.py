# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only controls for a GR power-gating disable request."""

import dearpygui.dearpygui as dpg


class GrGatingLivePanel:
    """Render status and controls for this instance's request."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.stale = False
        self.release_pending = False

    def build(self):
        dpg.add_text(
            "Hold requests that the driver disable GR power gating for this session. "
            "Driver readback is a partial check, not physical residency. "
            "The request is not automatically reapplied after a driver restart.",
            tag="gr_gating_live_explainer", wrap=self.app.s(900),
        )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Refresh", tag="gr_gating_live_read", callback=self.read)
            dpg.add_button(label="Hold disabled", tag="gr_gating_live_hold",
                           callback=self.hold, enabled=False)
            dpg.add_button(label="Release", tag="gr_gating_live_release",
                           callback=self.release, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="gr_gating_live_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="gr_gating_live_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(100))

    def _controller(self):
        from .gr_gating_live import GrGatingController

        gpu = self.app.gpu
        controllers = getattr(self.app, "_gr_gating_live_controllers", None)
        if not isinstance(controllers, dict):
            controllers = {}
            self.app._gr_gating_live_controllers = controllers
        key = id(gpu)
        controller = controllers.get(key)
        if controller is None or controller.gpu is not gpu:
            controller = GrGatingController(gpu)
            controllers[key] = controller
        return controller

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
        observed = snapshot.get("observed_controllers", [])
        if observed:
            lines.append("Observed request changes on LPWR controllers: "
                         + ", ".join(map(str, observed)) + " (partial readback)")
        if snapshot.get("last_verification_error"):
            lines.append("Last verification error: " + str(snapshot["last_verification_error"]))
        for index, controller in snapshot["controllers"].items():
            if not controller.get("supported"):
                continue
            enabled = controller.get("enabled_predicate")
            state = "unknown" if enabled is None else ("enabled" if enabled else "disabled")
            lines.append(f"LPWR controller {index}, latest driver observation: {state}")
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
        message = f"GR power gating {operation} failed: {exc}. {action}"
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
            self._show(self._controller().read(), "GR power-gating status refreshed.")
        except Exception as exc:  # noqa: BLE001
            self._failed("refresh", exc)

    def hold(self, *unused):
        if not self.app.guard():
            return
        try:
            self._show(self._controller().hold(), "Disable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold", exc)

    def release(self, *unused):
        try:
            self._show(self._controller().release(), "This instance's disable request was released.")
        except Exception as exc:  # noqa: BLE001
            self._failed("release", exc)
