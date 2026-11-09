# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The PERF diagnostics reader of the native core (drutacore/perf_diagnostics.h), increments P1 and P2.

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

Increment P2: ``decode_group(name, info, control, *, slot="", architecture=None)`` is
``perf_diagnostics.decode_group`` (dc_perf_decode_group; the dict is rebuilt here with Python's key
order and optional keys, ``captured_at`` is ``time.time()`` taken after the core returns, as Python
takes it after decoding). ``Reader(gpu, attempts=2)`` is ``PerfDiagnosticsReader``: ``read(groups)``
validates the names here (duplicates, then each name through the core's ``_layout``), then runs
dc_perf_read_group per group and builds the report, ``last_valid`` and ``last_errors`` as Python
does. Each handle has one native reader, so constructing a Reader resets that handle's sticky
identity (Python's fresh reader). ``sha256_hex(data)`` is the core's embedded SHA-256.

Python's ``except Exception`` branch of read() (a non-PerfDiagnosticError from the binding, the
capture or the escape, reported as transient "PERF read failed (Type): msg") has no native path: the
core reports every failure as a kinded error.

A group name that is not an ASCII str without NUL never reaches the core: its ``_layout``
ValueError is formatted here with Python's own repr (the core's repr covers ASCII only).

Documented divergence: the escape fields are marshalled with the packet, as
nvbackend.GPU._legacy_clk_escape builds its _Escape (``dict(fields, hAdapter=...)``, ctypes
``c_uint32`` fields). Python does that inside the escape, after enumerating the adapters, and not at
all when the escape cannot be issued; a field value ctypes refuses (a float, a str) is raised here
after the binding but before any driver call.
"""
from __future__ import annotations

import copy
import ctypes
import threading
import time

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


class _Entry(ctypes.Structure):  # dc_perf_entry
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32), ("control_type", ctypes.c_uint32),
                ("info_bytes", ctypes.c_uint32), ("control_bytes", ctypes.c_uint32),
                ("has_driver_label", ctypes.c_int32), ("driver_label", ctypes.c_char * 32),
                ("issue_count", ctypes.c_uint32), ("issue", ctypes.c_char * 64), ("has_policy", ctypes.c_int32),
                ("role_bytes_raw", ctypes.c_uint32 * 2), ("role_id", ctypes.c_uint32),
                ("controller_mask", ctypes.c_uint32), ("has_registry_flag", ctypes.c_int32),
                ("registry_controlled_flag_raw", ctypes.c_uint32), ("info_raw", ctypes.c_uint8 * 0x350),
                ("control_raw", ctypes.c_uint8 * 0x2C4)]


class _Flag(ctypes.Structure):  # dc_perf_flag
    _fields_ = [("label", ctypes.c_char * 32), ("raw", ctypes.c_uint32), ("has_boolean", ctypes.c_int32),
                ("boolean", ctypes.c_int32)]


class _Target(ctypes.Structure):  # dc_perf_target
    _fields_ = [("field", ctypes.c_char * 32), ("has_index", ctypes.c_int32), ("index", ctypes.c_uint32),
                ("present", ctypes.c_int32), ("has_flag_raw", ctypes.c_int32), ("flag_raw", ctypes.c_uint32),
                ("has_issue", ctypes.c_int32), ("issue", ctypes.c_char * 64)]


class _Snapshot(ctypes.Structure):  # dc_perf_snapshot
    _fields_ = [("size", ctypes.c_uint32), ("group", ctypes.c_char * 16), ("capacity", ctypes.c_uint32),
                ("mask_bytes", ctypes.c_uint32), ("mask", ctypes.c_uint8 * 64), ("info_command", ctypes.c_uint32),
                ("control_command", ctypes.c_uint32), ("info_bytes", ctypes.c_uint32),
                ("control_bytes", ctypes.c_uint32), ("info_sha256", ctypes.c_char * 65),
                ("control_sha256", ctypes.c_char * 65), ("has_control_sha256", ctypes.c_int32),
                ("flag_count", ctypes.c_uint32), ("flags", _Flag * 3), ("target_count", ctypes.c_uint32),
                ("targets", _Target * 3), ("issue_count", ctypes.c_uint32), ("issues", (ctypes.c_char * 64) * 3),
                ("entry_count", ctypes.c_uint32), ("entries", _Entry * 512)]


class _GroupResult(ctypes.Structure):  # dc_perf_group_result
    _fields_ = [("size", ctypes.c_uint32), ("status", ctypes.c_int32), ("attempts", ctypes.c_uint32),
                ("has_target", ctypes.c_int32), ("slot", ctypes.c_char * 32), ("architecture", ctypes.c_uint32),
                ("snapshot", _Snapshot)]


DC_PERF_STATUS_READY, DC_PERF_STATUS_EMPTY, DC_PERF_STATUS_FAILED = 0, 1, 2

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
    "dc_perf_decode_group": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                                              ctypes.c_size_t, ctypes.c_int32, _P(_Snapshot), _P(_Error)]),
    "dc_perf_read_group": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, _P(_GroupResult),
                                            _P(_Error)]),
    "dc_sha256_hex": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char * 65]),
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


# ---- increment P2: SHA-256, decode_group and the read loop ---------------------------------------- #

def sha256_hex(data):
    """hashlib.sha256(data).hexdigest() through the core's embedded SHA-256."""
    data = bytes(data)
    out = (ctypes.c_char * 65)()
    status = load().dc_sha256_hex(data, len(data), out)
    if status != DC_OK:
        native._raise(status)
    return out.value.decode("ascii")


