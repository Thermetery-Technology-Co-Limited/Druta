# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The clock-BIOS reader of the native core (drutacore/clock_bios.h), increments C1 and C2.

On a ``druta.native.Gpu``, ``control_once(gpu, identity)`` is
``clock_bios_runtime._control_once(gpu, reader, identity)`` with the handle's own reader, and
``control_shape(value, identity)`` is ``clock_bios_runtime._control_shape``: each returns what
Python returns or raises the same exception type with the same text (``ClockBiosError(message)``,
or ``PerfDiagnosticError(kind, message)``). Every handle keeps one reader with its own sticky
target binding; ``reset(gpu)`` is a fresh reader. This is a parallel implementation for the parity
tests; the Python module stays the one the backend uses.

C2: ``info_once(gpu)`` is ``_info_once(gpu, reader)`` on that same handle reader, returning
``(identity, info bytes)``; ``decode(raw)`` is the pure ``_decode`` for objects without the buffer
protocol and for one-dimensional unsigned-byte buffers (other buffers are refused, see ``decode``);
``read(gpu)`` is ``read(gpu)`` with its own fresh reader per call, returning ``(observation, None)``
or ``(None, message)`` with the observation dict rebuilt in Python's key order and value types.

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


INFO_SIZE = 12336


class _Requests(ctypes.Structure):  # dc_clock_bios_requests
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("khz", ctypes.c_int32 * CAPACITY)]


class _Row(ctypes.Structure):  # dc_clock_bios_row
    _fields_ = [("type", ctypes.c_uint32), ("base_type", ctypes.c_uint32), ("api_domain_mask", ctypes.c_uint32),
                ("typed", ctypes.c_int32), ("minimum_mhz", ctypes.c_int32), ("maximum_mhz", ctypes.c_int32)]


class _Rows(ctypes.Structure):  # dc_clock_bios_rows
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("rows", _Row * CAPACITY)]


class _Observation(ctypes.Structure):  # dc_clock_bios_observation
    _fields_ = [("size", ctypes.c_uint32), ("available", ctypes.c_int32), ("identity", _Target),
                ("info", ctypes.c_uint8 * INFO_SIZE), ("has_user_control", ctypes.c_int32),
                ("user_control_mask", ctypes.c_uint32), ("user_control", ctypes.c_uint8 * CONTROL_SIZE),
                ("rows", _Rows), ("stable_reads", ctypes.c_uint32), ("has_user_control_error", ctypes.c_int32),
                ("user_control_error", ctypes.c_char * 1024)]


_P = ctypes.POINTER
_IDENTITY_FN = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, _P(_Target))  # dc_clock_bios_identity_fn
_SIGNATURES = {
    "dc_clock_bios_control": (ctypes.c_int32, [ctypes.c_void_p, _IDENTITY_FN, ctypes.c_void_p, _P(ctypes.c_uint32),
                                               ctypes.c_void_p, ctypes.c_uint32, _P(_Error)]),
    "dc_clock_bios_control_shape": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint64,
                                                     _P(_Requests), _P(_Error)]),
    "dc_clock_bios_info": (ctypes.c_int32, [ctypes.c_void_p, _P(_Target), ctypes.c_void_p, ctypes.c_uint32,
                                            _P(_Error)]),
    "dc_clock_bios_decode": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_uint32, _P(_Rows), _P(_Error)]),
    "dc_clock_bios_read": (ctypes.c_int32, [ctypes.c_void_p, _P(_Observation), _P(_Error)]),
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


def _identity(target):
    """The plain identity dict _binding(reader) returns, in its key order."""
    return {"slot": _text(target.slot), "architecture": target.architecture, "device_id": target.device_id,
            "subsystem_id": target.subsystem_id}


