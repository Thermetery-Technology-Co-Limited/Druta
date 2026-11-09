# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The PERF diagnostics reader of the native core (drutacore/perf_diagnostics.h), increment P1.

``layouts()`` is ``perf_diagnostics.LAYOUTS`` as plain dicts (field name -> value) in the same
order, ``probe_architectures()`` is ``PROBE_ARCHITECTURES`` and ``get_commands()`` is
``_GET_COMMANDS``. On a ``druta.native.Gpu``, ``transport(gpu)`` is
``PerfDiagnosticsReader._transport`` and ``get(gpu, command, size, transport, mask=None)`` is
``PerfDiagnosticsReader._get``: each returns what Python returns or raises the same exception
(``PerfDiagnosticError(kind, message, status)``, or ``ValueError`` for the allow-set and mask
rules). Every handle keeps one reader with its own sticky target binding; ``reset(gpu)`` is a
fresh reader. This is a parallel implementation for the parity tests; the Python module stays the
one the backend uses.

The C ABI takes unsigned 32-bit commands and sizes and a typed mask, so Python's own argument
type rules stay here: a command or size that is not an int in 0..0xFFFFFFFF can never be in
_GET_COMMANDS and is passed as 0 (refused with the same text); a mask that is not ``bytes`` is
passed as DC_PERF_MASK_OTHER (refused for CONTROL, as any mask is for INFO).
"""
from __future__ import annotations

import ctypes
import threading

from . import native
from .native import DC_E_FAILED, DC_OK, NativeUnavailable, _Error, _RmTransport, _text, _transport_struct, _transport_value

DC_PERF_GROUP_COUNT = 3
DC_PERF_MASK_NONE, DC_PERF_MASK_BYTES, DC_PERF_MASK_OTHER = 0, 1, 2
DC_E_ARGUMENT = -1
_UINT32_MAX = 0xFFFFFFFF
_FIELDS = ("info_id", "control_id", "info_command", "control_command", "info_size", "control_size", "capacity",
           "mask_bytes", "info_offset", "info_stride", "control_offset", "control_stride")
_KINDS = {1: "transient", 3: "unsupported", 4: "identity", 5: "malformed"}
_INVALID = 2  # DC_ERR_INVALID: a plain ValueError raised before the driver was touched


class _Layout(ctypes.Structure):  # dc_perf_layout
    _fields_ = [("size", ctypes.c_uint32), ("name", ctypes.c_char * 16)] + [(f, ctypes.c_uint32) for f in _FIELDS]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_perf_layouts": (ctypes.c_int32, [_P(_Layout), ctypes.c_uint32, _P(ctypes.c_uint32)]),
    "dc_perf_probe_architecture": (ctypes.c_int32, [ctypes.c_uint32]),
    "dc_perf_get_allowed": (ctypes.c_int32, [ctypes.c_uint32, ctypes.c_uint32]),
    "dc_perf_transport": (ctypes.c_int32, [ctypes.c_void_p, _P(_RmTransport), _P(_Error)]),
    "dc_perf_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, _P(_RmTransport),
                                     ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p,
                                     ctypes.c_uint32, _P(_Error)]),
    "dc_perf_reset": (ctypes.c_int32, [ctypes.c_void_p]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the PERF diagnostics entry points bound; raises NativeUnavailable."""
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
            raise NativeUnavailable(f"native core has no PERF diagnostics reader: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


# ---- the pure tables ---------------------------------------------------------------------------- #

def layouts():
    """perf_diagnostics.LAYOUTS as {name: {field: value}} in LAYOUTS order."""
    rows = (_Layout * DC_PERF_GROUP_COUNT)()
    for row in rows:
        row.size = ctypes.sizeof(_Layout)
    count = ctypes.c_uint32()
    status = load().dc_perf_layouts(rows, DC_PERF_GROUP_COUNT, ctypes.byref(count))
    if status != DC_OK:
        native._raise(status)
    return {_text(row.name): {f: getattr(row, f) for f in _FIELDS} for row in rows[:min(count.value, len(rows))]}


def probe_architectures():
    """PROBE_ARCHITECTURES over NVML's architecture enum range."""
    return frozenset(a for a in range(0, 64) if load().dc_perf_probe_architecture(a))


def get_commands():
    """_GET_COMMANDS: every (command, size) the core allows, from its own layouts."""
    allowed = set()
    for layout in layouts().values():
        for command, size in ((layout["info_command"], layout["info_size"]),
                              (layout["control_command"], layout["control_size"])):
            if load().dc_perf_get_allowed(command, size):
                allowed.add((command, size))
    return frozenset(allowed)


# ---- the reader --------------------------------------------------------------------------------- #

def _status(error):
    """PerfDiagnosticError.status: the RM status of a rejection, the NTSTATUS of a failed escape."""
    if error.rm_status:
        return error.rm_status
    if error.has_ntstatus and error.ntstatus:
        return error.ntstatus
    return None


def _raise_failure(error):
    from .perf_diagnostics import PerfDiagnosticError
    message = _text(error.message)
    if error.kind == _INVALID:
        raise ValueError(message)
    raise PerfDiagnosticError(_KINDS.get(error.kind, "transient"), message, _status(error))


def _new_error():
    error = _Error()
    error.size = ctypes.sizeof(_Error)
    return error


def transport(gpu):
    """PerfDiagnosticsReader._transport: (tuple(header), dict(fields)) or PerfDiagnosticError."""
    out, error = _RmTransport(), _new_error()
    out.size = ctypes.sizeof(_RmTransport)
    status = load().dc_perf_transport(gpu._handle, ctypes.byref(out), ctypes.byref(error))
    if status == DC_E_FAILED:
        _raise_failure(error)
    if status != DC_OK:
        native._raise(status)
    header, fields = _transport_value(out)
    return tuple(header), dict(fields)


def _u32(value):
    return value if type(value) is int and 0 <= value <= _UINT32_MAX else 0


def get(gpu, command, size, transport, mask=None):
    """PerfDiagnosticsReader._get(command, size, transport, mask): the response bytes."""
    if mask is None:
        kind, data = DC_PERF_MASK_NONE, None
    elif isinstance(mask, bytes):
        kind, data = DC_PERF_MASK_BYTES, mask
    else:
        kind, data = DC_PERF_MASK_OTHER, None
    command, size = _u32(command), _u32(size)
    request = _transport_struct(transport)
    capacity = size if load().dc_perf_get_allowed(command, size) else 0
    out, error = ctypes.create_string_buffer(max(capacity, 1)), _new_error()
    status = load().dc_perf_get(gpu._handle, command, size, ctypes.byref(request), data,
                                len(data) if data is not None else 0, kind, out, capacity, ctypes.byref(error))
    if status == DC_E_FAILED:
        _raise_failure(error)
    if status == DC_E_ARGUMENT and transport is None:
        raise TypeError("cannot unpack non-iterable NoneType object")
    if status != DC_OK:
        native._raise(status)
    return out.raw[:size]


def reset(gpu):
    """A fresh reader on this handle: forget its sticky target identity."""
    status = load().dc_perf_reset(gpu._handle)
    if status != DC_OK:
        native._raise(status)
