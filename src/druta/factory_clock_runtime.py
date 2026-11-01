# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Factory clock metadata with exact preservation of user clock requests.

The v10 array-A loader sets the typed v50 factory delta. Core also propagates
to an XBAR domain whose enabled subentries contain type 1. Evaluated VF points
and P-state frequencies are derived outputs, not saved tuning requests.
Only guarded GET calls occur here; registry_overrides owns activation/undo.
"""

from __future__ import annotations

import copy
import ctypes
import math
import re
import struct
from collections.abc import Mapping

from . import clock_bios_runtime as clock
from .registry_flags import _same_state

_KEY = re.compile(r"factory\.([01])\.offset\Z")
_DOMAINS = ("GPC", "DRAM")
_BOOST_SIZE, _BOOST_VERSION = 9248, 0x12420
_BINDING_KEYS = {"schema", "table_offset", "header_hex", "record_index", "record_offset", "domain", "clock_binding"}


class FactoryClockError(RuntimeError):
    pass


def _mhz(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not float(value).is_integer():
        raise FactoryClockError("Factory offset requires whole MHz")
    if not -2147483648 <= value * 1000 <= 2147483647:
        raise FactoryClockError("Factory offset exceeds signed kHz encoding")
    return int(value)


def _boost_shape(value):
    if not isinstance(value, dict) or set(value) != {"n_entries", "payload_hex"}:
        raise FactoryClockError("Complete VF tuning requests are unavailable")
    count = clock._integer(value["n_entries"], 1, 128, "VF request inventory is invalid")
    raw = clock._hex(value["payload_hex"], _BOOST_SIZE)
    if struct.unpack_from("<I", raw)[0] != _BOOST_VERSION:
        raise FactoryClockError("VF tuning request layout is not understood")
    if int.from_bytes(raw[4:20], "little") != (1 << count) - 1:
        raise FactoryClockError("VF tuning requests changed their inventory")
    return copy.deepcopy(value)


def _canonical(value):
    if not isinstance(value, dict) or set(value) != {"clock", "boost"}:
        raise FactoryClockError("Malformed factory clock observation")
    return {"clock": clock._canonical(value["clock"]), "boost": _boost_shape(value["boost"])}


def _shape(observation):
    if not isinstance(observation, dict) or set(observation) != {"stable_reads", "state"}:
        raise FactoryClockError("Factory clock observation is unavailable")
    if type(observation["stable_reads"]) is not int or observation["stable_reads"] != 2:
        raise FactoryClockError("Factory clocks need two stable fresh reads")
    return _canonical(observation["state"])


def _boost_once(gpu):
    from .nvbackend import _BoostTable, _set_point_masks

    getter = getattr(gpu.nvapi, "BoostTableGet", None)
    layout = gpu.vfp_layout() if getter else None
    if not getter or layout is None or ctypes.sizeof(_BoostTable) != _BOOST_SIZE:
        raise FactoryClockError("Complete VF tuning request GET is unavailable")
    count = clock._integer(layout.n_entries, 1, 128, "VF request inventory is invalid")
    storage, data = clock._guarded(_BOOST_SIZE)
    packet = _BoostTable.from_buffer(data)
    packet.version = _BOOST_VERSION
    _set_point_masks(packet, count)
    status = getter(gpu.nvapi.gpu, ctypes.byref(packet))
    clock._guards(storage)
    if type(status) is not int or status != 0:
        raise FactoryClockError(f"VF tuning request GET failed: {status}")
    return _boost_shape({"n_entries": count, "payload_hex": bytes(data).hex()})


def read(gpu):
    """Bounded retries retain full INFO, user CONTROL and VF delta requests."""
    last = None
    for _ in range(2):
        try:
            with gpu._lock:
                samples = []
                for _ in range(2):
                    observed, error = clock.read(gpu)
                    if error:
                        raise FactoryClockError(error)
                    state = clock._shape(observed, writable=True)
                    boost = _boost_once(gpu)
                    if clock._binding(clock.PerfDiagnosticsReader(gpu)) != state["identity"]:
                        raise FactoryClockError("Factory clock target changed during GET")
                    samples.append({"clock": state, "boost": boost})
                if not _same_state(samples[0], samples[1]):
                    raise FactoryClockError("Factory clock metadata or user requests changed between reads")
                result = {"stable_reads": 2, "state": samples[1]}
                _shape(result)
                return result, None
        except Exception as exc:
            last = str(exc)
    return None, last or "Factory clock readback is unavailable"


def _binding(value, record):
    if not isinstance(value, dict) or set(value) != _BINDING_KEYS:
        raise FactoryClockError("Factory field lacks its exact BIOS binding")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise FactoryClockError("Factory BIOS binding schema is invalid")
    at = clock._integer(value["table_offset"], 0, 16 * 1024 * 1024 - 27, "Factory table offset is invalid")
    header = clock._hex(value["header_hex"], 7)
    if header[:6] != bytes.fromhex("100705020502") or header[6] not in (0, 1):
        raise FactoryClockError("Factory table geometry is not understood")
    if (
        type(value["record_index"]) is not int
        or value["record_index"] != record
        or type(value["record_offset"]) is not int
        or value["record_offset"] != at + 7 + 5 * record
        or value["domain"] != _DOMAINS[record]
    ):
        raise FactoryClockError("Factory record does not match its selected domain")
    cb = value["clock_binding"]
    clock._bios_binding(cb, cb.get("bios_index") if isinstance(cb, dict) else None)
    return copy.deepcopy(value)


def _targets(state, binding):
    """Follow API domain identity and the serialized XBAR eligibility data."""
    cs = state["clock"]
    cb = binding["clock_binding"]
    index = cb["bios_index"]
    clock._selected_row(cs, index, binding["domain"], cb)
    raw = bytes.fromhex(cs["info_hex"])
    indices = [index]
    if binding["record_index"] == 0:
        rows = clock._decode(raw)
        xbar = [int(i) for i, row in rows.items() if row["api_domain_mask"] == 2]
        if len(xbar) > 1:
            raise FactoryClockError("XBAR API domain is not unique")
        if xbar:
            pos = clock.HEADER + xbar[0] * clock.STRIDE
            if raw[pos] != 16 or raw[pos + 1] != 16:
                raise FactoryClockError("XBAR factory eligibility needs a typed programmable domain")
            mask = struct.unpack_from("<I", raw, pos + 0x3C)[0]
            if mask >> 7:
                raise FactoryClockError("XBAR factory eligibility exceeds the understood subentry inventory")
            if any(mask & (1 << i) and raw[pos + 0x40 + 24 * i] == 1 for i in range(7)):
                indices.append(xbar[0])
    for i in indices:
        pos = clock.HEADER + i * clock.STRIDE
        if raw[pos] != 16 or raw[pos + 1] != 16 or raw[pos + 0x2C] != 0:
            raise FactoryClockError("Selected factory delta is not typed signed kHz")
    return indices


def _selected(changes):
    if not isinstance(changes, Mapping) or any(not isinstance(k, str) for k in changes):
        raise FactoryClockError("Factory changes need a field/value mapping")
    if any(
        k.startswith("factory.") and _KEY.fullmatch(k) is None and re.fullmatch(r"factory\.[23]\.rated", k) is None
        for k in changes
    ):
        raise FactoryClockError("Only array-A effective factory offsets can be changed")
    return {k: v for k, v in changes.items() if _KEY.fullmatch(k)}


def _field_bindings(inspection, changes):
    fields = {f["key"]: f for f in (inspection or {}).get("fields", [])}
    result = {}
    for key in _selected(changes):
        record = int(_KEY.fullmatch(key)[1])
        field = fields.get(key, {})
        binding = _binding(field.get("factory_binding"), record)
        if (
            field.get("capability") != "editable"
            or field.get("units") != "MHz"
            or field.get("encoding") != "i"
            or field.get("width") != 4
            or field.get("scale") != 1000
            or field.get("mask") is not None
            or field.get("offsets") != [binding["record_offset"] + 1]
            or field.get("factory_flag_offset") != binding["record_offset"]
            or field.get("factory_effective") is not True
        ):
            raise FactoryClockError("Factory offset field or encoding differs from its BIOS record")
        result[key] = (binding, _mhz(field.get("current")))
    return result


def validate_inspection(inspection, changes, saved):
    """Bind a saved contract to the newly read BIOS, before any mutation."""
    bindings = _field_bindings(inspection, changes)
    if not bindings and saved == {}:
        return
    validate_contracts(changes, saved)
    for key, (binding, current) in bindings.items():
        entry = saved["selected"][key]
        if not _same_state(binding, entry["binding"]) or current != entry["before_mhz"]:
            raise FactoryClockError("Factory BIOS binding changed since preview")


def contracts(gpu, inspection, changes):
    selected = _selected(changes)
    if not selected:
        return {}
    if len(selected) != len(changes):
        raise FactoryClockError("Apply factory offsets separately from other BIOS fields")
    observation, error = read(gpu)
    if error:
        raise FactoryClockError(error)
    before = _shape(observation)
    bindings = _field_bindings(inspection, selected)
    raw = bytearray.fromhex(before["clock"]["info_hex"])
    entries = {}
    for key, wanted in selected.items():
        binding, old = bindings[key]
        indices = _targets(before, binding)
        entries[key] = {"binding": binding, "indices": indices, "before_mhz": old, "after_mhz": _mhz(wanted)}
        for i in indices:
            pos = clock.HEADER + i * clock.STRIDE + 0x30
            if struct.unpack_from("<i", raw, pos)[0] != old * 1000:
                raise FactoryClockError("Loaded factory delta differs from the selected BIOS record")
            struct.pack_into("<i", raw, pos, _mhz(wanted) * 1000)
    after = copy.deepcopy(before)
    after["clock"]["info_hex"] = raw.hex()
    saved = {"schema": 1, "selected": entries, "before": before, "after": after}
    validate_contracts(changes, saved)
    return saved


def validate_contracts(changes, saved):
    selected = _selected(changes)
    if not selected and saved == {}:
        return
    if (
        not selected
        or len(selected) != len(changes)
        or not isinstance(saved, dict)
        or set(saved) != {"schema", "selected", "before", "after"}
        or type(saved["schema"]) is not int
        or saved["schema"] != 1
        or not isinstance(saved["selected"], dict)
        or set(saved["selected"]) != set(selected)
    ):
        raise FactoryClockError("Factory changes and saved contracts disagree")
    before, after = _canonical(saved["before"]), _canonical(saved["after"])
    raw = bytearray.fromhex(before["clock"]["info_hex"])
    used, table = set(), None
    for key, wanted in selected.items():
        record = int(_KEY.fullmatch(key)[1])
        entry = saved["selected"][key]
        if not isinstance(entry, dict) or set(entry) != {"binding", "indices", "before_mhz", "after_mhz"}:
            raise FactoryClockError("Malformed selected factory contract")
        binding = _binding(entry["binding"], record)
        current_table = (binding["table_offset"], binding["header_hex"])
        if table is not None and table != current_table:
            raise FactoryClockError("Factory records disagree about their BIOS table")
        table = current_table
        indices = _targets(before, binding)
        if not _same_state(indices, entry["indices"]) or used.intersection(indices):
            raise FactoryClockError("Saved factory targets omit, duplicate or change an affected domain")
        used.update(indices)
        old, new = _mhz(entry["before_mhz"]), _mhz(entry["after_mhz"])
        if type(entry["before_mhz"]) is not int or type(entry["after_mhz"]) is not int or new != _mhz(wanted):
            raise FactoryClockError("Saved factory values disagree with the requested change")
        for i in indices:
            pos = clock.HEADER + i * clock.STRIDE + 0x30
            if struct.unpack_from("<i", raw, pos)[0] != old * 1000:
                raise FactoryClockError("Saved factory baseline disagrees with the raw INFO delta")
            struct.pack_into("<i", raw, pos, new * 1000)
    expected = copy.deepcopy(before)
    expected["clock"]["info_hex"] = raw.hex()
    if not _same_state(after, expected):
        raise FactoryClockError("Factory contract changes an unselected INFO field or a user tuning request")


def verify(observation, saved, *, restore=False):
    if saved == {}:
        return []
    try:
        changes = {key: entry["after_mhz"] for key, entry in saved["selected"].items()}
    except (KeyError, TypeError, AttributeError) as exc:
        raise FactoryClockError("Malformed saved factory clock contract") from exc
    validate_contracts(changes, saved)
    if not _same_state(_shape(observation), saved["before" if restore else "after"]):
        raise FactoryClockError("Factory clock INFO or retained user tuning requests differ from expectation")
    return list(saved["selected"])
