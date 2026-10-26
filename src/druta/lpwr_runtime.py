# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Loaded Pstate/DIFR configuration and disable-only BIOS contracts.

The transaction owner writes the BIOS override and restarts the selected GPU.
This module only reads the understood native INFO/attribute paths. Configured
support and Pstate masks are not physical residency or power measurements.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import re
import struct
from collections.abc import Mapping

from . import clock_bios_runtime as clock
from . import vbios_fields
from .clock_vf_registry import _identity
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

KEY = re.compile(r"lpwr\.difr\.(0|[1-9][0-9]*)\.enabled\Z")
CONTROLLERS = {9: "DFPR", 10: "MS DIFR SW-ASR", 11: "MS DIFR CG"}
PSTATE_COMMAND, PSTATE_SIZE = 0x2080A0A7, 0x450C
ATTRIBUTE_COMMAND, ATTRIBUTE_SIZE = 0x2080A801, 0x404
SUPPORT, STATES = 0x39, 0x36
_HEX256 = re.compile(r"[0-9a-f]{64}\Z")
_OBS_KEYS = {
    "schema",
    "stable_reads",
    "identity",
    "pstate_info_hex",
    "pstates",
    "controllers",
    "support_hex",
    "states_hex",
}


class LpwrError(RuntimeError):
    """An understood configuration or exact disable projection is unavailable."""


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise LpwrError("LPWR metadata needs an integer in its encoded range")
    return value


def _hex(value, size):
    if not isinstance(value, str) or len(value) != size * 2:
        raise LpwrError("LPWR bytes have an unexpected length")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise LpwrError("LPWR bytes are not hexadecimal") from exc
    if raw.hex() != value:
        raise LpwrError("LPWR bytes are not canonical hexadecimal")
    return raw


def selected(changes):
    """Select only this category; no selected keys requires no capability GET."""
    if not isinstance(changes, Mapping) or any(not isinstance(key, str) for key in changes):
        raise LpwrError("DIFR changes need a field/value mapping")
    result = {}
    for key, value in changes.items():
        if not key.startswith("lpwr.difr."):
            continue
        match = KEY.fullmatch(key)
        if not match or len(match[1]) > 1 or not 0 <= int(match[1]) < 5:
            raise LpwrError("DIFR field index is outside the understood loaded rows")
        if value is not False:
            raise LpwrError("DIFR edits may only disable a captured enabled entry")
        result[key] = value
    if result and len(result) != len(changes):
        raise LpwrError("Apply DIFR disables separately from other BIOS controls")
    return result


def _pstates(raw):
    if len(raw) != PSTATE_SIZE or raw[0] == 0xA5 or raw[8] in (0, 0xA5):
        raise LpwrError("Loaded Pstate INFO is unavailable or its layout is uninitialized")
    mask = struct.unpack_from("<I", raw, 4)[0]
    if not mask or mask == 0xA5A5A5A5:
        raise LpwrError("Loaded Pstate inventory was not returned")
    rows = []
    for index in range(32):
        if not mask >> index & 1:
            continue
        row = raw[12 + 552 * index : 12 + 552 * (index + 1)]
        pstate = struct.unpack_from("<I", row, 4)[0]
        if row[0] == 0xA5 or row[0] != row[14] or pstate in (0, 0xA5A5A5A5):
            raise LpwrError("Loaded Pstate type or identifier was not returned consistently")
        rows.append(
            {
                "index": index,
                "type": row[0],
                "pstate_id_mask": pstate,
                "level": row[12],
                "lpwr_index": row[13],
            }
        )
    return {"group": raw[0], "version": raw[8], "mask": mask, "rows": rows}


def _request(attribute, indices):
    raw = bytearray(ATTRIBUTE_SIZE)
    struct.pack_into("<I", raw, 0, len(indices))
    for number, index in enumerate(indices):
        struct.pack_into("<HHH", raw, 4 + 16 * number, 3, index, attribute)
    return bytes(raw)


def _attributes(raw, attribute, indices):
    before = _request(attribute, indices)
    if len(raw) != ATTRIBUTE_SIZE or not indices:
        raise LpwrError("DIFR attribute packet geometry is not understood")
    untouched, result = bytearray(raw), {}
    for number, index in enumerate(indices):
        at = 4 + 16 * number
        flags, value = struct.unpack_from("<HI", raw, at + 6)
        if flags not in (0, 1) or (flags and attribute == SUPPORT and value not in (0, 1)):
            raise LpwrError("DIFR availability or support reply is malformed")
        untouched[at + 6 : at + 12] = before[at + 6 : at + 12]
        result[str(index)] = (bool(flags), value if flags else None)
    if bytes(untouched) != before:
        raise LpwrError("DIFR attribute reply changed routing or opaque bytes")
    return result


