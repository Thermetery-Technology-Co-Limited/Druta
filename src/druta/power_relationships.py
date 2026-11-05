# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Decode configured power relationship metadata from a validated INFO packet.

This is a read-only interpretation of the known V4 INFO geometry. Relationship
records identify targets; separate policy fields identify configured source
references. Neither identifies arbitration clients or proves runtime execution.
Evidence: experiments/power-relationship-info-20261105.md and
experiments/power-type33-relationship-ranges-20261105.md. No device identity
or driver version selects this layout.
"""

import struct
from collections.abc import Mapping, Sequence

_INFO = 0x2080A618
_SIZE = 20512
_POLICY_BASE, _POLICY_STRIDE = 0xCC, 0x104
_REL_MASK, _REL_BASE, _REL_STRIDE = 0x41CC, 0x41D0, 0x24
_CAPACITY = 32


def decode(layout, info):
    """Return configured relation records without reading STATUS or CONTROL.

    A type-0 relationship has a unitless Q4.12 ``weight_raw``. Unknown
    relationship types are retained without interpreting their payload. Target
    metadata is independently unavailable when the target is absent or its
    policy row is not an understood V4 row with a known unit.
    """
    result = {"available": False, "reason": None, "relationships": []}
    if not isinstance(layout, Mapping) or not isinstance(layout.get("sizes"), Mapping):
        result["reason"] = "The policy INFO layout is unavailable."
        return result
    sizes = layout["sizes"]
    size = sizes.get(_INFO, sizes.get(str(_INFO)))
    commands = layout.get("commands", {})
    if (not isinstance(commands, Mapping)
            or commands.get(_INFO, commands.get(str(_INFO), _INFO)) != _INFO
            or layout.get("info") not in ((_POLICY_BASE, _POLICY_STRIDE), [_POLICY_BASE, _POLICY_STRIDE])
            or type(size) is not int or size != _SIZE):
        result["reason"] = "The policy INFO geometry or command is not understood."
        return result
    if (not isinstance(info, Sequence) or isinstance(info, (str, bytes, bytearray, memoryview))
            or len(info) != _SIZE // 4
            or any(type(word) is not int or not 0 <= word <= 0xFFFFFFFF for word in info)):
        result["reason"] = "The policy INFO packet is invalid or incomplete."
        return result

    raw = struct.pack(f"<{len(info)}I", *info)
    policy_mask = info[1]
    relation_mask = struct.unpack_from("<I", raw, _REL_MASK)[0]
    for index in range(_CAPACITY):
        if not relation_mask >> index & 1:
            continue
        offset = _REL_BASE + index * _REL_STRIDE
        kind, target = raw[offset], raw[offset + 1]
        record = {
            "index": index,
            "type": kind,
            "target": target,
            "weight_raw": struct.unpack_from("<H", raw, offset + 6)[0] if kind == 0 else None,
            "target_metadata": None,
            "target_reason": None,
        }
        if target >= _CAPACITY or not policy_mask >> target & 1:
            record["target_reason"] = "The target policy is absent from the INFO mask."
        else:
            at = _POLICY_BASE + target * _POLICY_STRIDE
            version, header, minimum, default, maximum = struct.unpack_from("<5I", raw, at)
            unit_code = (header >> 16) & 0xFF
            unit = {0: "mW", 1: "mA"}.get(unit_code)
            if version & 0xFF != 0x40:
                record["target_reason"] = "The target policy row format is not understood."
            elif unit is None:
                record["target_reason"] = "The target policy unit is not understood."
            else:
                record["target_metadata"] = {
                    "policy": target,
                    "type": header & 0xFF,
                    "channel": (header >> 8) & 0xFF,
                    "unit": unit,
                    "minimum": minimum,
                    "default": default,
                    "maximum": maximum,
                }
        result["relationships"].append(record)
    by_index = {record["index"]: record for record in result["relationships"]}
    result["source_references"] = []
    result["linked_references"] = []
    result["range_references"] = []
    result["source_uninterpreted_policies"] = []
    for index in range(_CAPACITY):
        if not policy_mask >> index & 1:
            continue
        at = _POLICY_BASE + index * _POLICY_STRIDE
        kind = raw[at + 4]
        if kind == 33:
            first, last = struct.unpack_from("<HH", raw, at + 0x34)
            source = {"policy": index, "first": first, "last": last,
                      "relationships": [], "available": False, "reason": None}
            if raw[at] != 0x40:
                source["reason"] = "The source policy row format is not understood."
            elif first > last:
                # The host skips a reversed range before looking up any index,
                # including a possible 0xFFFF endpoint; do not invent a sentinel.
                source["available"] = True
            elif last >= _CAPACITY:
                source["reason"] = "The source relationship range exceeds INFO capacity."
            else:
                indices = list(range(first, last + 1))
                if any(i not in by_index or by_index[i]["type"] != 0
                       or by_index[i]["target_metadata"] is None for i in indices):
                    source["reason"] = "A referenced relationship or target is unavailable."
                else:
                    source["relationships"] = indices
                    source["available"] = True
            result["range_references"].append(source)
            continue
        if kind == 32:
            relation = struct.unpack_from("<H", raw, at + 0x34)[0]
            record = by_index.get(relation)
            link = {"policy": index, "relationship": relation,
                    "target": record["target"] if record else None,
                    "available": False, "reason": None}
            if raw[at] != 0x40:
                link["reason"] = "The source policy row format is not understood."
            elif relation >= _CAPACITY or record is None:
                link["reason"] = "The linked relationship is absent from the INFO mask."
            elif record["type"] != 0 or record["target_metadata"] is None:
                link["reason"] = "The linked relationship or target is unavailable."
            else:
                link["available"] = True
            result["linked_references"].append(link)
            continue
        if kind != 0:
            result["source_uninterpreted_policies"].append(index)
            continue
        first, last = struct.unpack_from("<HH", raw, at + 0xBC)
        flag_a, flag_b = raw[at + 0xB9], raw[at + 0xC0]
        source = {"policy": index, "available": False, "reason": None,
                  "enabled": None, "first": first, "last": last, "relationships": []}
        if raw[at] != 0x40:
            source["reason"] = "The source policy row format is not understood."
        elif flag_a not in (0, 1) or flag_b not in (0, 1):
            source["reason"] = "The source relationship flags are not understood."
        else:
            source["enabled"] = bool(flag_a or flag_b)
            if not source["enabled"]:
                source["available"] = True
            elif first > last or last >= _CAPACITY:
                source["reason"] = "The source relationship range is invalid."
            else:
                indices = list(range(first, last + 1))
                if any(i not in by_index or by_index[i]["type"] != 0
                       or by_index[i]["target_metadata"] is None for i in indices):
                    source["reason"] = "A referenced relationship or target is unavailable."
                else:
                    source["relationships"] = indices
                    source["available"] = True
        result["source_references"].append(source)
    result["available"] = True
    result["policy_mask"] = policy_mask
    result["relationship_mask"] = relation_mask
    return result
