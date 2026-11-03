# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent session-owned individual power-gating inhibit controls."""

import dearpygui.dearpygui as dpg

from . import lowpower_profiles

_CONTROLLERS = (
    (0, "Graphics (GR)"), (4, "Memory subsystem (MS)"),
    (10, "MS DIFR SW-ASR"), (11, "MS DIFR CG"),
    (12, "NVDEC"), (13, "NVENC"), (14, "OFA"), (15, "NVJPG"),
)


class IndividualPowerGatingPanel:
    """Keep each controller's evidence and request independent."""

    def __init__(self, app):
        self.app = app
        self.snapshots = {index: None for index, _ in _CONTROLLERS}
        self.stale = {index: False for index, _ in _CONTROLLERS}
        self.release_pending = {index: False for index, _ in _CONTROLLERS}

    @staticmethod
    def _tag(index, suffix):
        return f"individual_power_gating_{index}_{suffix}"

    def build(self):
        dpg.add_text(
            "Each control holds an individual power-gating inhibit for this session. "
            "The request targets the selected controller; readback confirms a driver request, not physical power gating, memory power, or residency. "
            "Requests are not reapplied after a driver restart.",
            tag="individual_power_gating_explainer", wrap=self.app.s(900),
        )
        dpg.add_text("A saved profile describes only this instance's request. Load it manually to apply; "
                     "requests are not replayed after a driver restart.", wrap=self.app.s(900))
        for index, name in _CONTROLLERS:
            dpg.add_text(name, tag=self._tag(index, "heading"))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Refresh", tag=self._tag(index, "read"), callback=self.read, user_data=index)
                dpg.add_button(label="Save request profile", tag=self._tag(index, "save_profile"),
                               callback=self.app.open_save_profile, user_data=("lowpower_requests", "individual", index))
                dpg.add_button(label="Hold disabled", tag=self._tag(index, "hold"), callback=self.hold,
                               user_data=index, enabled=False)
                dpg.add_button(label="Release", tag=self._tag(index, "release"), callback=self.release,
                               user_data=index, enabled=False)
            dpg.add_text("Refresh status before requesting a change.", tag=self._tag(index, "status"),
                         wrap=self.app.s(900))
            dpg.add_input_text(tag=self._tag(index, "readback"), readonly=True, multiline=True,
                               width=-1, height=self.app.s(65))

    def _controller(self, index):
        return lowpower_profiles.controller(self.app, "individual", index)

    @staticmethod
    def _format(snapshot):
        support = "available" if snapshot.get("available") and snapshot.get("support") else "unavailable"
        lines = [f"{snapshot['name']} request support: {support}"]
        if snapshot.get("uncertain_outcome", False):
            lines.append("This instance's request outcome is uncertain; refresh status. Release may already have been attempted.")
        else:
            lines.append("This instance's disable request: " + ("held" if snapshot["owned_request"] else "not held"))
        masks = snapshot.get("reason_masks", {})
        if type(masks.get(0x48)) is int and type(masks.get(0x49)) is int:
            observed = "present" if masks[0x48] & 1 else "not present"
            lines.append(f"Reason-zero inhibit readback: {observed} (may include other owners)")
            lines.append(f"Reason masks: 0x48=0x{masks[0x48]:08X}, 0x49=0x{masks[0x49]:08X}")
        if snapshot.get("last_verification_error"):
            lines.append("Last verification error: " + str(snapshot["last_verification_error"]))
        return "\n".join(lines)

    def _configure_actions(self, index):
        snapshot = self.snapshots[index]
        safe = snapshot is not None and not snapshot.get("uncertain_outcome", False)
        owned = bool(safe and snapshot["owned_request"])
        masks = snapshot.get("reason_masks", {}) if snapshot else {}
        no_existing = (type(masks.get(0x48)) is int and type(masks.get(0x49)) is int
                       and not (masks[0x48] & 1))
        hold = bool(safe and not self.stale[index] and snapshot.get("support") and snapshot.get("available")
                    and not owned and no_existing)
        release = bool((owned or self.release_pending[index] or (snapshot and snapshot.get("uncertain_outcome", False)))
                       and not (snapshot and snapshot.get("release_attempted", False)))
        dpg.configure_item(self._tag(index, "hold"), enabled=hold)
        dpg.configure_item(self._tag(index, "release"), enabled=release)

    def _show(self, index, snapshot, message):
        self.snapshots[index] = snapshot
        self.stale[index] = False
        self.release_pending[index] = bool(snapshot.get("uncertain_outcome", False))
        dpg.set_value(self._tag(index, "readback"), self._format(snapshot))
        dpg.set_value(self._tag(index, "status"), message)
        self._configure_actions(index)

    def _failed(self, index, operation, exc):
        if operation != "refresh":
            self.snapshots[index] = None
            self.release_pending[index] = True
            dpg.set_value(self._tag(index, "readback"), "")
        self.stale[index] = True
        self._configure_actions(index)
        action = "Refresh status." if operation == "refresh" else "Refresh status; release may be needed or already attempted."
        message = f"{dict(_CONTROLLERS)[index]} {operation} failed: {exc}. {action}"
        dpg.set_value(self._tag(index, "status"), message)
        self.app.log(message, False)

    def read(self, sender=None, app_data=None, user_data=None):
        index = user_data
        try:
            self._show(index, self._controller(index).read(), "Status refreshed.")
        except Exception as exc:  # noqa: BLE001
            self._failed(index, "refresh", exc)

    def hold(self, sender=None, app_data=None, user_data=None):
        index = user_data
        if not self.app.guard():
            return
        try:
            self._show(index, lowpower_profiles.change(self.app, "individual", "disabled", index), "Disable request held by this instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed(index, "hold", exc)

    def release(self, sender=None, app_data=None, user_data=None):
        index = user_data
        try:
            snapshot = lowpower_profiles.change(self.app, "individual", "released", index)
            if snapshot.get("uncertain_outcome", False):
                message = ("Release outcome remains uncertain; no new release was dispatched."
                           if snapshot.get("release_attempted", False)
                           else "Release outcome remains uncertain; refresh status.")
            else:
                message = "This instance's request was released."
            self._show(index, snapshot, message)
        except Exception as exc:  # noqa: BLE001
            self._failed(index, "release", exc)
