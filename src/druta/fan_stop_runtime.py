# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only global fan-stop state and exact registry activation contracts.

BIOSv20 flags1/2 set support/default; loaded INFO14/15 and CONTROL48 expose
support/default/current. PMU platform support is enforced by the driver loader.
If no affected loaded policy reports support, preflight platform capability is
unknown: enabling succeeds only after exact driver acceptance/readback. No
board/driver gates or physical fan-response guarantees are inferred.

Evidence: experiments/registry-overrides/vbios-fields/fan-mode-stop-contract.json
and fan-stop-research-9576aa0d5cf54845943d6bcbd29b37d1.json.
No setters, registry operations or restart calls are present in this module.
"""

from __future__ import annotations

import copy
import ctypes
import struct
from collections.abc import Mapping
from contextlib import nullcontext

from . import fan_policy_runtime as policy
from . import fan_runtime as fan
from .bios_runtime import BiosRuntimeError
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

KEY = "fan.zero_rpm"
GETS = {
    "policy_info": (policy.INFO_COMMAND, policy.INFO_BYTES),
    "policy_control": (policy.CONTROL_COMMAND, policy.CONTROL_BYTES),
    "routes": (policy.ROUTING_COMMAND, policy.ROUTING_BYTES),
    "cooler_info": (fan.INFO_COMMAND, fan.INFO_BYTES),
    "cooler_control": (fan.CONTROL_COMMAND, fan.CONTROL_BYTES),
}
LAYOUT = {
    "group": "fan_stop_global",
    "capacity": 16,
    "packets": {k: {"command": f"0x{c:08X}", "bytes": n} for k, (c, n) in GETS.items()},
}


class FanStopRuntimeError(BiosRuntimeError):
    pass


def _fail(message):
    raise FanStopRuntimeError(message)


def _boolean(value):
    if type(value) not in (bool, int) or value not in (0, 1):
        _fail("Global fan stop requires a boolean")
    return bool(value)


def _selection(changes):
    if not isinstance(changes, Mapping):
        _fail("Fan-stop changes must be a mapping")
    if KEY not in changes:
        return []
    _boolean(changes[KEY])
    if any(isinstance(k, str) and k != KEY and k.startswith(("fan.", "fan_stop.", "fan_curve.")) for k in changes):
        _fail("Apply global fan stop separately from other fan settings")
    return [KEY]


def _decode(state):
    if not isinstance(state, dict) or set(state) != {"identity", "layout", "raw"}:
        _fail("Fan-stop state has invalid shape")
    if not fan._same(state["layout"], LAYOUT):
        _fail("Fan-stop packet layout is unrecognized")
    try:
        identity = policy._identity(state["identity"])
        if not isinstance(state["raw"], dict) or set(state["raw"]) != set(GETS):
            _fail("Fan-stop state omits raw packet peers")
        raw = {name: policy._bytes(state["raw"][name], size) for name, (_, size) in GETS.items()}
        if any(state["raw"][k] != value.hex() for k, value in raw.items()):
            _fail("Fan-stop raw packets need canonical hexadecimal encoding")
        decoded = policy.decode(raw["policy_info"], raw["policy_control"], raw["routes"])
        cooler = fan.decode(raw["cooler_info"], raw["cooler_control"])
        cooler.update(identity=identity, slot=identity["slot"], architecture=identity["architecture"], stable_reads=2)
        fan._shape(cooler, stable=True)
    except (ValueError, BiosRuntimeError) as exc:
        _fail("Fan-stop state cannot be decoded: " + str(exc))
    if decoded["policy_layout"] != 2 or not decoded["mask"] or not decoded["routing_available"]:
        _fail("Fan stop requires loaded policy layout2 and routing")
    return raw, decoded, cooler


def _state(observation):
    if not isinstance(observation, Mapping) or not fan._same(observation.get("stable_reads"), 2):
        _fail("Fan stop needs two stable fresh raw reads")
    state = observation.get("state")
    _decode(state)
    return copy.deepcopy(state)


def _get(reader, name, mask=None):
    command, size = GETS[name]
    header, fields = reader._transport()
    if len(header) != 17 or any(header[i] != value for i, value in policy.ENVELOPE.items()):
        _fail("Fan stop requires the understood NVDA envelope")
    if any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in fields.values()):
        _fail("Fan-stop escape fields are malformed")
    total, guard = 68 + size, 64
    storage = (ctypes.c_ubyte * (total + 2 * guard))()
    ctypes.memset(ctypes.addressof(storage), 0xA5, ctypes.sizeof(storage))
    packet = (ctypes.c_uint32 * (total // 4)).from_buffer(storage, guard)
    ctypes.memset(ctypes.addressof(packet), 0, total)
    for i, value in enumerate(header):
        packet[i] = value
    packet[2], packet[5], packet[11] = total, 0, 0
    packet[14], packet[15], packet[16] = command, size, 0
    if mask is not None:
        packet[18] = mask
    expected = tuple(packet[:16])
    reader._binding()
    status = reader.gpu._legacy_clk_escape(packet, fields)
    reader._binding()
    if any(v != 0xA5 for v in storage[:guard]) or any(v != 0xA5 for v in storage[-guard:]):
        _fail("Fan-stop GET exceeded packet bounds")
    if any(packet[i] != v for i, v in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
        _fail("Fan-stop GET changed its bound envelope")
    if type(status) is not int or status != 0 or packet[11] != 1:
        raise PerfDiagnosticError("transient", "Fan-stop GET transport/completion failed")
    if packet[16] != 0:
        kind = "unsupported" if packet[16] in (0x1F, 0x56) else "transient"
        raise PerfDiagnosticError(kind, f"Fan-stop GET RM status0x{packet[16]:X}")
    return bytes(packet)[68:]


def read(gpu):
    """Return raw configuration observation/error, with bounded read-only retries."""
    reader = PerfDiagnosticsReader(gpu)
    for attempt in range(2):
        try:
            states = []
            with getattr(gpu, "_lock", nullcontext()):
                for _ in range(2):
                    api, slot, architecture = reader._binding()
                    identity = {
                        "slot": slot,
                        "architecture": architecture,
                        "device_id": api.selected.get("devid"),
                        "subsystem_id": api.selected.get("subsys"),
                    }
                    raw = {"policy_info": _get(reader, "policy_info")}
                    mask = int.from_bytes(raw["policy_info"][4:8], "little")
                    if not 0 < mask < 1 << 16:
                        _fail("Fan-stop policy inventory is empty or out of bounds")
                    raw["policy_control"] = _get(reader, "policy_control", mask)
                    raw["routes"] = _get(reader, "routes")
                    raw["cooler_info"] = _get(reader, "cooler_info")
                    mask = int.from_bytes(raw["cooler_info"][4:8], "little")
                    if mask >> 16:
                        _fail("Fan-stop cooler inventory is out of bounds")
                    raw["cooler_control"] = _get(reader, "cooler_control", mask)
                    state = {
                        "identity": identity,
                        "layout": copy.deepcopy(LAYOUT),
                        "raw": {k: v.hex() for k, v in raw.items()},
                    }
                    _decode(state)
                    states.append(state)
            if not fan._same(*states):
                raise PerfDiagnosticError("transient", "Fan-stop raw configuration changed between reads")
            return {"state": states[1], "stable_reads": 2}, None
        except FanStopRuntimeError as exc:
            return None, str(exc)
        except PerfDiagnosticError as exc:
            if exc.kind != "transient" or attempt:
                return None, str(exc)
        except Exception as exc:
            if attempt:
                return None, str(exc)
        gpu._current_limit_transport = None
        gpu._current_limit_layout_choice = None
    return None, "Fan-stop metadata is unavailable"


def _indices(value):
    if (
        not isinstance(value, list)
        or not value
        or any(type(i) is not int or not 0 <= i < 16 for i in value)
        or value != sorted(set(value))
    ):
        _fail("Global fan-stop affected policy inventory is invalid")
    return value


def _rows(state, indices, enabled, *, require_ancillary):
    _, decoded, cooler = _decode(state)
    rows = {r["index"]: r for r in decoded["entries"]}
    coolers = {r["index"]: r for r in cooler["entries"]}
    for i in indices:
        row = rows.get(i)
        if row is None or row["type"] != 2 or row["control_type"] != 2 or row["issues"]:
            _fail("Global fan stop selects an unknown or malformed policy")
        if any(
            row[k] != int(enabled)
            for k in ("stop_support_flag_raw", "stop_default_enabled_raw", "stop_current_enabled_raw")
        ):
            _fail("Global fan-stop support/default/current flags disagree")
        if not row["routes"]:
            _fail("Global fan stop selects an unrouted policy")
        for route in row["routes"]:
            c = coolers.get(route["cooler_index"], {})
            if c.get("type") not in (2, 3) or c.get("control_type") != c.get("type") or not c.get("client_ids"):
                _fail("Global fan-stop policy routes to an unknown cooler")
        if require_ancillary:
            ctl = bytes.fromhex(row["control_hex"])
            lower, upper, nonzero = struct.unpack_from("<III", ctl, 0x54)
            if (
                row["stop_low_q8"] > row["stop_high_q8"]
                or not row["stop_word_44_raw"]
                or row["stop_selector_46_raw"] != 255
                and (lower > upper or not nonzero)
            ):
                _fail("Fan-stop enable requires valid thresholds and ancillary values on every affected policy")
    return rows, decoded, cooler


def _project(state, indices, enabled):
    after = copy.deepcopy(state)
    raw = {k: bytearray.fromhex(v) for k, v in state["raw"].items()}
    for i in indices:
        off, ctl = 16 + i * 20, 20 + i * 96
        raw["policy_info"][off + 14] = raw["policy_info"][off + 15] = int(enabled)
        raw["policy_control"][ctl + 0x48] = int(enabled)
    after["raw"] = {k: v.hex() for k, v in raw.items()}
    return after


def contracts(gpu, inspection, changes):
    if not _selection(changes):
        return {}
    after = _boolean(changes[KEY])
    observation, error = read(gpu)
    if error:
        _fail("Fan-stop metadata is unavailable: " + error)
    state = _state(observation)
    if not isinstance(inspection, Mapping):
        _fail("BIOS fan-stop metadata is unavailable")
    fields = inspection.get("fields", [])
    if not isinstance(fields, list):
        _fail("BIOS fan-stop fields are unavailable")
    indexed = {}
    for field in fields:
        if not isinstance(field, Mapping) or not isinstance(field.get("key"), str) or field["key"] in indexed:
            _fail("BIOS fan-stop fields are malformed or duplicated")
        indexed[field["key"]] = field
    field = indexed.get(KEY, {})
    if field.get("capability") != "editable" or field.get("units") != "bool":
        _fail("BIOS global fan stop is not editable")
    indices = _indices(field.get("policy_indices"))
    before = _boolean(field.get("current"))
    rows, decoded, cooler = _rows(state, indices, before, require_ancillary=before or after)
    binding = inspection.get("fan_policy_binding")
    if not isinstance(binding, Mapping) or not fan._same(binding.get("version"), 32):
        _fail("BIOS fan stop needs the understood policyv20 binding")
    try:
        bios_rows = policy._indexed(binding.get("policies"), "BIOS fan-stop policy")
        routes = policy._indexed(binding.get("routes"), "BIOS fan-stop routing")
    except BiosRuntimeError as exc:
        _fail(str(exc))
    if set(bios_rows) != set(indices) or not fan._same(binding.get("client_id_to_index"), cooler["client_id_to_index"]):
        _fail("Global fan-stop ownership or cooler role map differs")
    runtime_routes = {r["index"]: r for r in decoded["routing_entries"] if r["type"] == 1}
    if set(routes) != set(runtime_routes):
        _fail("BIOS and loaded fan-stop route inventories differ")
    for i, route in runtime_routes.items():
        for runtime, source in (
            ("route_kind_raw", "kind"),
            ("cooler_index", "cooler_index"),
            ("word_04_raw", "word"),
            ("policy_mask_a", "policy_mask_a"),
            ("policy_mask_b", "policy_mask_b"),
        ):
            if not fan._same(route[runtime], routes[i].get(source)):
                _fail("BIOS and loaded fan-stop route identity differs")
    for i in indices:
        row, bios = rows[i], bios_rows[i]
        flags = bios.get("flags")
        if type(flags) is not int or not 0 <= flags <= 255 or flags & 6 != (6 if before else 0):
            _fail("BIOS global fan-stop flags disagree")
        expected = {
            "policy_selector_raw": bios.get("selector_raw"),
            "point2_thermal_relative_flag": (flags >> 3) & 1,
            "flag4_raw": (flags >> 4) & 1,
            "point2_thermal_offset_q8": bios.get("thermal_offset_q8"),
            "curve_unit_selector_raw": int(not flags & 1),
            "stop_word_44_raw": bios.get("stop_word_44"),
            "stop_selector_46_raw": bios.get("stop_selector_46"),
        }
        if any(not fan._same(row[k], v) for k, v in expected.items()):
            _fail("BIOS and loaded fan-stop policy identity differs")
        runtime_values = {f"fan_stop.{i}.low": row["stop_low_q8"], f"fan_stop.{i}.high": row["stop_high_q8"]}
        for point in row["curve"]:
            for role, name in (("temperature", "temperature_q8"), ("level", "duty_q16"), ("rpm", "rpm")):
                runtime_values[f"fan_curve.{i}.{point['point']}.{role}"] = point[name]
        for key, current in runtime_values.items():
            if key == f"fan_curve.{i}.2.temperature" and row["point2_thermal_relative_flag"]:
                continue
            source = indexed.get(key, {})
            unit = "C" if key.endswith((".temperature", ".low", ".high")) else "%" if key.endswith(".level") else "RPM"
            if source.get("units") != unit:
                _fail("BIOS fan-stop peer values or units are unavailable")
            try:
                value = (
                    policy._stored_temperature(source.get("current"))
                    if unit == "C"
                    else fan._value("level_minimum" if unit == "%" else "rpm_minimum", source.get("current"))
                )
            except BiosRuntimeError as exc:
                _fail(str(exc))
            if value != current:
                _fail("Loaded fan-stop peer configuration differs from the BIOS")
    result = {
        KEY: {
            "schema": 1,
            "kind": "fan_stop_global",
            "unit": "bool",
            "selected_fields": [KEY],
            "affected_indices": indices.copy(),
            "before": before,
            "after": after,
            "platform_support_before": "validated_by_loaded_support" if before else "not_observed",
            "before_state": state,
            "after_state": _project(state, indices, after),
        }
    }
    validate_contracts(changes, result)
    return result


def validate_contracts(changes, saved):
    selected = _selection(changes)
    if not isinstance(saved, Mapping) or set(saved) != set(selected):
        _fail("Global fan-stop receipt does not cover the selected field")
    if not selected:
        return []
    r = saved[KEY]
    keys = {
        "schema",
        "kind",
        "unit",
        "selected_fields",
        "affected_indices",
        "before",
        "after",
        "platform_support_before",
        "before_state",
        "after_state",
    }
    if not isinstance(r, Mapping) or set(r) != keys:
        _fail("Malformed global fan-stop receipt")
    expected = {
        "schema": 1,
        "kind": "fan_stop_global",
        "unit": "bool",
        "selected_fields": [KEY],
        "after": _boolean(changes[KEY]),
    }
    if any(not fan._same(r[k], v) for k, v in expected.items()) or type(r["before"]) is not bool:
        _fail("Fan-stop receipt identity or requested value differs")
    indices = _indices(r["affected_indices"])
    evidence = "validated_by_loaded_support" if r["before"] else "not_observed"
    if r["platform_support_before"] != evidence:
        _fail("Fan-stop receipt invents platform-capability evidence")
    _rows(r["before_state"], indices, r["before"], require_ancillary=r["before"] or r["after"])
    _rows(r["after_state"], indices, r["after"], require_ancillary=r["before"] or r["after"])
    if not fan._same(r["after_state"], _project(r["before_state"], indices, r["after"])):
        _fail("Fan-stop receipt changes bytes beyond its exact support/default/current projection")
    return selected


def verify(observation, saved, *, restore=False):
    if not isinstance(saved, Mapping):
        _fail("Fan-stop contracts must be a mapping")
    if not saved:
        return []
    if set(saved) != {KEY} or not isinstance(saved[KEY], Mapping) or type(saved[KEY].get("after")) is not bool:
        _fail("Malformed fan-stop contract")
    selected = validate_contracts({KEY: saved[KEY]["after"]}, saved)
    if isinstance(observation, Mapping) and "fan_stop" in observation:
        observation = observation["fan_stop"]
    state = _state(observation)
    if not fan._same(state, saved[KEY]["before_state" if restore else "after_state"]):
        _fail("Fan-stop flags, identity, routing or opaque peer bytes differ from the expected active state")
    return selected
