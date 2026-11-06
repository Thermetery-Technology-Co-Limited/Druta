# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only controls for engine clock-gating requests."""

import dearpygui.dearpygui as dpg

from . import lowpower_profiles


class EngineClockGatingPanel:
    """Render status and controls for this instance's request."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None
        self.stale = False
        self.release_pending = False

    def build(self):
        dpg.add_text(
            "Hold enabled or disabled requests that state for engine clock gating in this session. "
            "An enabled request rejects conflicting profiler requests; other driver mechanisms can still interfere. "
            "Mask readback is a partial check and does not guarantee physical gating or residency. "
            "The request is not automatically reapplied after a driver restart.",
            tag="engine_clock_gating_explainer", wrap=self.app.s(900),
        )
        dpg.add_text("A saved profile describes only this instance's request. Load it manually to apply; "
                     "requests are not replayed after a driver restart.", wrap=self.app.s(900))
        with dpg.group(horizontal=True):
            dpg.add_button(label="Refresh", tag="engine_clock_gating_read", callback=self.read)
            dpg.add_button(label="Save request profile", tag="engine_clock_gating_save_profile",
                           callback=self.app.open_save_profile, user_data=("lowpower_requests", "elcg", None))
            dpg.add_button(label="Hold disabled", tag="engine_clock_gating_hold",
                           callback=self.hold, enabled=False)
            dpg.add_button(label="Hold enabled", tag="engine_clock_gating_hold_enabled",
                           callback=self.hold_enabled, enabled=False)
            dpg.add_button(label="Release", tag="engine_clock_gating_release",
                           callback=self.release, enabled=False)
        dpg.add_text("Refresh status before requesting a change.", tag="engine_clock_gating_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="engine_clock_gating_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(100))

    def _controller(self):
        return lowpower_profiles.controller(self.app, "elcg")

    def reset_view(self):
        """Forget the previous adapter's display after a GPU switch."""
        self.snapshot = None
        self.stale = False
        self.release_pending = False
        dpg.set_value("engine_clock_gating_readback", "")
        dpg.set_value("engine_clock_gating_status", "Refresh status before requesting a change.")
        self._configure_actions()

    @staticmethod
    def _enable_eligible(snapshot):
        supported = snapshot.get("supported_engines")
        eligible = snapshot.get("eligible_engines")
        return (snapshot.get("available") is True and type(supported) is int
                and type(eligible) is int and 0 < supported < (1 << 64) and eligible == supported)

    @staticmethod
    def _format(snapshot):
        lines = ["Request readback: " + ("available" if snapshot["support"] else "unavailable")]
        if snapshot.get("uncertain_outcome", False):
            lines.append("This instance's request outcome is uncertain; use Release.")
        else:
            mode = snapshot.get("owned_mode")
            state = f"held {mode}" if snapshot["owned_request"] and mode in ("enabled", "disabled") else (
                "held (mode unknown)" if snapshot["owned_request"] else "not held"
            )
            lines.append("This instance's request: " + state)
        eligible = snapshot["eligible_engines"]
        lines.append("Engines with no recorded disable reason: "
                     + (str(bin(eligible).count("1")) if eligible is not None else "unknown"))
        observed = snapshot.get("observed_engines", [])
        if observed:
            lines.append("Observed changes on engine bits: " + ", ".join(map(str, observed))
                         + " (partial readback)")
        if snapshot.get("arbitration_verified", False):
            lines.append("Conflicting request rejection verified for this enabled hold.")
        if (snapshot["support"] and not snapshot["owned_request"]
                and snapshot.get("available") is True
                and type(snapshot.get("supported_engines")) is int
                and type(eligible) is int and eligible != snapshot["supported_engines"]):
            lines.append("Other disable reasons are active; release those before holding enabled.")
        elif snapshot["support"] and not snapshot["owned_request"] and not EngineClockGatingPanel._enable_eligible(snapshot):
            lines.append("Complete engine eligibility readback is required before holding enabled.")
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
        dpg.configure_item("engine_clock_gating_hold_enabled", enabled=bool(
            safe and not self.stale and not owned and snapshot["support"] and self._enable_eligible(snapshot)))
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
            self._show(lowpower_profiles.change(self.app, "elcg", "disabled"), "Disable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold", exc)

    def hold_enabled(self, *unused):
        if not self.app.guard():
            return
        try:
            self._show(lowpower_profiles.change(self.app, "elcg", "enabled"), "Enable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold enabled", exc)

    def release(self, *unused):
        try:
            self._show(lowpower_profiles.change(self.app, "elcg", "released"), "This instance's request was released.")
        except Exception as exc:  # noqa: BLE001
            self._failed("release", exc)
