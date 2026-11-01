# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact GET-only verification of the loaded linked-current configuration.

Type32's relationship selector, cached current and returned PMU current are
distinct fields. The transaction owner changes the BIOS and restarts the GPU;
this module preserves every INFO/CONTROL byte except selected cached currents.
Dynamic peer STATUS values are not configuration and are not compared.
"""

from __future__ import annotations

import copy
import math
import re
import struct
from collections.abc import Mapping

from . import power_arbitration as arbitration
from .clock_vf_registry import _identity as validate_identity
from .perf_diagnostics import PerfDiagnosticsReader
from .registry_flags import _same_state
from .voltage_bios_runtime import _identity

_KEY = re.compile(r"power\.(0|[1-9][0-9]*)\.linked_current\Z")
KEY = _KEY
INFO_SIZE, INFO_BASE, INFO_STRIDE = 20512, 0xCC, 0x104
CONTROL_SIZE, CONTROL_BASE, CONTROL_STRIDE = 13876, 0x14, 0xC4
STATUS_SIZE, STATUS_BASE, STATUS_STRIDE = 397048, 0x9C, 0x1730
_OBSERVATION_KEYS = {"schema", "stable_reads", "identity", "info_hex", "control_hex", "status_mask", "status_rows"}


class LinkedCurrentError(RuntimeError):
    pass


def _integer(value, low, high, message):
    if type(value) is not int or not low <= value <= high:
        raise LinkedCurrentError(message)
    return value


def _ma(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value != int(value):
        raise LinkedCurrentError("Linked current requires whole milliamps")
    result = int(value)
    if not 0 < result <= 0x7FFFFFFF or struct.unpack("<f", struct.pack("<f", result))[0] != result:
        raise LinkedCurrentError("Linked current needs positive, exactly float32-representable milliamps")
    return result


def _hex(value, size):
    if not isinstance(value, str) or len(value) != size * 2:
        raise LinkedCurrentError("Linked-current packet has an unexpected length")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise LinkedCurrentError("Linked-current packet is not hexadecimal") from exc
    if len(raw) != size or raw.hex() != value:
        raise LinkedCurrentError("Linked-current packet is not canonical hexadecimal")
    return raw


def _u32(raw, at):
    return struct.unpack_from("<I", raw, at)[0]


def validate_observation(observation):
    if (
        not isinstance(observation, dict)
        or set(observation) != _OBSERVATION_KEYS
        or type(observation["schema"]) is not int
        or observation["schema"] != 1
        or type(observation["stable_reads"]) is not int
        or observation["stable_reads"] != 2
    ):
        raise LinkedCurrentError("Malformed linked-current observation")
    try:
        identity = validate_identity(observation["identity"])
    except (ValueError, RuntimeError, TypeError) as exc:
        raise LinkedCurrentError(str(exc)) from exc
    info = _hex(observation["info_hex"], INFO_SIZE)
    control = _hex(observation["control_hex"], CONTROL_SIZE)
    mask = _u32(info, 4)
    if (
        type(observation["status_mask"]) is not int
        or observation["status_mask"] != mask
        or struct.unpack_from("<5I", control) != (0, 0, 0, 255, mask)
    ):
        raise LinkedCurrentError("Linked-current policy masks or CONTROL header disagree")
    rows = observation["status_rows"]
    indices = {str(i) for i in range(32) if mask >> i & 1}
    if not isinstance(rows, dict) or set(rows) != indices:
        raise LinkedCurrentError("Linked-current STATUS inventory differs from INFO")
    for index, row in rows.items():
        kind = info[INFO_BASE + int(index) * INFO_STRIDE + 4]
        keys = {"type", "linked_current_hex"} if kind == 32 else {"type"}
        if not isinstance(row, dict) or set(row) != keys:
            raise LinkedCurrentError("Malformed linked-current STATUS row")
        _integer(row["type"], 0, 255, "Malformed STATUS policy type")
        if kind == 32:
            _hex(row["linked_current_hex"], 4)
    result = copy.deepcopy(observation)
    result["identity"] = identity
    return result


def _once(gpu, reader):
    identity = _identity(reader)
    layout, info, control, status = gpu._power_policy_table()
    decoded = arbitration.decode(layout, info, control, status)
    if not decoded["available"]:
        raise LinkedCurrentError(decoded["reason"])
    if control is None or status is None:
        raise LinkedCurrentError("No loaded linked-current policy inventory")
    if _identity(reader) != identity:
        raise LinkedCurrentError("Linked-current target changed during GET")
    info_raw = struct.pack(f"<{len(info)}I", *info)
    control_raw = struct.pack(f"<{len(control)}I", *control)
    status_raw = struct.pack(f"<{len(status)}I", *status)
    rows = {}
    for index in range(32):
        if not decoded["mask"] >> index & 1:
            continue
        at = STATUS_BASE + index * STATUS_STRIDE
        row = {"type": status_raw[at]}
        if info_raw[INFO_BASE + index * INFO_STRIDE + 4] == 32:
            row["linked_current_hex"] = status_raw[at + 0x148 : at + 0x14C].hex()
        rows[str(index)] = row
    return validate_observation(
        {
            "schema": 1,
            "stable_reads": 2,
            "identity": identity,
            "info_hex": info_raw.hex(),
            "control_hex": control_raw.hex(),
            "status_mask": decoded["mask"],
            "status_rows": rows,
        }
    )


def read(gpu):
    """At most two pairs of fresh GETs; no SET, restart or request replay."""
    reader, last = PerfDiagnosticsReader(gpu), None
    for _ in range(2):
        try:
            with gpu._lock:
                first, second = _once(gpu, reader), _once(gpu, reader)
            if not _same_state(first, second):
                raise LinkedCurrentError("Linked-current configuration changed between fresh reads")
            return second, None
        except Exception as exc:
            last = str(exc)
    return None, last or "Linked-current readback unavailable"


def _selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(key, str) for key in changes):
        raise LinkedCurrentError("Linked-current changes need a field/value mapping")
    for key in changes:
        if key.endswith(".linked_current") and not _KEY.fullmatch(key):
            raise LinkedCurrentError("Malformed linked-current field key")
    selected = {key: value for key, value in changes.items() if _KEY.fullmatch(key)}
    if selected and len(selected) != len(changes):
        raise LinkedCurrentError("Apply linked-current edits separately from other BIOS fields")
    for key, value in selected.items():
        _integer(int(_KEY.fullmatch(key)[1]), 0, 31, "Linked policy index exceeds the understood inventory")
        _ma(value)
    return selected


def _live_row(observation, index, kind, unit):
    info = bytes.fromhex(observation["info_hex"])
    control = bytes.fromhex(observation["control_hex"])
    if not observation["status_mask"] >> index & 1:
        raise LinkedCurrentError("Selected linked policy is missing from the runtime inventory")
    ip, cp = INFO_BASE + index * INFO_STRIDE, CONTROL_BASE + index * CONTROL_STRIDE
    if (
        info[ip] != 0x40
        or info[ip + 4] != kind
        or info[ip + 6] != unit
        or _u32(control, cp) != kind
        or observation["status_rows"][str(index)]["type"] != kind
    ):
        raise LinkedCurrentError("Selected policy type, table format or units disagree")
    return info, control, ip, cp


def _selected_current(observation, index):
    _, control, _, cp = _live_row(observation, index, 32, 0)
    cached = _u32(control, cp + 0x28)
    raw = bytes.fromhex(observation["status_rows"][str(index)]["linked_current_hex"])
    value = struct.unpack("<f", raw)[0]
    if _ma(cached) != _ma(value) or raw != struct.pack("<f", cached):
        raise LinkedCurrentError("Linked cached request and PMU current disagree or were rounded")
    return cached


def _table(value):
    names = {"offset", "size", "version", "header_size", "stride", "count"}
    if not isinstance(value, dict) or set(value) != names:
        raise LinkedCurrentError("Linked current lacks its power table geometry")
    if any(type(value[k]) is not int for k in names):
        raise LinkedCurrentError("Power table geometry must contain integers")
    if value["version"] != 0x40 or value["header_size"] not in (0x38, 0x3D) or value["stride"] != 0x66:
        raise LinkedCurrentError("Linked current needs the understood V4 power table")
    _integer(value["count"], 1, 255, "Power table count is invalid")
    _integer(value["offset"], 0, 16 * 1024 * 1024, "Power table offset is invalid")
    if value["size"] != value["header_size"] + value["count"] * value["stride"]:
        raise LinkedCurrentError("Power table size and row geometry disagree")
    return copy.deepcopy(value)


def _policy(value, table, *, source):
    if not isinstance(value, dict) or set(value) != {
        "index",
        "vbios_type",
        "runtime_type",
        "channel",
        "units",
        "limits",
    }:
        raise LinkedCurrentError("Malformed linked policy binding")
    _integer(value["index"], 0, min(31, table["count"] - 1), "Linked policy index is unavailable")
    _integer(value["channel"], 0, 255, "Linked policy channel is invalid")
    wanted = (13, 32, "mW") if source else (11, 30, "mA")
    if (
        any(type(value[k]) is not int for k in ("vbios_type", "runtime_type"))
        or (value["vbios_type"], value["runtime_type"], value["units"]) != wanted
    ):
        raise LinkedCurrentError("Linked policy class translation or units differ")
    limits = value["limits"]
    if not isinstance(limits, list) or len(limits) != 3:
        raise LinkedCurrentError("Linked policy requires all three BIOS limits")
    for item in limits:
        _integer(item, 0, 0xFFFFFFFF, "Linked policy limit is invalid")
    if limits != sorted(limits):
        raise LinkedCurrentError("Linked policy limits are inconsistent")
    return copy.deepcopy(value)


def _binding(value, index):
    if not isinstance(value, dict) or set(value) != {"table", "source", "target", "relationship", "linked_offset"}:
        raise LinkedCurrentError("Malformed linked-current BIOS binding")
    table = _table(value["table"])
    source, target = _policy(value["source"], table, source=True), _policy(value["target"], table, source=False)
    if source["index"] != index or source["index"] == target["index"]:
        raise LinkedCurrentError("Linked-current source and target binding disagree")
    row = table["offset"] + table["header_size"] + index * table["stride"]
    if type(value["linked_offset"]) is not int or value["linked_offset"] != row + 61:
        raise LinkedCurrentError("Linked-current BIOS word offset differs")
    relation = value["relationship"]
    if not isinstance(relation, dict) or set(relation) != {
        "index",
        "kind",
        "stride",
        "count",
        "offset",
        "target_policy",
        "parameter_u16",
    }:
        raise LinkedCurrentError("Malformed linked-current relationship binding")
    for name in relation:
        _integer(relation[name], 0, 0xFFFFFFFF, "Relationship metadata must contain unsigned integers")
    if relation["kind"] != 1 or relation["stride"] != 18 or not 0 < relation["count"] <= 255:
        raise LinkedCurrentError("Linked current requires an understood kind1 relationship")
    if (
        relation["index"] >= relation["count"]
        or relation["target_policy"] != target["index"]
        or relation["parameter_u16"] > 0xFFFF
        or relation["offset"] != table["offset"] + table["size"] + relation["index"] * relation["stride"]
    ):
        raise LinkedCurrentError("Relationship index, target or row offset disagrees")
    return copy.deepcopy(value)


def _fields(inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    if (
        not isinstance(inspection, Mapping)
        or not isinstance(inspection.get("tables"), list)
        or not isinstance(inspection.get("fields"), list)
    ):
        raise LinkedCurrentError("Linked-current BIOS inspection is unavailable")
    tables = [t for t in inspection["tables"] if isinstance(t, dict) and t.get("name") == "power"]
    if len(tables) != 1:
        raise LinkedCurrentError("Linked current needs one power table")
    table = _table({k: tables[0].get(k) for k in ("offset", "size", "version", "header_size", "stride", "count")})

    def field(key):
        found = [f for f in inspection["fields"] if isinstance(f, dict) and f.get("key") == key]
        if len(found) != 1:
            raise LinkedCurrentError("Missing or ambiguous BIOS field: " + key)
        return found[0]

    def encoding(item, units, offset):
        if (
            item.get("capability") != "editable"
            or item.get("units") != units
            or item.get("encoding") != "I"
            or type(item.get("width")) is not int
            or item["width"] != 4
            or type(item.get("scale")) is not int
            or item["scale"] != 1
            or item.get("mask") is not None
            or item.get("offsets") != [offset]
            or any(type(v) is not int for v in item.get("offsets", []))
        ):
            raise LinkedCurrentError("Linked-current BIOS field encoding differs")

    def policy(index, is_source):
        _integer(index, 0, min(31, table["count"] - 1), "BIOS linked policy index is invalid")
        row = table["offset"] + table["header_size"] + index * table["stride"]
        default = field(f"power.{index}.default")
        runtime = default.get("runtime_binding", {})
        binding = {
            "index": runtime.get("policy_index"),
            "vbios_type": runtime.get("vbios_type"),
            "runtime_type": runtime.get("runtime_type"),
            "channel": runtime.get("channel"),
            "units": runtime.get("units"),
            "limits": [],
        }
        for ordinal, role in enumerate(("minimum", "default", "maximum")):
            item = field(f"power.{index}.{role}")
            encoding(item, binding["units"], row + 13 + ordinal * 4)
            if item.get("policy") != index or type(item.get("policy")) is not int:
                raise LinkedCurrentError("BIOS policy ordinal differs")
            binding["limits"].append(item.get("current"))
        if binding["index"] != index:
            raise LinkedCurrentError("Runtime policy binding differs from its BIOS ordinal")
        return _policy(binding, table, source=is_source)

    result = {}
    for key in selected:
        index = int(_KEY.fullmatch(key)[1])
        item = field(key)
        row = table["offset"] + table["header_size"] + index * table["stride"]
        encoding(item, "mA", row + 61)
        if type(item.get("policy")) is not int or item["policy"] != index:
            raise LinkedCurrentError("Linked-current source policy differs")
        relation = item.get("relationship_binding")
        if not isinstance(relation, dict):
            raise LinkedCurrentError("Linked current lacks its resolved relationship")
        relation = {**relation, "index": item.get("linked_relationship")}
        binding = _binding(
            {
                "table": table,
                "source": policy(index, True),
                "target": policy(item.get("linked_policy"), False),
                "relationship": relation,
                "linked_offset": row + 61,
            },
            index,
        )
        if (
            type(item.get("linked_relationship_offset")) is not int
            or item["linked_relationship_offset"] != relation["offset"]
        ):
            raise LinkedCurrentError("Linked relationship offset disagrees with its metadata")
        result[key] = (binding, _ma(item.get("current")))
    return result


def _project(before, selected):
    control = bytearray.fromhex(before["control_hex"])
    for key, entry in selected.items():
        index = int(_KEY.fullmatch(key)[1])
        binding = _binding(entry["binding"], index)
        for name, kind, unit in (("source", 32, 0), ("target", 30, 1)):
            policy = binding[name]
            info, _, ip, _ = _live_row(before, policy["index"], kind, unit)
            if info[ip + 5] != policy["channel"] or list(struct.unpack_from("<3I", info, ip + 8)) != policy["limits"]:
                raise LinkedCurrentError("Loaded policy channel or limits differ from the BIOS binding")
        ip = INFO_BASE + index * INFO_STRIDE
        if (
            struct.unpack_from("<H", bytes.fromhex(before["info_hex"]), ip + 0x34)[0]
            != binding["relationship"]["index"]
        ):
            raise LinkedCurrentError("Loaded linked relationship selector differs from the BIOS")
        old, new = _ma(entry["before_ma"]), _ma(entry["after_ma"])
        if (
            any(type(entry[name]) is not int for name in ("before_ma", "after_ma"))
            or _selected_current(before, index) != old
        ):
            raise LinkedCurrentError("Loaded linked current differs from its captured BIOS value")
        low, _, high = binding["target"]["limits"]
        if not low <= old <= high or not low <= new <= high:
            raise LinkedCurrentError("Linked current lies outside the resolved target policy bounds")
        struct.pack_into("<I", control, CONTROL_BASE + index * CONTROL_STRIDE + 0x28, new)
    return control.hex()


def contracts(gpu, inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    bindings = _fields(inspection, selected)
    observation, error = read(gpu)
    if error:
        raise LinkedCurrentError(error)
    before = validate_observation(observation)
    entries = {
        key: {"binding": bindings[key][0], "before_ma": bindings[key][1], "after_ma": _ma(value)}
        for key, value in selected.items()
    }
    saved = {"schema": 1, "selected": entries, "before": before, "after_control_hex": _project(before, entries)}
    validate_contracts(changes, saved)
    return saved


def validate_contracts(changes, saved):
    selected = _selected(changes)
    if not selected and saved == {}:
        return
    if (
        not selected
        or not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "before", "after_control_hex"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or not isinstance(saved["selected"], dict)
        or set(saved["selected"]) != set(selected)
    ):
        raise LinkedCurrentError("Linked-current changes and receipt disagree")
    before = validate_observation(saved["before"])
    for key, wanted in selected.items():
        entry = saved["selected"][key]
        if not isinstance(entry, dict) or set(entry) != {"binding", "before_ma", "after_ma"}:
            raise LinkedCurrentError("Malformed selected linked-current receipt")
        if type(entry["after_ma"]) is not int or entry["after_ma"] != _ma(wanted):
            raise LinkedCurrentError("Saved linked-current value differs from its change")
    _hex(saved["after_control_hex"], CONTROL_SIZE)
    if saved["after_control_hex"] != _project(before, saved["selected"]):
        raise LinkedCurrentError("Linked-current projection changes an unselected CONTROL byte")


def validate_inspection(inspection, changes, saved):
    validate_contracts(changes, saved)
    bindings = _fields(inspection, changes)
    for key, (binding, old) in bindings.items():
        entry = saved["selected"][key]
        if not _same_state(binding, entry["binding"]) or old != entry["before_ma"]:
            raise LinkedCurrentError("Linked-current BIOS relationship or value changed after preview")


def verify(observation, saved, *, restore=False):
    if saved == {}:
        return []
    try:
        changes = {key: entry["after_ma"] for key, entry in saved["selected"].items()}
    except (KeyError, TypeError, AttributeError) as exc:
        raise LinkedCurrentError("Malformed linked-current receipt") from exc
    validate_contracts(changes, saved)
    current = validate_observation(observation)
    before = saved["before"]
    if (
        current["identity"] != before["identity"]
        or current["info_hex"] != before["info_hex"]
        or current["control_hex"] != (before["control_hex"] if restore else saved["after_control_hex"])
    ):
        raise LinkedCurrentError("Linked-current identity or preserved INFO/CONTROL differs")
    for key, entry in saved["selected"].items():
        if _selected_current(current, int(_KEY.fullmatch(key)[1])) != entry["before_ma" if restore else "after_ma"]:
            raise LinkedCurrentError("Selected linked-current cache or PMU readback differs")
    return list(saved["selected"])
