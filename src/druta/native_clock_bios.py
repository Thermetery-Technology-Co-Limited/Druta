# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The clock-BIOS reader of the native core (drutacore/clock_bios.h), increment C1.

On a ``druta.native.Gpu``, ``control_once(gpu, identity)`` is
``clock_bios_runtime._control_once(gpu, reader, identity)`` with the handle's own reader, and
``control_shape(value, identity)`` is ``clock_bios_runtime._control_shape``: each returns what
Python returns or raises the same exception type with the same text (``ClockBiosError(message)``,
or ``PerfDiagnosticError(kind, message)``). Every handle keeps one reader with its own sticky
target binding; ``reset(gpu)`` is a fresh reader. This is a parallel implementation for the parity
tests; the Python module stays the one the backend uses.

The C ABI takes typed arguments, so Python's own type rules stay here:
- ``identity`` is passed as a ``dc_target`` only when it is a dict of exactly the four keys with a
  str slot that fits the field and numbers equal to ints in 0..0xFFFFFFFF; any other value equals
  no binding and is passed as NULL (the GET is still issued, as Python compares after it).
- ``_control_shape``'s dict-shape checks run here; the mask is passed when it is an int (not a
  bool) in 0..2**64-1 and as 0 otherwise (refused with the same text), and ``payload_hex`` as one
  byte per code point (non-ASCII as a byte the core never takes for hex or whitespace), or NULL
  when it is not a str.
"""
from __future__ import annotations

import copy
import ctypes
import threading

from . import native
from .native import DC_E_FAILED, DC_E_UNRECOGNIZED, DC_OK, NativeUnavailable, _Error, _Target, _text

CONTROL_SIZE = 24996
CAPACITY = 32
_KINDS = {1: "transient", 2: "invalid", 3: "unsupported", 4: "identity", 5: "malformed"}
_UINT32_MAX = 0xFFFFFFFF
_UINT64_MAX = 0xFFFFFFFFFFFFFFFF
_IDENTITY_KEYS = {"slot", "architecture", "device_id", "subsystem_id"}


class _Requests(ctypes.Structure):  # dc_clock_bios_requests
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("khz", ctypes.c_int32 * CAPACITY)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_clock_bios_control": (ctypes.c_int32, [ctypes.c_void_p, _P(_Target), _P(ctypes.c_uint32), ctypes.c_void_p,
                                               ctypes.c_uint32, _P(_Error)]),
    "dc_clock_bios_control_shape": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint64,
                                                     _P(_Requests), _P(_Error)]),
    "dc_clock_bios_reset": (ctypes.c_int32, [ctypes.c_void_p]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the clock-BIOS entry points bound; raises NativeUnavailable."""
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
            raise NativeUnavailable(f"native core has no clock-BIOS reader: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


def _layout():
    from .clock_bios_runtime import _CONTROL_LAYOUT
    return _CONTROL_LAYOUT


def _new_error():
    error = _Error()
    error.size = ctypes.sizeof(_Error)
    return error


def _raise_failure(status, error):
    message = _text(error.message)
    if status == DC_E_FAILED:
        from .perf_diagnostics import PerfDiagnosticError
        raise PerfDiagnosticError(_KINDS.get(error.kind, "transient"), message)
    if status == DC_E_UNRECOGNIZED:
        from .clock_bios_runtime import ClockBiosError
        raise ClockBiosError(message)
    native._raise(status)


def _u32_equal(value):
    """The int in 0..0xFFFFFFFF that `value` compares equal to, or None."""
    try:
        number = int(value)
    except Exception:
        return None
    try:
        if number != value:
            return None
    except Exception:
        return None
    return number if 0 <= number <= _UINT32_MAX else None


def _target(identity):
    """identity as a dc_target, or None for a value that equals no binding."""
    if not isinstance(identity, dict) or set(identity) != _IDENTITY_KEYS or not isinstance(identity["slot"], str):
        return None
    try:
        slot = identity["slot"].encode()
    except UnicodeError:
        return None
    if len(slot) >= _Target.slot.size or b"\0" in slot:
        return None
    numbers = {key: _u32_equal(identity[key]) for key in ("architecture", "device_id", "subsystem_id")}
    if any(v is None for v in numbers.values()):
        return None
    target = _Target()
    target.size = ctypes.sizeof(_Target)
    target.slot = slot
    target.architecture, target.device_id = numbers["architecture"], numbers["device_id"]
    target.subsystem_id = numbers["subsystem_id"]
    return target


def control_once(gpu, identity):
    """clock_bios_runtime._control_once(gpu, reader, identity): the user-clock CONTROL dict."""
    target, error = _target(identity), _new_error()
    mask, payload = ctypes.c_uint32(), ctypes.create_string_buffer(CONTROL_SIZE)
    status = load().dc_clock_bios_control(gpu._handle, ctypes.byref(target) if target is not None else None,
                                          ctypes.byref(mask), payload, CONTROL_SIZE, ctypes.byref(error))
    if status != DC_OK:
        _raise_failure(status, error)
    return {**_layout(), "mask": mask.value, "payload_hex": payload.raw.hex()}


def control_shape(value, identity):
    """clock_bios_runtime._control_shape(value, identity): (deep copy of value, {row: kHz})."""
    from .clock_bios_runtime import ClockBiosError
    layout = _layout()
    if not isinstance(value, dict) or set(value) != {*layout, "mask", "payload_hex"}:
        raise ClockBiosError("Complete user-clock requests are unavailable")
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in layout.items()):
        raise ClockBiosError("User-clock request layout is not understood")
    mask = value["mask"]
    mask = mask if type(mask) is int and 0 <= mask <= _UINT64_MAX else 0
    text = value["payload_hex"]
    data = text.encode("latin-1", errors="replace") if isinstance(text, str) else None
    length = min(len(data), _UINT32_MAX) if data is not None else 0
    out, error = _Requests(), _new_error()
    out.size = ctypes.sizeof(_Requests)
    status = load().dc_clock_bios_control_shape(data, length, mask, ctypes.byref(out), ctypes.byref(error))
    if status != DC_OK:
        _raise_failure(status, error)
    requests = {index: out.khz[index] for index in range(CAPACITY) if out.present >> index & 1}
    return copy.deepcopy(value), requests


def reset(gpu):
    """A fresh reader on this handle: forget its sticky target identity."""
    status = load().dc_clock_bios_reset(gpu._handle)
    if status != DC_OK:
        native._raise(status)