def _rows(rows):
    """_decode's dict from a dc_clock_bios_rows."""
    out = {}
    for index in range(CAPACITY):
        if not rows.present >> index & 1:
            continue
        row = rows.rows[index]
        typed = bool(row.typed)
        out[str(index)] = {
            "type": row.type,
            "base_type": row.base_type,
            "api_domain_mask": row.api_domain_mask,
            "minimum_mhz": row.minimum_mhz if typed else None,
            "maximum_mhz": row.maximum_mhz if typed else None,
        }
    return out


def decode(raw):
    """clock_bios_runtime._decode(raw): {str(index): row} for every masked INFO row.

    Python's own type rules first: ``len(raw)`` raises for an object without a length and the
    geometry check follows, as in ``_decode``. A 12336-element object without the buffer protocol
    then raises the ``TypeError`` ``struct.unpack_from`` raises, and a strided 1-D view the
    ``BufferError`` it raises. Any C-contiguous one-dimensional buffer of unsigned single-byte items
    (bytes, bytearray, ``array('B')``, a byte memoryview, a ctypes ``c_uint8`` array) is decoded from
    its bytes, which is what ``_decode`` reads from it. Every other buffer is refused with a
    ``TypeError``: ``_decode`` indexes it by element (signed values for format 'b', bytes objects
    for 'c', and byte offsets mixed with element slices for multi-byte or multi-dimensional items),
    which no native decode mirrors.
    """
    if len(raw) != INFO_SIZE:
        from .clock_bios_runtime import ClockBiosError
        raise ClockBiosError("Clock INFO geometry is not understood")
    try:
        view = memoryview(raw)
    except TypeError:
        raise TypeError(f"a bytes-like object is required, not '{type(raw).__name__}'") from None
    with view:
        if view.ndim != 1 or view.itemsize != 1 or view.format.lstrip("@=<>!") != "B":
            raise TypeError("native_clock_bios.decode mirrors _decode for single-byte unsigned buffers only, "
                            f"not format {view.format!r} with {view.ndim} dimension(s)")
        if not view.c_contiguous:
            raise BufferError("memoryview: underlying buffer is not C-contiguous")
        data = view.tobytes()
    out, error = _Rows(), _new_error()
    out.size = ctypes.sizeof(_Rows)
    status = load().dc_clock_bios_decode(data, len(data), ctypes.byref(out), ctypes.byref(error))
    if status != DC_OK:
        _raise_failure(status, error)
    return _rows(out)


def info_once(gpu):
    """clock_bios_runtime._info_once(gpu, reader) on the handle's reader: (identity, INFO bytes)."""
    target, error = _Target(), _new_error()
    target.size = ctypes.sizeof(_Target)
    payload = ctypes.create_string_buffer(INFO_SIZE)
    status = load().dc_clock_bios_info(gpu._handle, ctypes.byref(target), payload, INFO_SIZE, ctypes.byref(error))
    if status != DC_OK:
        _raise_failure(status, error)
    return _identity(target), payload.raw


def read(gpu):
    """clock_bios_runtime.read(gpu): (observation, None) or (None, message)."""
    from .clock_bios_runtime import _LAYOUT
    out, error = _Observation(), _new_error()
    out.size = ctypes.sizeof(_Observation)
    status = load().dc_clock_bios_read(gpu._handle, ctypes.byref(out), ctypes.byref(error))
    if status == DC_E_FAILED:
        # An exception inside the core (only allocation can throw) ends the read where Python's
        # `except Exception` would record str(exc) as the failure of that attempt: read() never raises.
        return None, _text(error.message) or "Clock INFO is unavailable"
    if status != DC_OK:
        native._raise(status)
    if not out.available:
        return None, _text(error.message)
    control = None
    if out.has_user_control:
        control = {**_layout(), "mask": out.user_control_mask, "payload_hex": bytes(out.user_control).hex()}
    observation = {
        **_LAYOUT,
        "identity": _identity(out.identity),
        "info_hex": bytes(out.info).hex(),
        "user_control": control,
        "rows": _rows(out.rows),
        "stable_reads": out.stable_reads,
        "user_control_error": _text(out.user_control_error) if out.has_user_control_error else None,
    }
    return observation, None


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