def _controllers(support_raw, states_raw):
    supports = _attributes(support_raw, SUPPORT, tuple(CONTROLLERS))
    indices = tuple(i for i in CONTROLLERS if supports[str(i)] == (True, 1))
    if states_raw is not None and not indices:
        raise LpwrError("DIFR state reply has no independently supported controller")
    states = _attributes(states_raw, STATES, indices) if states_raw is not None else {}
    result = {}
    for index in CONTROLLERS:
        available, value = supports[str(index)]
        state_available, mask = states.get(str(index), (False, None))
        result[str(index)] = {
            "available": available,
            "supported": bool(value) if available else None,
            "states_available": state_available,
            "configured_pstate_mask": mask,
        }
    return result


def _observation(value):
    if not isinstance(value, dict) or set(value) != _OBS_KEYS:
        raise LpwrError("Malformed LPWR observation")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise LpwrError("Unknown LPWR observation schema")
    if type(value["stable_reads"]) is not int or value["stable_reads"] != 2:
        raise LpwrError("Two fresh stable LPWR captures are required")
    identity = _identity(value["identity"])
    pstates = _pstates(_hex(value["pstate_info_hex"], PSTATE_SIZE))
    support = _hex(value["support_hex"], ATTRIBUTE_SIZE)
    states = None if value["states_hex"] is None else _hex(value["states_hex"], ATTRIBUTE_SIZE)
    controllers = _controllers(support, states)
    if not _same_state(pstates, value["pstates"]) or not _same_state(controllers, value["controllers"]):
        raise LpwrError("Typed LPWR observation differs from its reply bytes")
    return {"identity": identity, "pstates": pstates, "controllers": controllers}


