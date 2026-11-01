# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only controls for an engine clock-gating disable request."""

import dearpygui.dearpygui as dpg


class EngineClockGatingPanel:
    """Render status and controls for this instance's request."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.stale = False
        self.release_pending = False

    def build(self):
        dpg.add_text(
            "Hold requests that the driver disable engine clock gating. "
            "Mask readback is a partial check, not physical gating or residency. "
            "The request is not automatically reapplied after a driver restart.",
            tag="engine_clock_gating_explainer", wrap=self.app.s(900),
        )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Refresh", tag="engine_clock_gating_read", callback=self.read)
            dpg.add_button(label="Hold disabled", tag="engine_clock_gating_hold",
                           callback=self.hold, enabled=False)
            dpg.add_button(label="Release", tag="engine_clock_gating_release",
                           callback=self.release, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="engine_clock_gating_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="engine_clock_gating_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(100))

    def _controller(self):
        from .engine_clock_gating_live import EngineClockGatingController

        gpu = self.app.gpu
        controllers = getattr(self.app, "_engine_clock_gating_controllers", None)
        if not isinstance(controllers, dict):
            controllers = {}
            self.app._engine_clock_gating_controllers = controllers
        key = id(gpu)
        controller = controllers.get(key)
        if controller is None or controller.gpu is not gpu:
            controller = EngineClockGatingController(gpu)
            controllers[key] = controller
        return controller

    def reset_view(self):
        """Forget the previous adapter's display after a GPU switch."""
        self.snapshot = None
        self.stale = False
        self.release_pending = False
        dpg.set_value("engine_clock_gating_readback", "")
        dpg.set_value("engine_clock_gating_status", "Refresh status before requesting a change.")
        self._configure_actions()

    @staticmethod
    def _format(snapshot):
        lines = ["Request readback: " + ("available" if snapshot["support"] else "unavailable")]
        if snapshot.get("uncertain_outcome", False):
            lines.append("This instance's request outcome is uncertain; use Release.")
        else:
            lines.append("This instance's disable request: "
                         + ("held" if snapshot["owned_request"] else "not held"))
        eligible = snapshot["eligible_engines"]
        lines.append("Engines with no recorded disable reason: "
                     + (str(eligible.bit_count()) if eligible is not None else "unknown"))
        observed = snapshot.get("observed_engines", [])
        if observed:
            lines.append("Observed changes on engine bits: " + ", ".join(map(str, observed))
                         + " (partial readback)")
        if snapshot.get("last_verification_error"):
            lines.append("Last verification error: " + str(snapshot["last_verification_error"]))
        return "\n".join(lines)

    def _configure_actions(self):
        snapshot = self.snapshot
        safe = snapshot is not None and not snapshot.get("uncertain_outcome", False)
        owned = safe and snapshot["owned_request"]
        dpg.configure_item("engine_clock_gating_hold", enabled=bool(
            safe and not self.stale and snapshot["support"]
            and snapshot["eligible_engines"] and not owned))
        dpg.configure_item("engine_clock_gating_release", enabled=bool(
            owned or self.release_pending or (snapshot and snapshot.get("uncertain_outcome", False))))

    def _failed(self, operation, exc):
        if operation != "refresh":
            self.snapshot = None
            self.release_pending = True
            dpg.set_value("engine_clock_gating_readback", "")
        self.stale = True
        self._configure_actions()
        action = "Refresh status." if operation == "refresh" else "Use Release or refresh status."
        message = f"Engine clock gating {operation} failed: {exc}. {action}"
        dpg.set_value("engine_clock_gating_status", message)
        self.app.log(message, False)

    def _show(self, snapshot, message):
        self.snapshot = snapshot
        self.stale = False
        self.release_pending = bool(snapshot.get("uncertain_outcome", False))
        dpg.set_value("engine_clock_gating_readback", self._format(snapshot))
        dpg.set_value("engine_clock_gating_status", message)
        self._configure_actions()

    def read(self, *unused):
        try:
            self._show(self._controller().read(), "Engine clock-gating status refreshed.")
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
