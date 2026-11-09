# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pure power-arbitration decoder of the native core (drutacore/power_arbitration.h).

``decode(layout, info, control, status)`` returns what ``power_arbitration.decode`` returns for
the same arguments, as a dict of the same shape and key order, so the two compare equal with
``==``. ``geometry(layout)`` is ``power_arbitration._geometry`` returning its refusal (or None)
instead of raising, and ``row_formats()`` is ``power_arbitration.ROW_FORMATS``. This is a parallel
implementation for the parity tests; the Python module stays the one the backend uses.

Read-only: nothing here touches a GPU. Addresses in the native docstrings are documentation,
never used to locate data.

The C ABI takes fixed unsigned 32-bit fields, so the checks that only exist because Python is
dynamically typed stay here: a layout (or its "sizes") that is not a mapping, a geometry item, a
size or a status mask word that is not an exact int (a bool or a float is refused), a "str"
size key, a "commands" value that is not a mapping, and a packet element that is not an unsigned
32-bit int. Each is mapped onto an input the native check refuses with the same reason at the
same point of the same check order, so the reason text always comes from the core.
"""
from __future__ import annotations

import ctypes
import threading
from collections.abc import Mapping, Sequence

from . import native
from .native import DC_OK, DC_E_UNRECOGNIZED, NativeUnavailable
from .native_power_layouts import POWER_GET_COMMANDS, _PowerLayout

DC_ARBITRATION_POLICIES_MAX = 32
DC_ARBITRATION_INPUTS_MAX = 8
DC_ARBITRATION_OBSERVATIONS_MAX = 6
DC_ARBITRATION_TEXT = 128
DC_ARBITRATION_ROW_FORMATS_MAX = 8
_UINT32_MAX = (1 << 32) - 1
# The three GETs the decoder checks, as (layout key, command) in power_arbitration._GEOMETRY order.
_PAIRS = (("info", 0x2080A618), ("status", 0x2080A619), ("control", 0x2080A61A))
_CHECKED = tuple(command for _, command in _PAIRS)


class _Words(ctypes.Structure):  # dc_arbitration_words
    _fields_ = [("words", ctypes.POINTER(ctypes.c_uint32)), ("count", ctypes.c_uint32),
                ("present", ctypes.c_int32), ("valid", ctypes.c_int32)]


class _Input(ctypes.Structure):  # dc_arbitration_input
    _fields_ = [("client", ctypes.c_uint32), ("value", ctypes.c_uint32)]


class _Vector(ctypes.Structure):  # dc_arbitration_vector
    _fields_ = [("available", ctypes.c_int32), ("reason", ctypes.c_char * DC_ARBITRATION_TEXT),
                ("method", ctypes.c_char * 16), ("method_code", ctypes.c_uint32), ("count", ctypes.c_uint32),
                ("output", ctypes.c_uint32), ("initial_value", ctypes.c_uint32),
                ("inputs", _Input * DC_ARBITRATION_INPUTS_MAX), ("has_computed_output", ctypes.c_int32),
                ("computed_output", ctypes.c_uint32), ("consistent", ctypes.c_int32)]


class _Row(ctypes.Structure):  # dc_arbitration_row
    _fields_ = [("policy", ctypes.c_uint32), ("type", ctypes.c_uint32), ("channel", ctypes.c_uint32),
                ("has_unit", ctypes.c_int32), ("unit", ctypes.c_char * 4), ("unit_code", ctypes.c_uint32),
                ("available", ctypes.c_int32), ("reason", ctypes.c_char * DC_ARBITRATION_TEXT),
                ("requested", ctypes.c_uint32), ("effective", ctypes.c_uint32), ("value", ctypes.c_uint32),
                ("delta", ctypes.c_int32), ("host", _Vector), ("pmu", _Vector),
                ("observation_count", ctypes.c_uint32),
                ("observations", (ctypes.c_char * DC_ARBITRATION_TEXT) * DC_ARBITRATION_OBSERVATIONS_MAX)]


class _Report(ctypes.Structure):  # dc_arbitration_report
    _fields_ = [("size", ctypes.c_uint32), ("schema", ctypes.c_uint32), ("available", ctypes.c_int32),
                ("reason", ctypes.c_char * DC_ARBITRATION_TEXT), ("has_mask", ctypes.c_int32),
                ("mask", ctypes.c_uint32), ("policy_count", ctypes.c_uint32),
                ("policies", _Row * DC_ARBITRATION_POLICIES_MAX)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_power_arbitration_decode": (ctypes.c_int32, [_P(_PowerLayout), _P(_Words), _P(_Words), _P(_Words),
                                                     _P(_Report)]),
    "dc_power_arbitration_geometry": (ctypes.c_int32, [_P(_PowerLayout), ctypes.c_char_p, ctypes.c_uint32]),
    "dc_power_arbitration_vector": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32,
                                                     _P(_Vector)]),
    "dc_power_arbitration_row_formats": (ctypes.c_int32, [_P(ctypes.c_uint32), ctypes.c_uint32,
                                                          _P(ctypes.c_uint32)]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the decoder bound; raises NativeUnavailable."""
    global _bound
    library = native.load()
    with _lock:
        if _bound is library:
            return library
        try:
            for name, (restype, argtypes) in _SIGNATURES.items():
                function = getattr(library, name)
                function.restype = restype
                function.argtypes = argtypes
        except AttributeError as exc:
            raise NativeUnavailable(f"native core has no power-arbitration decoder: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


def _exact_u32(value):
    # Python's ``type(x) is int`` (no bool, no float) plus the range the C field can carry.
    return type(value) is int and 0 <= value <= _UINT32_MAX


def _layout_struct(layout):
    """The layout as dc_power_layout, or None where _geometry refuses it as unavailable.

    Dynamic checks (kept here, see the module docstring): a value the C field cannot carry
    exactly becomes 0, which no understood geometry, size, command or mask word equals, so the
    core refuses it with Python's reason at the same step.
    """
    if not isinstance(layout, Mapping) or not isinstance(layout.get("sizes"), Mapping):
        return None
    row = _PowerLayout()
    row.size = ctypes.sizeof(_PowerLayout)
    sizes = layout["sizes"]
    for index, command in enumerate(POWER_GET_COMMANDS):
        # JSON receipts use string keys; the live backend uses integer keys.
        value = sizes.get(command, sizes.get(str(command)))
        row.sizes[index] = value if _exact_u32(value) else 0
    for name in ("info", "status", "control"):
        actual = layout.get(name)
        pair = (0, 0)
        if (isinstance(actual, (tuple, list)) and len(actual) == 2
                and all(_exact_u32(item) for item in actual)):
            pair = tuple(actual)
        setattr(row, f"{name}_base", pair[0])
        setattr(row, f"{name}_stride", pair[1])
    mask_word = layout.get("status_mask_word", 1)
    row.has_status_mask_word = 1
    row.status_mask_word = mask_word if _exact_u32(mask_word) else 0
    commands = layout.get("commands", {})
    row.has_commands = 1
    if isinstance(commands, Mapping):
        for index, command in enumerate(POWER_GET_COMMANDS):
            row.commands[index] = command
        # Python's any() stops at the first mismatch; so does this, so a value whose != raises
        # raises at the same point.
        for index, command in enumerate(POWER_GET_COMMANDS):
            if command not in _CHECKED:
                continue
            if commands.get(command, commands.get(str(command), command)) != command:
                row.commands[index] = 0
                break
    return row


def _words(value, size):
    """dc_arbitration_words for a packet; ``keep`` holds the buffer alive for the call."""
    words = _Words()
    if value is None:
        return words, None
    words.present = 1
    words.valid = 1
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray, memoryview))
            or len(value) != size // 4):
        return words, None  # count 0: the core's length refusal
    if not all(_exact_u32(word) for word in value):
        words.valid = 0  # the core's value refusal, after its length check
        words.count = len(value)
        buffer = (ctypes.c_uint32 * len(value))()
    else:
        buffer = (ctypes.c_uint32 * len(value))(*value)
        words.count = len(value)
    words.words = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32))
    return words, buffer