def _get(reader, identity, command, size, request=None):
    if clock._binding(reader) != identity:
        raise PerfDiagnosticError("identity", "LPWR target changed before GET")
    header, fields = reader._transport()
    if (
        len(header) != 17
        or any(header[i] != value for i, value in clock._ENVELOPE.items())
        or any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in fields.values())
    ):
        raise PerfDiagnosticError("malformed", "LPWR requires the understood paired NVDA transport")
    storage, buffer = clock._guarded(68 + size)
    packet = (ctypes.c_uint32 * ((68 + size) // 4)).from_buffer(buffer)
    for i, value in enumerate(header):
        packet[i] = value
    packet[2], packet[14], packet[15], packet[16] = 68 + size, command, size, 0
    packet[5] = packet[11] = 0
    if request is not None:
        ctypes.memmove(ctypes.addressof(buffer) + 68, request, size)
    expected = tuple(packet[:16])
    try:
        status = reader.gpu._legacy_clk_escape(packet, dict(fields))
    except Exception as exc:
        raise PerfDiagnosticError("transient", f"LPWR GET transport failed: {exc}") from exc
    clock._guards(storage)
    if clock._binding(reader) != identity:
        raise PerfDiagnosticError("identity", "LPWR target changed during GET")
    if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
        raise PerfDiagnosticError("malformed", "LPWR GET changed its envelope or geometry")
    if type(status) is not int or status != 0 or packet[11] != 1:
        raise PerfDiagnosticError("transient", f"LPWR transport failed: NT={status}, completion={packet[11]}")
    if packet[16]:
        code = int(packet[16])
        raise PerfDiagnosticError(
            "unsupported" if code in (0x1F, 0x56) else "transient", f"LPWR RM status 0x{code:X}", code
        )
    return bytes(buffer)[68:]


def _once(reader, identity):
    info = _get(reader, identity, PSTATE_COMMAND, PSTATE_SIZE)
    _pstates(info)
    support = _get(reader, identity, ATTRIBUTE_COMMAND, ATTRIBUTE_SIZE, _request(SUPPORT, tuple(CONTROLLERS)))
    values = _attributes(support, SUPPORT, tuple(CONTROLLERS))
    indices = tuple(i for i in CONTROLLERS if values[str(i)] == (True, 1))
    states = None
    if indices:
        try:
            states = _get(reader, identity, ATTRIBUTE_COMMAND, ATTRIBUTE_SIZE, _request(STATES, indices))
        except PerfDiagnosticError as exc:
            if exc.kind != "unsupported":
                raise
    result = {
        "schema": 1,
        "stable_reads": 2,
        "identity": identity,
        "pstate_info_hex": info.hex(),
        "pstates": _pstates(info),
        "support_hex": support.hex(),
        "states_hex": states.hex() if states is not None else None,
        "controllers": _controllers(support, states),
    }
    _observation(result)
    return result


def read(gpu):
    """Read loaded configuration. False support remains an available value."""
    reader = PerfDiagnosticsReader(gpu)
    try:
        with gpu._lock:
            identity = clock._binding(reader)
            for attempt in range(2):
                try:
                    first, second = _once(reader, identity), _once(reader, identity)
                    if not _same_state(_observation(first), _observation(second)):
                        raise PerfDiagnosticError(
                            "unstable", "LPWR routes or controller configuration changed between reads"
                        )
                    return second, None
                except PerfDiagnosticError as exc:
                    if exc.kind != "transient" or attempt == 1:
                        raise
                    gpu._current_limit_transport = None
                    gpu._current_limit_layout_choice = None
    except Exception as exc:
        return None, str(exc)
    return None, "LPWR configuration is unavailable"


def _tables(image):
    parsed = vbios_fields._Image(image)
    result = {}
    for name, pointer, owner, hs in (("index", 0x74, 0x24, 6), ("difr", 0xE8, 0x8E, 11)):
        table = parsed.table("lpwr_" + name, "P", pointer, ((0x10, hs, 18),), owner)
        if table["object_id"] != owner or not 0 < table["count"] < 6:
            raise LpwrError("LPWR table lacks the understood bounded object ownership")
        result[name] = {
            "offset": table["offset"],
            "owner": owner,
            "raw_hex": parsed.span(table["offset"], table["size"]).hex(),
        }
    at = parsed.pointer("P", 0)
    header = parsed.span(at, 14)
    if header[0:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise LpwrError("DIFR projection requires the populated PERF v70 aggregation path")
    stride = header[2] + header[4] * header[5]
    size = 14 + stride * header[3]
    if parsed.own(at, size, 0x0E) != 0x0E:
        raise LpwrError("PERF table lacks its bounded object owner")
    raw = parsed.span(at, size)
    result["perf"] = {
        "offset": at,
        "owner": 0x0E,
        "header_hex": header.hex(),
        "row_first_hex": bytes(raw[14 + i * stride] for i in range(header[3])).hex(),
        "table_sha256": hashlib.sha256(raw).hexdigest(),
    }
    _table_data(result)
    return result


def _table_data(tables):
    if not isinstance(tables, dict) or set(tables) != {"index", "difr", "perf"}:
        raise LpwrError("Missing bounded LPWR model tables")
    decoded, spans = {}, []
    for name, owner, hs in (("index", 0x24, 6), ("difr", 0x8E, 11)):
        value = tables[name]
        if not isinstance(value, dict) or set(value) != {"offset", "owner", "raw_hex"}:
            raise LpwrError("Malformed LPWR table evidence")
        at = _int(value["offset"], 0, 16 * 1024 * 1024)
        if type(value["owner"]) is not int or value["owner"] != owner:
            raise LpwrError("LPWR table owner differs")
        text = value["raw_hex"]
        if not isinstance(text, str) or not 2 * (hs + 18) <= len(text) <= 2 * (hs + 5 * 18):
            raise LpwrError("LPWR table extent is invalid")
        raw = _hex(text, len(text) // 2)
        if raw[:3] != bytes((0x10, hs, 18)) or not 0 < raw[3] < 6 or len(raw) != hs + 18 * raw[3]:
            raise LpwrError("LPWR table geometry differs")
        decoded[name] = raw
        spans.append((at, at + len(raw)))
    perf = tables["perf"]
    if not isinstance(perf, dict) or set(perf) != {"offset", "owner", "header_hex", "row_first_hex", "table_sha256"}:
        raise LpwrError("Malformed PERF aggregation evidence")
    at = _int(perf["offset"], 0, 16 * 1024 * 1024)
    if type(perf["owner"]) is not int or perf["owner"] != 0x0E:
        raise LpwrError("PERF table owner differs")
    header = _hex(perf["header_hex"], 14)
    if header[:2] != b"\x70\x0e" or not header[2] or not header[3]:
        raise LpwrError("PERF v70 composite geometry is not understood")
    first = _hex(perf["row_first_hex"], header[3])
    if all(value == 255 for value in first):
        raise LpwrError("PERF is not on the populated aggregation branch")
    if not isinstance(perf["table_sha256"], str) or not _HEX256.fullmatch(perf["table_sha256"]):
        raise LpwrError("Missing exact PERF table fingerprint")
    spans.append((at, at + 14 + (header[2] + header[4] * header[5]) * header[3]))
    spans.sort()
    if any(end > 16 * 1024 * 1024 for _, end in spans) or any(
        a[1] > b[0] for a, b in zip(spans, spans[1:], strict=False)
    ):
        raise LpwrError("LPWR model table spans overlap or exceed the bounded image")
    return decoded


def _model(tables, pstates, disable=()):
    data = _table_data(tables)
    index, difr = data["index"], data["difr"]
    flags = [difr[11 + 18 * i] if i not in disable else difr[11 + 18 * i] & ~1 for i in range(difr[3])]
    masks = {str(i): 0 for i in CONTROLLERS}
    for row in pstates["rows"]:
        selector = row["lpwr_index"]
        if selector >= 6:
            slot = 5
        else:
            if selector >= index[3]:
                raise LpwrError("A valid LPWR selector addresses an unloaded index row")
            slot = index[6 + 18 * selector + 10]
            slot = slot if slot < 6 else 5
        if slot != 5 and slot >= len(flags):
            # Unloaded DIFR slots retain the traced zero construction flags.
            continue
        value = flags[slot] if slot < len(flags) else 0
        if value & 1:
            for controller, bit in zip(CONTROLLERS, (2, 4, 8), strict=True):
                if value & bit:
                    masks[str(controller)] |= row["pstate_id_mask"]
    result = {}
    for controller, bit in zip(CONTROLLERS, (2, 4, 8), strict=True):
        supported = any(value & 1 and value & bit for value in flags)
        result[str(controller)] = {
            "available": True,
            "supported": supported,
            "states_available": supported,
            "configured_pstate_mask": masks[str(controller)] if supported else None,
        }
    return result


def _derive(changes, before, tables):
    requested = selected(changes)
    core = _observation(before)
    data = _table_data(tables)
    if not _same_state(core["controllers"], _model(tables, core["pstates"])):
        raise LpwrError("Loaded DIFR support or configured masks do not match the bounded BIOS model")
    bindings, disabled = {}, []
    for key in requested:
        index = int(KEY.fullmatch(key)[1])
        if index >= data["difr"][3]:
            raise LpwrError("Selected DIFR entry is absent")
        offset = 11 + 18 * index
        value = data["difr"][offset]
        if not value & 1:
            raise LpwrError("Selected DIFR entry is already disabled")
        bindings[key] = {
            "index": index,
            "offset": tables["difr"]["offset"] + offset,
            "before_byte": value,
            "after_byte": value & ~1,
        }
        disabled.append(index)
    return bindings, _model(tables, core["pstates"], disabled)


def contracts(gpu, image_bytes, changes):
    """Capture a disable projection; never enable, write, or restart hardware."""
    if not selected(changes):
        return {}
    tables = _tables(image_bytes)
    before, error = read(gpu)
    if error or before is None:
        raise LpwrError("DIFR configuration is unavailable: " + str(error))
    bindings, after = _derive(changes, before, tables)
    return {
        "schema": 1,
        "kind": "lpwr_difr_disable",
        "before_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "before": copy.deepcopy(before),
        "tables": tables,
        "selected": bindings,
        "after": after,
    }


def validate_contracts(changes, saved):
    """Pure validation for apply and recovery, including exact selected binding."""
    requested = selected(changes)
    if not requested and saved == {}:
        return
    if (
        not requested
        or not isinstance(saved, dict)
        or set(saved)
        != {
            "schema",
            "kind",
            "before_image_sha256",
            "before",
            "tables",
            "selected",
            "after",
        }
    ):
        raise LpwrError("Missing or malformed DIFR disable contract")
    if type(saved["schema"]) is not int or saved["schema"] != 1 or saved["kind"] != "lpwr_difr_disable":
        raise LpwrError("Unknown DIFR disable contract")
    digest = saved["before_image_sha256"]
    if not isinstance(digest, str) or not _HEX256.fullmatch(digest):
        raise LpwrError("Invalid DIFR before-image binding")
    bindings, after = _derive(requested, saved["before"], saved["tables"])
    if not _same_state(bindings, saved["selected"]) or not _same_state(after, saved["after"]):
        raise LpwrError("Saved DIFR projection differs from selected fields or BIOS model")


def verify(observation, saved, restore=False):
    """Verify loaded routes/support/masks; preserve raw frequency data diagnostically."""
    if saved == {}:
        return []
    if not isinstance(saved, dict) or not isinstance(saved.get("selected"), dict):
        raise LpwrError("Malformed DIFR disable contract")
    changes = dict.fromkeys(saved["selected"], False)
    validate_contracts(changes, saved)
    before, actual = _observation(saved["before"]), _observation(observation)
    if not _same_state(actual["identity"], before["identity"]) or not _same_state(actual["pstates"], before["pstates"]):
        raise LpwrError("Loaded DIFR target or Pstate routing changed")
    expected = before["controllers"] if restore else saved["after"]
    if not _same_state(actual["controllers"], expected):
        raise LpwrError("Loaded DIFR support or configured masks differ from the expected state")
    return sorted(changes)
