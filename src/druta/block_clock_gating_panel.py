# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only controls for a block clock-gating disable request."""

import dearpygui.dearpygui as dpg

from . import lowpower_profiles


class BlockClockGatingPanel:
    """Render status and controls for this instance's request."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.stale = False
        self.release_pending = False

    def build(self):
        dpg.add_text(
            "Hold requests that the driver disable block clock gating. "
            "Register readback is a partial check, not physical gating or residency. "
            "The request is not automatically reapplied after a driver restart.",
            tag="block_clock_gating_explainer", wrap=self.app.s(900),
        )
        dpg.add_text("A saved profile describes only this instance's request. Load it manually to apply; "
                     "requests are not replayed after a driver restart.", wrap=self.app.s(900))
        with dpg.group(horizontal=True):
            dpg.add_button(label="Refresh", tag="block_clock_gating_read", callback=self.read)
            dpg.add_button(label="Save request profile", tag="block_clock_gating_save_profile",
                           callback=self.app.open_save_profile, user_data=("lowpower_requests", "blcg", None))
            dpg.add_button(label="Hold disabled", tag="block_clock_gating_hold",
                           callback=self.hold, enabled=False)
            dpg.add_button(label="Release", tag="block_clock_gating_release",
                           callback=self.release, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="block_clock_gating_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="block_clock_gating_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(100))

    def _controller(self):
        return lowpower_profiles.controller(self.app, "blcg")

    def reset_view(self):
        """Forget the previous adapter's display after a GPU switch."""
        self.snapshot = None
        self.stale = False
        self.release_pending = False
        dpg.set_value("block_clock_gating_readback", "")
        dpg.set_value("block_clock_gating_status", "Refresh status before requesting a change.")
        self._configure_actions()

    @staticmethod
    def _format(snapshot):
        lines = ["Request readback: " + ("available" if snapshot["support"] else "unavailable")]
        if snapshot.get("uncertain_outcome", False):
            lines.append("This instance's request outcome is uncertain; use Release.")
        else:
            lines.append("This instance's disable request: "
                         + ("held" if snapshot["owned_request"] else "not held"))
        for address, value in snapshot.get("registers", {}).items():
            lines.append(f"Register {address}: 0x{value:08X}")
        masks = snapshot.get("allowed_clear_masks", {})
        if (snapshot["support"] and not snapshot["owned_request"]
                and not any(value & masks.get(address, 0)
                            for address, value in snapshot.get("registers", {}).items())):
            lines.append("No register bits are available to verify a new request.")
        observed = snapshot.get("observed_register_bits", {})
        observed_count = sum(bool(mask) for mask in observed.values())
        if observed_count:
            lines.append(f"Registers with observed changes: {observed_count} (partial readback)")
        if snapshot.get("last_verification_error"):
            lines.append("Last verification error: " + str(snapshot["last_verification_error"]))
        return "\n".join(lines)

    def _configure_actions(self):
        snapshot = self.snapshot
        safe = snapshot is not None and not snapshot.get("uncertain_outcome", False)
        owned = safe and snapshot["owned_request"]
        registers = snapshot.get("registers", {}) if snapshot else {}
        masks = snapshot.get("allowed_clear_masks", {}) if snapshot else {}
        observable = any(value & masks.get(address, 0) for address, value in registers.items())
        dpg.configure_item("block_clock_gating_hold", enabled=bool(
            safe and not self.stale and snapshot["support"] and observable and not owned))
        dpg.configure_item("block_clock_gating_release", enabled=bool(
            owned or self.release_pending or (snapshot and snapshot.get("uncertain_outcome", False))))

    def _failed(self, operation, exc):
        if operation != "refresh":
            self.snapshot = None
            self.release_pending = True
            dpg.set_value("block_clock_gating_readback", "")
        self.stale = True
        self._configure_actions()
        action = "Refresh status." if operation == "refresh" else "Use Release or refresh status."
        message = f"Block clock gating {operation} failed: {exc}. {action}"
        dpg.set_value("block_clock_gating_status", message)
        self.app.log(message, False)

    def _show(self, snapshot, message):
        self.snapshot = snapshot
        self.stale = False
        self.release_pending = bool(snapshot.get("uncertain_outcome", False))
        dpg.set_value("block_clock_gating_readback", self._format(snapshot))
        dpg.set_value("block_clock_gating_status", message)
        self._configure_actions()

    def read(self, *unused):
        try:
            self._show(self._controller().read(), "Block clock-gating status refreshed.")
        except Exception as exc:  # noqa: BLE001
            self._failed("refresh", exc)

    def hold(self, *unused):
        if not self.app.guard():
            return
        try:
            self._show(lowpower_profiles.change(self.app, "blcg", "disabled"), "Disable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold", exc)

    def release(self, *unused):
        try:
            self._show(lowpower_profiles.change(self.app, "blcg", "released"), "This instance's disable request was released.")
        except Exception as exc:  # noqa: BLE001
            self._failed("release", exc)

