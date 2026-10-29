# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit framebuffer trial using the ordinary registry receipt helper."""

from dataclasses import asdict
from pathlib import Path

from . import all_pstates_registry, startup
from . import registry_overrides as overrides
from .ui_capture import UiCapture

KEY = "pstates.all_editable"
FIELD = "pstate." + KEY
TAG = "reg_field_" + FIELD


class RegistryUiTrial(UiCapture):
    def __init__(self, dpg, app, directory, *, resume=False):
        self.root = Path(directory)
        self.resume = resume
        if resume:
            self.trial = startup.read_json(self.root / "trial.json")
            if not isinstance(self.trial, dict) or self.trial.get("phase") not in ("applying", "restoring"):
                raise RuntimeError("Registry UI trial has no resumable phase")
        else:
            self.root.mkdir(exist_ok=False)
            self.trial = {"schema": 1, "phase": "starting", "error": None, "receipt": None}
            self._save_trial()
        phase = self.trial["phase"]
        super().__init__(dpg, app, self.root / ("initial" if not resume else phase))
        if not self.active:
            return
        try:
            if self.panel is None:
                raise RuntimeError("Registry panel is unavailable")
            self.app._registry_ui_trial = str(self.root)
            self.stages = ["before", "preview", "apply"] if not resume else (
                ["active", "restore"] if phase == "applying" else ["restored"]
            )
            self.stage = 0
            self._stage(self.stages[0])
        except Exception as exc:
            self._finish(exc)

    def _save_trial(self):
        startup.atomic_json(self.root / "trial.json", self.trial)

    def _verify(self, phase):
        receipt = overrides.load_receipt(self.trial["receipt"])
        target = overrides.bind_target(self.app.gpu)
        if asdict(target) != self.trial["target"] or receipt["target"] != self.trial["target"]:
            raise RuntimeError("Trial adapter or receipt target changed")
        expected = {KEY: self.trial["wanted"]}
        if (receipt.get("pstate_changes") != expected or any(receipt.get(k) for k in
                ("changes", "named_changes", "flag_changes", "clock_vf_changes", "startup_changes"))):
            raise RuntimeError("Trial receipt does not contain exactly the selected P-state change")
        contract = receipt.get("all_pstate_contracts", {})
        if (set(contract) != {KEY} or contract[KEY].get("before") is not self.trial["original"]
                or contract[KEY].get("after") is not self.trial["wanted"]):
            raise RuntimeError("Trial P-state contract differs from the staged selection")
        if receipt.get("before") != self.trial["registry_before"]:
            raise RuntimeError("Receipt original registry differs from the captured baseline")
        if phase == "prepared" and receipt.get("state") != "prepared":
            raise RuntimeError("Trial receipt is not prepared")
        if phase != "prepared":
            expected_state = "active" if phase == "active" else "restored"
            if receipt.get("state") != expected_state:
                raise RuntimeError("Trial receipt state is not " + expected_state)
        registry = overrides.WindowsRegistry(target).snapshot()
        if registry != receipt["after" if phase == "active" else "before"]:
            raise RuntimeError("Registry readback differs from the saved receipt")
        observation, error = all_pstates_registry.read(self.app.gpu)
        if error:
            raise RuntimeError("P-state readback failed: " + error)
        all_pstates_registry.verify(observation, contract, restore=phase != "active")
        startup.atomic_json(self.root / ("pstates-" + ("before" if phase == "prepared" else phase) + ".json"),
                            observation)
        return receipt

    def _stage(self, name):
        self.dpg.set_value("registry_header", True)
        self.live_scroll = "registry_header"
        if name == "before":
            self.panel.refresh()
            field = self.panel.fields.get(FIELD, {})
            if (self.panel.active is not None or field.get("capability") != "editable"
                    or type(field.get("current")) is not bool or FIELD not in self.panel.inputs):
                raise RuntimeError("An independent editable P-state baseline is required")
            target = overrides.bind_target(self.app.gpu)
            self.trial.update(target=asdict(target), original=field["current"], wanted=not field["current"],
                              registry_before=overrides.WindowsRegistry(target).snapshot())
            self.dpg.set_value(TAG, field["current"])
            self.trial["phase"] = "before"
            self._save_trial()
        elif name == "preview":
            self.dpg.set_value(TAG, self.trial["wanted"])
            self.panel.prepare()
            receipt = self.panel.preview
            if not receipt:
                raise RuntimeError("Registry panel did not prepare a receipt: " + self.dpg.get_value("reg_status"))
            self.trial["receipt"] = receipt["path"]
            self._verify("prepared")
            self.trial.update(phase="preview", preview=self.dpg.get_value("reg_preview"),
                              status=self.dpg.get_value("reg_status"))
            self._save_trial()
        elif name in ("active", "restored"):
            result = getattr(self.app, "_recovery_result", None)
            self.trial.setdefault("helper_results", []).append(result)
            failed = not result or not result.get("ok") or result.get("reboot_required")
            if failed:
                self.trial["error"] = self.trial.get("error") or "Registry helper failed: " + str(result)
                self._save_trial()
            self.panel.refresh()
            receipt = self._verify(name)
            if failed and name != "active":
                raise RuntimeError(self.trial["error"])
            field = self.panel.fields.get(FIELD, {})
            expected = self.trial["wanted"] if name == "active" else self.trial["original"]
            if field.get("current") is not expected or self.dpg.get_value(TAG) is not expected:
                raise RuntimeError("P-state checkbox differs from verified readback")
            if name == "active" and (not self.panel.active or self.panel.active["path"] != receipt["path"]):
                raise RuntimeError("Panel did not recover the same active receipt")
            self.trial["phase"] = name
            self.trial["status"] = self.dpg.get_value("reg_status")
            self._save_trial()
        elif name in ("apply", "restore"):
            self._verify("prepared" if name == "apply" else "active")
            self.trial["phase"] = "applying" if name == "apply" else "restoring"
            self._save_trial()
            self.active = False
            getattr(self.panel, name)()
            if not getattr(self.app, "_closing", False):
                raise RuntimeError("Registry panel did not launch the " + name + " helper")
            self.manifest["state"] = "handoff"
            self._save()
        self.dpg.set_value("ctl_clocks", "Developer registry trial: " + name)
        if name in ("before", "preview", "active", "restored"):
            parent = self.dpg.get_item_parent(TAG)
            while parent:
                if self.dpg.get_item_type(parent).endswith("mvCollapsingHeader"):
                    self.dpg.set_value(parent, True)
                parent = self.dpg.get_item_parent(parent)
            self.live_scroll = "reg_preview" if name == "preview" else TAG
            self.manifest.setdefault("widgets", {})[name] = {
                "checkbox": self.dpg.get_value(TAG), "status": self.dpg.get_value("reg_status"),
                "preview": self.dpg.get_value("reg_preview"),
            }
            self._save()
        return True

    def tick(self):
        super().tick()
        if not self.active and self.trial.get("phase") == "restored" and not self.manifest["errors"]:
            self.trial["phase"] = "attention" if self.trial.get("error") else "complete"
            self._save_trial()

    def _finish(self, error=None):
        self.app._registry_ui_trial = None
        if error is not None:
            self.trial["error"] = self.trial.get("error") or str(error)
            self.trial["phase"] = "attention"
            self._save_trial()
        super()._finish(error)
