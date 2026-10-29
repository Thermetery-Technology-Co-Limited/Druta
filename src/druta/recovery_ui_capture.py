# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only framebuffer capture of an existing recovery-result relaunch."""

import os

from .ui_capture import UiCapture


class RecoveryUiCapture(UiCapture):
    """Capture the registry recovery controls and the actual Control log."""

    def __init__(self, dpg, app, directory):
        self.scroll_wait = 0
        self.scroll_target = None
        super().__init__(dpg, app, directory)
        if not self.active:
            return
        try:
            if not getattr(app, "_recovery_result_valid", False) or not self.panel:
                raise RuntimeError("Accepted recovery result and registry panel are required")
            self.manifest["process_id"] = os.getpid()
            self.manifest["recovery_result"] = app._recovery_result.copy()
            self.stages = ["recovery-panel", "recovery-log"]
            self.stage = 0
            self._stage(self.stages[0])
            self._save()
        except Exception as exc:
            self._finish(exc)

    def _restore_widgets(self):
        if self.snapshot is not None:
            for tag, value in self.snapshot.get("headers", {}).items():
                self.dpg.set_value(tag, value)

    def _stage(self, name):
        self.dpg.set_value("registry_header", True)
        if name == "recovery-panel":
            self.panel.recover()
            status = self.dpg.get_value("reg_status")
            if status.startswith("Could not discover saved undo:"):
                raise RuntimeError(status)
            self.scroll_target = "registry_header"
        else:
            self.scroll_target = "log"
        self.scroll_wait = 2
        log = self.dpg.get_value("log")
        if "Device recovery: " + self.app._recovery_result["message"] not in log:
            raise RuntimeError("Recovery result is not present in the Control log")
        self.manifest.setdefault("views", {})[name] = {
            "status": self.dpg.get_value("reg_status"),
            "undo_enabled": self.dpg.get_item_configuration("reg_restore_button")["enabled"],
            "receipt": ({"path": self.panel.active["path"], "state": self.panel.active["state"]}
                        if self.panel.active is not None else None),
            "log": log,
        }
        return True

    def tick(self):
        if self.active and self.scroll_wait:
            try:
                self.scroll_wait -= 1
                if not self.scroll_wait:
                    target = self.dpg.get_item_state(self.scroll_target)["rect_min"][1]
                    origin = self.dpg.get_item_state("page_control")["pos"][1]
                    current = self.dpg.get_y_scroll("page_control")
                    maximum = self.dpg.get_y_scroll_max("page_control")
                    self.dpg.set_y_scroll("page_control", max(0, min(current + target - origin, maximum)))
                return
            except Exception as exc:
                self._finish(exc)
                return
        super().tick()

    def _finish(self, error=None):
        super()._finish(error)
        self.dpg.stop_dearpygui()
