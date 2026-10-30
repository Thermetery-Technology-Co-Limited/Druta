# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Selected-adapter registry transactions with durable, exact undo.

Registry writes are staging. Only a fresh-process image read after the GPU
restart confirms activation. No function in this module reboots Windows.
"""

from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import hashlib
import os
import re
import struct
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from . import (
    all_pstates_registry,
    bios_runtime,
    clock_bios_runtime,
    clock_vf_registry,
    devicerecovery,
    devicereset,
    factory_clock_runtime,
    fan_policy_runtime,
    fan_runtime,
    fan_stop_runtime,
    grpg_runtime,
    linked_current_runtime,
    lpwr_runtime,
    media_lpwr_runtime,
    model_registry,
    ms_runtime,
    nvdec_runtime,
    power_runtime,
    rated_clock_runtime,
    registry_flags,
    startup,
    voltage_bios_runtime,
    voltage_ceiling_runtime,
    voltage_rel_runtime,
    voltage_request_replay,
    voltage_startup_runtime,
)

_CLASS = r"{4d36e968-e325-11ce-bfc1-08002be10318}"
_RECORD = re.compile(r"romOverride([0-9a-f]{2})\Z", re.I)
_IMAGE_CAPACITY = 0xFA000
_RECORD_PAYLOAD = 246  # 256-byte registry buffer, 10-byte header
_BINARY, _DWORD = 3, 4
_SPEEDO_VALUES = frozenset(("RMHwSpeedoOverride", "RMHwSpeedo1Override", "RMHwSpeedo2Override"))
_MODEL_VALUES = _SPEEDO_VALUES | model_registry.IDDQ_NAMES


class OverrideError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    slot: str
    instance_id: str
    driver_key: str


def managed_name(name):
    return bool(
        _RECORD.fullmatch(name)
        or name.casefold()
        in {
            "vbios",
            "enablegpufirmware",
            "rmperflimitsoverride",
            voltage_startup_runtime.REGISTRY_NAME.casefold(),
            all_pstates_registry.REGISTRY_NAME.casefold(),
            clock_vf_registry.REGISTRY_NAME.casefold(),
            *(key.casefold() for key in _MODEL_VALUES),
        }
    )


def _encoded(value, kind):
    if kind == _BINARY:
        return {"type": kind, "value": bytes(value).hex()}
    if kind == _DWORD:
        return {"type": kind, "value": int(value)}
    # Preserve pre-existing unexpected types, rather than normalizing them.
    return {"type": kind, "value": value}


def _decoded(entry):
    return bytes.fromhex(entry["value"]) if entry["type"] == _BINARY else entry["value"]


def bind_target(gpu):
    """Join the paired API handle to Windows' current driver key."""
    if gpu.pairing_error or not gpu.nvapi.ok or not gpu.nvml.ok:
        raise OverrideError("NVAPI and NVML must identify the same selected GPU")
    target = devicereset.resolve_target(gpu.slot())
    identity = gpu.nvapi.selected or {}
    hardware = target.instance_id.split("\\", 2)[1].upper()
    if (
        f"DEV_{identity.get('devid', -1):04X}" not in hardware
        or f"SUBSYS_{identity.get('subsys', -1):08X}" not in hardware
    ):
        raise OverrideError("Windows and NVAPI adapter identities disagree")
    return Target(target.slot, target.instance_id, driver_key_for_instance(target.instance_id))


def driver_key_for_instance(instance_id):
    """Read Windows' mapping even when the GPU has not started successfully."""
    import winreg

    if not instance_id.upper().startswith("PCI\\VEN_10DE&"):
        raise OverrideError("The saved target is not an NVIDIA PCI device")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Enum\\" + instance_id) as key:
        driver, kind = winreg.QueryValueEx(key, "Driver")
    if kind != winreg.REG_SZ or not re.fullmatch(re.escape(_CLASS) + r"\\[0-9]{4}", driver, re.I):
        raise OverrideError("Windows returned an unexpected display-driver key")
    return "SYSTEM\\CurrentControlSet\\Control\\Class\\" + driver


def read_vbios(gpu):
    """Two identical bounded NVAPI V2 reads, with no registry or GPU writes."""
    if gpu.pairing_error or not gpu.nvapi.ok:
        raise OverrideError("The selected NVAPI adapter is unavailable")

    class Image(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("size", ctypes.c_uint32), ("buffer", ctypes.c_void_p)]

    if ctypes.sizeof(Image) != 16:
        raise OverrideError("VBIOS getter requires the understood 64-bit layout")
    getter = gpu.nvapi._i(0xFC13EE11, ctypes.c_void_p, ctypes.POINTER(Image))
    if not getter:
        raise OverrideError("This driver does not expose the VBIOS image getter")

    def once():
        data = (ctypes.c_ubyte * _IMAGE_CAPACITY)()
        block = Image(0x20010, _IMAGE_CAPACITY, ctypes.addressof(data))
        code = getter(gpu.nvapi.gpu, ctypes.byref(block))
        if code != 0:
            raise OverrideError(f"VBIOS getter returned status {code}")
        if block.version != 0x20010 or not 32 <= block.size <= _IMAGE_CAPACITY:
            raise OverrideError("VBIOS getter returned an unexpected size/version")
        return bytes(data[: block.size])

    with gpu._lock:
        for _ in range(3):
            first, second = once(), once()
            if first == second:
                return first
    raise OverrideError("VBIOS image changed between reads; retry when the GPU is stable")


def snapshot_cli(argv):
    """Private child command: load the driver once, save observations, exit."""
    from .nvbackend import GPU

    parser = argparse.ArgumentParser(prog="Druta --registry-snapshot")
    parser.add_argument("slot")
    parser.add_argument("output")
    args = parser.parse_args(argv)
    path = Path(args.output).resolve()
    if path.parent != state_directory().resolve() or not re.fullmatch(r"probe-[0-9a-f]{32}\.json", path.name):
        raise OverrideError("Invalid observation output path")
    gpu = GPU(args.slot)
    target = bind_target(gpu)
    try:
        image, image_error = read_vbios(gpu), None
    except Exception as exc:
        image, image_error = None, str(exc)
    policies, error = gpu.read_power_policies()
    vfe, vfe_error = read_model_inputs(gpu)
    flags, flag_error = registry_flags.read(gpu)
    pstates, pstate_error = all_pstates_registry.read(gpu)
    clock_vf, clock_vf_error = clock_vf_registry.read(gpu)
    clock_bios, clock_bios_error = clock_bios_runtime.read(gpu)
    factory_clock, factory_clock_error = factory_clock_runtime.read(gpu)
    rated_clock, rated_clock_error = rated_clock_runtime.read(gpu)
    voltage_policy, voltage_policy_error = voltage_bios_runtime.read(gpu)
    voltage_ceiling, voltage_ceiling_error = voltage_ceiling_runtime.read(gpu)
    linked_current, linked_current_error = linked_current_runtime.read(gpu)
    try:
        clock_ranges, clock_error = bios_runtime.read_clock_ranges(gpu)
    except Exception as exc:
        clock_ranges, clock_error = {}, str(exc)
    try:
        thermal, thermal_error = bios_runtime.read_thermal_thresholds(gpu)
    except Exception as exc:
        thermal, thermal_error = {}, str(exc)
    fans, fan_error = fan_runtime.read(gpu)
    fan_policies, fan_policy_error = fan_policy_runtime.read(gpu)
    fan_stop, fan_stop_error = fan_stop_runtime.read(gpu)
    try:
        from .voltage_request_gate import VoltageRequestGate

        voltage_gate, voltage_gate_error = VoltageRequestGate(gpu).read(), None
    except Exception as exc:
        voltage_gate, voltage_gate_error = None, str(exc)
    startup.atomic_json(
        path,
        {
            "target": asdict(target),
            "image": base64.b64encode(image).decode("ascii") if image is not None else None,
            "image_error": image_error,
            "static": gpu.static,
            "policies": policies,
            "policy_error": error,
            "vfe": vfe,
            "vfe_error": vfe_error,
            "driver_flags": flags,
            "driver_flags_error": flag_error,
            "all_pstates": pstates,
            "all_pstates_error": pstate_error,
            "clock_vf": clock_vf,
            "clock_vf_error": clock_vf_error,
            "clock_bios": clock_bios,
            "clock_bios_error": clock_bios_error,
            "factory_clock": factory_clock,
            "factory_clock_error": factory_clock_error,
            "rated_clock": rated_clock,
            "rated_clock_error": rated_clock_error,
            "voltage_policy": voltage_policy,
            "voltage_policy_error": voltage_policy_error,
            "voltage_ceiling": voltage_ceiling,
            "voltage_ceiling_error": voltage_ceiling_error,
            "linked_current": linked_current,
            "linked_current_error": linked_current_error,
            "clock_ranges": clock_ranges,
            "clock_ranges_error": clock_error,
            "thermal_thresholds": thermal,
            "thermal_thresholds_error": thermal_error,
            "fan_bounds": fans,
            "fan_bounds_error": fan_error,
            "fan_policies": fan_policies,
            "fan_policies_error": fan_policy_error,
            "fan_stop": fan_stop,
            "fan_stop_error": fan_stop_error,
            "voltage_request_gate": voltage_gate,
            "voltage_request_gate_error": voltage_gate_error,
            "configured_power_mw": gpu.read_power_limit_mw(),
        },
    )
    return 0