def _text(field):
    return field.decode("utf-8") or None


def _vector(v):
    if not v.available:
        return {"available": False, "reason": _text(v.reason)}
    return {
        "available": True,
        "reason": None,
        "method": v.method.decode("utf-8"),
        "method_code": v.method_code,
        "count": v.count,
        "output": v.output,
        "initial_value": v.initial_value,
        "inputs": [{"client": item.client, "value": item.value} for item in v.inputs[:v.count]],
        "computed_output": v.computed_output if v.has_computed_output else None,
        "consistent": None if v.consistent < 0 else bool(v.consistent),
    }


def _row(r):
    row = {
        "policy": r.policy,
        "type": r.type,
        "channel": r.channel,
        "unit": r.unit.decode("utf-8") if r.has_unit else None,
        "unit_code": r.unit_code,
    }
    if not r.available:
        return {**row, "available": False, "reason": _text(r.reason), "host": _vector(r.host),
                "pmu": _vector(r.pmu), "observations": []}
    return {
        **row,
        "available": True,
        "reason": None,
        "requested": r.requested,
        "effective": r.effective,
        "value": r.value,
        "delta": r.delta,
        "host": _vector(r.host),
        "pmu": _vector(r.pmu),
        "observations": [r.observations[i].value.decode("utf-8") for i in range(r.observation_count)],
    }