def _new_snapshot():
    snapshot = _Snapshot()
    snapshot.size = ctypes.sizeof(_Snapshot)
    return snapshot


def _core_name(name):
    """The name as the core takes it, or None for one only Python's repr can describe."""
    if isinstance(name, str) and name.isascii() and "\0" not in name:
        return name.encode("ascii")
    return None


def _decode(name, info, control):
    """dc_perf_decode_group: (snapshot struct, None) or (None, the exception to raise)."""
    encoded = _core_name(name)
    if encoded is None:
        return None, ValueError(f"Unknown PERF diagnostic group {name!r}")
    out, error = _new_snapshot(), _new_error()
    status = load().dc_perf_decode_group(encoded, info, len(info), control, len(control or b""),
                                         0 if control is None else 1, ctypes.byref(out), ctypes.byref(error))
    if status == DC_E_FAILED:
        try:
            _raise_failure(error)
        except (ValueError, RuntimeError) as exc:
            return None, exc
    if status != DC_OK:
        native._raise(status)
    return out, None


def _layout(name):
    """_layout(name): None, or its ValueError (an unhashable name raises TypeError, as LAYOUTS[name])."""
    hash(name)
    _, failure = _decode(name, b"", None)
    if isinstance(failure, ValueError):
        raise failure


_INFO_CHECKS = ("Unexpected PERF INFO parameter geometry", "PERF INFO mask exceeds group capacity")


def _text_of(field):
    return field.decode("ascii")