def fresh_snapshot(slot):
    """A new process discards all NVAPI/NVML caches across device restart."""
    path = state_directory() / ("probe-" + uuid.uuid4().hex + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    command = startup.application_command("--registry-snapshot", slot, str(path), windowed=True)
    result = subprocess.run(
        command, capture_output=True, timeout=35, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    if result.returncode != 0 or not path.is_file():
        raise OverrideError("Fresh driver observation failed (exit " + str(result.returncode) + ")")
    data = startup.read_json(path)
    data["target"] = Target(**data["target"])
    data["image"] = base64.b64decode(data["image"], validate=True) if data["image"] is not None else None
    return data


def verify_policy_fields(snapshot, changes, contracts=None, *, restore=False):
    """Verify recognized power metadata against the driver's policy getter."""
    try:
        return power_runtime.verify(snapshot, changes, contracts, restore=restore)
    except power_runtime.PowerRuntimeError as exc:
        raise OverrideError(str(exc)) from exc


def policy_contracts(gpu, inspection, changes):
    """Bind selected BIOS fields to actual runtime units, channel and metadata."""
    try:
        return power_runtime.contracts(gpu, inspection, changes)
    except power_runtime.PowerRuntimeError as exc:
        raise OverrideError(str(exc)) from exc


def read_model_inputs(gpu):
    from .vfe_inputs import VfeInputReader

    try:
        return asdict(VfeInputReader(gpu).read()), None
    except Exception as exc:
        return None, str(exc)


def model_input(vfe, name):
    matches = [item for item in (vfe or {}).get("inputs", ()) if item["registry_name"] == name]
    if len(matches) != 1 or not matches[0]["cache_ready"] or matches[0]["signed"]:
        raise OverrideError(name + ": a unique, ready unsigned model input is required")
    return matches[0]


def compose_named(snapshot, changes, vfe):
    """Compose typed direct Speedo/IDDQ model-input substitutions."""
    desired = copy.deepcopy(snapshot)
    if not changes or any(name not in _MODEL_VALUES for name in changes):
        raise OverrideError("An unvalidated named registry control was selected")
    if model_registry.IDDQ_NAMES.intersection(changes):
        try:
            model_registry.contracts(vfe, changes)
        except model_registry.ModelRegistryError as exc:
            raise OverrideError(str(exc)) from exc
    for name, value in changes.items():
        item = model_input(vfe, name)
        if type(value) is not int or not 0 < value <= 0xFFFFFFFF or not item["minimum"] <= value <= item["maximum"]:
            raise OverrideError(name + ": request must be an integer within the reported model-input range")
        existing = next((key for key in desired if key.casefold() == name.casefold()), name)
        desired[existing] = _encoded(value, _DWORD)
    return desired


def validate_existing_model_inputs(snapshot, vfe):
    """Reject a pending named override that a restart would newly activate."""
    entries = {name.casefold(): entry for name, entry in snapshot.items()}
    visible = {item["registry_name"] for item in (vfe or {}).get("inputs", ())}
    for name in _MODEL_VALUES:
        entry = entries.get(name.casefold())
        if entry is None and name not in visible:
            continue
        if (
            name in model_registry.IDDQ_NAMES
            and entry is None
            and not any(
                item.get("override_present") for item in (vfe or {}).get("inputs", ()) if item["registry_name"] == name
            )
        ):
            # An unavailable unrelated IDDQ input need not block other controls.
            continue
        item = model_input(vfe, name)
        if entry is None:
            agrees = not item["override_present"]
        else:
            agrees = (
                entry["type"] == _DWORD
                and item["override_present"]
                and item["override"] == entry["value"] == item["active"]
            )
        if not agrees:
            raise OverrideError(name + ": registry and active model input disagree; reconcile pending settings first")


def verify_named_inputs(snapshot, receipt, *, restore=False):
    checked = []
    vfe = snapshot.get("vfe")
    if receipt.get("model_peer_contracts"):
        try:
            return model_registry.verify(vfe, receipt["model_peer_contracts"], restore=restore)
        except model_registry.ModelRegistryError as exc:
            raise OverrideError(str(exc)) from exc
    for name, value in receipt.get("named_changes", {}).items():
        current = model_input(vfe, name)
        before = receipt["model_contracts"][name]
        immutable = (
            "index",
            "fuse_id",
            "fuse_version_field_id",
            "raw_hardware",
            "raw_hardware_signed",
            "default",
            "default_signed",
            "hardware_scale_q12",
            "hardware_offset",
        )
        if any(current[key] != before[key] for key in immutable):
            raise OverrideError(name + ": model-input identity or hardware data changed")
        wanted = (
            {key: before[key] for key in ("active", "override", "override_present")}
            if restore
            else {"active": value, "override": value, "override_present": True}
        )
        if any(current[key] != desired for key, desired in wanted.items()):
            raise OverrideError(name + ": active driver readback differs from the registry request")
        checked.append(name)
    return checked


class WindowsRegistry:
    def __init__(self, target):
        if driver_key_for_instance(target.instance_id).casefold() != target.driver_key.casefold():
            raise OverrideError("Windows' driver registry mapping differs from the saved target")
        self.target = target

    def snapshot(self):
        import winreg

        result = {}
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, self.target.driver_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                name, value, kind = winreg.EnumValue(key, i)
                if managed_name(name):
                    result[name] = _encoded(value, kind)
        return result

    def replace(self, expected, desired):
        """Compare the whole managed set, then modify only differing values."""
        import winreg

        if self.snapshot() != expected:
            raise OverrideError("Registry settings changed since the preview; refresh before applying")
        if not all(managed_name(name) for name in set(expected) | set(desired)):
            raise OverrideError("Transaction contains an unrelated registry value")
        changed = []
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            self.target.driver_key,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
        ) as key:
            try:
                for name in sorted(set(expected) | set(desired)):
                    if expected.get(name) == desired.get(name):
                        continue
                    changed.append(name)
                    if name in desired:
                        winreg.SetValueEx(key, name, 0, desired[name]["type"], _decoded(desired[name]))
                    else:
                        winreg.DeleteValue(key, name)
                winreg.FlushKey(key)
                if self.snapshot() != desired:
                    raise OverrideError("Registry readback differs from the planned settings")
            except Exception:
                # Undo our writes only. Refuse to erase a concurrent outside edit.
                actual = self.snapshot()
                for name in reversed(changed):
                    if actual.get(name) not in (expected.get(name), desired.get(name)):
                        raise OverrideError(f"Concurrent registry change to {name}; recovery is required") from None
                    if name in expected:
                        winreg.SetValueEx(key, name, 0, expected[name]["type"], _decoded(expected[name]))
                    elif name in actual:
                        winreg.DeleteValue(key, name)
                winreg.FlushKey(key)
                raise


def _record_indices(snapshot):
    found = {}
    for name, entry in snapshot.items():
        match = _RECORD.fullmatch(name)
        if not match:
            continue
        index = int(match[1], 16)
        if index in found:
            raise OverrideError("Duplicate registry record indices")
        found[index] = name
        if entry["type"] != _BINARY:
            raise OverrideError(f"Existing {name} is not binary; preserve it and resolve the conflict first")
    if set(found) != set(range(len(found))):
        raise OverrideError("Existing override records have a gap; appending would activate hidden records")
    return found


def compose_records(image, plan, snapshot, version, oem, *, firmware_policy="preserve"):
    """Append minimal patches; preserve every pre-existing record byte."""
    if firmware_policy not in ("preserve", "legacy-enable"):
        raise OverrideError("Unknown saved firmware-selector policy")
    if hashlib.sha256(image).hexdigest() != plan["baseline_sha256"]:
        raise OverrideError("The running BIOS changed since the preview")
    if type(version) is not int or not 0 < version <= 0xFFFFFFFF or type(oem) is not int or not 0 <= oem <= 255:
        raise OverrideError("The BIOS selector is not understood")
    records = _record_indices(snapshot)
    desired = copy.deepcopy(snapshot)
    expected = bytearray(image)
    touched = set()
    chunks = []
    for patch in plan["patches"]:
        at = patch["offset"]
        before, after = bytes.fromhex(patch["before"]), bytes.fromhex(patch["after"])
        if (
            type(at) is not int
            or at < 0
            or not before
            or len(before) != len(after)
            or at + len(before) > len(image)
            or image[at : at + len(before)] != before
        ):
            raise OverrideError("A patch does not match the captured image")
        region = set(range(at, at + len(before)))
        if touched & region:
            raise OverrideError("Planned patches overlap")
        touched |= region
        expected[at : at + len(after)] = after
        for start in range(0, len(after), _RECORD_PAYLOAD):
            payload = after[start : start + _RECORD_PAYLOAD]
            chunks.append(
                struct.pack(">I", version)
                + bytes([oem])
                + struct.pack("<I", at + start)
                + bytes([len(payload)])
                + payload
            )
    if not chunks:
        raise OverrideError("No BIOS field changes were selected")
    if len(records) + len(chunks) > 256:
        raise OverrideError("The existing overrides leave insufficient record slots; restore previous changes first")
    if hashlib.sha256(expected).hexdigest() != plan["expected_sha256"]:
        raise OverrideError("Patch result differs from the planner's expected image")
    for i, payload in enumerate(chunks, len(records)):
        desired[f"romOverride{i:02X}"] = _encoded(payload, _BINARY)
    if firmware_policy == "legacy-enable":
        # Compatibility for prepared receipts made by older builds. New BIOS
        # edits preserve firmware policy; patch consumption has independent
        # exact-image and semantic checks after the selected GPU restarts.
        flag_name = next((n for n in desired if n.casefold() == "enablegpufirmware"), "EnableGpuFirmware")
        desired[flag_name] = _encoded(0x11, _DWORD)
    return desired


def validate_existing_image(snapshot, image, version, oem):
    """Refuse a restart if already-staged overrides disagree with live BIOS."""
    indices = _record_indices(snapshot)
    vbios = next((value for name, value in snapshot.items() if name.casefold() == "vbios"), None)
    expected = bytearray(image)
    if vbios is not None:
        if vbios["type"] != _BINARY:
            raise OverrideError("The existing vbios override format is not understood")
        expected = bytearray(_decoded(vbios))
        if len(expected) != len(image):
            raise OverrideError("The existing vbios override does not match the captured image extent")
    for index in sorted(indices):
        raw = _decoded(snapshot[indices[index]])
        if len(raw) < 10 or len(raw) > 256 or len(raw) != 10 + raw[9]:
            raise OverrideError("Existing BIOS patch has an unrecognized record extent")
        selected = int.from_bytes(raw[:4], "big")
        if selected and (selected != version or (raw[4] and raw[4] != oem)):
            continue
        offset, size = int.from_bytes(raw[5:9], "little"), raw[9]
        if offset + size > len(expected):
            raise OverrideError("Existing BIOS patch extends beyond the captured image")
        expected[offset : offset + size] = raw[10:]
    if expected != image:
        raise OverrideError(
            "Existing registry BIOS overrides differ from the running image; reconcile pending settings first"
        )


def state_directory():
    return startup.state_dir() / "registry-overrides"


def _active_path(target):
    key = hashlib.sha256(target.instance_id.casefold().encode()).hexdigest()[:24]
    return state_directory() / ("active-" + key + ".json")


def active_receipt(target):
    pointer = startup.read_json(_active_path(target), {})
    if not pointer.get("receipt"):
        return None
    return load_receipt(pointer["receipt"])


def recovery_receipt(slot):
    """Find durable undo independently of working NVAPI/NVML/VBIOS getters."""
    matches = []
    for pointer_path in state_directory().glob("active-*.json"):
        pointer = startup.read_json(pointer_path, {})
        if not pointer.get("receipt"):
            continue
        receipt = load_receipt(pointer["receipt"])
        target = Target(**receipt["target"])
        if target.slot == slot:
            # Validate Windows' Enum mapping without needing a started GPU.
            WindowsRegistry(target)
            matches.append(receipt)
    if len(matches) > 1:
        raise OverrideError("Multiple saved targets share this PCI slot; inspect recovery receipts")
    return matches[0] if matches else None


def load_receipt(path):
    path = Path(path).resolve()
    if path.parent != state_directory().resolve() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
        raise OverrideError("Invalid override receipt path")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise OverrideError("Override receipt is too large")
    receipt = startup.read_json(path)
    if not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("id") != path.stem:
        raise OverrideError("Invalid override receipt")
    receipt["path"] = str(path)
    return receipt


def _clock_bios_scope(inspection, changes):
    """Derive typed INFO coverage from the actual selected BIOS table."""
    selected = {
        key: value for key, value in changes.items() if re.fullmatch(r"clock\.(0|[1-9]\d*)\.(minimum|maximum)", key)
    }
    if not selected:
        return None, {}
    fields = {field["key"]: field for field in (inspection or {}).get("fields", [])}
    versions = [fields.get(key, {}).get("clock_table_version") for key in selected]
    if any(type(value) is not int or value not in (0x40, 0x50) for value in versions) or len(set(versions)) != 1:
        raise OverrideError("Selected BIOS clock table is not understood; refresh the preview")
    version = versions[0]
    return version, selected if version == 0x50 else {}


def _validate_clock_bios_receipt(receipt, target):
    """Pure receipt coverage; legacy recovery may omit newer contracts."""
    names = {"clock_bios_contract_version", "clock_bios_table_version", "clock_bios_changes", "clock_bios_contracts"}
    if not names.intersection(receipt):
        return
    if not names.issubset(receipt) or not registry_flags._same_state(receipt["clock_bios_contract_version"], 1):
        raise OverrideError("Missing or unrecognized BIOS clock contracts")
    selected = {
        key: value
        for key, value in receipt["changes"].items()
        if re.fullmatch(r"clock\.(0|[1-9]\d*)\.(minimum|maximum)", key)
    }
    version = receipt["clock_bios_table_version"]
    if selected:
        if type(version) is not int or version not in (0x40, 0x50):
            raise OverrideError("Saved BIOS clock table version is invalid")
    elif version is not None:
        raise OverrideError("Saved BIOS clock scope has no selected fields")
    expected = selected if version == 0x50 else {}
    if not registry_flags._same_state(expected, receipt["clock_bios_changes"]):
        raise OverrideError("Saved BIOS clock contracts omit or change selected fields")
    saved = receipt["clock_bios_contracts"]
    clock_bios_runtime.validate_contracts(expected, saved)
    if saved and saved["before"]["identity"]["slot"] != target.slot:
        raise OverrideError("Saved BIOS clock contract does not match the selected GPU")


def _bind_voltage_replay(saved, observation, target):
    """Bind a compact replay capture to the independently saved voltage contract."""
    state = voltage_request_replay.validate(saved)
    if not state["complete"] or state["identity"]["slot"] != target.slot:
        raise OverrideError("Voltage replay needs complete requests for the selected GPU")
    expected = {
        "identity": state["identity"],
        "rails": {index: {"version": 0x20AC8, "payload_hex": raw} for index, raw in state["public"].items()},
    }
    if (
        not registry_flags._same_state(state["policy"], observation["policy"])
        or not registry_flags._same_state(expected, observation["rail_control"])
        or not registry_flags._same_state(state["boost_percent"], observation["boost_percent"])
    ):
        raise OverrideError("Saved voltage replay differs from the captured voltage requests")
    return state


def _prepare_voltage_replay(gpu, target, observations):
    observations = [item["before"] for item in observations if item]
    if not observations:
        return {}
    try:
        api = getattr(gpu, "nvapi", None)
        if api is None or api._i(voltage_request_replay.SET_EXPORT, ctypes.c_void_p, ctypes.c_void_p) is None:
            raise voltage_request_replay.ReplayError("unsupported", "Full voltage request SET is unavailable")
        saved = voltage_request_replay.read_checked(gpu)
        if not saved["complete"]:
            raise voltage_request_replay.ReplayError("unsupported", "Full voltage request inventory is unavailable")
        for observation in observations:
            _bind_voltage_replay(saved, observation, target)
        return {"schema": 1, "before": saved}
    except voltage_request_replay.ReplayError as exc:
        if exc.kind != "unsupported":
            raise OverrideError("Voltage request capture failed: " + str(exc)) from exc
        # Preserve independent older read-only capabilities. A known nonzero
        # boost still requires complete replay before any registry mutation.
        for observation in observations:
            voltage_ceiling_runtime.require_restartable_request(observation)
        return {}


def _validate_voltage_replay(receipt, target, observations):
    present = {"voltage_replay_contract_version", "voltage_replay_contracts"}.intersection(receipt)
    if not present:
        return {}
    if len(present) != 2 or not registry_flags._same_state(receipt["voltage_replay_contract_version"], 1):
        raise OverrideError("Missing or unrecognized voltage replay contract")
    saved = receipt["voltage_replay_contracts"]
    if saved == {}:
        return {}
    if (
        not isinstance(saved, dict)
        or set(saved) != {"schema", "before"}
        or not registry_flags._same_state(saved["schema"], 1)
    ):
        raise OverrideError("Malformed voltage replay contract")
    observations = [item["before"] for item in observations if item]
    if not observations:
        raise OverrideError("Voltage replay has no bound voltage transaction")
    for observation in observations:
        _bind_voltage_replay(saved["before"], observation, target)
    return saved


def compose_startup(snapshot, changes):
    selected = voltage_startup_runtime._selected(changes)
    if changes and not selected:
        raise OverrideError("Unrecognized startup voltage control")
    result = copy.deepcopy(snapshot)
    if selected:
        name = next(
            (name for name in result if name.casefold() == voltage_startup_runtime.REGISTRY_NAME.casefold()),
            voltage_startup_runtime.REGISTRY_NAME,
        )
        result[name] = _encoded(voltage_startup_runtime.registry_dword(selected[voltage_startup_runtime.KEY]), _DWORD)
    return result


def _validate_startup_receipt(receipt, target):
    changes = receipt.get("startup_changes", {})
    saved = receipt.get("voltage_startup_contracts", {})
    names = {"voltage_startup_contract_version", "voltage_startup_contracts"}
    if not names.intersection(receipt) and not changes:
        return {}
    if not names.issubset(receipt) or not registry_flags._same_state(receipt["voltage_startup_contract_version"], 1):
        raise OverrideError("Missing or unrecognized startup voltage contract")
    voltage_startup_runtime.validate_contracts(changes, saved)
    if not saved:
        return {}
    if receipt["changes"] or any(
        receipt.get(name) for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes")
    ):
        raise OverrideError("Apply startup voltage adjustment separately from other controls")
    if saved["before"]["requests"]["identity"]["slot"] != target.slot:
        raise OverrideError("Startup voltage contract identifies another GPU")
    if not receipt.get("verify_bios") or receipt["after_image_sha256"] != receipt["before_image_sha256"]:
        raise OverrideError("Startup adjustment must preserve the exact running BIOS")
    if compose_startup(receipt["before"], changes) != receipt["after"]:
        raise OverrideError("Startup registry projection differs from the selected adjustment")
    return saved


def _lpwr_separate(changes, **other_changes):
    selected = {key: value for key, value in changes.items() if lpwr_runtime.KEY.fullmatch(key)}
    if selected and (len(selected) != len(changes) or any(other_changes.values())):
        raise OverrideError("Apply DIFR entry or subfeature disables separately from other controls")
    return selected


def _validate_lpwr_receipt(receipt, target, *, restore=False):
    """Bind new DIFR contracts offline while preserving older image-only undo."""
    names = {"lpwr_contract_version", "lpwr_contracts"}
    selected = {key: value for key, value in receipt["changes"].items() if lpwr_runtime.KEY.fullmatch(key)}
    if not names.intersection(receipt):
        if selected and not restore:
            raise OverrideError("This DIFR preview needs fresh runtime contracts; prepare it again")
        return {}
    if not names.issubset(receipt) or not registry_flags._same_state(receipt["lpwr_contract_version"], 1):
        raise OverrideError("Missing or unrecognized DIFR contracts")
    saved = receipt["lpwr_contracts"]
    lpwr_runtime.validate_contracts(receipt["changes"], saved)
    if not saved:
        return {}
    _lpwr_separate(
        receipt["changes"],
        **{
            name: receipt.get(name)
            for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes")
        },
    )
    if saved["before"]["identity"]["slot"] != target.slot:
        raise OverrideError("Saved DIFR contract does not match the selected GPU")
    if (
        not receipt.get("verify_bios", bool(receipt["plan"]["patches"]))
        or saved["before_image_sha256"] != receipt["before_image_sha256"]
    ):
        raise OverrideError("Saved DIFR contract does not match the captured BIOS image")
    return saved


def _grpg_separate(changes, **other_changes):
    selected = {key: value for key, value in changes.items() if grpg_runtime.KEY.fullmatch(key)}
    if selected and (len(selected) != len(changes) or any(other_changes.values())):
        raise OverrideError("Apply GRPG entry disables separately from other controls")
    return selected


def _validate_grpg_receipt(receipt, target, *, restore=False):
    """Bind qualified GRPG receipts without preventing legacy image-only undo."""
    names = {"grpg_contract_version", "grpg_contracts"}
    selected = {key: value for key, value in receipt["changes"].items() if grpg_runtime.KEY.fullmatch(key)}
    if not names.intersection(receipt):
        if selected and not restore:
            raise OverrideError("This GRPG preview needs fresh runtime contracts; prepare it again")
        return {}
    if not names.issubset(receipt) or not registry_flags._same_state(receipt["grpg_contract_version"], 1):
        raise OverrideError("Missing or unrecognized GRPG contracts")
    saved = receipt["grpg_contracts"]
    grpg_runtime.validate_contracts(receipt["changes"], saved)
    if not saved:
        return {}
    _grpg_separate(
        receipt["changes"],
        **{
            name: receipt.get(name)
            for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes")
        },
    )
    if saved["before"]["identity"]["slot"] != target.slot:
        raise OverrideError("Saved GRPG contract does not match the selected GPU")
    if (
        not receipt.get("verify_bios", bool(receipt["plan"]["patches"]))
        or saved["before_image_sha256"] != receipt["before_image_sha256"]
    ):
        raise OverrideError("Saved GRPG contract does not match the captured BIOS image")
    return saved


def _ms_separate(changes, **other_changes):
    selected = {key: value for key, value in changes.items() if ms_runtime.KEY.fullmatch(key)}
    if selected and (len(selected) != len(changes) or any(other_changes.values())):
        raise OverrideError("Apply MS entry disables separately from other controls")
    return selected


def _validate_ms_receipt(receipt, target, *, restore=False):
    """Require fresh MS runtime contracts while retaining legacy image-only Undo."""
    names = {"ms_contract_version", "ms_contracts"}
    selected = {key: value for key, value in receipt["changes"].items() if ms_runtime.KEY.fullmatch(key)}
    if not names.intersection(receipt):
        if selected and not restore:
            raise OverrideError("This MS preview needs fresh runtime contracts; prepare it again")
        return {}
    if not names.issubset(receipt) or not registry_flags._same_state(receipt["ms_contract_version"], 1):
        raise OverrideError("Missing or unrecognized MS contracts")
    saved = receipt["ms_contracts"]
    ms_runtime.validate_contracts(receipt["changes"], saved)
    if not saved:
        return {}
    _ms_separate(
        receipt["changes"],
        **{
            name: receipt.get(name)
            for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes")
        },
    )
    if saved["before"]["identity"]["slot"] != target.slot:
        raise OverrideError("Saved MS contract does not match the selected GPU")
    if (
        not receipt.get("verify_bios", bool(receipt["plan"]["patches"]))
        or saved["before_image_sha256"] != receipt["before_image_sha256"]
    ):
        raise OverrideError("Saved MS contract does not match the captured BIOS image")
    return saved


def _nvdec_separate(changes, **other_changes):
    selected = {key: value for key, value in changes.items() if nvdec_runtime.KEY.fullmatch(key)}
    if selected and (len(selected) != len(changes) or any(other_changes.values())):
        raise OverrideError("Apply NVDEC entry disables separately from other controls")
    return selected


def _validate_nvdec_receipt(receipt, target, *, restore=False):
    """Require fresh NVDEC runtime contracts while retaining legacy image-only Undo."""
    names = {"nvdec_contract_version", "nvdec_contracts"}
    selected = {key: value for key, value in receipt["changes"].items() if nvdec_runtime.KEY.fullmatch(key)}
    if not names.intersection(receipt):
        if selected and not restore:
            raise OverrideError("This NVDEC preview needs fresh runtime contracts; prepare it again")
        return {}
    if not names.issubset(receipt) or not registry_flags._same_state(receipt["nvdec_contract_version"], 1):
        raise OverrideError("Missing or unrecognized NVDEC contracts")
    saved = receipt["nvdec_contracts"]
    nvdec_runtime.validate_contracts(receipt["changes"], saved)
    if not saved:
        return {}
    _nvdec_separate(
        receipt["changes"],
        **{
            name: receipt.get(name)
            for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes")
        },
    )
    if saved["before"]["identity"]["slot"] != target.slot:
        raise OverrideError("Saved NVDEC contract does not match the selected GPU")
    if (
        not receipt.get("verify_bios", bool(receipt["plan"]["patches"]))
        or saved["before_image_sha256"] != receipt["before_image_sha256"]
    ):
        raise OverrideError("Saved NVDEC contract does not match the captured BIOS image")
    return saved


def _media_separate(changes, **other_changes):
    selected = {key: value for key, value in changes.items() if media_lpwr_runtime.KEY.fullmatch(key)}
    if selected and (len(selected) != len(changes) or any(other_changes.values())):
        raise OverrideError("Apply Media LPWR entry disables separately from other controls")
    return selected


def _validate_media_receipt(receipt, target, *, restore=False):
    """Require fresh Media LPWR runtime contracts while retaining legacy image-only Undo."""
    names = {"media_contract_version", "media_contracts"}
    selected = {key: value for key, value in receipt["changes"].items() if media_lpwr_runtime.KEY.fullmatch(key)}
    if not names.intersection(receipt):
        if selected and not restore:
            raise OverrideError("This Media LPWR preview needs fresh runtime contracts; prepare it again")
        return {}
    if not names.issubset(receipt) or not registry_flags._same_state(receipt["media_contract_version"], 1):
        raise OverrideError("Missing or unrecognized Media LPWR contracts")
    saved = receipt["media_contracts"]
    media_lpwr_runtime.validate_contracts(receipt["changes"], saved)
    if not saved:
        return {}
    _media_separate(
        receipt["changes"],
        **{
            name: receipt.get(name)
            for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes")
        },
    )
    if saved["before"]["identity"]["slot"] != target.slot:
        raise OverrideError("Saved Media LPWR contract does not match the selected GPU")
    if (
        not receipt.get("verify_bios", bool(receipt["plan"]["patches"]))
        or saved["before_image_sha256"] != receipt["before_image_sha256"]
    ):
        raise OverrideError("Saved Media LPWR contract does not match the captured BIOS image")
    return saved


def prepare(
    gpu,
    changes,
    *,
    registry=None,
    named_changes=None,
    flag_changes=None,
    pstate_changes=None,
    clock_vf_changes=None,
    startup_changes=None,
):
    """Save a concrete preview and undo data before any registry mutation."""
    from . import vbios_fields

    target = bind_target(gpu)
    named_changes = named_changes or {}
    flag_changes = flag_changes or {}
    pstate_changes = pstate_changes or {}
    clock_vf_changes = clock_vf_changes or {}
    startup_changes = startup_changes or {}
    _lpwr_separate(
        changes,
        named_changes=named_changes,
        flag_changes=flag_changes,
        pstate_changes=pstate_changes,
        clock_vf_changes=clock_vf_changes,
        startup_changes=startup_changes,
    )
    _grpg_separate(
        changes,
        named_changes=named_changes,
        flag_changes=flag_changes,
        pstate_changes=pstate_changes,
        clock_vf_changes=clock_vf_changes,
        startup_changes=startup_changes,
    )
    _ms_separate(
        changes,
        named_changes=named_changes,
        flag_changes=flag_changes,
        pstate_changes=pstate_changes,
        clock_vf_changes=clock_vf_changes,
        startup_changes=startup_changes,
    )
    _nvdec_separate(
        changes,
        named_changes=named_changes,
        flag_changes=flag_changes,
        pstate_changes=pstate_changes,
        clock_vf_changes=clock_vf_changes,
        startup_changes=startup_changes,
    )
    _media_separate(
        changes,
        named_changes=named_changes,
        flag_changes=flag_changes,
        pstate_changes=pstate_changes,
        clock_vf_changes=clock_vf_changes,
        startup_changes=startup_changes,
    )
    if startup_changes and (changes or named_changes or flag_changes or pstate_changes or clock_vf_changes):
        raise OverrideError("Apply startup voltage adjustment separately from other controls")
    if (
        not changes
        and not named_changes
        and not flag_changes
        and not pstate_changes
        and not clock_vf_changes
        and not startup_changes
    ):
        raise OverrideError("No changes selected")
    if pstate_changes and any(key.startswith("clock.") for key in changes):
        raise OverrideError("Apply all-P-state editability and BIOS clock-range changes in separate transactions")
    if clock_vf_changes and any(key.startswith("clock.") for key in changes):
        raise OverrideError("Apply clock VF flags and BIOS clock-range changes in separate transactions")
    if any(key.startswith("factory.") for key in changes) and (
        named_changes or flag_changes or pstate_changes or clock_vf_changes
    ):
        raise OverrideError("Apply factory clocks separately from other registry controls")
    if voltage_ceiling_runtime._selected(changes) and (
        named_changes or flag_changes or pstate_changes or clock_vf_changes
    ):
        raise OverrideError("Apply constant voltage ceilings separately from other registry controls")
    if voltage_rel_runtime._selected(changes) and (named_changes or flag_changes or pstate_changes or clock_vf_changes):
        raise OverrideError("Apply REL equation bounds separately from other registry controls")
    before = (registry or WindowsRegistry(target)).snapshot()
    verify_bios = bool(
        changes or startup_changes or any(_RECORD.fullmatch(name) or name.casefold() == "vbios" for name in before)
    )
    image = read_vbios(gpu) if verify_bios else None
    inspection = vbios_fields.inspect(image) if verify_bios else None
    before_digest = hashlib.sha256(image).hexdigest() if image is not None else None
    plan = (
        vbios_fields.plan(image, changes)
        if changes
        else {"baseline_sha256": before_digest, "expected_sha256": before_digest, "patches": []}
    )
    contracts = policy_contracts(gpu, inspection, changes) if changes else {}
    if any(linked_current_runtime.KEY.fullmatch(key) for key in changes) and (
        named_changes or flag_changes or pstate_changes or clock_vf_changes
    ):
        raise OverrideError("Apply linked-current limits separately from other registry controls")
    linked_current = linked_current_runtime.contracts(gpu, inspection, changes)
    lpwr = lpwr_runtime.contracts(gpu, image, changes)
    grpg = grpg_runtime.contracts(gpu, image, changes)
    ms = ms_runtime.contracts(gpu, image, changes)
    nvdec = nvdec_runtime.contracts(gpu, image, changes)
    media = media_lpwr_runtime.contracts(gpu, image, changes)
    clocks = bios_runtime.clock_contracts(gpu, inspection, changes)
    clock_version, clock_changes = _clock_bios_scope(inspection, changes)
    clock_bios = clock_bios_runtime.contracts(gpu, inspection, changes)
    factory_clock = factory_clock_runtime.contracts(gpu, inspection, changes)
    rated_clock = rated_clock_runtime.contracts(gpu, inspection, changes)
    thermal = bios_runtime.thermal_contracts(gpu, inspection, changes)
    fans = fan_runtime.contracts(gpu, inspection, changes)
    voltage = voltage_bios_runtime.contracts(gpu, inspection, changes)
    voltage_ceiling = voltage_ceiling_runtime.contracts(gpu, inspection, changes)
    voltage_rel = voltage_rel_runtime.contracts(gpu, inspection, changes)
    voltage_replay = _prepare_voltage_replay(gpu, target, (voltage_ceiling, voltage_rel))
    startup_contracts = voltage_startup_runtime.contracts(gpu, startup_changes)
    fan_policies = fan_policy_runtime.contracts(gpu, inspection, changes)
    fan_stop = fan_stop_runtime.contracts(gpu, inspection, changes)
    identity = inspection["identity"] if inspection else None
    if verify_bios:
        validate_existing_image(before, image, identity["vbios_version_raw"], identity["oem_revision"])
    after = (
        compose_records(image, plan, before, identity["vbios_version_raw"], identity["oem_revision"])
        if changes
        else copy.deepcopy(before)
    )
    vfe, error = read_model_inputs(gpu)
    validate_existing_model_inputs(before, vfe)
    if named_changes:
        if error:
            raise OverrideError(error)
        after = compose_named(after, named_changes, vfe)
    model_contracts = {name: model_input(vfe, name) for name in named_changes}
    model_peers = (
        model_registry.contracts(vfe, named_changes) if model_registry.IDDQ_NAMES.intersection(named_changes) else {}
    )
    flags = None
    if flag_changes or any(name.casefold() == "rmperflimitsoverride" for name in before):
        flags, error = registry_flags.read(gpu)
        if error:
            raise OverrideError(error)
        registry_flags.validate_existing(before, flags)
    flag_contracts = registry_flags.contracts(flag_changes, flags)
    if flag_changes:
        after = registry_flags.compose(after, flag_changes, flags)
    pstates = None
    if pstate_changes or any(name.casefold() == all_pstates_registry.REGISTRY_NAME.casefold() for name in before):
        pstates, error = all_pstates_registry.read(gpu)
        if error:
            raise OverrideError(error)
        all_pstates_registry.validate_existing(before, pstates)
    pstate_contracts = all_pstates_registry.contracts(pstate_changes, pstates)
    if pstate_changes:
        after = all_pstates_registry.compose(after, pstate_changes, pstates)
    clock_vf = None
    if clock_vf_changes or any(name.casefold() == clock_vf_registry.REGISTRY_NAME.casefold() for name in before):
        clock_vf, error = clock_vf_registry.read(gpu)
        if error:
            raise OverrideError(error)
        clock_vf_registry.validate_existing(before, clock_vf)
    clock_vf_contracts = clock_vf_registry.contracts(clock_vf_changes, clock_vf)
    if clock_vf_changes:
        after = clock_vf_registry.compose(after, clock_vf_changes, clock_vf)
    after = compose_startup(after, startup_changes)
    if after == before:
        raise OverrideError("The selected settings already match the registry")
    parent = active_receipt(target)
    if parent and (parent["state"] != "active" or parent["after"] != before):
        raise OverrideError(
            "Registry differs from Druta's last saved state; inspect and restore before composing changes"
        )
    ident = uuid.uuid4().hex
    path = state_directory() / (ident + ".json")
    receipt = {
        "schema": 1,
        "id": ident,
        "path": str(path),
        "target": asdict(target),
        "created_at": time.time(),
        "state": "prepared",
        "changes": changes,
        "named_changes": named_changes,
        "model_contracts": model_contracts,
        "model_peer_contracts": model_peers,
        "flag_changes": flag_changes,
        "flag_contracts": flag_contracts,
        "pstate_changes": pstate_changes,
        "all_pstate_contracts": pstate_contracts,
        "clock_vf_changes": clock_vf_changes,
        "clock_vf_contracts": clock_vf_contracts,
        "startup_changes": startup_changes,
        "voltage_startup_contract_version": 1,
        "voltage_startup_contracts": startup_contracts,
        "plan": plan,
        "before": before,
        "after": after,
        "policy_contracts": contracts,
        "clock_contracts": clocks,
        "clock_bios_contract_version": 1,
        "clock_bios_table_version": clock_version,
        "clock_bios_changes": clock_changes,
        "clock_bios_contracts": clock_bios,
        "factory_clock_contract_version": 1,
        "factory_clock_contracts": factory_clock,
        "rated_clock_contract_version": 1,
        "rated_clock_contracts": rated_clock,
        "thermal_contracts": thermal,
        "fan_contracts": fans,
        "voltage_contracts": voltage,
        "voltage_ceiling_contract_version": 1,
        "voltage_ceiling_contracts": voltage_ceiling,
        "voltage_rel_contract_version": 1,
        "voltage_rel_contracts": voltage_rel,
        "linked_current_contract_version": 1,
        "linked_current_contracts": linked_current,
        "lpwr_contract_version": 1,
        "lpwr_contracts": lpwr,
        "grpg_contract_version": 1,
        "grpg_contracts": grpg,
        "ms_contract_version": 1,
        "ms_contracts": ms,
        "nvdec_contract_version": 1,
        "nvdec_contracts": nvdec,
        "media_contract_version": 1,
        "media_contracts": media,
        "voltage_replay_contract_version": 1,
        "voltage_replay_contracts": voltage_replay,
        "fan_policy_contracts": fan_policies,
        "fan_policy_contract_version": 2,
        "fan_stop_contract_version": 1,
        "fan_stop_contracts": fan_stop,
        "verify_bios": verify_bios,
        "firmware_selector_policy": "preserve",
        "before_image_sha256": before_digest,
        "after_image_sha256": plan["expected_sha256"],
        "parent": parent["path"] if parent else None,
        "events": [],
        "ok": False,
        "reboot_required": False,
        "message": "Prepared; registry unchanged",
    }
    _validate_lpwr_receipt(receipt, target)
    _validate_grpg_receipt(receipt, target)
    _validate_ms_receipt(receipt, target)
    _validate_nvdec_receipt(receipt, target)
    _validate_media_receipt(receipt, target)
    startup.atomic_json(path, receipt)
    # The exact source image makes restoration auditable without an external ROM.
    if image is not None:
        backup = path.with_suffix(".rom")
        with backup.open("xb") as stream:
            stream.write(image)
    return receipt


def launch_helper(receipt, *, restore=False, ui_trial=None):
    command = startup.application_command(
        "--registry-overrides",
        receipt["path"],
        "--action",
        "restore" if restore else "apply",
        "--parent-pid",
        str(os.getpid()),
        windowed=True,
    )
    slot = receipt.get("target", {}).get("slot")
    if slot:
        command.extend(["--expected-slot", slot])
    if ui_trial is not None:
        command.extend(["--ui-pstates-trial-resume", str(ui_trial)])
    return subprocess.Popen(command, close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@contextmanager
def transaction_lock(target):
    """One helper owns this adapter across staging, restart, and verification."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    name = "Local\\DrutaRegistry-" + hashlib.sha256(target.instance_id.casefold().encode()).hexdigest()
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    owned = False
    try:
        status = kernel.WaitForSingleObject(handle, 0)
        if status not in (0, 0x80):  # An abandoned mutex has transferred ownership.
            raise OverrideError("Another registry transaction is operating on this GPU")
        owned = True
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def execute(receipt, **kwargs):
    with transaction_lock(Target(**receipt["target"])):
        return _execute(receipt, **kwargs)


def _execute(
    receipt,
    *,
    restore=False,
    gpu_factory=None,
    registry_factory=WindowsRegistry,
    restart=None,
    bind=bind_target,
    reader=read_vbios,
    save=startup.atomic_json,
    voltage_capture=None,
    voltage_replay=None,
):
    """Apply/restore, restart and verify; failure attempts exact rollback."""
    if gpu_factory is None:
        gpu_factory = fresh_snapshot

        def bind(snapshot):
            return snapshot["target"]

        def reader(snapshot):
            return snapshot["image"]

    target = Target(**receipt["target"])
    registry = registry_factory(target)
    restart = restart or devicereset.restart_target
    expected_state = receipt["after"] if restore else receipt["before"]
    desired_state = receipt["before"] if restore else receipt["after"]
    expected_image = receipt["before_image_sha256"] if restore else receipt["after_image_sha256"]
    requires_image = receipt.get("verify_bios", bool(receipt["plan"]["patches"]))
    mutation_attempted = False
    pointer_saved = False
    saved_requests = {}
    startup_contracts = {}

    def capture_requests():
        if voltage_capture is not None:
            return voltage_capture(target)
        from . import voltage_replay_worker

        return voltage_replay_worker.capture(target)

    def replay_requests(image_hash, phase, *, desired=None):
        if not saved_requests:
            return None
        desired = saved_requests["before"] if desired is None else desired
        # This is outside the bounded GET/restart-read retry loop. A failed
        # child or subsequent snapshot must never repeat this SET implicitly.
        persist("voltage-replay-attempted", phase=phase, expected_image_sha256=image_hash)
        try:
            if voltage_replay is not None:
                result = voltage_replay(target, desired, image_hash)
            else:
                from . import voltage_replay_worker

                result = voltage_replay_worker.apply(target, desired, image_hash)
            after = voltage_request_replay.validate(result["after"])
            if result.get("verified") is not True or not registry_flags._same_state(
                voltage_request_replay._requested(after),
                voltage_request_replay._requested(desired),
            ):
                raise OverrideError("Voltage replay did not preserve the full captured requests")
            persist(
                "voltage-replay-verified",
                phase=phase,
                status=result.get("status"),
                worker_paths=result.get("worker_paths"),
            )
        except Exception as exc:
            persist(
                "voltage-replay-failed",
                critical=False,
                phase=phase,
                error=str(exc),
                worker_paths=getattr(exc, "worker_paths", getattr(exc, "paths", None)),
            )
            raise
        return read_fresh()[1]

    def persist(event, *, critical=True, **data):
        receipt["events"].append({"time": time.time(), "event": event, **data})
        try:
            save(receipt["path"], receipt)
        except Exception as exc:
            receipt.setdefault("journal_errors", []).append(str(exc))
            if critical:
                raise

    def get_gpu():
        gpu = gpu_factory(target.slot)
        if bind(gpu) != target:
            raise OverrideError("Selected GPU or its driver registry mapping changed")
        return gpu

    def clock_vf_observation(gpu):
        if isinstance(gpu, dict):
            return gpu.get("clock_vf")
        observation, error = clock_vf_registry.read(gpu)
        if error:
            raise OverrideError(error)
        return observation

    def typed_observation(gpu, name, module):
        if isinstance(gpu, dict):
            return gpu.get(name)
        observation, error = module.read(gpu)
        if error:
            raise OverrideError(error)
        return observation

    def lpwr_observation(gpu):
        # LPWR is scoped to the selected DIFR transaction. It must not become
        # an unconditional dependency of normal snapshots or legacy GPUs.
        if isinstance(gpu, dict):
            if "lpwr" in gpu:
                if gpu.get("lpwr_error"):
                    raise OverrideError(gpu["lpwr_error"])
                return gpu["lpwr"]
            if gpu.get("target") != target:
                raise OverrideError("Fresh LPWR observation has no validated selected target")
            from .nvbackend import GPU

            gpu = GPU(target.slot)
            if bind_target(gpu) != target:
                raise OverrideError("Selected GPU or its driver mapping changed before LPWR readback")
        observation, error = lpwr_runtime.read(gpu, **({"features": True} if lpwr_contracts.get("schema") == 2 else {}))
        if error:
            raise OverrideError(error)
        return observation

    def grpg_observation(gpu):
        # GRPG is read only for its selected transaction, not every snapshot.
        if isinstance(gpu, dict):
            if "grpg" in gpu:
                if gpu.get("grpg_error"):
                    raise OverrideError(gpu["grpg_error"])
                return gpu["grpg"]
            if gpu.get("target") != target:
                raise OverrideError("Fresh GRPG observation has no validated selected target")
            from .nvbackend import GPU

            gpu = GPU(target.slot)
            if bind_target(gpu) != target:
                raise OverrideError("Selected GPU or its driver mapping changed before GRPG readback")
        if grpg_contracts["schema"] in (5, 6, 7, 8, 9):
            observation, error = grpg_runtime.read(gpu, main=True)
        elif grpg_contracts["schema"] == 4:
            observation, error = grpg_runtime.read(gpu, auxiliary=True, bit1=True)
        elif grpg_contracts["schema"] == 3:
            observation, error = grpg_runtime.read(gpu, bit1=True)
        elif grpg_contracts["schema"] == 2:
            observation, error = grpg_runtime.read(gpu, auxiliary=True)
        else:
            observation, error = grpg_runtime.read(gpu)
        if error:
            raise OverrideError(error)
        return observation

    def ms_observation(gpu):
        if isinstance(gpu, dict):
            if "ms" in gpu:
                if gpu.get("ms_error"):
                    raise OverrideError(gpu["ms_error"])
                return gpu["ms"]
            if gpu.get("target") != target:
                raise OverrideError("Fresh MS observation has no validated selected target")
            from .nvbackend import GPU

            gpu = GPU(target.slot)
            if bind_target(gpu) != target:
                raise OverrideError("Selected GPU or its driver mapping changed before MS readback")
        observation, error = ms_runtime.read(gpu)
        if error:
            raise OverrideError(error)
        return observation

    def nvdec_observation(gpu):
        if isinstance(gpu, dict):
            if "nvdec" in gpu:
                if gpu.get("nvdec_error"):
                    raise OverrideError(gpu["nvdec_error"])
                return gpu["nvdec"]
            if gpu.get("target") != target:
                raise OverrideError("Fresh NVDEC observation has no validated selected target")
            from .nvbackend import GPU

            gpu = GPU(target.slot)
            if bind_target(gpu) != target:
                raise OverrideError("Selected GPU or its driver mapping changed before NVDEC readback")
        observation, error = nvdec_runtime.read(gpu)
        if error:
            raise OverrideError(error)
        return observation

    def media_observation(gpu):
        if isinstance(gpu, dict):
            if "media" in gpu:
                if gpu.get("media_error"):
                    raise OverrideError(gpu["media_error"])
                return gpu["media"]
            if gpu.get("target") != target:
                raise OverrideError("Fresh Media LPWR observation has no validated selected target")
            from .nvbackend import GPU

            gpu = GPU(target.slot)
            if bind_target(gpu) != target:
                raise OverrideError("Selected GPU or its driver mapping changed before Media LPWR readback")
        observation, error = media_lpwr_runtime.read(gpu)
        if error:
            raise OverrideError(error)
        return observation

    def startup_observation(gpu):
        if not isinstance(gpu, dict):
            return voltage_startup_runtime.read_checked(gpu)
        return voltage_startup_runtime.validate_observation(
            {
                "schema": 1,
                "requests": capture_requests(),
                "equations": (gpu.get("voltage_ceiling") or {}).get("equations"),
            }
        )

    def read_fresh():
        """Bounded observation recovery; never repeat a restart or SET here."""
        last = None
        for attempt in range(8):
            try:
                fresh = get_gpu()
                image = reader(fresh) if requires_image else None
                if requires_image and image is None:
                    raise OverrideError("Fresh BIOS image readback is unavailable")
                return hashlib.sha256(image).hexdigest() if image is not None else None, fresh
            except Exception as exc:
                last = exc
                if attempt < 7:
                    time.sleep(0.5)
        raise OverrideError(f"Fresh driver read failed after restart: {last}")

    def reset_and_read(*, journal_required=True):
        reset_target = devicereset.DeviceTarget(target.slot, target.instance_id, "")
        # Resolve the display name afresh; restart_target rechecks every field.
        if restart is devicereset.restart_target:
            reset_target = devicereset.resolve_target(target.slot)
            if reset_target.instance_id != target.instance_id:
                raise OverrideError("PnP identity changed before restart")
        outcome = restart(reset_target)
        receipt["reboot_required"] = outcome.reboot_required
        persist("restart", critical=journal_required, result=asdict(outcome))
        if not outcome.ok:
            raise OverrideError(outcome.message)
        return read_fresh()

    try:
        # Receipt binding is pure and applies to recovery as well as staging.
        # Valid undo must not require a working GPU before registry cleanup.
        _validate_clock_bios_receipt(receipt, target)
        lpwr_contracts = _validate_lpwr_receipt(receipt, target, restore=restore)
        grpg_contracts = _validate_grpg_receipt(receipt, target, restore=restore)
        ms_contracts = _validate_ms_receipt(receipt, target, restore=restore)
        nvdec_contracts = _validate_nvdec_receipt(receipt, target, restore=restore)
        media_contracts = _validate_media_receipt(receipt, target, restore=restore)
        startup_contracts = _validate_startup_receipt(receipt, target)
        factory_contracts = receipt.get("factory_clock_contracts", {})
        factory_version = "factory_clock_contract_version" in receipt
        factory_selected = any(key.startswith("factory.") for key in receipt["changes"])
        if "factory_clock_contracts" in receipt and not factory_version:
            raise OverrideError("Factory clock contract version is missing")
        if factory_version and (
            not registry_flags._same_state(receipt["factory_clock_contract_version"], 1)
            or "factory_clock_contracts" not in receipt
        ):
            raise OverrideError("Missing or unrecognized factory clock contracts")
        if factory_version or factory_contracts or (factory_selected and not restore):
            factory_clock_runtime.validate_contracts(receipt["changes"], factory_contracts)
            if factory_contracts and factory_contracts["before"]["clock"]["identity"]["slot"] != target.slot:
                raise OverrideError("Saved factory clock contract does not match the selected GPU")
        rated_contracts = receipt.get("rated_clock_contracts", {})
        rated_version = "rated_clock_contract_version" in receipt
        rated_selected = any(rated_clock_runtime._KEY.fullmatch(key) for key in receipt["changes"])
        if "rated_clock_contracts" in receipt and not rated_version:
            raise OverrideError("Rated clock contract version is missing")
        if rated_version and (
            not registry_flags._same_state(receipt["rated_clock_contract_version"], 1)
            or "rated_clock_contracts" not in receipt
        ):
            raise OverrideError("Missing or unrecognized rated clock contracts")
        if rated_version or rated_contracts or (rated_selected and not restore):
            rated_clock_runtime.validate_contracts(receipt["changes"], rated_contracts)
            if rated_contracts and rated_contracts["before"]["factory"]["clock"]["identity"]["slot"] != target.slot:
                raise OverrideError("Saved rated clock contract does not match the selected GPU")
        if (
            factory_selected
            and any(
                receipt.get(name) for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes")
            )
            and not restore
        ):
            raise OverrideError("Apply factory clocks separately from other registry controls")
        stop_contracts = receipt.get("fan_stop_contracts", {})
        stop_version = "fan_stop_contract_version" in receipt
        if stop_version and (
            not registry_flags._same_state(receipt["fan_stop_contract_version"], 1)
            or "fan_stop_contracts" not in receipt
        ):
            raise OverrideError("Missing or unrecognized zero-RPM contracts")
        if stop_version or stop_contracts or ("fan.zero_rpm" in receipt["changes"] and not restore):
            fan_stop_runtime.validate_contracts(receipt["changes"], stop_contracts)
            if stop_contracts and stop_contracts["fan.zero_rpm"]["before_state"]["identity"]["slot"] != target.slot:
                raise OverrideError("Saved zero-RPM contract does not match the selected GPU")
        named = receipt.get("named_changes", {})
        model_peers = receipt.get("model_peer_contracts", {})
        if model_peers or model_registry.IDDQ_NAMES.intersection(named):
            model_registry.validate_contracts(named, model_peers)
            if model_peers["before"]["slot"] != target.slot:
                raise OverrideError("Saved model-input contract does not match the selected GPU")
            if not registry_flags._same_state(
                {name: model_input(model_peers["before"], name) for name in named}, receipt.get("model_contracts")
            ):
                raise OverrideError("Saved model-input contracts disagree")
        coordinate_changes = any(re.fullmatch(r"fan_curve\.\d+\.[012]\.(level|rpm)", key) for key in receipt["changes"])
        if "fan_policy_contract_version" in receipt and "fan_policy_contracts" not in receipt:
            raise OverrideError("Saved fan-policy contracts are missing")
        if "fan_policy_contracts" in receipt or (coordinate_changes and not restore):
            fan_changes = receipt["changes"]
            if "fan_policy_contract_version" in receipt:
                if not registry_flags._same_state(receipt["fan_policy_contract_version"], 2):
                    raise OverrideError("Unrecognized fan-policy receipt version")
            elif restore:
                # Earlier image-only coordinate experiments had no runtime
                # contracts. Preserve their exact registry undo; new writes
                # always require the effective coordinate contract.
                fan_changes = {
                    key: value
                    for key, value in fan_changes.items()
                    if not (
                        re.fullmatch(r"fan_curve\.\d+\.[012]\.(level|rpm)", key)
                        and key not in receipt["fan_policy_contracts"]
                    )
                }
            fan_policy_runtime.validate_contracts(fan_changes, receipt.get("fan_policy_contracts", {}))
            if any(
                contract["unchanged"]["state"]["identity"]["slot"] != target.slot
                for contract in receipt.get("fan_policy_contracts", {}).values()
            ):
                raise OverrideError("Saved fan-policy contract does not match the selected GPU")
        ceiling_contracts = receipt.get("voltage_ceiling_contracts", {})
        ceiling_version = "voltage_ceiling_contract_version" in receipt
        ceiling_selected = voltage_ceiling_runtime._selected(receipt["changes"])
        if "voltage_ceiling_contracts" in receipt and not ceiling_version:
            raise OverrideError("Constant voltage contract version is missing")
        if ceiling_version and (
            not registry_flags._same_state(receipt["voltage_ceiling_contract_version"], 1)
            or "voltage_ceiling_contracts" not in receipt
        ):
            raise OverrideError("Missing or unrecognized constant voltage contracts")
        if ceiling_version or ceiling_contracts or (ceiling_selected and not restore):
            voltage_ceiling_runtime.validate_contracts(receipt["changes"], ceiling_contracts)
            if ceiling_contracts and ceiling_contracts["before"]["policy"]["identity"]["slot"] != target.slot:
                raise OverrideError("Saved constant voltage contract does not match the selected GPU")
        if (
            ceiling_selected
            and not restore
            and any(
                receipt.get(name) for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes")
            )
        ):
            raise OverrideError("Apply constant voltage ceilings separately from other registry controls")
        rel_contracts = receipt.get("voltage_rel_contracts", {})
        rel_version = "voltage_rel_contract_version" in receipt
        rel_selected = voltage_rel_runtime._selected(receipt["changes"])
        if "voltage_rel_contracts" in receipt and not rel_version:
            raise OverrideError("REL equation-bound contract version is missing")
        if rel_version and (
            not registry_flags._same_state(receipt["voltage_rel_contract_version"], 1)
            or "voltage_rel_contracts" not in receipt
        ):
            raise OverrideError("Missing or unrecognized REL equation-bound contracts")
        if rel_version or rel_contracts or (rel_selected and not restore):
            voltage_rel_runtime.validate_contracts(receipt["changes"], rel_contracts)
            if rel_contracts and rel_contracts["before"]["policy"]["identity"]["slot"] != target.slot:
                raise OverrideError("Saved REL contract does not match the selected GPU")
        if (
            rel_selected
            and not restore
            and any(
                receipt.get(name) for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes")
            )
        ):
            raise OverrideError("Apply REL equation bounds separately from other registry controls")
        linked_contracts = receipt.get("linked_current_contracts", {})
        linked_version = "linked_current_contract_version" in receipt
        linked_selected = {
            key: value for key, value in receipt["changes"].items() if linked_current_runtime.KEY.fullmatch(key)
        }
        if "linked_current_contracts" in receipt and not linked_version:
            raise OverrideError("Linked-current contract version is missing")
        if linked_version and (
            not registry_flags._same_state(receipt["linked_current_contract_version"], 1)
            or "linked_current_contracts" not in receipt
        ):
            raise OverrideError("Missing or unrecognized linked-current contracts")
        if linked_version or linked_contracts or (linked_selected and not restore):
            linked_current_runtime.validate_contracts(receipt["changes"], linked_contracts)
            if linked_contracts and linked_contracts["before"]["identity"]["slot"] != target.slot:
                raise OverrideError("Saved linked-current contract does not match the selected GPU")
        if (
            linked_selected
            and not restore
            and any(
                receipt.get(name) for name in ("named_changes", "flag_changes", "pstate_changes", "clock_vf_changes")
            )
        ):
            raise OverrideError("Apply linked-current limits separately from other registry controls")
        saved_requests = _validate_voltage_replay(receipt, target, (ceiling_contracts, rel_contracts))
        if startup_contracts:
            saved_requests = {"schema": 1, "before": startup_contracts["before"]["requests"]}
        if not restore and not saved_requests:
            for saved_voltage in (ceiling_contracts, rel_contracts):
                if saved_voltage:
                    voltage_ceiling_runtime.require_restartable_request(saved_voltage["before"])
        if "voltage_contracts" in receipt:
            voltage_bios_runtime.validate_contracts(receipt["changes"], receipt["voltage_contracts"])
            if (
                receipt["voltage_contracts"]
                and receipt["voltage_contracts"]["before"]["identity"]["slot"] != target.slot
            ):
                raise OverrideError("Saved voltage contract does not match the selected GPU")
        clock_vf_registry.validate_contracts(receipt.get("clock_vf_changes", {}), receipt.get("clock_vf_contracts", {}))
        if any(
            contract["unchanged"]["identity"]["slot"] != target.slot
            for contract in receipt.get("clock_vf_contracts", {}).values()
        ):
            raise OverrideError("Saved clock VF contracts do not match the selected GPU")
        if not restore and receipt["state"] != "prepared":
            raise OverrideError("This preview was already used; refresh before applying")
        if not restore:
            gpu = get_gpu()
            from . import vbios_fields

            image = reader(gpu) if requires_image else None
            if requires_image:
                if image is None or hashlib.sha256(image).hexdigest() != receipt["before_image_sha256"]:
                    raise OverrideError("The running BIOS changed after the preview")
                inspection = vbios_fields.inspect(image)
                identity = inspection["identity"]
                validate_existing_image(expected_state, image, identity["vbios_version_raw"], identity["oem_revision"])
                if receipt["plan"]["patches"] and vbios_fields.plan(image, receipt["changes"]) != receipt["plan"]:
                    raise OverrideError("Saved changes no longer match the typed BIOS plan")
                clock_version, clock_changes = _clock_bios_scope(inspection, receipt["changes"])
                if clock_changes and "clock_bios_contract_version" not in receipt:
                    raise OverrideError("This clock preview needs fresh runtime contracts; prepare it again")
                if "clock_bios_contract_version" in receipt and (
                    not registry_flags._same_state(clock_version, receipt["clock_bios_table_version"])
                    or not registry_flags._same_state(clock_changes, receipt["clock_bios_changes"])
                ):
                    raise OverrideError("Saved BIOS clock scope differs from the actual selected table")
                if stop_contracts:
                    stop_field = next(
                        (field for field in inspection.get("fields", []) if field["key"] == "fan.zero_rpm"), {}
                    )
                    if not registry_flags._same_state(
                        stop_field.get("policy_indices"), stop_contracts["fan.zero_rpm"]["affected_indices"]
                    ):
                        raise OverrideError("Saved zero-RPM contract omits an affected BIOS policy")
                if factory_contracts:
                    factory_clock_runtime.validate_inspection(inspection, receipt["changes"], factory_contracts)
                if rated_contracts:
                    rated_clock_runtime.validate_inspection(inspection, receipt["changes"], rated_contracts)
                if ceiling_contracts:
                    voltage_ceiling_runtime.validate_inspection(inspection, receipt["changes"], ceiling_contracts)
                if rel_contracts:
                    voltage_rel_runtime.validate_inspection(inspection, receipt["changes"], rel_contracts)
                if linked_contracts:
                    linked_current_runtime.validate_inspection(inspection, receipt["changes"], linked_contracts)
            rebuilt = (
                compose_records(
                    image,
                    receipt["plan"],
                    expected_state,
                    identity["vbios_version_raw"],
                    identity["oem_revision"],
                    firmware_policy=receipt.get("firmware_selector_policy", "legacy-enable"),
                )
                if receipt["plan"]["patches"]
                else copy.deepcopy(expected_state)
            )
            named = receipt.get("named_changes", {})
            vfe = gpu.get("vfe") if isinstance(gpu, dict) else read_model_inputs(gpu)[0]
            validate_existing_model_inputs(expected_state, vfe)
            if model_peers:
                model_registry.verify(vfe, model_peers, restore=True)
            if named:
                for name in named:
                    if model_input(vfe, name) != receipt["model_contracts"][name]:
                        raise OverrideError(name + ": active model input changed after preview")
                rebuilt = compose_named(rebuilt, named, vfe)
            flags = gpu.get("driver_flags") if isinstance(gpu, dict) else None
            if not isinstance(gpu, dict) and (
                receipt.get("flag_changes") or any(name.casefold() == "rmperflimitsoverride" for name in expected_state)
            ):
                flags = registry_flags.read(gpu)[0]
            registry_flags.validate_existing(expected_state, flags)
            if receipt.get("flag_changes"):
                registry_flags.verify(flags, receipt["flag_contracts"], restore=True)
                rebuilt = registry_flags.compose(rebuilt, receipt["flag_changes"], flags)
            pstates = gpu.get("all_pstates") if isinstance(gpu, dict) else None
            if not isinstance(gpu, dict) and (
                receipt.get("pstate_changes")
                or any(name.casefold() == all_pstates_registry.REGISTRY_NAME.casefold() for name in expected_state)
            ):
                pstates = all_pstates_registry.read(gpu)[0]
            all_pstates_registry.validate_existing(expected_state, pstates)
            if receipt.get("pstate_changes"):
                if any(key.startswith("clock.") for key in receipt["changes"]):
                    raise OverrideError("All-P-state editability and BIOS clock ranges require separate transactions")
                all_pstates_registry.verify(pstates, receipt["all_pstate_contracts"], restore=True)
                rebuilt = all_pstates_registry.compose(rebuilt, receipt["pstate_changes"], pstates)
            vf_changes = receipt.get("clock_vf_changes", {})
            vf_contracts = receipt.get("clock_vf_contracts", {})
            clock_vf = None
            if (
                vf_changes
                or vf_contracts
                or any(name.casefold() == clock_vf_registry.REGISTRY_NAME.casefold() for name in expected_state)
            ):
                clock_vf = clock_vf_observation(gpu)
            clock_vf_registry.validate_existing(expected_state, clock_vf)
            if vf_changes and any(key.startswith("clock.") for key in receipt["changes"]):
                raise OverrideError("Clock VF flags and BIOS clock ranges require separate transactions")
            clock_vf_registry.verify(clock_vf, vf_contracts, restore=True)
            if vf_contracts != clock_vf_registry.contracts(vf_changes, clock_vf):
                raise OverrideError("Saved clock VF contracts do not match the selected changes")
            if vf_changes:
                rebuilt = clock_vf_registry.compose(rebuilt, vf_changes, clock_vf)
            if startup_contracts:
                rebuilt = compose_startup(rebuilt, receipt["startup_changes"])
            if rebuilt != desired_state:
                raise OverrideError("Saved registry transaction does not match its bounded image plan")
            if isinstance(gpu, dict):
                verify_policy_fields(gpu, receipt["changes"], receipt.get("policy_contracts", {}), restore=True)
                bios_runtime.verify_clock_ranges(gpu, receipt.get("clock_contracts", {}), before=True)
                bios_runtime.verify_thermal_thresholds(gpu, receipt.get("thermal_contracts", {}), restore=True)
                fan_runtime.verify(gpu, receipt.get("fan_contracts", {}), restore=True)
            if receipt.get("voltage_contracts"):
                voltage = gpu.get("voltage_policy") if isinstance(gpu, dict) else voltage_bios_runtime.read(gpu)[0]
                voltage_bios_runtime.verify(voltage, receipt["voltage_contracts"], restore=True)
            if receipt.get("fan_policy_contracts"):
                fan_policies = gpu.get("fan_policies") if isinstance(gpu, dict) else fan_policy_runtime.read(gpu)[0]
                fan_policy_runtime.verify(fan_policies, receipt["fan_policy_contracts"], restore=True)
            if receipt.get("clock_bios_contracts"):
                clock_bios_runtime.verify(
                    typed_observation(gpu, "clock_bios", clock_bios_runtime),
                    receipt["clock_bios_contracts"],
                    restore=True,
                )
            if stop_contracts:
                fan_stop_runtime.verify(
                    typed_observation(gpu, "fan_stop", fan_stop_runtime), stop_contracts, restore=True
                )
            if factory_contracts:
                factory_clock_runtime.verify(
                    typed_observation(gpu, "factory_clock", factory_clock_runtime), factory_contracts, restore=True
                )
            if rated_contracts:
                rated_clock_runtime.verify(
                    typed_observation(gpu, "rated_clock", rated_clock_runtime), rated_contracts, restore=True
                )
            if ceiling_contracts:
                voltage_ceiling_runtime.verify(
                    typed_observation(gpu, "voltage_ceiling", voltage_ceiling_runtime), ceiling_contracts, restore=True
                )
            if rel_contracts:
                voltage_rel_runtime.verify(
                    typed_observation(gpu, "voltage_ceiling", voltage_ceiling_runtime), rel_contracts, restore=True
                )
            if linked_contracts:
                linked_current_runtime.verify(
                    typed_observation(gpu, "linked_current", linked_current_runtime), linked_contracts, restore=True
                )
            if lpwr_contracts:
                lpwr_runtime.verify(lpwr_observation(gpu), lpwr_contracts, restore=True)
            if grpg_contracts:
                grpg_runtime.verify(grpg_observation(gpu), grpg_contracts, restore=True)
            if ms_contracts:
                ms_runtime.verify(ms_observation(gpu), ms_contracts, restore=True)
            if nvdec_contracts:
                nvdec_runtime.verify(nvdec_observation(gpu), nvdec_contracts, restore=True)
            if media_contracts:
                media_lpwr_runtime.verify(media_observation(gpu), media_contracts, restore=True)
            if saved_requests:
                current_requests = voltage_request_replay.validate(capture_requests())
                if not registry_flags._same_state(current_requests, saved_requests["before"]):
                    raise OverrideError("Voltage requests changed after the preview")
            if startup_contracts:
                voltage_startup_runtime.verify(startup_observation(gpu), startup_contracts, restore=True)
        current_state = registry.snapshot()
        if restore:
            from .registry_recovery import restore_state

            desired_state = restore_state(receipt["before"], receipt["after"], current_state)
        elif current_state != expected_state:
            raise OverrideError("Registry settings differ from the saved transaction")
        receipt["state"] = "restoring" if restore else "staging"
        persist("begin", action="restore" if restore else "apply")
        # A process kill or power loss at any later instruction must leave an
        # undo entry discoverable from the application, before the first write.
        save(_active_path(target), {"receipt": receipt["path"]})
        pointer_saved = True
        mutation_attempted = True
        registry.replace(current_state, desired_state)
        receipt["state"] = "restarting"
        persist("registry-verified")
        observed, fresh = reset_and_read()
        if observed != expected_image:
            raise OverrideError("The driver did not load exactly the expected BIOS image")
        if startup_contracts and not restore:
            voltage_startup_runtime.verify_activation(startup_observation(fresh), startup_contracts)
        if saved_requests:
            fresh = replay_requests(
                expected_image,
                "restore" if restore else "apply",
                desired=startup_contracts["after_requests"] if startup_contracts and not restore else None,
            )
            image = reader(fresh) if requires_image else None
            if image is None or hashlib.sha256(image).hexdigest() != expected_image:
                raise OverrideError("Running BIOS changed after voltage replay")
        if isinstance(fresh, dict):
            contracts = receipt.get("policy_contracts", {})
            checked_changes = (
                {key: value["before"] for key, value in contracts.items()} if restore else receipt["changes"]
            )
            receipt["runtime_verified_fields"] = verify_policy_fields(
                fresh, checked_changes, contracts, restore=restore
            )
            receipt["runtime_verified_fields"] += verify_named_inputs(fresh, receipt, restore=restore)
            receipt["runtime_verified_fields"] += bios_runtime.verify_clock_ranges(
                fresh, receipt.get("clock_contracts", {}), restore=restore
            )
            receipt["runtime_verified_fields"] += registry_flags.verify(
                fresh.get("driver_flags"), receipt.get("flag_contracts", {}), restore=restore
            )
            receipt["runtime_verified_fields"] += all_pstates_registry.verify(
                fresh.get("all_pstates"), receipt.get("all_pstate_contracts", {}), restore=restore
            )
            receipt["runtime_verified_fields"] += bios_runtime.verify_thermal_thresholds(
                fresh, receipt.get("thermal_contracts", {}), restore=restore
            )
            receipt["runtime_verified_fields"] += fan_runtime.verify(
                fresh, receipt.get("fan_contracts", {}), restore=restore
            )
            receipt["runtime_policies"] = fresh.get("policies", [])
        if receipt.get("clock_vf_contracts") or receipt.get("clock_vf_changes"):
            if not isinstance(fresh, dict):
                receipt["runtime_verified_fields"] = []
            receipt.setdefault("runtime_verified_fields", []).extend(
                clock_vf_registry.verify(
                    clock_vf_observation(fresh), receipt.get("clock_vf_contracts", {}), restore=restore
                )
            )
        if model_peers and not isinstance(fresh, dict):
            receipt.setdefault("runtime_verified_fields", []).extend(
                verify_named_inputs({"vfe": read_model_inputs(fresh)[0]}, receipt, restore=restore)
            )
        if receipt.get("voltage_contracts"):
            voltage = fresh.get("voltage_policy") if isinstance(fresh, dict) else voltage_bios_runtime.read(fresh)[0]
            receipt.setdefault("runtime_verified_fields", []).extend(
                voltage_bios_runtime.verify(voltage, receipt["voltage_contracts"], restore=restore)
            )
        if receipt.get("fan_policy_contracts"):
            fan_policies = fresh.get("fan_policies") if isinstance(fresh, dict) else fan_policy_runtime.read(fresh)[0]
            receipt.setdefault("runtime_verified_fields", []).extend(
                fan_policy_runtime.verify(fan_policies, receipt["fan_policy_contracts"], restore=restore)
            )
        if receipt.get("clock_bios_contracts"):
            receipt.setdefault("runtime_verified_fields", []).extend(
                clock_bios_runtime.verify(
                    typed_observation(fresh, "clock_bios", clock_bios_runtime),
                    receipt["clock_bios_contracts"],
                    restore=restore,
                )
            )
        if stop_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                fan_stop_runtime.verify(
                    typed_observation(fresh, "fan_stop", fan_stop_runtime), stop_contracts, restore=restore
                )
            )
        if factory_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                factory_clock_runtime.verify(
                    typed_observation(fresh, "factory_clock", factory_clock_runtime), factory_contracts, restore=restore
                )
            )
        if rated_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                rated_clock_runtime.verify(
                    typed_observation(fresh, "rated_clock", rated_clock_runtime), rated_contracts, restore=restore
                )
            )
        if ceiling_contracts:
            ceiling_observation = typed_observation(fresh, "voltage_ceiling", voltage_ceiling_runtime)
            receipt.setdefault("runtime_verified_fields", []).extend(
                voltage_ceiling_runtime.verify(ceiling_observation, ceiling_contracts, restore=restore)
            )
            receipt["runtime_voltage_policy"] = ceiling_observation["policy"]
        if rel_contracts:
            rel_observation = typed_observation(fresh, "voltage_ceiling", voltage_ceiling_runtime)
            receipt.setdefault("runtime_verified_fields", []).extend(
                voltage_rel_runtime.verify(rel_observation, rel_contracts, restore=restore)
            )
            receipt["runtime_voltage_policy"] = rel_observation["policy"]
        if linked_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                linked_current_runtime.verify(
                    typed_observation(fresh, "linked_current", linked_current_runtime),
                    linked_contracts,
                    restore=restore,
                )
            )
        if lpwr_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                lpwr_runtime.verify(lpwr_observation(fresh), lpwr_contracts, restore=restore)
            )
        if grpg_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                grpg_runtime.verify(grpg_observation(fresh), grpg_contracts, restore=restore)
            )
        if ms_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                ms_runtime.verify(ms_observation(fresh), ms_contracts, restore=restore)
            )
        if nvdec_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                nvdec_runtime.verify(nvdec_observation(fresh), nvdec_contracts, restore=restore)
            )
        if media_contracts:
            receipt.setdefault("runtime_verified_fields", []).extend(
                media_lpwr_runtime.verify(media_observation(fresh), media_contracts, restore=restore)
            )
        if startup_contracts:
            startup_actual = startup_observation(fresh)
            receipt.setdefault("runtime_verified_fields", []).extend(
                voltage_startup_runtime.verify(startup_actual, startup_contracts, restore=restore)
            )
            receipt["runtime_voltage_policy"] = startup_actual["requests"]["policy"]
        if "runtime_verified_fields" in receipt:
            receipt["runtime_verified_fields"] = list(dict.fromkeys(receipt["runtime_verified_fields"]))
        receipt["state"] = "restored" if restore else "active"
        receipt["ok"] = True
        receipt["message"] = "Transaction registry settings restored" if restore else "Registry overrides active"
        if requires_image:
            receipt["message"] += "; exact running BIOS image verified"
        if receipt.get("runtime_verified_fields"):
            receipt["message"] += "; selected driver readbacks verified"
        save(_active_path(target), {"receipt": receipt.get("parent") if restore else receipt["path"]})
        persist("verified", observed_image_sha256=observed)
    except Exception as exc:
        receipt["ok"] = False
        receipt["message"] = str(exc)
        receipt["state"] = "failed"
        persist("failure", critical=False, error=str(exc))
        if mutation_attempted:
            try:
                actual = registry.snapshot()
                from .registry_recovery import restore_state

                rollback_state = restore_state(receipt["before"], receipt["after"], actual)
                registry.replace(actual, rollback_state)
                persist("registry-rollback-verified", critical=False)
                if receipt["reboot_required"]:
                    raise OverrideError("Registry restored; Windows requested reboot, which was not performed")
                if restore:
                    # Retain the original settings on failed restoration. Do
                    # not reintroduce the override the user is trying to undo.
                    raise OverrideError("Previous registry settings restored; GPU activation still needs recovery")
                observed, rollback = reset_and_read(journal_required=False)
                rollback_image = receipt["before_image_sha256"]
                if observed != rollback_image:
                    raise OverrideError("Rollback image differs from the captured original")
                if saved_requests:
                    rollback = replay_requests(rollback_image, "rollback")
                    image = reader(rollback) if requires_image else None
                    if image is None or hashlib.sha256(image).hexdigest() != rollback_image:
                        raise OverrideError("Running BIOS changed after rollback voltage replay")
                if isinstance(rollback, dict):
                    verify_named_inputs(rollback, receipt, restore=True)
                    contracts = receipt.get("policy_contracts", {})
                    verify_policy_fields(
                        rollback, {key: value["before"] for key, value in contracts.items()}, contracts, restore=True
                    )
                    bios_runtime.verify_clock_ranges(rollback, receipt.get("clock_contracts", {}), restore=True)
                    registry_flags.verify(rollback.get("driver_flags"), receipt.get("flag_contracts", {}), restore=True)
                    all_pstates_registry.verify(
                        rollback.get("all_pstates"), receipt.get("all_pstate_contracts", {}), restore=True
                    )
                    bios_runtime.verify_thermal_thresholds(rollback, receipt.get("thermal_contracts", {}), restore=True)
                    fan_runtime.verify(rollback, receipt.get("fan_contracts", {}), restore=True)
                if receipt.get("clock_vf_contracts") or receipt.get("clock_vf_changes"):
                    clock_vf_registry.verify(
                        clock_vf_observation(rollback), receipt.get("clock_vf_contracts", {}), restore=True
                    )
                if model_peers and not isinstance(rollback, dict):
                    verify_named_inputs({"vfe": read_model_inputs(rollback)[0]}, receipt, restore=True)
                if receipt.get("voltage_contracts"):
                    voltage = (
                        rollback.get("voltage_policy")
                        if isinstance(rollback, dict)
                        else voltage_bios_runtime.read(rollback)[0]
                    )
                    voltage_bios_runtime.verify(voltage, receipt["voltage_contracts"], restore=True)
                if receipt.get("fan_policy_contracts"):
                    fan_policies = (
                        rollback.get("fan_policies")
                        if isinstance(rollback, dict)
                        else fan_policy_runtime.read(rollback)[0]
                    )
                    fan_policy_runtime.verify(fan_policies, receipt["fan_policy_contracts"], restore=True)
                if receipt.get("clock_bios_contracts"):
                    clock_bios_runtime.verify(
                        typed_observation(rollback, "clock_bios", clock_bios_runtime),
                        receipt["clock_bios_contracts"],
                        restore=True,
                    )
                if stop_contracts:
                    fan_stop_runtime.verify(
                        typed_observation(rollback, "fan_stop", fan_stop_runtime),
                        stop_contracts,
                        restore=True,
                    )
                if factory_contracts:
                    factory_clock_runtime.verify(
                        typed_observation(rollback, "factory_clock", factory_clock_runtime),
                        factory_contracts,
                        restore=True,
                    )
                if rated_contracts:
                    rated_clock_runtime.verify(
                        typed_observation(rollback, "rated_clock", rated_clock_runtime),
                        rated_contracts,
                        restore=True,
                    )
                if ceiling_contracts:
                    voltage_ceiling_runtime.verify(
                        typed_observation(rollback, "voltage_ceiling", voltage_ceiling_runtime),
                        ceiling_contracts,
                        restore=True,
                    )
                if rel_contracts:
                    voltage_rel_runtime.verify(
                        typed_observation(rollback, "voltage_ceiling", voltage_ceiling_runtime),
                        rel_contracts,
                        restore=True,
                    )
                if linked_contracts:
                    linked_current_runtime.verify(
                        typed_observation(rollback, "linked_current", linked_current_runtime),
                        linked_contracts,
                        restore=True,
                    )
                if lpwr_contracts:
                    lpwr_runtime.verify(lpwr_observation(rollback), lpwr_contracts, restore=True)
                if grpg_contracts:
                    grpg_runtime.verify(grpg_observation(rollback), grpg_contracts, restore=True)
                if ms_contracts:
                    ms_runtime.verify(ms_observation(rollback), ms_contracts, restore=True)
                if nvdec_contracts:
                    nvdec_runtime.verify(nvdec_observation(rollback), nvdec_contracts, restore=True)
                if media_contracts:
                    media_lpwr_runtime.verify(media_observation(rollback), media_contracts, restore=True)
                if startup_contracts:
                    voltage_startup_runtime.verify(startup_observation(rollback), startup_contracts, restore=True)
                receipt["state"] = "rolled-back"
                receipt["message"] += "; previous registry and running image restored"
                save(_active_path(target), {"receipt": receipt.get("parent")})
                persist("rollback-verified", critical=False, observed_image_sha256=observed)
            except Exception as recovery:
                receipt["state"] = "recovery-required"
                receipt["message"] += "; recovery required: " + str(recovery)
                # Keep a durable recovery pointer even when the live state is uncertain.
                try:
                    save(_active_path(target), {"receipt": receipt["path"]})
                except Exception:
                    pass  # The pre-mutation pointer already identifies this receipt.
                persist("rollback-failure", critical=False, error=str(recovery))
        elif pointer_saved:
            save(_active_path(target), {"receipt": receipt["path"] if restore else receipt.get("parent")})
    return receipt


def cli(argv, notify):
    parser = argparse.ArgumentParser(prog="Druta --registry-overrides")
    parser.add_argument("receipt")
    parser.add_argument("--action", choices=("apply", "restore"), default="apply")
    parser.add_argument("--parent-pid", type=int)
    capture = parser.add_mutually_exclusive_group()
    capture.add_argument("--ui-pstates-trial-resume")
    capture.add_argument("--ui-capture-recovery")
    parser.add_argument("--expected-slot")
    args = parser.parse_args(argv)
    if args.expected_slot is not None:
        try:
            args.expected_slot = devicereset._parse_slot(args.expected_slot)[0]
        except devicereset.DeviceResetError as exc:
            parser.error(str(exc))
    if args.ui_capture_recovery:
        if not args.expected_slot:
            parser.error("--ui-capture-recovery requires --expected-slot")
        capture_path = os.path.abspath(args.ui_capture_recovery)
        if os.path.exists(capture_path):
            parser.error("--ui-capture-recovery requires a new directory")
        if not os.path.isdir(os.path.dirname(capture_path)):
            parser.error("--ui-capture-recovery requires an existing parent directory")
    parent_exited = args.parent_pid is None
    execute_started = False

    def mark_trial_error(message, *, relaunch_error=None):
        if not args.ui_pstates_trial_resume:
            return
        try:
            trial_path = Path(args.ui_pstates_trial_resume) / "trial.json"
            trial = startup.read_json(trial_path)
            if isinstance(trial, dict):
                trial["error"] = trial.get("error") or message
                if relaunch_error is not None:
                    trial["relaunch_error"] = relaunch_error
                trial["phase"] = "attention"
                startup.atomic_json(trial_path, trial)
        except Exception:
            pass  # The receipt remains the recovery source if the manifest cannot be saved.

    try:
        if args.parent_pid is not None:
            devicerecovery.wait_for_exit(args.parent_pid)
            parent_exited = True
        receipt = load_receipt(args.receipt)
        if args.expected_slot is not None:
            actual_slot = devicereset._parse_slot(receipt.get("target", {}).get("slot"))[0]
            if actual_slot != args.expected_slot:
                raise OverrideError("Receipt target does not match the expected PCI slot")
        execute_started = True
        result = execute(receipt, restore=args.action == "restore")
    except Exception as exc:
        result = {
            "target": {"slot": args.expected_slot or locals().get("receipt", {}).get("target", {}).get("slot", "")},
            "ok": False,
            "reboot_required": False,
            "path": args.receipt,
            "message": "Override helper failed: " + str(exc),
        }
    if not parent_exited:
        mark_trial_error(result["message"])
        notify(result["message"] + "\nThe original Druta process is still running; no second application opened.")
        return 1
    if args.ui_pstates_trial_resume and not execute_started:
        mark_trial_error(result["message"])
        notify(result["message"] + "\nThe trial did not execute; no second application opened.")
        return 1
    # Reuse the existing recovery-result UI and avoid startup-profile restoration.
    log = startup.state_dir() / "device-recovery" / (uuid.uuid4().hex + ".json")
    try:
        startup.atomic_json(
            log,
            {
                "slot": result["target"]["slot"],
                "ok": result["ok"],
                "reboot_required": result["reboot_required"],
                "message": result["message"] + "\nDetails: " + result["path"],
            },
        )
        selection = ["--gpu", result["target"]["slot"]] if result["target"]["slot"] else []
        resume = ["--ui-pstates-trial-resume", args.ui_pstates_trial_resume] if args.ui_pstates_trial_resume else []
        if args.ui_capture_recovery:
            resume = ["--ui-capture-recovery", args.ui_capture_recovery]
        subprocess.Popen(
            startup.application_command(*selection, "--recovery-result", str(log), *resume, windowed=True),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        message = result["message"] + "\nCould not reopen Druta: " + str(exc)
        mark_trial_error(message, relaunch_error=str(exc))
        notify(message)
    return 0 if result["ok"] else 1


def recovery_cli(argv, notify):
    """Restore saved settings without constructing GPU or starting the GUI."""
    parser = argparse.ArgumentParser(prog="Druta --registry-recover")
    parser.add_argument("slot")
    args = parser.parse_args(argv)
    try:
        receipt = recovery_receipt(args.slot)
        if not receipt:
            raise OverrideError("No saved registry transaction for this PCI slot")
    except Exception as exc:
        notify("Registry recovery unavailable: " + str(exc))
        return 1
    return cli([receipt["path"], "--action", "restore", "--expected-slot", args.slot], notify)