def decode(layout, info, control, status):
    """power_arbitration.decode through the native core."""
    library = load()
    row = _layout_struct(layout)
    keep = []
    packets = []
    for value, size in ((info, 20512), (control, 13876), (status, 397048)):
        words, buffer = _words(value, size)
        keep.append(buffer)
        packets.append(words)
    report = _Report()
    report.size = ctypes.sizeof(_Report)
    code = library.dc_power_arbitration_decode(ctypes.byref(row) if row is not None else None,
                                               *(ctypes.byref(w) for w in packets), ctypes.byref(report))
    if code != DC_OK or report.policy_count > DC_ARBITRATION_POLICIES_MAX:
        raise RuntimeError(f"dc_power_arbitration_decode: status {code}")
    result = {"schema": report.schema, "available": bool(report.available), "reason": _text(report.reason),
              "policies": [_row(report.policies[i]) for i in range(report.policy_count)]}
    if report.has_mask:
        result["mask"] = report.mask
    return result


def geometry(layout):
    """power_arbitration._geometry: None when understood, else its refusal text."""
    library = load()
    row = _layout_struct(layout)
    reason = ctypes.create_string_buffer(DC_ARBITRATION_TEXT)
    code = library.dc_power_arbitration_geometry(ctypes.byref(row) if row is not None else None, reason,
                                                 DC_ARBITRATION_TEXT)
    if code == DC_OK:
        return None
    if code != DC_E_UNRECOGNIZED:
        raise RuntimeError(f"dc_power_arbitration_geometry: status {code}")
    return reason.value.decode("utf-8")


def arbitration(row, offset):
    """power_arbitration._arbitration(row, offset) for a bytes-like row and a non-negative offset."""
    library = load()
    data = bytes(row)
    out = _Vector()
    code = library.dc_power_arbitration_vector(data, len(data), offset, ctypes.byref(out))
    if code != DC_OK:
        raise RuntimeError(f"dc_power_arbitration_vector: status {code}")
    return _vector(out)


def row_formats():
    """power_arbitration.ROW_FORMATS: the INFO row-format bytes the decoder accepts.

    V3 rows are accepted on one board's evidence; diagnostic only, never a control gate.
    """
    library = load()
    out = (ctypes.c_uint32 * DC_ARBITRATION_ROW_FORMATS_MAX)()
    count = ctypes.c_uint32()
    code = library.dc_power_arbitration_row_formats(out, DC_ARBITRATION_ROW_FORMATS_MAX, ctypes.byref(count))
    if code != DC_OK or count.value > DC_ARBITRATION_ROW_FORMATS_MAX:
        raise RuntimeError(f"dc_power_arbitration_row_formats: status {code}, count {count.value}")
    return frozenset(out[:count.value])
