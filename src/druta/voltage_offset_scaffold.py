# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan and verify independent VFE constant heads for reported policy offsets.

The caller supplies fresh, target-bound VFE equation and variable INFO reads.
This module performs no registry, restart, or GPU operations. A loaded head
is a definition, not proof of a changed evaluated or physical voltage.
"""

import hashlib
import math
import struct

from . import vbios_fields as vb
from . import vfe_equation_runtime as eq

_ROLES = {"REL": 13, "ALT": 15, "OV": 17, "VMIN": 19}
_EMPTY = bytes.fromhex("00ffff000000000000000000000000000000000000000000ff")
_VARIABLE_INFO_SIZE = 0xC364


class VoltageOffsetScaffoldError(ValueError):
    """The requested scaffold is not supported by the supplied evidence."""


def _requests(requests, rail_count):
    if not isinstance(requests, (list, tuple)) or not requests:
        raise VoltageOffsetScaffoldError("Select at least one rail policy")
    result = []
    for item in requests:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise VoltageOffsetScaffoldError("Each selection needs a rail and policy")
        rail, role = item
        if type(rail) is not int or not 0 <= rail < rail_count or not isinstance(role, str) or role not in _ROLES:
            raise VoltageOffsetScaffoldError("Unknown rail or policy")
        if (rail, role) in result:
            raise VoltageOffsetScaffoldError("Duplicate rail policy")
        result.append((rail, role))
    return result


def plan(image, before, variable_info, requests):
    """Return a compose-records plan and head bindings from captured inputs.

    Variable INFO must be a fresh successful target-bound native GET; its raw
    bytes alone do not prove freshness or bind it to the equation observation.
    """
    if not isinstance(variable_info, bytes) or len(variable_info) != _VARIABLE_INFO_SIZE:
        raise VoltageOffsetScaffoldError("A complete native variable INFO packet is required")
    info, _, mask = eq._shape(before)
    variable_mask = int.from_bytes(variable_info[4:36], "little")
    p = vb._Image(image)
    vfe, rail = p.pointer("P", 0x20), p.pointer("P", 0x68)
    b = p.b
    if b[vfe:vfe + 5] != bytes((0x30, 14, 29, 255, 25)) or b[rail:rail + 3] != bytes((0x30, 5, 65)):
        raise VoltageOffsetScaffoldError("VFE or rail layout is not understood")
    count = b[vfe + 5] | b[vfe + 10] << 8
    rail_count = b[rail + 3]
    if not 0 < count <= min(4096, eq.CAPACITY) or rail_count not in (1, 2):
        raise VoltageOffsetScaffoldError("VFE or rail geometry is not understood")
    selected = _requests(requests, rail_count)
    base = vfe + 14 + 29 * 255
    term_count = struct.unpack_from("<H", b, vfe + 12)[0]
    if b[vfe + 11] != 5:
        raise VoltageOffsetScaffoldError("VFE term geometry is not understood")
    end = base + 25 * count + 5 * term_count
    p.own(vfe, end - vfe, 0x14)
    graph = p.voltage_graph(vfe, base, count)
    clocks = p.voltage_clock_roots(base, count)
    p.voltage()  # Validate all known rail selectors, including unnamed ones.
    rows = [b[base + 25*i:base + 25*(i+1)] for i in range(count)]
    if mask >> count or any(bool(mask >> i & 1) != bool(row[0]) for i, row in enumerate(rows)):
        raise VoltageOffsetScaffoldError("Loaded equation inventory differs from the image")
    possible_bytes = set()
    for rail_index in range(rail_count):
        row = rail + 5 + 65*rail_index
        possible_bytes.update(b[row+offset] for offset in vb.VOLTAGE_RAIL_POSSIBLE_VFE_BYTES
                              if b[row+offset] != 255)
        for offset in vb.VOLTAGE_RAIL_VFE_SELECTORS:
            target = struct.unpack_from("<H", b, row+offset)[0]
            if target != 0xffff and (target >= count or not rows[target][0]):
                raise VoltageOffsetScaffoldError("A rail selector points outside the active graph")
    slots = [i for i, row in enumerate(rows) if row == _EMPTY and not mask >> i & 1
             and i not in graph and i not in clocks and i not in possible_bytes]
    if len(slots) < len(selected):
        raise VoltageOffsetScaffoldError("Not enough canonical inactive equation slots")
    template = None
    preferred = [struct.unpack_from("<H", b, rail + 5 + 65*i + _ROLES["ALT"])[0]
                 for i in dict.fromkeys(rail_index for rail_index, _ in selected)]
    order = list(dict.fromkeys(i for i in preferred if i < count))
    preferred_set = set(order)
    order.extend(i for i in range(count) if i not in preferred_set)
    for i in order:
        row = rows[i]
        var = row[1]
        if row[0] != 1 or row[23] != 2 or var >= 255 or not variable_mask >> var & 1:
            continue
        values = struct.unpack_from("<5f", row, 3)
        if not all(math.isfinite(x) for x in values) or values[0] != values[1] or values[0] != values[2] or values[3:] != (0, 0):
            continue
        try:
            loaded = eq.constant_row(before, i)
        except eq.VfeEquationError:
            continue
        nxt = row[2] | row[24] << 8
        if loaded == {"index": i, "value_uv": int(values[0]), "next_index": nxt, "variable": var, "output_type": 2}:
            template = row
            break
    if template is None:
        raise VoltageOffsetScaffoldError("No loaded constant template with a present variable")
    after = bytearray(b)
    heads = []
    for (rail_index, role), index in zip(selected, slots, strict=False):
        rail_row = rail + 5 + 65 * rail_index
        if b[rail_row:rail_row + 2] != bytes((1, rail_index + 1)):
            raise VoltageOffsetScaffoldError("Rail identity is not understood")
        selector = rail_row + _ROLES[role]
        old = struct.unpack_from("<H", b, selector)[0]
        if old >= count or not rows[old][0] or not mask >> old & 1 or rows[old][23] & 15 != 2:
            raise VoltageOffsetScaffoldError("Selected policy root is unavailable or not in microvolts")
        if info[eq.HEADER + old*eq.INFO_STRIDE + 0x6E] != 2:
            raise VoltageOffsetScaffoldError("Loaded policy root output is not in microvolts")
        native_type = {1: 3, 2: 2, 3: 1, 7: 6}.get(rows[old][0])
        pos = eq.HEADER + old*eq.INFO_STRIDE
        if (native_type is None or info[pos] != native_type or info[pos+0x6C] != native_type
                or struct.unpack_from("<H", info, pos+2)[0] != (rows[old][2] | rows[old][24] << 8)):
            raise VoltageOffsetScaffoldError("Loaded policy root type or next link differs from the image")
        at = base + 25*index
        after[at], after[at+1], after[at+2] = 1, template[1], old & 255
        after[at+3:at+15] = struct.pack("<3f", 0, 0, 0)
        after[at+23], after[at+24] = 2, old >> 8
        struct.pack_into("<H", after, selector, index)
        heads.append({"rail": rail_index, "role": role, "key": f"voltage.{rail_index}.{role}.offset",
                      "index": index, "next_index": old, "variable": template[1], "output_type": 2,
                      "selector_offset": selector, "row_offset": at})
    q = vb._Image(after)
    q.voltage_graph(vfe, base, count)
    if q.voltage_clock_roots(base, count) != clocks or after[base+25*count:end] != b[base+25*count:end]:
        raise VoltageOffsetScaffoldError("Scaffold changed a clock root or term")
    patches = []
    for i, (old, new) in enumerate(zip(b, after, strict=True)):
        if old != new:
            patches.append({"offset": i, "before": bytes((old,)).hex(), "after": bytes((new,)).hex(),
                            "field_keys": ["voltage.offset.scaffold"]})
    result = {"baseline_sha256": hashlib.sha256(b).hexdigest(), "expected_sha256": hashlib.sha256(after).hexdigest(),
              "patches": patches}
    return result, heads


def verify_loaded(image, before, variable_info, requests, loaded_image, loaded):
    """Verify owned scaffold regions and every pre-existing native packet byte."""
    planned, heads = plan(image, before, variable_info, requests)
    if not isinstance(loaded_image, (bytes, bytearray)):
        raise VoltageOffsetScaffoldError("Loaded scaffold image is unavailable")
    expected = bytearray(image)
    for patch in planned["patches"]:
        offset = patch["offset"]
        expected[offset:offset+len(bytes.fromhex(patch["after"]))] = bytes.fromhex(patch["after"])
    original, current = vb._Image(image), vb._Image(loaded_image)
    vfe, rail = original.pointer("P", 0x20), original.pointer("P", 0x68)
    if current.pointer("P", 0x20) != vfe or current.pointer("P", 0x68) != rail:
        raise VoltageOffsetScaffoldError("Scaffold table pointers changed")
    count = image[vfe+5] | image[vfe+10] << 8
    terms = struct.unpack_from("<H", image, vfe+12)[0]
    end = vfe+14+29*255+25*count+5*terms
    rail_end = rail+5+65*image[rail+3]
    current.own(vfe, end-vfe, 0x14)
    current.own(rail, rail_end-rail)
    if loaded_image[vfe:end] != expected[vfe:end] or loaded_image[rail:rail_end] != expected[rail:rail_end]:
        raise VoltageOffsetScaffoldError("Loaded VFE or rail scaffold differs from the plan")
    old_info, old_control, old_mask = eq._shape(before)
    new_info, new_control, new_mask = eq._shape(loaded)
    if before["identity"] != loaded["identity"]:
        raise VoltageOffsetScaffoldError("Loaded equation adapter changed")
    added = sum(1 << h["index"] for h in heads)
    if old_mask & added or new_mask != old_mask | added:
        raise VoltageOffsetScaffoldError("Loaded scaffold inventory differs")
    for old, new, stride in ((old_info, new_info, eq.INFO_STRIDE), (old_control, new_control, eq.CONTROL_STRIDE)):
        normalized = bytearray(new)
        normalized[4:4+eq.MASK_BYTES] = old[4:4+eq.MASK_BYTES]
        for head in heads:
            at = eq.HEADER + head["index"] * stride
            normalized[at:at+stride] = old[at:at+stride]
        if bytes(normalized) != old:
            raise VoltageOffsetScaffoldError("Pre-existing equation packet bytes changed")
    for head in heads:
        actual = eq.constant_row(loaded, head["index"])
        expected = {"index": head["index"], "value_uv": actual["value_uv"], "next_index": head["next_index"],
                    "variable": head["variable"], "output_type": 2}
        if actual != expected:
            raise VoltageOffsetScaffoldError("Loaded constant head differs")
        head["value_uv"] = actual["value_uv"]
    return planned, heads


def verify_activation(image, before, variable_info, requests, loaded_image, loaded):
    """Require the complete planned image immediately after activation."""
    planned, heads = verify_loaded(image, before, variable_info, requests, loaded_image, loaded)
    if hashlib.sha256(loaded_image).hexdigest() != planned["expected_sha256"]:
        raise VoltageOffsetScaffoldError("Activated image differs from the complete plan")
    return planned, heads


def live_contract(image, before, variable_info, requests, loaded_image, loaded, key, value_uv):
    """Build a signed constant-writer contract; caller verifies policy effects."""
    try:
        eq._uv(value_uv)
    except eq.VfeEquationError as exc:
        raise VoltageOffsetScaffoldError(str(exc)) from exc
    _, heads = verify_loaded(image, before, variable_info, requests, loaded_image, loaded)
    head = next((h for h in heads if h["key"] == key), None)
    if head is None:
        raise VoltageOffsetScaffoldError("Selected offset is not in the scaffold")
    selection = {key: {"index": head["index"], "before_uv": head["value_uv"], "after_uv": value_uv,
                       "next_index": head["next_index"], "variable": head["variable"], "output_type": 2}}
    return eq.contracts(loaded, selection)
