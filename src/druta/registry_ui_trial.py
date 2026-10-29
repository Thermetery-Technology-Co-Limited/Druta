# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit framebuffer trial using the ordinary registry receipt helper."""

import hashlib
import struct
from dataclasses import asdict
from pathlib import Path

from . import all_pstates_registry, client_power_runtime, power_runtime, startup, vbios_fields
from . import registry_overrides as overrides
from .ui_capture import UiCapture

KEY = "pstates.all_editable"
FIELD = "pstate." + KEY
TAG = "reg_field_" + FIELD


class RegistryUiTrial(UiCapture):
    def __init__(self, dpg, app, directory, *, resume=False, kind="pstates"):
        self.root = Path(directory)
        self.resume = resume
        self.power_scroll_pending = 0
        if resume:
            self.trial = startup.read_json(self.root / "trial.json")
            if not isinstance(self.trial, dict) or self.trial.get("phase") not in ("applying", "restoring"):
                raise RuntimeError("Registry UI trial has no resumable phase")
        else:
            self.root.mkdir(exist_ok=False)
            if kind not in ("pstates", "power"):
                raise RuntimeError("Unknown registry UI trial")
            self.trial = {"schema": 1, "kind": kind, "phase": "starting", "error": None, "receipt": None}
            self._save_trial()
        self.kind = self.trial.get("kind", "pstates")
        if self.kind not in ("pstates", "power"):
            raise RuntimeError("Unknown saved registry UI trial")
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
        if self.kind == "power":
            return self._verify_power(receipt, target, phase)
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

    def _verify_power(self, receipt, target, phase):
        key = self.trial["field"]
        changes = {key: self.trial["wanted"]}
        if receipt.get("changes") != changes or any(receipt.get(k) for k in (
            "named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes"
        )):
            raise RuntimeError("Trial receipt does not contain exactly the selected power maximum")
        if receipt.get("plan") != self.trial["plan"]:
            raise RuntimeError("Trial power receipt differs from the original singleton plan")
        contract = receipt.get("policy_contracts", {})
        if set(contract) != {key}:
            raise RuntimeError("Trial power contract is missing or contains other fields")
        item = contract[key]
        expected_after = dict(self.trial["bounds"], maximum=self.trial["wanted"])
        if (item.get("mode") != "native" or item.get("before") != self.trial["original"]
                or item.get("rom_before_bounds") != self.trial["bounds"]
                or item.get("rom_after_bounds") != expected_after
                or item.get("policy") != self.trial["native_index"]
                or any(item.get(k) != self.trial["policy_identity"][k] for k in ("type", "channel", "unit"))):
            raise RuntimeError("Trial power contract differs from the staged selection")
        if receipt.get("before") != self.trial["registry_before"] or receipt.get("after") != self.trial["registry_after"]:
            raise RuntimeError("Receipt registry differs from the captured singleton plan")
        expected_state = {"prepared": "prepared", "active": "active", "restored": "restored"}[phase]
        if receipt.get("state") != expected_state:
            raise RuntimeError("Trial receipt state is not " + expected_state)
        registry = overrides.WindowsRegistry(target).snapshot()
        if registry != receipt["after" if phase == "active" else "before"]:
            raise RuntimeError("Registry readback differs from the saved receipt")
        image = overrides.read_vbios(self.app.gpu)
        digest = hashlib.sha256(image).hexdigest()
        expected_digest = receipt["after_image_sha256" if phase == "active" else "before_image_sha256"]
        if receipt["before_image_sha256"] != self.trial["image_sha256"] or digest != expected_digest:
            raise RuntimeError("Running image differs from the saved power receipt")
        rows, error = self.app.gpu.read_power_policies()
        if error:
            raise RuntimeError("Power policy readback failed: " + str(error))
        power_runtime.verify({"policies": rows}, changes, contract, restore=phase != "active")
        controller = client_power_runtime.ClientPowerController(self.app.gpu)
        metadata = controller.read_metadata()
        client = controller.read()
        if (client["identity"] != metadata["identity"] or client["binding"] != metadata["binding"]
                or client["control_hex"] != self.trial["client_control_hex"]
                or client["native_control_hex"] != self.trial["native_control_hex"]):
            raise RuntimeError("Client power request or peer controls changed")
        initial = startup.read_json(self.root / "power-initial.json")["client"]
        if client["requested_mw"] != initial["requested_mw"]:
            raise RuntimeError("Client power request changed")
        legacy = self.trial["binding"].get("layout") == "legacy32"
        positions = {
            "info_hex": (8 if legacy else client_power_runtime.INFO_BASE)
                        + self.trial["binding"]["client_index"] * client_power_runtime.INFO_STRIDE + 12,
            "role_info_hex": 20,
            "native_info_hex": (88 if legacy else client_power_runtime.NATIVE_INFO_BASE)
                               + self.trial["native_index"] * (228 if legacy else client_power_runtime.NATIVE_INFO_STRIDE) + 16,
        }
        for name, offset in positions.items():
            expected = bytearray.fromhex(initial[name])
            if offset + 4 > len(expected):
                raise RuntimeError("Client power INFO geometry is not understood")
            if phase == "active":
                struct.pack_into("<I", expected, offset, self.trial["wanted"])
            if client[name] != expected.hex() or metadata[name] != expected.hex():
                raise RuntimeError("Client power INFO changed beyond the selected maximum")
        binding = metadata["binding"]
        baseline = self.trial["binding"]
        if any(binding.get(k) != baseline.get(k) for k in ("client_index", "native_index", "semantic_role", "client", "units", "minimum_mw", "rated_mw")):
            raise RuntimeError("Client power role or policy binding changed")
        expected_max = self.trial["wanted"] if phase == "active" else self.trial["original"]
        if binding.get("maximum_mw") != expected_max or metadata.get("identity") != self.trial["metadata_identity"]:
            raise RuntimeError("Client power metadata differs from the expected maximum or adapter")
        startup.atomic_json(self.root / ("power-" + ("before" if phase == "prepared" else phase) + ".json"),
                            {"metadata": metadata, "client": client, "policies": rows, "image_sha256": digest})
        return receipt

    def _stage(self, name):
        self.dpg.set_value("registry_header", True)
        self.live_scroll = "registry_header"
        if name == "before":
            self.panel.refresh()
            if self.kind == "power":
                controller = client_power_runtime.ClientPowerController(self.app.gpu)
                metadata = controller.read_metadata()
                client = controller.read()
                if client["identity"] != metadata["identity"] or client["binding"] != metadata["binding"]:
                    raise RuntimeError("Client power request and metadata binding differ")
                binding = metadata["binding"]
                index = binding.get("native_index")
                if type(index) is not int or binding.get("semantic_role") != 1 or binding.get("units") != "mW":
                    raise RuntimeError("Board power role and native policy are unavailable")
                self.field = f"power.{index}.maximum"
                self.tag = "reg_field_" + self.field
                field = self.panel.fields.get(self.field, {})
                if (self.panel.active is not None or field.get("capability") != "editable"
                        or field.get("units") != "mW" or field.get("mask") is not None
                        or type(field.get("current")) is not int or self.field not in self.panel.inputs
                        or field["current"] != binding.get("maximum_mw")):
                    raise RuntimeError("An independently bound editable board power maximum is required")
                peers = [self.panel.fields.get(f"power.{index}.{name}", {})
                         for name in ("minimum", "default", "maximum")]
                binding_info = field.get("runtime_binding", {})
                policy_identity = {"type": binding_info.get("runtime_type"), "channel": field.get("channel"),
                                   "unit": field.get("units")}
                if (type(policy_identity["type"]) is not int or type(policy_identity["channel"]) is not int
                        or any(peer.get("policy") != index or peer.get("channel") != policy_identity["channel"]
                               or peer.get("units") != "mW" or peer.get("runtime_binding") != binding_info
                               or peer.get("policy_type") != binding_info.get("vbios_type")
                               or type(peer.get("current")) is not int for peer in peers)):
                    raise RuntimeError("Board power BIOS class, channel or bounds are not fully bound")
                bounds = {name: peer["current"] for name, peer in zip(
                    ("minimum", "default", "maximum"), peers, strict=True)}
                if (bounds["minimum"] != binding.get("minimum_mw")
                        or bounds["default"] != binding.get("rated_mw")
                        or binding_info.get("policy_index") != index
                        or binding_info.get("channel") != policy_identity["channel"]
                        or binding_info.get("units") != "mW"):
                    raise RuntimeError("Board power BIOS and client limits disagree")
                wanted = field["current"] + 5000
                if wanted > field["maximum"]:
                    raise RuntimeError("Board power maximum increase exceeds the editable field bounds")
                image = overrides.read_vbios(self.app.gpu)
                digest = hashlib.sha256(image).hexdigest()
                if digest != self.panel.inspection["sha256"]:
                    raise RuntimeError("Board power BIOS changed since panel refresh")
                plan = vbios_fields.plan(image, {self.field: wanted})
                if (not plan.get("patches") or any(patch.get("field_keys") != [self.field]
                                                   for patch in plan["patches"])):
                    raise RuntimeError("Board power plan is not confined to the selected maximum")
                target = overrides.bind_target(self.app.gpu)
                registry_before = overrides.WindowsRegistry(target).snapshot()
                identity = vbios_fields.inspect(image)["identity"]
                registry_after = overrides.compose_records(image, plan, registry_before,
                                                          identity["vbios_version_raw"], identity["oem_revision"])
                self.trial.update(target=asdict(target), field=self.field, native_index=index,
                                  original=field["current"], wanted=wanted, binding=binding, bounds=bounds,
                                  policy_identity=policy_identity,
                                  metadata_identity=metadata["identity"], image_sha256=digest, plan=plan,
                                  client_control_hex=client["control_hex"], native_control_hex=client["native_control_hex"],
                                  registry_before=registry_before, registry_after=registry_after)
                startup.atomic_json(self.root / "power-initial.json", {"metadata": metadata, "client": client})
                self.dpg.set_value(self.tag, field["current"] / self.panel.inputs[self.field][1])
                self.trial["phase"] = "before"
                self._save_trial()
                return self._frame_details(name)
            self.field, self.tag = FIELD, TAG
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
            self.field = self.trial.get("field", FIELD)
            self.tag = "reg_field_" + self.field
            factor = self.panel.inputs[self.field][1]
            self.dpg.set_value(self.tag, self.trial["wanted"] / factor if self.kind == "power" else self.trial["wanted"])
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
            self.field = self.trial.get("field", FIELD)
            self.tag = "reg_field_" + self.field
            field = self.panel.fields.get(self.field, {})
            expected = self.trial["wanted"] if name == "active" else self.trial["original"]
            shown = self.dpg.get_value(self.tag)
            if self.kind == "power":
                if field.get("current") != expected or shown * self.panel.inputs[self.field][1] != expected:
                    raise RuntimeError("Power input differs from verified readback")
            elif field.get("current") is not expected or shown is not expected:
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
        return self._frame_details(name)

    def _frame_details(self, name):
        self.dpg.set_value("ctl_clocks", "Developer registry trial: " + name)
        if name in ("before", "preview", "active", "restored"):
            parent = self.dpg.get_item_parent(self.tag)
            while parent:
                if self.dpg.get_item_type(parent).endswith("mvCollapsingHeader"):
                    self.dpg.set_value(parent, True)
                parent = self.dpg.get_item_parent(parent)
            self.live_scroll = "reg_preview" if name == "preview" else self.tag
            if self.kind == "power" and name != "preview":
                # Item positions can be relative to nested parents. Wait for a
                # rendered layout and use screen rectangles for page scrolling.
                self.live_scroll = None
                self.power_scroll_pending = 2
            widget = {"field": self.field, "value": self.dpg.get_value(self.tag),
                      "status": self.dpg.get_value("reg_status"), "preview": self.dpg.get_value("reg_preview")}
            if self.kind == "pstates":
                widget["checkbox"] = widget["value"]
            self.manifest.setdefault("widgets", {})[name] = widget
            self._save()
        return True

    def tick(self):
        if self.active and self.power_scroll_pending:
            self.power_scroll_pending -= 1
            if self.power_scroll_pending:
                return
            try:
                item_y = self.dpg.get_item_rect_min(self.tag)[1]
                page_y = self.dpg.get_item_pos("page_control")[1]
                scroll = self.dpg.get_y_scroll("page_control") + item_y - page_y - 80
                self.dpg.set_y_scroll("page_control", max(0, min(scroll, self.dpg.get_y_scroll_max("page_control"))))
                self.power_scroll_pending = 0
                # Let the scroll render before requesting the framebuffer.
                self.wait = max(self.wait, 2)
            except Exception as exc:
                self._finish(exc)
                return
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
