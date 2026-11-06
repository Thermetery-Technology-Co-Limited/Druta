# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed INFO verification for persistent relationship-weight overrides."""

import copy
import math
import re
import struct
from collections.abc import Mapping

from . import power_relationships, vbios_fields
from .clock_vf_registry import _identity
from .perf_diagnostics import PerfDiagnosticsReader
from .voltage_bios_runtime import _identity as _paired_identity

KEY = re.compile(r"power_relationship\.(0|[1-9][0-9]*)\.weight\Z")


class PowerWeightError(RuntimeError):
    pass


def _raw(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise PowerWeightError("Relationship weight must be a finite multiplier")
    raw = value * 4096
    if not 0 <= raw <= 65535 or int(raw) != raw:
        raise PowerWeightError("Relationship weight must be exactly representable as unsigned Q4.12")
    return int(raw)


def _selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(k, str) for k in changes):
        raise PowerWeightError("Weight changes require a field mapping")
    for key in changes:
        if key.startswith("power_relationship.") and not KEY.fullmatch(key):
            raise PowerWeightError("Malformed relationship weight key")
    result = {k: v for k, v in changes.items() if KEY.fullmatch(k)}
    if result and len(result) != len(changes):
        raise PowerWeightError("Apply relationship weights separately from other BIOS fields")
    for key, value in result.items():
        if int(KEY.fullmatch(key)[1]) >= 32:
            raise PowerWeightError("Relationship index exceeds INFO capacity")
        _raw(value)
    return result


def read(gpu):
    """Two matching INFO-only observations, with one bounded retry."""
    last = None
    for _ in range(2):
        try:
            with gpu._lock:
                reader = PerfDiagnosticsReader(gpu)

                def once(reader=reader):
                    identity = _paired_identity(reader)
                    layout = gpu._current_limit_abi()
                    info = gpu._current_limit_rm(0x2080A618)
                    if _paired_identity(reader) != identity:
                        raise PowerWeightError("Adapter changed during INFO read")
                    report = power_relationships.decode(layout, info)
                    if not report.get("available"):
                        raise PowerWeightError(report.get("reason") or "Relationship INFO unavailable")
                    report["identity"] = identity
                    report["info_hex"] = struct.pack(f"<{len(info)}I", *info).hex()
                    _identity(identity)
                    return report

                first, second = once(), once()
            if first != second:
                raise PowerWeightError("Relationship INFO changed between reads")
            return second, None
        except Exception as exc:
            last = str(exc)
    return None, last or "Relationship INFO unavailable"