def _build(s, slot, architecture):
    """decode_group's dict from a dc_perf_snapshot, in Python's key order."""
    entries = []
    for e in s.entries[:s.entry_count]:
        row = {
            "index": e.index,
            "type": e.type,
            "control_type": e.control_type,
            "driver_label": _text_of(e.driver_label) if e.has_driver_label else None,
            "info_hex": ctypes.string_at(ctypes.addressof(e.info_raw), e.info_bytes).hex(),
            "control_hex": ctypes.string_at(ctypes.addressof(e.control_raw), e.control_bytes).hex(),
            "issues": [_text_of(e.issue)] if e.issue_count else [],
        }
        if e.has_policy:
            row["role_bytes_raw"] = [e.role_bytes_raw[0], e.role_bytes_raw[1]]
            row["role_id"] = e.role_id
            row["controller_mask"] = e.controller_mask
            if e.has_registry_flag:
                row["registry_controlled_flag_raw"] = e.registry_controlled_flag_raw
        entries.append(row)
    flags = {_text_of(f.label): {"raw": f.raw, "boolean": bool(f.boolean) if f.has_boolean else None}
             for f in s.flags[:s.flag_count]}
    targets = [{
        "registry_name": "RmPerfCfPolicyOverrides",
        "field": _text_of(t.field),
        "index": t.index if t.has_index else None,
        "present": bool(t.present),
        "flag_raw": t.flag_raw if t.has_flag_raw else None,
        "issue": _text_of(t.issue) if t.has_issue else None,
    } for t in s.targets[:s.target_count]]
    return {
        "group": _text_of(s.group),
        "slot": slot,
        "architecture": architecture,
        "captured_at": time.time(),
        "capacity": s.capacity,
        "mask_hex": bytes(s.mask[:s.mask_bytes]).hex(),
        "present_count": len(entries),
        "info_command": f"0x{s.info_command:08X}",
        "control_command": f"0x{s.control_command:08X}",
        "info_bytes": s.info_bytes,
        "control_bytes": s.control_bytes,
        "info_sha256": _text_of(s.info_sha256),
        "control_sha256": _text_of(s.control_sha256) if s.has_control_sha256 else None,
        "entries": entries,
        "flags": flags,
        "registry_targets": targets,
        "issues": [_text_of(text.value) for text in s.issues[:s.issue_count]],
    }


def decode_group(name, info, control, *, slot="", architecture=None):
    """perf_diagnostics.decode_group through dc_perf_decode_group."""
    _layout(name)
    info = bytes(info)
    if control is not None:
        try:
            control = bytes(control)
        except Exception as exc:  # Python converts CONTROL after the INFO geometry and capacity checks
            _, failure = _decode(name, info, None)
            if failure is not None and str(failure) in _INFO_CHECKS:
                raise failure from None
            raise exc
    snapshot, failure = _decode(name, info, control)
    if failure is not None:
        raise failure
    return _build(snapshot, slot, architecture)


class Reader:
    """perf_diagnostics.PerfDiagnosticsReader on a druta.native.Gpu: read() through dc_perf_read_group.

    The handle owns one native reader; constructing a Reader resets its sticky identity."""

    def __init__(self, gpu, *, attempts=2):
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError("PERF read attempts must be between 1 and 3")
        self.gpu, self.attempts = gpu, attempts
        self.last_valid = {}
        self.last_errors = {}
        reset(gpu)

    def read(self, groups=None):
        from .perf_diagnostics import PerfDiagnosticError
        names = tuple(layouts()) if groups is None else tuple(groups)
        if len(set(names)) != len(names):
            raise ValueError("PERF groups must not be duplicated")
        for name in names:
            _layout(name)
        report = {"slot": "", "architecture": None, "captured_at": time.time(), "groups": {}}
        for name in names:
            out, error = _GroupResult(), _new_error()
            out.size, out.snapshot.size = ctypes.sizeof(_GroupResult), ctypes.sizeof(_Snapshot)
            status = load().dc_perf_read_group(self.gpu._handle, name.encode("ascii"), self.attempts,
                                               ctypes.byref(out), ctypes.byref(error))
            if status == DC_E_FAILED:
                _raise_failure(error)
            if status != DC_OK:
                native._raise(status)
            if out.has_target:
                report["slot"], report["architecture"] = _text(out.slot), out.architecture
            if out.status != DC_PERF_STATUS_FAILED:
                snapshot = _build(out.snapshot, _text(out.slot), out.architecture)
                self.last_valid[name] = copy.deepcopy(snapshot)
                self.last_errors.pop(name, None)
                result = {
                    "status": "ready" if snapshot["present_count"] else "empty",
                    "snapshot": snapshot,
                    "last_valid": None,
                    "error": None,
                    "attempts": out.attempts,
                }
            else:
                failure = PerfDiagnosticError(_KINDS.get(error.kind, "transient"), _text(error.message),
                                              _status(error))
                self.last_errors[name] = failure
                result = {
                    "status": failure.kind,
                    "snapshot": None,
                    "last_valid": copy.deepcopy(self.last_valid.get(name)),
                    "error": str(failure),
                    "attempts": out.attempts,
                }
            report["groups"][name] = result
        return report
