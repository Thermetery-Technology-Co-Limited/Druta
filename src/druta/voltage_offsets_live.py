# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""App-owned independent VFE policy offsets and saved profile replay.

Values are signed microvolt definition requests. Reported policy readback is
verified independently; this does not assert a measured physical voltage.
"""

import copy
import ctypes
import hashlib
import json
import struct
from dataclasses import asdict

from . import clock_bios_runtime as clock
from . import devicereset, startup
from . import registry_overrides as ro
from . import vbios_fields as vb
from . import vfe_equation_runtime as equations
from . import voltage_ceiling_runtime as ceiling
from . import voltage_offset_scaffold as scaffold
from .nvbackend import GPU

_POLICY = {"REL": "reliability", "ALT": "alt_reliability", "OV": "overvoltage", "VMIN": "vmin"}
_VAR_SIZE = 0xC364


class VoltageOffsetsError(RuntimeError):
    """An offset operation could not be admitted or verified."""


def _path(slot):
    if not isinstance(slot, str) or not slot:
        raise VoltageOffsetsError("An adapter slot is required")
    return startup.state_dir() / ("voltage-offset-scaffold-" + hashlib.sha256(slot.encode()).hexdigest() + ".json")


def _variable_info(gpu, target):
    """Read the already-understood native VFE variable INFO packet twice."""
    reader = clock.PerfDiagnosticsReader(gpu)
    result = []
    for _ in range(2):
        with gpu._lock:
            identity = clock._binding(reader)
            if ro.bind_target(gpu) != target:
                raise VoltageOffsetsError("Selected adapter changed")
            envelope, fields = reader._transport()
            if len(envelope) != 17 or any(envelope[i] != value for i, value in clock._ENVELOPE.items()):
                raise VoltageOffsetsError("Variable INFO transport is not understood")
            storage, data = clock._guarded(68 + _VAR_SIZE)
            packet = (ctypes.c_uint32 * ((68 + _VAR_SIZE) // 4)).from_buffer(data)
            for i, word in enumerate(envelope):
                packet[i] = word
            packet[2], packet[14], packet[15], packet[16] = len(data), 0x2080A0B1, _VAR_SIZE, 0
            packet[5] = packet[11] = 0
            expected = tuple(packet[:16])
            try:
                status = gpu._legacy_clk_escape(packet, dict(fields))
            finally:
                clock._guards(storage)
            if (status != 0 or packet[16] != 0 or packet[11] != 1 or packet[5] not in (0, 1)
                    or any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11))
                    or clock._binding(reader) != identity or ro.bind_target(gpu) != target):
                raise VoltageOffsetsError("Variable INFO read failed or changed adapter")
            result.append(bytes(data[68:]))
    if result[0] != result[1]:
        raise VoltageOffsetsError("Variable INFO changed between reads")
    return result[0]


def _voltage(gpu):
    value, error = ceiling.read(gpu)
    if error or value is None:
        raise VoltageOffsetsError(error or "Voltage readback unavailable")
    return value


def _load(slot):
    try:
        value = json.loads(_path(slot).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VoltageOffsetsError("No readable offset scaffold profile for this adapter") from exc
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("slot") != slot:
        raise VoltageOffsetsError("Offset scaffold profile does not match the adapter")
    return value


def _save(slot, value):
    startup.atomic_json(_path(slot), value)


def _recognize(slot, profile):
    gpu = GPU(slot)
    target = ro.bind_target(gpu)
    if asdict(target) != profile["target"]:
        raise VoltageOffsetsError("Offset scaffold belongs to another adapter")
    current_image = ro.read_vbios(gpu)
    observed = _voltage(gpu)
    variable = _variable_info(gpu, target)
    original = bytes.fromhex(profile["original_image_hex"])
    requests = profile["requests"]
    _, heads = scaffold.verify_loaded(original, profile["before_equations"], variable, requests,
                                       current_image, observed["equations"])
    if [{k: v for k, v in h.items() if k != "value_uv"} for h in heads] != profile["heads"]:
        raise VoltageOffsetsError("Offset scaffold head bindings changed")
    return gpu, target, original, variable, current_image, observed, heads


def enable(slot, requested=None):
    """Install zero heads for selected present rail policies, then verify them."""
    if _path(slot).exists():
        raise VoltageOffsetsError("A scaffold profile already exists for this adapter")
    gpu = GPU(slot)
    target = ro.bind_target(gpu)
    registry = ro.WindowsRegistry(target)
    before_registry = registry.snapshot()
    if ro.active_receipt(target) is not None:
        raise VoltageOffsetsError("An existing registry transaction is active")
    image = ro.read_vbios(gpu)
    observed = _voltage(gpu)
    ceiling.require_restartable_request(observed)
    variable = _variable_info(gpu, target)
    unsupported = []
    if requested is None:
        parser = vb._Image(image)
        table = parser.pointer("P", 0x68)
        count = parser.b[table + 3]
        requested = []
        for i in range(count):
            for role, offset in scaffold._ROLES.items():
                key = f"voltage.{i}.{role}.offset"
                root = struct.unpack_from("<H", image, table+5+65*i+offset)[0]
                if root == 0xffff or str(i) not in observed["policy"]["rails"]:
                    unsupported.append(key)
                else:
                    try:
                        scaffold.plan(image, observed["equations"], variable, [*requested, (i, role)])
                    except (scaffold.VoltageOffsetScaffoldError, vb.FormatError, equations.VfeEquationError):
                        unsupported.append(key)
                    else:
                        requested.append((i, role))
    requests = [list(x) for x in requested]
    plan, heads = scaffold.plan(image, observed["equations"], variable, requests)
    identity = vb.inspect(image)["identity"]
    desired = ro.compose_records(image, plan, before_registry, identity["vbios_version_raw"], identity["oem_revision"])
    profile = {"schema": 1, "slot": slot, "target": asdict(target), "original_image_hex": image.hex(),
               "before_equations": observed["equations"], "requests": requests, "heads": heads,
               "values": {h["key"]: 0 for h in heads}, "unsupported": unsupported}
    with ro.transaction_lock(target):
        if (registry.snapshot() != before_registry or ro.active_receipt(target) is not None
                or ro.bind_target(gpu) != target or ro.read_vbios(gpu) != image or _path(slot).exists()):
            raise VoltageOffsetsError("Registry state changed before scaffold activation")
        reset_target = devicereset.resolve_target(slot)
        if reset_target.instance_id != target.instance_id:
            raise VoltageOffsetsError("GPU restart target changed")
        # Keep the binding available to the subsequent fresh process after restart.
        _save(slot, profile)
        registry.replace(before_registry, desired)
        result = devicereset.restart_target(reset_target)
        if not result.ok:
            raise VoltageOffsetsError(result.message)
    fresh = GPU(slot)
    if ro.bind_target(fresh) != target:
        raise VoltageOffsetsError("Adapter changed after scaffold activation")
    loaded_image = ro.read_vbios(fresh)
    loaded = _voltage(fresh)
    variables = _variable_info(fresh, target)
    scaffold.verify_activation(image, observed["equations"], variables, requests, loaded_image, loaded["equations"])
    if any(loaded[name] != observed[name] for name in ("policy", "rail_control", "boost_percent")):
        raise VoltageOffsetsError("Zero scaffold changed reported policies or requests")
    return read(slot)


def read(slot):
    """Read scaffold values and reported policy for the selected adapter."""
    profile = _load(slot)
    _, _, _, _, _, observed, heads = _recognize(slot, profile)
    return {"slot": slot, "values": {h["key"]: h["value_uv"] for h in heads},
            "policy": copy.deepcopy(observed["policy"]), "saved_values": copy.deepcopy(profile["values"]),
            "unsupported": list(profile.get("unsupported", []))}


def capture(slot):
    """Capture the currently loaded offset profile for later replay."""
    return read(slot)["values"]


def _policy_contract(policy, *, derived_boost):
    result = copy.deepcopy(policy)
    if derived_boost:
        for row in result["rails"].values():
            row.pop("boost_uv", None)
            row["policy_uv"].pop("reliability", None)
            row["policy_uv"].pop("effective", None)
    return result


def set_offsets(slot, values):
    """Set selected offsets through existing singleton VFE constant writes."""
    if not isinstance(values, dict) or not values:
        raise VoltageOffsetsError("Select at least one policy offset")
    profile = _load(slot)
    initial = _recognize(slot, profile)
    available = {h["key"]: h for h in initial[-1]}
    for key, wanted in values.items():
        if key not in available:
            raise VoltageOffsetsError("Requested policy has no installed scaffold")
        equations._uv(wanted)
        if str(available[key]["rail"]) not in initial[-2]["policy"]["rails"]:
            raise VoltageOffsetsError("Selected rail policy readback is unavailable")
    for key, wanted in values.items():
        gpu, _, original, variable, current_image, before, heads = _recognize(slot, profile)
        head = next((h for h in heads if h["key"] == key), None)
        if head is None:
            raise VoltageOffsetsError("Requested policy has no installed scaffold")
        rail = str(head["rail"])
        if rail not in before["policy"]["rails"]:
            raise VoltageOffsetsError("Selected rail policy readback is unavailable")
        contract = scaffold.live_contract(original, profile["before_equations"], variable, profile["requests"],
                                           current_image, before["equations"], key, wanted)
        if wanted == head["value_uv"]:
            profile["values"][key] = wanted
            _save(slot, profile)
            continue
        equations.write_constants(gpu, contract)
        after = _voltage(gpu)
        equations.verify(after["equations"], contract)
        expected = copy.deepcopy(before["policy"])
        expected["rails"][rail]["policy_uv"][_POLICY[head["role"]]] += wanted - head["value_uv"]
        # Effective is an independent firmware-derived observation.
        expected["rails"][rail]["policy_uv"]["effective"] = after["policy"]["rails"][rail]["policy_uv"]["effective"]
        derived_boost = before["boost_percent"] != 0 and head["role"] in ("REL", "ALT")
        if (_policy_contract(after["policy"], derived_boost=derived_boost)
                != _policy_contract(expected, derived_boost=derived_boost)
                or any(after[k] != before[k] for k in ("rail_control", "boost_percent"))):
            raise VoltageOffsetsError("Reported policy or preserved requests differ after offset write")
        profile["values"][key] = wanted
        _save(slot, profile)
    return read(slot)


def replay(slot, profile):
    """Apply a previously captured policy-offset value mapping."""
    return set_offsets(slot, profile)
