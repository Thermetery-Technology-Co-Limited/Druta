# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Array-B base/boost metadata with exact VPstate and clock readback.

The v10 loader replaces unsigned whole-MHz GPC frequencies in the selected
VPstate and its original copy. Selector17 follows when its original frequency
equals the selected record's original frequency. INFO exposes that predicate;
no board defaults, driver versions, or guesses from current clocks are used.
Only GET operations occur here. The transaction owner activates and restores.
"""

from __future__ import annotations

import copy
import ctypes
import math
import re
import struct
from collections.abc import Mapping

from . import clock_bios_runtime as clock
from . import factory_clock_runtime as factory
from .registry_flags import _same_state

_KEY = re.compile(r"factory\.([23])\.rated\Z")
_OFFSET_KEY = re.compile(r"factory\.[01]\.offset\Z")
INFO_COMMAND, INFO_SIZE, INFO_HEADER, INFO_STRIDE = 0x2080A0A4, 0x10744, 0x4C, 0x108
CONTROL_COMMAND, CONTROL_SIZE, CONTROL_HEADER, CONTROL_STRIDE = 0x2080A0A6, 0x87A0, 0x28, 0x88
QUERY_COMMAND, QUERY_SIZE = 0x2080A026, 0x214
_SELECTORS = {"base": (16, 0x800, 1), "peer": (17, 0x200, None), "boost": (18, 0x400, 2)}
_BINDING_KEYS = factory._BINDING_KEYS


class RatedClockError(RuntimeError):
    pass


def _mhz(value, *, inactive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or not float(value).is_integer():
        raise RatedClockError("Rated clock requires whole MHz")
    if not (0 if inactive else 1) <= value <= 65535:
        raise RatedClockError("Rated clock exceeds unsigned 16-bit MHz; use Restore to remove an override")
    return int(value)


def _get(gpu, command, payload):
    if (command, len(payload)) not in (
        (INFO_COMMAND, INFO_SIZE),
        (CONTROL_COMMAND, CONTROL_SIZE),
        (QUERY_COMMAND, QUERY_SIZE),
    ):
        raise RatedClockError("Only the understood VPstate GET packets are allowed")
    reader = clock.PerfDiagnosticsReader(gpu)
    bound = clock._binding(reader)
    header, fields = reader._transport()
    if any(header[i] != v for i, v in clock._ENVELOPE.items()):
        raise RatedClockError("VPstate GET requires the understood NVDA envelope")
    if any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values()):
        raise RatedClockError("VPstate transport fields are malformed")
    size = 68 + len(payload)
    storage, data = clock._guarded(size)
    packet = (ctypes.c_uint32 * (size // 4)).from_buffer(data)
    for i, word in enumerate(header):
        packet[i] = word
    packet[2], packet[14], packet[15], packet[16] = size, command, len(payload), 0
    packet[5] = packet[11] = 0
    expected = tuple(packet[:16])
    data[68:] = payload
    status = gpu._legacy_clk_escape(packet, dict(fields))
    clock._guards(storage)
    if clock._binding(reader) != bound:
        raise RatedClockError("VPstate target changed during GET")
    if type(status) is not int or status != 0 or packet[16] or packet[11] != 1 or packet[5] not in (0, 1):
        raise RatedClockError(f"VPstate GET failed: {status}/{packet[16]}/{packet[11]}")
    if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)):
        raise RatedClockError("VPstate GET changed its bound envelope or geometry")
    return bytes(data)[68:]


def _public(gpu, mode):
    from .nvbackend import _ClkFreqs

    getter = getattr(gpu.nvapi, "AllClocks", None)
    if getter is None or ctypes.sizeof(_ClkFreqs) != 264:
        raise RatedClockError("Public rated-clock GET is unavailable")
    reader = clock.PerfDiagnosticsReader(gpu)
    bound = clock._binding(reader)
    storage, data = clock._guarded(264)
    packet = _ClkFreqs.from_buffer(data)
    struct.pack_into("<II", data, 0, 0x20108, mode)
    status = getter(gpu.nvapi.gpu, ctypes.byref(packet))
    clock._guards(storage)
    if type(status) is not int or status != 0 or clock._binding(reader) != bound:
        raise RatedClockError("Public rated-clock GET failed or changed target")
    return bytes(data).hex()


def _inventory(state):
    cs = state["factory"]["clock"]
    rows = clock._decode(bytes.fromhex(cs["info_hex"]))
    masks = sorted(row["api_domain_mask"] for row in rows.values())
    if not masks or len(masks) > 32 or len(set(masks)) != len(masks) or any(not m or m & (m - 1) for m in masks):
        raise RatedClockError("Rated-clock API domain inventory is not unique")
    gpc = [int(i) for i, row in rows.items() if row["api_domain_mask"] == 1]
    if len(gpc) != 1:
        raise RatedClockError("Rated clocks require one bound GPC domain")
    return masks, gpc[0]


def _parts(state):
    info = clock._hex(state["info_hex"], INFO_SIZE)
    control = clock._hex(state["control_hex"], CONTROL_SIZE)
    mask = int.from_bytes(info[4:36], "little")
    count = struct.unpack_from("<I", info, 40)[0]
    if info[0] != 3 or info[36] != 0x30 or not mask or mask >> 255 or not 1 <= count <= 32:
        raise RatedClockError("VPstate INFO needs the understood type3/0x30 layout")
    if control[0] != 3 or control[4:36] != info[4:36] or control[36] != 0:
        raise RatedClockError("VPstate CONTROL inventory or copy selector differs")
    masks, domain = _inventory(state)
    if domain >= count:
        raise RatedClockError("GPC ordinal is outside the VPstate frequency array")
    indices = {name: info[44 + selector] for name, (selector, _, _) in _SELECTORS.items()}
    for index in indices.values():
        if index == 255 or not mask & (1 << index):
            raise RatedClockError("A required rated-clock selector has no VPstate")
        ip, cp = INFO_HEADER + index * INFO_STRIDE, CONTROL_HEADER + index * CONTROL_STRIDE
        if info[ip : ip + 2] != b"\x03\x03" or control[cp : cp + 2] != b"\x03\x03":
            raise RatedClockError("Selected VPstate is not a typed MHz record")
        # INFO is the original-copy inventory; CONTROL reads the active copy.
        # Both are written by array B. Require their current pairs to agree.
        for j in range(count):
            if info[ip + 6 + 8 * j : ip + 10 + 8 * j] != control[cp + 6 + 4 * j : cp + 10 + 4 * j]:
                raise RatedClockError("VPstate active/original frequency requests disagree")
    return info, control, masks, domain, indices


def _canonical(value):
    if not isinstance(value, dict) or set(value) != {"factory", "info_hex", "control_hex", "native", "public"}:
        raise RatedClockError("Malformed rated-clock observation")
    state = copy.deepcopy(value)
    state["factory"] = factory._canonical(value["factory"])
    info, _, masks, domain, indices = _parts(state)
    if not isinstance(state["native"], dict) or set(state["native"]) != set(_SELECTORS):
        raise RatedClockError("Complete base/boost/linked native queries are required")
    if not isinstance(state["public"], dict) or set(state["public"]) != {"base", "boost"}:
        raise RatedClockError("Complete public rated-clock queries are required")
    for name, (_, selector, mode) in _SELECTORS.items():
        raw = clock._hex(state["native"][name], QUERY_SIZE)
        if (
            struct.unpack_from("<I", raw)[0] != selector
            or raw[4] != indices[name]
            or struct.unpack_from("<I", raw, 16)[0] != len(masks)
        ):
            raise RatedClockError("Native rated selector, VPstate index or count differs")
        if not struct.unpack_from("<I", raw, 8)[0] & 0x10000000:
            raise RatedClockError("Native query lost its original-clock selector")
        if [struct.unpack_from("<I", raw, 20 + 16 * i)[0] for i in range(len(masks))] != masks:
            raise RatedClockError("Native rated query changed domain inventory")
        freq = struct.unpack_from("<I", raw, 28 + 16 * masks.index(1))[0]
        pos = INFO_HEADER + INFO_STRIDE * indices[name] + 6 + 8 * domain
        current, _, _, original_copy = struct.unpack_from("<HHHH", info, pos)
        if not current or current != original_copy or freq != current * 1000:
            raise RatedClockError("Rated queries and VPstate INFO frequency disagree")
        if mode is not None:
            public = clock._hex(state["public"][name], 264)
            version, flags, present, public_freq = struct.unpack_from("<IIII", public)
            if version != 0x20108 or flags & 15 != mode or not present & 1 or public_freq != freq:
                raise RatedClockError("Public rated clock disagrees with the native query")
    return state


def _shape(observation):
    if (
        not isinstance(observation, dict)
        or set(observation) != {"stable_reads", "state"}
        or type(observation["stable_reads"]) is not int
        or observation["stable_reads"] != 2
    ):
        raise RatedClockError("Rated clocks need two stable fresh reads")
    return _canonical(observation["state"])


def _once(gpu):
    observed, error = factory.read(gpu)
    if error:
        raise RatedClockError(error)
    preserved = factory._shape(observed)
    state = {"factory": preserved, "info_hex": _get(gpu, INFO_COMMAND, bytes(INFO_SIZE)).hex()}
    ctl = bytearray(CONTROL_SIZE)
    ctl[4:36] = bytes.fromhex(state["info_hex"])[4:36]
    state["control_hex"] = _get(gpu, CONTROL_COMMAND, ctl).hex()
    masks, _ = _inventory(state)
    state.update(native={}, public={})
    for name, (_, selector, mode) in _SELECTORS.items():
        packet = bytearray(QUERY_SIZE)
        struct.pack_into("<I", packet, 0, selector)
        struct.pack_into("<I", packet, 8, 0x10000000)
        struct.pack_into("<I", packet, 16, len(masks))
        for i, mask in enumerate(masks):
            struct.pack_into("<I", packet, 20 + 16 * i, mask)
        state["native"][name] = _get(gpu, QUERY_COMMAND, packet).hex()
        if mode is not None:
            state["public"][name] = _public(gpu, mode)
    if clock._binding(clock.PerfDiagnosticsReader(gpu)) != preserved["clock"]["identity"]:
        raise RatedClockError("Rated-clock target changed during combined GET")
    return _canonical(state)


def read(gpu):
    last = None
    for _ in range(2):
        try:
            with gpu._lock:
                first, second = _once(gpu), _once(gpu)
            if not _same_state(first, second):
                raise RatedClockError("Rated metadata or retained clock requests changed between reads")
            return {"stable_reads": 2, "state": second}, None
        except Exception as exc:
            last = str(exc)
    return None, last or "Rated-clock readback unavailable"


def _selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(k, str) for k in changes):
        raise RatedClockError("Rated changes need a field/value mapping")
    if any(k.startswith("factory.") and not (_KEY.fullmatch(k) or _OFFSET_KEY.fullmatch(k)) for k in changes):
        raise RatedClockError("Unrecognized factory clock field")
    return {k: v for k, v in changes.items() if _KEY.fullmatch(k)}


def _binding(value, record):
    if (
        not isinstance(value, dict)
        or set(value) != _BINDING_KEYS
        or type(value["schema"]) is not int
        or value["schema"] != 1
    ):
        raise RatedClockError("Rated clock lacks its exact BIOS binding")
    at = clock._integer(value["table_offset"], 0, 16 * 1024 * 1024 - 27, "Factory table offset is invalid")
    header = clock._hex(value["header_hex"], 7)
    if header[:6] != bytes.fromhex("100705020502") or header[6] not in (0, 1):
        raise RatedClockError("Factory table geometry is not understood")
    if (
        type(value["record_index"]) is not int
        or value["record_index"] != record
        or type(value["record_offset"]) is not int
        or value["record_offset"] != at + 7 + 5 * record
        or value["domain"] != "GPC"
    ):
        raise RatedClockError("Rated clock record/domain binding differs")
    cb = value["clock_binding"]
    clock._bios_binding(cb, cb.get("bios_index") if isinstance(cb, dict) else None)
    return copy.deepcopy(value)


def _fields(inspection, changes):
    fields = {f["key"]: f for f in (inspection or {}).get("fields", [])}
    result = {}
    for key in _selected(changes):
        field = fields.get(key, {})
        binding = _binding(field.get("factory_binding"), int(_KEY.fullmatch(key)[1]))
        if (
            field.get("capability") != "editable"
            or field.get("units") != "MHz"
            or field.get("encoding") != "I"
            or field.get("width") != 4
            or field.get("scale") != 1000
            or field.get("mask") is not None
            or field.get("offsets") != [binding["record_offset"] + 1]
            or field.get("factory_flag_offset") != binding["record_offset"]
            or field.get("factory_rated") is not True
            or type(field.get("factory_enabled")) is not bool
        ):
            raise RatedClockError("Rated field encoding differs from its BIOS record")
        old = _mhz(field.get("current"), inactive=True)
        if field["factory_enabled"] != bool(old):
            raise RatedClockError("Rated enable flag and stored frequency disagree")
        result[key] = (binding, old)
    return result


def _projection(before, binding, new):
    info, control, masks, domain, indices = _parts(before)
    cb = binding["clock_binding"]
    clock._selected_row(before["factory"]["clock"], cb["bios_index"], "GPC", cb)
    if cb["bios_index"] != domain:
        raise RatedClockError("Rated GPC ordinal disagrees with its BIOS domain")
    selected_name = "base" if binding["record_index"] == 2 else "boost"
    selected, peer = indices[selected_name], indices["peer"]

    def position(index):
        return INFO_HEADER + index * INFO_STRIDE + 6 + 8 * domain

    affected = {selected}
    # The predicate uses original word+4, never current frequency equality.
    if info[position(selected) + 4 : position(selected) + 6] == info[position(peer) + 4 : position(peer) + 6]:
        affected.add(peer)
    iout, cout = bytearray(info), bytearray(control)
    for index in affected:
        struct.pack_into("<H", iout, position(index), new)
        struct.pack_into("<H", iout, position(index) + 6, new)
        struct.pack_into("<H", cout, CONTROL_HEADER + index * CONTROL_STRIDE + 6 + 4 * domain, new)
    after = copy.deepcopy(before)
    after["info_hex"], after["control_hex"] = iout.hex(), cout.hex()
    for name, index in indices.items():
        if index not in affected:
            continue
        raw = bytearray.fromhex(after["native"][name])
        struct.pack_into("<I", raw, 28 + 16 * masks.index(1), new * 1000)
        after["native"][name] = raw.hex()
        if name in after["public"]:
            raw = bytearray.fromhex(after["public"][name])
            struct.pack_into("<I", raw, 12, new * 1000)
            after["public"][name] = raw.hex()
    return _canonical(after), sorted(affected)


def contracts(gpu, inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    if len(selected) != 1 or len(changes) != 1:
        raise RatedClockError("Apply one rated clock separately from other BIOS fields")
    observed, error = read(gpu)
    if error:
        raise RatedClockError(error)
    before = _shape(observed)
    key, wanted = next(iter(selected.items()))
    binding, old = _fields(inspection, selected)[key]
    new = _mhz(wanted)
    after, affected = _projection(before, binding, new)
    saved = {
        "schema": 1,
        "selected": {key: {"binding": binding, "before_mhz": old, "after_mhz": new, "indices": affected}},
        "before": before,
        "after": after,
    }
    validate_contracts(changes, saved)
    return saved


def validate_contracts(changes, saved):
    selected = _selected(changes)
    if not selected and saved == {}:
        return
    if (
        len(selected) != 1
        or len(changes) != 1
        or not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "before", "after"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or not isinstance(saved["selected"], dict)
        or set(saved["selected"]) != set(selected)
    ):
        raise RatedClockError("Rated changes and saved contracts disagree")
    key, wanted = next(iter(selected.items()))
    entry = saved["selected"][key]
    if not isinstance(entry, dict) or set(entry) != {"binding", "before_mhz", "after_mhz", "indices"}:
        raise RatedClockError("Malformed selected rated-clock contract")
    before, after = _canonical(saved["before"]), _canonical(saved["after"])
    binding = _binding(entry["binding"], int(_KEY.fullmatch(key)[1]))
    old, new = _mhz(entry["before_mhz"], inactive=True), _mhz(entry["after_mhz"])
    if type(entry["before_mhz"]) is not int or type(entry["after_mhz"]) is not int or new != _mhz(wanted):
        raise RatedClockError("Saved rated value differs from the request")
    if old:
        name = "base" if binding["record_index"] == 2 else "boost"
        if struct.unpack_from("<I", bytes.fromhex(before["public"][name]), 12)[0] != old * 1000:
            raise RatedClockError("Loaded rated clock differs from the enabled BIOS record")
    expected, indices = _projection(before, binding, new)
    if not _same_state(indices, entry["indices"]) or not _same_state(expected, after):
        raise RatedClockError("Rated contract changes an unselected field or omits a linked record")


def validate_inspection(inspection, changes, saved):
    bindings = _fields(inspection, changes)
    if not bindings and saved == {}:
        return
    validate_contracts(changes, saved)
    for key, (binding, old) in bindings.items():
        entry = saved["selected"][key]
        if not _same_state(binding, entry["binding"]) or old != entry["before_mhz"]:
            raise RatedClockError("Rated BIOS binding changed after preview")


def verify(observation, saved, *, restore=False):
    if saved == {}:
        return []
    try:
        changes = {key: entry["after_mhz"] for key, entry in saved["selected"].items()}
    except (KeyError, TypeError, AttributeError) as exc:
        raise RatedClockError("Malformed saved rated-clock contract") from exc
    validate_contracts(changes, saved)
    if not _same_state(_shape(observation), saved["before" if restore else "after"]):
        raise RatedClockError("Rated metadata or retained clock requests differ from expectation")
    return list(saved["selected"])
