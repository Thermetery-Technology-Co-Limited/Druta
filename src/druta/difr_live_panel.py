# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small UI for this Druta instance's counted DIFR disable request."""

import dearpygui.dearpygui as dpg


class DifrLivePanel:
    """Render read/hold/release controls for one selected GPU instance."""

    def __init__(self, app):
        self.app = app
        self.snapshot = None

    def build(self):
        dpg.add_text(
            "Hold sends one counted DIFR disable request for this Druta instance. "
            "The driver-enabled readback is separate and does not report residency. "
            "Release this request before switching GPUs or closing Druta; it is not released automatically. "
            "Factory-disabled entries remain read-only.",
            tag="difr_live_explainer",
            wrap=self.app.s(900),
        )
        with dpg.group(horizontal=True):
            dpg.add_button(label="Read", tag="difr_live_read", callback=self.read)
            dpg.add_button(label="Hold disable request", tag="difr_live_hold",
                           callback=self.hold, enabled=False)
            dpg.add_button(label="Release this request", tag="difr_live_release",
                           callback=self.release, enabled=False)
        dpg.add_text("Read DIFR status before requesting a change.", tag="difr_live_status",
                     wrap=self.app.s(900))
        dpg.add_input_text(tag="difr_live_readback", readonly=True, multiline=True,
                           width=-1, height=self.app.s(90))

    def _controller(self):
        from .difr_live_control import DifrController

        gpu = self.app.gpu
        controllers = getattr(self.app, "_difr_live_controllers", None)
        if not isinstance(controllers, dict):
            controllers = {}
            self.app._difr_live_controllers = controllers
        key = id(gpu)
        controller = controllers.get(key)
        if controller is None or controller.gpu is not gpu:
            controller = DifrController(gpu)
            controllers[key] = controller
        return controller

    @staticmethod
    def _format(snapshot):
        masks = snapshot["reason_masks"]
        uncertain = snapshot.get("uncertain_outcome", False)
        lines = [
            "DIFR request support: " + ("available" if snapshot["support"] else "unavailable"),
            "Disable request control: " + ("present" if snapshot["present"] else "unavailable"),
            "Driver-enabled readback (not residency): "
            + ("yes" if snapshot["enabled_predicate"] else "no"),
        ]
        if uncertain:
            lines.append("This Druta instance's request outcome is uncertain.")
            lines.append("Do not send another Hold or Release request.")
        else:
            lines.append("This Druta instance's disable request: "
                         + ("held" if snapshot["owned_request"] else "not held"))
        if (not uncertain and not snapshot["owned_request"]
                and any(value & 1 for value in masks.values())):
            lines.append("A disable request already exists; this instance cannot release it.")
        return "\n".join(lines)

    def _configure_actions(self):
        snapshot = self.snapshot
        if snapshot is None:
            hold_enabled = release_enabled = False
        elif snapshot.get("uncertain_outcome", False):
            hold_enabled = release_enabled = False
        else:
            masks = snapshot["reason_masks"]
            hold_enabled = (snapshot["support"] and snapshot["present"] and not snapshot["owned_request"]
                            and not any(value & 1 for value in masks.values()))
            release_enabled = bool(snapshot["owned_request"])
        dpg.configure_item("difr_live_hold", enabled=hold_enabled)
        dpg.configure_item("difr_live_release", enabled=release_enabled)

    def _failed(self, operation, exc):
        self.snapshot = None
        self._configure_actions()
        message = f"DIFR {operation} failed: {exc}. Read current status before taking another action."
        dpg.set_value("difr_live_readback", "")
        dpg.set_value("difr_live_status", message)
        self.app.log(message, False)

    def _show(self, snapshot, prefix=""):
        self.snapshot = snapshot
        dpg.set_value("difr_live_readback", self._format(snapshot))
        dpg.set_value("difr_live_status", prefix or "DIFR status read.")
        self._configure_actions()

    def read(self, *unused):
        try:
            snapshot = self._controller().read()
            self._show(snapshot)
        except Exception as exc:  # noqa: BLE001
            self._failed("read", exc)

    def hold(self, *unused):
        if not self.app.guard():
            return
        try:
            snapshot = self._controller().hold()
            self._show(snapshot, "Counted disable request held by this Druta instance.")
        except Exception as exc:  # noqa: BLE001
            self._failed("hold", exc)

    def release(self, *unused):
        if not self.app.guard():
            return
        try:
            snapshot = self._controller().release()
            self._show(snapshot, "This Druta instance's disable request was released.")
        except Exception as exc:  # noqa: BLE001
            self._failed("release", exc)
