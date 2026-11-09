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
- ``identity`` is never converted. The core reports the identity it bound (a ``dc_target``) to a
  callback at Python's point (after the guards and the binding, before the status check), which
  evaluates ``bound != identity`` with Python's semantics, ``bound`` being the plain dict
  ``_binding(reader)`` returns (slot str, then three ints). An exception the comparison raises is
  re-raised as Python would raise it; the callback never calls back into the GPU.
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


class _Requests(ctypes.Structure):  # dc_clock_bios_requests
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("khz", ctypes.c_int32 * CAPACITY)]


_P = ctypes.POINTER
_IDENTITY_FN = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, _P(_Target))  # dc_clock_bios_identity_fn
_SIGNATURES = {
    "dc_clock_bios_control": (ctypes.c_int32, [ctypes.c_void_p, _IDENTITY_FN, ctypes.c_void_p, _P(ctypes.c_uint32),
                                               ctypes.c_void_p, ctypes.c_uint32, _P(_Error)]),
    "dc_clock_bios_control_shape": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint64,
                                                     _P(_Requests), _P(_Error)]),
    "dc_clock_bios_reset": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_clock_bios_held": (ctypes.c_int32, [ctypes.c_void_p, _P(ctypes.c_int32)]),
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


def control_once(gpu, identity):
    """clock_bios_runtime._control_once(gpu, reader, identity): the user-clock CONTROL dict."""
    raised = []

    def differs(_context, bound):
        target = bound.contents
        current = {"slot": _text(target.slot), "architecture": target.architecture, "device_id": target.device_id,
                   "subsystem_id": target.subsystem_id}
        try:
            return 1 if current != identity else 0
        except BaseException as exc:  # raised below, at Python's point
            raised.append(exc)
            return 1

    error = _new_error()
    mask, payload = ctypes.c_uint32(), ctypes.create_string_buffer(CONTROL_SIZE)
    status = load().dc_clock_bios_control(gpu._handle, _IDENTITY_FN(differs), None, ctypes.byref(mask), payload,
                                          CONTROL_SIZE, ctypes.byref(error))
    if raised:
        raise raised[0]
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


def held(gpu):
    """Whether the handle's reader holds a sticky target identity (``reader._identity is not None``)."""
    value = ctypes.c_int32()
    status = load().dc_clock_bios_held(gpu._handle, ctypes.byref(value))
    if status != DC_OK:
        native._raise(status)
    return bool(value.value)
