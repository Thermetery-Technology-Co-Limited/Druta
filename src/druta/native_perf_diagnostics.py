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

The C ABI takes unsigned 32-bit words, so ``get`` keeps Python's own argument semantics here, in
_get's order: (1) the allow-set rule is ``(command, size) in`` a frozenset of the core's layout
pairs (Python's equality and hashing: an int subclass or a float equal to an allowed value passes);
(2) the mask rules; then the transport is marshalled exactly as _get builds its packet (``header,
fields = transport``, a ``c_uint32`` packet of ``17 + size // 4`` words, every header word stored
through ctypes, words 2/14/15/16 set). When that raises, the reader binds first (dc_perf_bind), so
the sticky identity is recorded and a binding refusal wins, and the marshalling exception (TypeError,
ValueError, IndexError: ctypes' and the interpreter's own text) is raised after it. Otherwise the
stored words go to dc_perf_get, which runs the rules and the binding again (idempotent) and the GET.

Documented divergence: the escape fields are marshalled with the packet, as
nvbackend.GPU._legacy_clk_escape builds its _Escape (``dict(fields, hAdapter=...)``, ctypes
``c_uint32`` fields). Python does that inside the escape, after enumerating the adapters, and not at
all when the escape cannot be issued; a field value ctypes refuses (a float, a str) is raised here
after the binding but before any driver call.
"""
from __future__ import annotations

import ctypes
import threading

from . import native
from .native import DC_E_FAILED, DC_OK, NativeUnavailable, _Error, _EscapeFields, _RmTransport, _text, _transport_value

DC_PERF_GROUP_COUNT = 3
DC_PERF_MASK_NONE, DC_PERF_MASK_BYTES, DC_PERF_MASK_OTHER = 0, 1, 2
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
    "dc_perf_bind": (ctypes.c_int32, [ctypes.c_void_p, _P(_Error)]),
    "dc_perf_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, _P(ctypes.c_uint32),
                                     ctypes.c_uint32, _P(_EscapeFields), ctypes.c_char_p, ctypes.c_uint32,
                                     ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, _P(_Error)]),
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


def bind(gpu):
    """PerfDiagnosticsReader._binding alone on this handle's reader: None, or the error it raises."""
    error = _new_error()
    status = load().dc_perf_bind(gpu._handle, ctypes.byref(error))
    if status == DC_E_FAILED:
        _raise_failure(error)
    if status != DC_OK:
        native._raise(status)


class _Escape(ctypes.Structure):  # nvbackend.GPU._Escape: the D3DKMT_ESCAPE the fields fill
    _fields_ = [("hAdapter", ctypes.c_uint32), ("hDevice", ctypes.c_uint32), ("Type", ctypes.c_uint32),
                ("Flags", ctypes.c_uint32), ("pPrivateDriverData", ctypes.c_void_p),
                ("PrivateDriverDataSize", ctypes.c_uint32), ("hContext", ctypes.c_uint32)]


def _marshal(command, size, transport):
    """_get's packet build with its own ctypes semantics: (packet, header words stored, fields)."""
    header, fields = transport
    packet = (ctypes.c_uint32 * (17 + size // 4))()
    count = 0
    for index, word in enumerate(header):
        packet[index] = word
        count = index + 1
    packet[2], packet[14], packet[15], packet[16] = ctypes.sizeof(packet), command, size, 0
    escape = _Escape(**dict(fields, hAdapter=0), pPrivateDriverData=None, PrivateDriverDataSize=0)
    words = (ctypes.c_uint32 * max(count, 1))(*packet[:count])
    return packet, words, count, _EscapeFields(0, escape.hDevice, escape.Type, escape.Flags, escape.hContext)


def get(gpu, command, size, transport, mask=None):
    """PerfDiagnosticsReader._get(command, size, transport, mask): the response bytes."""
    table = layouts().values()
    allowed = frozenset((g["info_command"], g["info_size"]) for g in table) | frozenset(
        (g["control_command"], g["control_size"]) for g in table)
    if (command, size) not in allowed:
        raise ValueError("Only understood read-only PERF commands are allowed")
    layout = next(g for g in table if command in (g["info_command"], g["control_command"]))
    if command == layout["control_command"]:
        if not isinstance(mask, bytes) or len(mask) != layout["mask_bytes"]:
            raise ValueError("PERF CONTROL GET requires an exact INFO mask")
    elif mask is not None:
        raise ValueError("PERF INFO GET does not accept a request mask")
    try:
        packet, words, count, fields = _marshal(command, size, transport)
    except Exception as exc:  # Python binds before it builds the packet: the binding's error wins
        failure = exc
    else:
        failure = None
    if failure is not None:
        bind(gpu)
        raise failure
    command_word, size_word = packet[14], packet[15]
    kind, data = (DC_PERF_MASK_BYTES, bytes(mask)) if mask is not None else (DC_PERF_MASK_NONE, None)
    capacity = ctypes.sizeof(packet) - 68
    out, error = ctypes.create_string_buffer(max(capacity, 1)), _new_error()
    status = load().dc_perf_get(gpu._handle, command_word, size_word, words, count, ctypes.byref(fields), data,
                                len(data) if data is not None else 0, kind, out, capacity, ctypes.byref(error))
    if status == DC_E_FAILED:
        _raise_failure(error)
    if status != DC_OK:
        native._raise(status)
    return out.raw[:size_word]


def reset(gpu):
    """A fresh reader on this handle: forget its sticky target identity."""
    status = load().dc_perf_reset(gpu._handle)
    if status != DC_OK:
        native._raise(status)
