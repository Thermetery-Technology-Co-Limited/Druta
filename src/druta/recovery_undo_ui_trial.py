# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit panel Undo trial for a pending recovery receipt."""

from dataclasses import asdict
from pathlib import Path

from . import registry_overrides as overrides
from . import startup
from .recovery_ui_capture import RecoveryUiCapture


class RecoveryUndoUiTrial(RecoveryUiCapture):
    """Capture a pending receipt, invoke its real Undo, then capture restoration."""

    def __init__(self, dpg, app, directory, *, resume=False):
        self.root = Path(directory)
        self.resume = resume
        if resume:
            self.trial = startup.read_json(self.root / "trial.json")
            if not isinstance(self.trial, dict) or self.trial.get("kind") != "recovery-undo" or self.trial.get("phase") != "restoring":
                raise RuntimeError("No resumable recovery Undo trial")
        else:
            self.root.mkdir(exist_ok=False)
            self.trial = {"schema": 1, "kind": "recovery-undo", "phase": "starting", "error": None, "receipt": None}
            self._save_trial()
        super().__init__(dpg, app, self.root / ("restoring" if resume else "initial"))
        if not self.active:
            return
        try:
            result = app._recovery_result
            target = asdict(overrides.bind_target(app.gpu))
            details = result.get("message", "").rsplit("\nDetails: ", 1)
            if result.get("slot") != target.get("slot") or len(details) != 2:
                raise RuntimeError("Recovery result target or receipt details differ")
            if not resume:
                receipt = self.panel.active
                if (not receipt or receipt.get("state") != "recovery-required" or
                        not self.dpg.get_item_configuration("reg_restore_button")["enabled"] or
                        result.get("ok") is not False or result.get("reboot_required") or
                        receipt.get("target") != target or receipt.get("parent") is not None or
                        receipt.get("path") != details[1] or
                        overrides.load_receipt(receipt["path"]) != receipt or
                        overrides.active_receipt(overrides.Target(**target)) != receipt):
                    raise RuntimeError("A bound recovery-required receipt and enabled Undo are required")
                self.trial.update(receipt=receipt["path"], target=target, pending_receipt=receipt, phase="pending")
                self.stages = ["recovery-panel", "recovery-log", "undo"]
                self.app._registry_ui_trial = str(self.root)
            else:
                receipt = overrides.load_receipt(self.trial["receipt"])
                pending = self.trial["pending_receipt"]
                if (target != self.trial.get("target") or receipt.get("target") != target or
                        any(receipt.get(key) != pending.get(key) for key in ("path", "target", "id", "before", "after", "plan", "parent")) or
                        receipt.get("state") != "restored" or receipt.get("ok") is not True or
                        result.get("ok") is not True or result.get("reboot_required") or
                        receipt["path"] != details[1] or
                        overrides.active_receipt(overrides.Target(**target)) is not None or self.panel.active is not None):
                    raise RuntimeError("Recovery helper restoration was not verified")
                self.trial.update(phase="restored", helper_result=result, restored_receipt=receipt)
                self.stages = ["recovery-panel", "recovery-log"]
            self._save_trial()
            self.stage = 0
            self._stage(self.stages[0])
        except Exception as exc:
            self._finish(exc)

    def _save_trial(self):
        startup.atomic_json(self.root / "trial.json", self.trial)

    def _stage(self, name):
        if name != "undo":
            return super()._stage(name)
        if (self.panel.active != self.trial["pending_receipt"] or
                asdict(overrides.bind_target(self.app.gpu)) != self.trial["target"] or
                overrides.active_receipt(overrides.Target(**self.trial["target"])) != self.trial["pending_receipt"]):
            raise RuntimeError("Pending recovery receipt or target changed before Undo")
        self.trial["phase"] = "restoring"
        self._save_trial()
        self.active = False
        self.panel.restore()
        if not getattr(self.app, "_closing", False):
            raise RuntimeError("Registry panel did not launch the recovery Undo helper")
        self.manifest["state"] = "handoff"
        self._save()
        return True

    def _finish(self, error=None):
        self.app._registry_ui_trial = None
        prior = self.trial.get("phase")
        if error is not None:
            self.trial["error"] = self.trial.get("error") or str(error)
            self.trial["phase"] = "attention"
            self._save_trial()
        super()._finish(error)
        if self.manifest["errors"]:
            self.trial["error"] = self.trial.get("error") or self.manifest["errors"][0]
            self.trial["phase"] = "attention"
            self._save_trial()
            path = self.directory / "failed-result.json"
        elif self.resume and prior == "restored":
            self.trial["phase"] = "complete"
            self._save_trial()
            path = self.root / "trial-result.json"
        else:
            return
        if not path.exists():
            startup.atomic_json(path, {"schema": 1, "phase": self.trial["phase"], "error": self.trial.get("error"),
                                       "receipt": self.trial.get("receipt"), "manifest_state": self.manifest["state"],
                                       "recovery_check_required": self.trial["phase"] == "attention"})