def _fields(inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    if not isinstance(inspection, Mapping):
        raise PowerWeightError("Missing BIOS inspection")
    tables = [t for t in inspection.get("tables", []) if t.get("name") == "power"]
    fields = inspection.get("fields", [])
    if len(tables) != 1 or not isinstance(fields, list):
        raise PowerWeightError("Missing unique power table")
    table = tables[0]
    if (table.get("version") != 0x40 or table.get("header_size") not in (0x38, 0x3d)
            or table.get("stride") != 0x66 or type(table.get("offset")) is not int
            or type(table.get("count")) is not int or table.get("size") != table["header_size"] + table["count"] * 0x66):
        raise PowerWeightError("Unsupported power table geometry")

    def field(key):
        found = [f for f in fields if isinstance(f, dict) and f.get("key") == key]
        if len(found) != 1:
            raise PowerWeightError("Missing or ambiguous BIOS field " + key)
        return found[0]

    out = {}
    for key in selected:
        i, item = int(KEY.fullmatch(key)[1]), field(key)
        target = item.get("target_policy")
        count = item.get("relationship_count")
        offset = table["offset"] + table["size"] + 18 * i
        if (type(count) is not int or not i < count <= 255 or type(target) is not int
                or not 0 <= target < min(table["count"], 32)
                or item.get("relationship_index") != i or item.get("relationship_kind") != 1
                or item.get("relationship_stride") != 18 or item.get("relationship_offset") != offset
                or item.get("offsets") != [offset + 2] or item.get("encoding") != "H"
                or item.get("width") != 2 or item.get("scale") != 4096 or item.get("mask") is not None
                or item.get("units") != "multiplier" or item.get("capability") != "editable"):
            raise PowerWeightError("Relationship BIOS geometry or encoding differs")
        policy = field(f"power.{target}.default")
        runtime = policy.get("runtime_binding", {})
        kind = runtime.get("vbios_type")
        translated = vbios_fields.power_runtime_type(0x40, kind)
        if (runtime.get("policy_index") != target or translated is None
                or runtime.get("runtime_type") != translated or runtime.get("units") not in ("mW", "mA")
                or type(runtime.get("channel")) is not int):
            raise PowerWeightError("Target BIOS policy has no understood runtime translation")
        limits = []
        row = table["offset"] + table["header_size"] + target * 0x66
        for ordinal, role in enumerate(("minimum", "default", "maximum")):
            p = field(f"power.{target}.{role}")
            if (p.get("offsets") != [row + 13 + ordinal * 4] or p.get("encoding") != "I"
                    or p.get("capability") != "editable" or p.get("runtime_binding") != runtime
                    or type(p.get("current")) is not int):
                raise PowerWeightError("Target policy limit binding differs")
            limits.append(p["current"])
        if limits != sorted(limits):
            raise PowerWeightError("Target policy limits are inconsistent")
        before = _raw(item.get("current"))
        if item.get("raw") != before:
            raise PowerWeightError("Relationship raw value differs from decoded weight")
        out[key] = ({"index": i, "offset": offset, "count": count, "target": target,
                     "target_metadata": {"policy": target, "type": translated,
                         "channel": runtime["channel"], "unit": runtime["units"],
                         "minimum": limits[0], "default": limits[1], "maximum": limits[2]}}, before)
    return out


def _check(observation, binding, raw):
    if not isinstance(observation, dict) or not observation.get("available"):
        raise PowerWeightError("Missing relationship INFO observation")
    try:
        _identity(observation.get("identity"))
        packet = bytes.fromhex(observation["info_hex"])
        if len(packet) != 20512:
            raise ValueError("invalid INFO size")
        parsed = power_relationships.decode(
            {"info": (0xCC, 0x104), "sizes": {0x2080A618: 20512}},
            struct.unpack("<5128I", packet),
        )
        if not parsed.get("available") or parsed["relationships"] != observation.get("relationships"):
            raise ValueError("INFO records do not match packet")
    except (KeyError, ValueError, TypeError, RuntimeError, struct.error) as exc:
        raise PowerWeightError("Malformed identity or relationship INFO") from exc
    records = observation.get("relationships")
    if not isinstance(records, list):
        raise PowerWeightError("Malformed relationship INFO records")
    matches = [r for r in records if isinstance(r, dict) and r.get("index") == binding["index"]]
    if len(matches) != 1:
        raise PowerWeightError("Selected relationship is absent or ambiguous")
    r = matches[0]
    if (r.get("type") != 0 or r.get("target") != binding["target"] or r.get("weight_raw") != raw
            or r.get("target_metadata") != binding["target_metadata"]):
        raise PowerWeightError("Loaded relationship or target metadata differs from BIOS")


def contracts(gpu, inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    fields = _fields(inspection, selected)
    before, error = read(gpu)
    if error:
        raise PowerWeightError(error)
    entries = {k: {"binding": fields[k][0], "before_raw": fields[k][1], "after_raw": _raw(v)}
               for k, v in selected.items()}
    saved = {"schema": 1, "selected": entries, "before": before}
    validate_contracts(changes, saved)
    return saved


def validate_contracts(changes, saved):
    selected = _selected(changes)
    if not selected and saved == {}:
        return
    if (not selected or not isinstance(saved, dict) or set(saved) != {"schema", "selected", "before"}
            or saved["schema"] != 1 or not isinstance(saved["selected"], dict)
            or set(saved["selected"]) != set(selected)):
        raise PowerWeightError("Weight receipt does not match selected changes")
    for key, value in selected.items():
        entry = saved["selected"][key]
        if (not isinstance(entry, dict) or set(entry) != {"binding", "before_raw", "after_raw"}
                or type(entry["before_raw"]) is not int or not 0 <= entry["before_raw"] <= 65535
                or type(entry["after_raw"]) is not int or entry["after_raw"] != _raw(value)
                or not isinstance(entry["binding"], dict)
                or entry["binding"].get("index") != int(KEY.fullmatch(key)[1])):
            raise PowerWeightError("Malformed weight receipt")
        _check(saved["before"], entry["binding"], entry["before_raw"])


def validate_inspection(inspection, changes, saved):
    validate_contracts(changes, saved)
    for key, (binding, old) in _fields(inspection, changes).items():
        if binding != saved["selected"][key]["binding"] or old != saved["selected"][key]["before_raw"]:
            raise PowerWeightError("Relationship BIOS binding changed after preview")


def verify(observation, saved, *, restore=False):
    if saved == {}:
        return []
    try:
        changes = {k: e["after_raw"] / 4096 for k, e in saved["selected"].items()}
    except (KeyError, TypeError, AttributeError) as exc:
        raise PowerWeightError("Malformed weight receipt") from exc
    validate_contracts(changes, saved)
    if not isinstance(observation, dict) or observation.get("identity") != saved["before"].get("identity"):
        raise PowerWeightError("Relationship adapter identity changed")
    try:
        expected = bytearray.fromhex(saved["before"]["info_hex"])
        actual = bytes.fromhex(observation["info_hex"])
        if len(expected) != 20512 or len(actual) != 20512:
            raise ValueError("invalid INFO size")
        if not restore:
            for entry in saved["selected"].values():
                struct.pack_into("<H", expected, 0x41D0 + entry["binding"]["index"] * 0x24 + 6, entry["after_raw"])
        if actual != expected:
            raise PowerWeightError("Unselected INFO bytes or selected weight differ")
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise PowerWeightError("Malformed INFO packet") from exc
    for entry in saved["selected"].values():
        _check(observation, entry["binding"], entry["before_raw" if restore else "after_raw"])
    return list(saved["selected"])


def annotate_fields(gpu, inspection):
    result = copy.deepcopy(inspection)
    candidates = [f for f in result.get("fields", []) if KEY.fullmatch(f.get("key", ""))]
    if not candidates:
        return result, None
    observation, error = read(gpu)
    for item in candidates:
        try:
            if error:
                raise PowerWeightError(error)
            key = item["key"]
            binding, raw = _fields(result, {key: item["current"]})[key]
            _check(observation, binding, raw)
        except (PowerWeightError, ValueError, KeyError, TypeError) as exc:
            item["capability"], item["reason"] = "read-only", str(exc)
    return result, error
