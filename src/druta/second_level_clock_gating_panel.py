# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only controls for second-level clock-gating requests."""

import dearpygui.dearpygui as dpg


class SecondLevelClockGatingPanel:
    """Render status and controls for this instance's request."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.stale = False
        self.release_pending = False

    def build(self):
        dpg.add_text(
            "Hold enabled or disabled requests that state for second-level clock gating in this session. "
            "An enabled request rejects conflicting profiler requests; other driver mechanisms can still interfere. "
            "Register readback is partial and does not guarantee physical gating or residency. "
            "The request is not automatically reapplied after a driver restart.",
            tag="second_level_clock_gating_explainer", wrap=self.app.s(900),
        )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Refresh", tag="second_level_clock_gating_read", callback=self.read)
            dpg.add_button(label="Hold disabled", tag="second_level_clock_gating_hold",
                           callback=self.hold, enabled=False)
            dpg.add_button(label="Hold enabled", tag="second_level_clock_gating_hold_enabled",
                           callback=self.hold_enabled, enabled=False)
            dpg.add_button(label="Release", tag="second_level_clock_gating_release",
                           callback=self.release, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="second_level_clock_gating_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="second_level_clock_gating_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(100))

    def _controller(self):
        from .second_level_clock_gating_live import SecondLevelClockGatingController

        gpu = self.app.gpu
        controllers = getattr(self.app, "_second_level_clock_gating_controllers", None)
        if not isinstance(controllers, dict):
            controllers = {}
            self.app._second_level_clock_gating_controllers = controllers
        key = id(gpu)
        controller = controllers.get(key)
        if controller is None or controller.gpu is not gpu:
            controller = SecondLevelClockGatingController(gpu)
            controllers[key] = controller
        return controller

    def reset_view(self):
        """Forget the previous adapter's display after a GPU switch."""
        self.snapshot = None
        self.stale = False
        self.release_pending = False
        dpg.set_value("second_level_clock_gating_readback", "")
        dpg.set_value("second_level_clock_gating_status", "Refresh status before requesting a change.")
        self._configure_actions()

    @staticmethod
    def _baseline_matches(snapshot, expected_key="expected_release_values"):
        registers = snapshot.get("registers", {})
        expected = snapshot.get(expected_key, {})
        return bool(registers) and all(key in expected and value == expected[key]
                                       for key, value in registers.items())

    @staticmethod
    def _enable_baseline_matches(snapshot):
        registers = snapshot.get("registers", {})
        enabled = snapshot.get("expected_release_values", {})
        disabled = snapshot.get("expected_disabled_values", {})
        return bool(registers) and all(
            key in enabled and key in disabled and value in (enabled[key], disabled[key])
            for key, value in registers.items()
        )

    @staticmethod
    def _format(snapshot):
        availability = ("partial" if snapshot.get("status") == "partial" else
                        "available" if snapshot["support"] else "unavailable")
        lines = ["Request readback: " + availability]
        if snapshot.get("uncertain_outcome", False):
            lines.append("This instance's request outcome is uncertain; use Release.")
        else:
            mode = snapshot.get("owned_mode")
            state = f"held {mode}" if snapshot["owned_request"] and mode in ("enabled", "disabled") else (
                "held (mode unknown)" if snapshot["owned_request"] else "not held"
            )
            lines.append("This instance's request: " + state)
        for address, value in snapshot.get("registers", {}).items():
            lines.append(f"Register {address}: 0x{value:08X}")
        for address in snapshot.get("unavailable_registers", {}):
            lines.append(f"Register {address}: unavailable")
        peers = snapshot.get("unverified_source_peers", [])
        if peers:
            lines.append(f"Other source-table registers: {len(peers)} unverified.")
            lines.append("The named request also changes those registers; their prior values are not verified.")
        if (snapshot["support"] and not snapshot["owned_request"]
                and not SecondLevelClockGatingPanel._enable_baseline_matches(snapshot)
                and not SecondLevelClockGatingPanel._baseline_matches(snapshot)):
            lines.append("Readbacks do not match the required starting values; a new request cannot be verified.")
        elif (snapshot["support"] and not snapshot["owned_request"]
              and not SecondLevelClockGatingPanel._baseline_matches(snapshot)):
            lines.append("Disabled hold needs the source release baseline; enabled hold may be available.")
        observed = snapshot.get("observed_registers", {})
        observed_count = len(observed)
        if observed_count:
            lines.append(f"Registers with observed changes: {observed_count} (partial readback)")
        if snapshot.get("arbitration_verified", False):
            lines.append("Conflicting request rejection verified for this enabled hold.")
        if snapshot.get("last_verification_error"):
            lines.append("Last verification error: " + str(snapshot["last_verification_error"]))
        return "\n".join(lines)

    def _configure_actions(self):
        snapshot = self.snapshot
        safe = snapshot is not None and not snapshot.get("uncertain_outcome", False)
        owned = safe and snapshot["owned_request"]
        enable_eligible = snapshot is not None and self._enable_baseline_matches(snapshot)
        eligible = snapshot is not None and enable_eligible and self._baseline_matches(snapshot)
        mode = snapshot.get("owned_mode") if snapshot else None
        dpg.configure_item("second_level_clock_gating_hold", enabled=bool(
            safe and not self.stale and ((owned and mode == "disabled") or (not owned and snapshot["support"] and eligible))))
        dpg.configure_item("second_level_clock_gating_hold_enabled", enabled=bool(
            safe and not self.stale and ((owned and mode == "enabled") or (not owned and snapshot["support"] and enable_eligible))))
        dpg.configure_item("second_level_clock_gating_release", enabled=bool(
            owned or self.release_pending or (snapshot and snapshot.get("uncertain_outcome", False))))

    def _failed(self, operation, exc):
        if operation != "refresh":
            self.snapshot = None
            self.release_pending = True
            dpg.set_value("second_level_clock_gating_readback", "")
        self.stale = True
        self._configure_actions()
        action = "Refresh status." if operation == "refresh" else "Use Release or refresh status."
        message = f"Second-level clock gating {operation} failed: {exc}. {action}"
        dpg.set_value("second_level_clock_gating_status", message)
        self.app.log(message, False)

    def _show(self, snapshot, message):
        self.snapshot = snapshot
        self.stale = False
        self.release_pending = bool(snapshot.get("uncertain_outcome", False))
        dpg.set_value("second_level_clock_gating_readback", self._format(snapshot))
        dpg.set_value("second_level_clock_gating_status", message)
        self._configure_actions()

    def read(self, *unused):
        try:
            self._show(self._controller().read(), "Second-level clock-gating status refreshed.")
        except Exception as exc:  # noqa: BLE001
            self._failed("refresh", exc)

    def hold(self, *unused):
        if not self.app.guard():
            return
        try:
            self._show(self._controller().hold(), "Disable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold", exc)

    def hold_enabled(self, *unused):
        if not self.app.guard():
            return
        try:
            self._show(self._controller().hold_enabled(), "Enable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold enabled", exc)

    def release(self, *unused):
        try:
            self._show(self._controller().release(), "This instance's request was released.")
        except Exception as exc:  # noqa: BLE001
            self._failed("release", exc)


