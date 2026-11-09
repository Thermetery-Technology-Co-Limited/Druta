# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The voltage-rail getters of the native core (drutacore/rails.h), increments R2 to R4.

On a ``druta.native.Gpu``: ``read_volt_rail_blocks(gpu, function, version)`` is
``GPU._read_volt_rail_blocks`` (``{rail: bytes}`` of the 8192-byte buffer the getter filled, where
Python returns the ctypes buffer itself); ``read_volt_rail_deltas(gpu)`` is
``_read_volt_rail_deltas``; ``read_volt_rail_state(gpu)`` is ``read_volt_rail_state`` (``None``
when no rail is kept); ``read_rail_live_mv(gpu, rail)`` is ``read_rail_live_mv``. ``caches(gpu)``
returns the four per-GPU caches as the GPU keeps them, as a dict of ``_volt_rail_masks_cache``,
``_volt_rail_retry_after``, ``_volt_rail_getter_diagnostics`` and ``_volt_rail_read_versions``;
``refresh(gpu)`` is the cache reset of ``_refresh_volt_rail_capabilities`` (its
``_volt_rail_write_error`` belongs to the writer and stays in Python). This is a parallel
implementation for the parity tests; the Python module stays the one the backend uses.

``function`` is the getter's name, ``"VoltRailsCtlGet"`` or ``"VoltRailsAbs"``; any other name
raises ValueError (Python would look it up on the NVAPI object). Of the same kind, before any call:
a ``version`` that is not an int raises TypeError (Python's u32 slot assignment raises TypeError for
a float, but only once it builds a request) and an int outside 0..0xFFFFFFFF raises ValueError
(Python's u32 slot would wrap it, so the read could never validate and the cached version and the
diagnostic text would carry the raw value; no caller passes one).

``read_rail_live_mv`` resolves ``rail`` as Python's dict lookup does: 0 and 1 (and values equal to
them, such as 1.0 or True) name a rail; anything else (1.5, "1", 2**32 + 1, None, -1, 2) is None
after the state read, which still happens. An unhashable ``rail`` raises TypeError before the read,
where Python raises it after the read.

Python's ``except`` branch (``status`` None, ``error`` ``str(exc)``) is reached when ctypes converts
a Windows structured exception raised inside the getter into OSError. The native core, like every
other NVAPI call it makes, has no such guard, so a faulting export is not survived; here a
diagnostic's ``"error"`` is always None and its ``"status"`` always an int. Dict keys come out in
sorted order; Python's insertion order is not tracked (the contents are equal).

Increment R3, the reference capture and the readers over it, on the same handle:
``read_volt_rail_limits(gpu)``, ``volt_rail_profile(gpu)`` (``GPU._volt_rail_profile``),
``volt_rail_diagnostics(gpu)``, ``volt_rail_limits_supported(gpu, rail=None)``,
``volt_rail_limit_fields(gpu, rail)``, ``stock_limit_mv(gpu, rail, key)`` and
``volt_rail_limits_mv(gpu)``. The core keeps ``_volt_rail_reference`` and ``_volt_rail_initial_uv``
per handle (``refresh`` keeps them, as ``_refresh_volt_rail_capabilities`` does) and reads the
architecture where Python calls ``self.arch()``. The dicts are rebuilt in Python's key order and
value types: a limits row is ``type`` (int), the four fields (float), ``_base_mv`` (the reference's
bases or ``{}``), ``_headroom_mv`` (None), ``_absolute_ok`` (bool), ``_boost_mv`` (float),
``_effective_mv`` (float or None) and ``_reference_kind``; the profile is ``bases``, ``headroom_mv``,
``poweron`` (tuples of ints), ``fields`` (``VOLT_LIMIT_FIELDS``), ``control_version``,
``settle_vmin``, ``min_mv`` and ``max_mv``, its rail-keyed dicts in the reference's insertion order.

Divergences of R3:

- A rail argument is looked up as Python's dict and set lookups do (1.0 and True find rail 1;
  anything else finds nothing). An unhashable rail raises TypeError before any read; Python raises
  it after its reads, and ``volt_rail_limits_supported`` / ``stock_limit_mv`` return False / None
  without raising when the profile is None.
- ``stock_limit_mv`` raises KeyError for a key outside ``VOLT_LIMIT_FIELDS`` (TypeError for an
  unhashable key) at the point Python does: after the reads and the profile and first-read checks.
- ``volt_rail_diagnostics``: ``"getters"`` is a rebuilt copy of the getter diagnostics cache (Python
  returns the cache dict itself); ``"adapter"`` comes from ``gpu.static``; ``"last_write_error"``,
  ``"observed_packets"`` and ``"write_protocol"`` are None, [] and None, because the rail writer
  that owns them is not ported yet.
- ``_headroom_mv`` and the profile's ``headroom_mv`` are always None: the reference holds only
  ``"bases"`` until the hold-headroom writer is ported.

Increment R4, the rail write sequence: ``write_rail_records(gpu, records)`` is
``GPU._write_rail_records`` (the native.Gpu method ``_write_rail_records`` delegates to it) and
``verify_rail_records(gpu, records, boost)`` is ``GPU._verify_rail_records``. The core runs the whole
sequence under the process-wide rail hook mutex; the binding clears ``_volt_rail_write_error`` first
and sets the three writer attributes only when a locked write got past its preconditions.

Divergences of R4:

- A record value that is not a plain int in the int32 range, or a bool rail key (True for rail 1),
  is refused with prepare_write's text before any escape. Python issues the GET first and refuses
  inside the hook, so its ``_volt_rail_observed_packets`` holds that GET's summary where the core's
  is empty. A key equal to a rail for which ``1 << key`` raises (1.0, a Decimal or Fraction) is not
  one of these: it raises Python's own TypeError at the same point Python does (the rail mask), with
  the same reads before it and the attributes untouched.
- A key that names no rail is never inspected natively, so its value's ``len()`` is not taken:
  where Python's settle check raises TypeError for an unsized value under such a key (settle_vmin
  set and no earlier record settling), the core returns (False, None).
- ``len(values)`` is taken before any read, so an unsized value raises TypeError before the reads
  Python does first.
- A values sequence other than a list or tuple is compared as Python does (never equal to the
  read-back list) but would be printed as a tuple; ``verify_rail_records`` itself raises TypeError
  for anything other than a list or tuple of at most four plain ints, a rail key that is not a plain
  int (Python prints "rail True" / "rail 1.0"), or a boost other than None or an int32 (Python would
  compare and print any value). The write path only verifies records it could
  write, which are always exact.

Increment R5, the verified setters: ``set_volt_rail_limits(gpu, rail, limits)``,
``set_volt_rail_limits_raw(gpu, rail, limits_uv)`` and ``reset_volt_rail_limits(gpu, rail=None,
fields=None)`` are the GPU methods of those names (``limits`` / ``limits_uv`` the keyword dicts; the
native.Gpu methods keep Python's keyword interface and delegate here). The session flags
``volt_limits_write_enabled`` and ``voltage_xoc_enabled`` are read from ``gpu`` at each call and
passed to the core, which never stores them. The writer's three attributes are set as Python's
``_write_rail_records`` calls leave them. ``hold_record(gpu)`` / ``set_hold_record(gpu, record)``
read and set the core's minimal ``_hold_headroom`` (``{"rail", "written_uv", "prior_uv"}``; R6 owns
the fuller record), which the setters adopt into and clear.

Divergences of R5:

- ``rail`` must be a plain int or a key that names no rail: a bool or float equal to a rail (True,
  1.0) raises TypeError before any read (Python carries it into the write, where prepare_write or
  the rail mask refuses it). A limit value must be an int or a float (TypeError otherwise, before
  any read; an int beyond the double range raises OverflowError before any read, where Python's
  ``:.0f`` raises it after them); a value under a keyword that names no field is never converted.
- ``reset_volt_rail_limits`` materializes ``fields`` once with ``tuple()``: a one-shot iterator is
  seen by every per-rail reset (Python's first per-rail call consumes it). Its texts (``bad`` and
  ``joined``) are computed before any read, so a TypeError of ``sorted`` / ``join`` is raised then.
- The unknown, bad and joined texts are refused above 1024 UTF-8 bytes (ValueError) so every
  composed message fits ``dc_outcome``. Below that they reach the core exactly: each travels as
  presence + pointer + byte length, so an empty name (the keyword or field ``""``) refuses with
  "not a rail limit: " as Python does, a NUL byte is carried, and a lone surrogate is carried with
  ``surrogatepass`` (and decoded back from the message the same way).
- ``dc_outcome.message`` is NUL-terminated (it has no length field), so a NUL inside an unknown or
  bad name shortens the printed refusal at that NUL ("not a rail limit: x" for the name x, NUL,
  y, where Python prints the whole name). The refusal decision, its place in the order
  and everything else the call does are the same.
- When a setter fails after its write ran (DC_E_FAILED: only an allocation failure reaches that),
  the writer's three attributes are still set from that write before the error is raised, as
  Python's ``_write_rail_records`` had already set them.
- ``set_hold_record`` accepts None (or an empty dict) or a dict whose ``rail`` is a plain int and
  whose ``written_uv`` / ``prior_uv`` map field names to int32 values; anything else raises.
"""

from __future__ import annotations

import ctypes
import threading

from . import native
from .native import DC_OK, NativeUnavailable

CONTROL, ABS = 0, 1
FUNCTIONS = {"VoltRailsCtlGet": CONTROL, "VoltRailsAbs": ABS}
_NAMES = {v: k for k, v in FUNCTIONS.items()}
RAILS = 2
BLOCK_SIZE = 8192
VOLT_LIMIT_FIELDS = ("reliability", "alt_reliability", "overvoltage", "vmin")
REFERENCE_KIND = "first-read estimate (driver status is quantized)"
LIVE_RAIL_FIELDS = ("type", "live", "reliability", "alt_reliability", "overvoltage", "effective", "vmin")


class _BlockSet(ctypes.Structure):  # dc_rails_block_set
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32),
                ("blocks", (ctypes.c_uint8 * BLOCK_SIZE) * RAILS)]


class _Delta(ctypes.Structure):  # dc_rails_delta
    _fields_ = [("type", ctypes.c_int32), ("reserved", ctypes.c_int32)] + [
        (name, ctypes.c_double) for name in VOLT_LIMIT_FIELDS]


class _DeltaSet(ctypes.Structure):  # dc_rails_delta_set
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("rails", _Delta * RAILS)]


class _StateRow(ctypes.Structure):  # dc_rails_state_row
    _fields_ = [("type", ctypes.c_uint32), ("reserved", ctypes.c_uint32), ("boost_mv", ctypes.c_double)] + [
        (name, ctypes.c_double) for name in LIVE_RAIL_FIELDS[1:]]


class _StateSet(ctypes.Structure):  # dc_rails_state_set
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("rails", _StateRow * RAILS)]


class _Diagnostic(ctypes.Structure):  # dc_rails_diagnostic
    _fields_ = [("present", ctypes.c_int32), ("version", ctypes.c_uint32), ("status", ctypes.c_int32),
                ("valid", ctypes.c_int32), ("attempts", ctypes.c_int32)]


class _LimitRow(ctypes.Structure):  # dc_rails_limit_row
    _fields_ = [("type", ctypes.c_int32), ("has_base", ctypes.c_int32), ("fields", ctypes.c_double * 4),
                ("base", ctypes.c_double * 4), ("absolute_ok", ctypes.c_int32), ("has_effective", ctypes.c_int32),
                ("boost_mv", ctypes.c_double), ("effective_mv", ctypes.c_double)]


class _LimitSet(ctypes.Structure):  # dc_rails_limit_set
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("rails", _LimitRow * RAILS)]


class _Profile(ctypes.Structure):  # dc_rails_profile_set
    _fields_ = [("size", ctypes.c_uint32), ("has_profile", ctypes.c_int32),
                ("has_reference", ctypes.c_int32 * RAILS), ("bases", (ctypes.c_double * 4) * RAILS),
                ("has_initial", ctypes.c_int32 * RAILS), ("initial", (ctypes.c_int32 * 4) * RAILS),
                ("order_count", ctypes.c_uint32), ("order", ctypes.c_uint32 * RAILS),
                ("control_version", ctypes.c_uint32), ("settle_vmin", ctypes.c_int32),
                ("min_mv", ctypes.c_double), ("max_mv", ctypes.c_double)]


class _DiagnosticRail(ctypes.Structure):  # dc_rails_diagnostic_rail
    _fields_ = [("present", ctypes.c_int32), ("available", ctypes.c_int32), ("reason_code", ctypes.c_int32),
                ("reason", ctypes.c_char * 128)]


class _Diagnosis(ctypes.Structure):  # dc_rails_diagnosis
    _fields_ = [("size", ctypes.c_uint32), ("has_architecture", ctypes.c_int32), ("architecture", ctypes.c_uint32),
                ("rails", _DiagnosticRail * RAILS), ("available", ctypes.c_int32), ("reason_code", ctypes.c_int32),
                ("reason", ctypes.c_char * 128), ("has_architecture_source", ctypes.c_int32),
                ("architecture_source", ctypes.c_char * 32)]


class _LimitsMv(ctypes.Structure):  # dc_rails_limits_mv_set
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32), ("mv", (ctypes.c_double * 4) * RAILS)]


class _Caches(ctypes.Structure):  # dc_rails_caches
    _fields_ = [("size", ctypes.c_uint32), ("has_known", ctypes.c_int32 * 2), ("known", ctypes.c_uint32 * 2),
                ("has_retry", (ctypes.c_int32 * RAILS) * 2), ("retry_after", (ctypes.c_double * RAILS) * 2),
                ("diagnostics", (_Diagnostic * RAILS) * 2), ("has_version", ctypes.c_int32 * 2),
                ("version", ctypes.c_uint32 * 2)]


KEY_PLAIN, KEY_INT, KEY_NO_SHIFT = 0, 1, 2  # DC_RAILS_KEY_*


class _Record(ctypes.Structure):  # dc_rails_record
    _fields_ = [("rail", ctypes.c_int32), ("key_kind", ctypes.c_int32), ("count", ctypes.c_uint32),
                ("kind", ctypes.c_int32), ("exact", ctypes.c_int32), ("reserved", ctypes.c_int32),
                ("values", ctypes.c_int64 * 4)]


class _Records(ctypes.Structure):  # dc_rails_records
    _fields_ = [("size", ctypes.c_uint32), ("count", ctypes.c_uint32), ("foreign", ctypes.c_int32),
                ("reserved", ctypes.c_int32), ("records", _Record * RAILS)]


class _WriteOutcome(ctypes.Structure):  # dc_rails_write_outcome
    _fields_ = [("size", ctypes.c_uint32), ("ok", ctypes.c_int32), ("has_status", ctypes.c_int32),
                ("status", ctypes.c_uint32), ("attributes", ctypes.c_int32), ("writes", ctypes.c_uint32),
                ("type_error", ctypes.c_int32), ("reserved", ctypes.c_int32),
                ("result", native._RailWriteResult)]


class _VerifyResult(ctypes.Structure):  # dc_rails_verify_result
    _fields_ = [("size", ctypes.c_uint32), ("has_back", ctypes.c_int32), ("error", ctypes.c_char * 256),
                ("back", _LimitSet)]


class _LimitField(ctypes.Structure):  # dc_rails_limit_field
    _fields_ = [("passed", ctypes.c_int32), ("has_value", ctypes.c_int32), ("value_mv", ctypes.c_double)]


class _LimitRequest(ctypes.Structure):  # dc_rails_limit_request
    _fields_ = [("size", ctypes.c_uint32), ("count", ctypes.c_uint32), ("order", ctypes.c_uint32 * 4),
                ("fields", _LimitField * 4), ("has_unknown", ctypes.c_int32), ("unknown_length", ctypes.c_uint32),
                ("unknown", ctypes.c_char_p)]


class _RawRequest(ctypes.Structure):  # dc_rails_raw_request
    _fields_ = [("size", ctypes.c_uint32), ("count", ctypes.c_uint32), ("order", ctypes.c_uint32 * 4),
                ("passed", ctypes.c_int32 * 4), ("uv", ctypes.c_int32 * 4)]


class _ResetRequest(ctypes.Structure):  # dc_rails_reset_request
    _fields_ = [("size", ctypes.c_uint32), ("has_fields", ctypes.c_int32), ("field_mask", ctypes.c_uint32),
                ("has_bad", ctypes.c_int32), ("bad", ctypes.c_char_p), ("bad_length", ctypes.c_uint32),
                ("has_joined", ctypes.c_int32), ("joined", ctypes.c_char_p), ("joined_length", ctypes.c_uint32),
                ("reserved", ctypes.c_int32)]


class _SetterWrites(ctypes.Structure):  # dc_rails_setter_writes
    _fields_ = [("size", ctypes.c_uint32), ("calls", ctypes.c_uint32), ("attributes", ctypes.c_int32),
                ("last_attributes", ctypes.c_int32), ("result", native._RailWriteResult)]


class _HoldRecord(ctypes.Structure):  # dc_rails_hold_record
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_int32), ("rail", ctypes.c_int32),
                ("has_written", ctypes.c_int32 * 4), ("written_uv", ctypes.c_int32 * 4),
                ("has_prior", ctypes.c_int32 * 4), ("prior_uv", ctypes.c_int32 * 4)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_rails_blocks": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32, _P(_BlockSet)]),
    "dc_rails_deltas": (ctypes.c_int32, [ctypes.c_void_p, _P(_DeltaSet)]),
    "dc_rails_state": (ctypes.c_int32, [ctypes.c_void_p, _P(_StateSet)]),
    "dc_rails_live_mv": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(ctypes.c_int32),
                                          _P(ctypes.c_double)]),
    "dc_rails_caches_get": (ctypes.c_int32, [ctypes.c_void_p, _P(_Caches)]),
    "dc_rails_refresh": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_rails_limits": (ctypes.c_int32, [ctypes.c_void_p, _P(_LimitSet)]),
    "dc_rails_profile": (ctypes.c_int32, [ctypes.c_void_p, _P(_Profile)]),
    "dc_rails_diagnostics": (ctypes.c_int32, [ctypes.c_void_p, _P(_Diagnosis)]),
    "dc_rails_limits_supported": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                                                   _P(ctypes.c_int32)]),
    "dc_rails_limit_fields": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(ctypes.c_uint32)]),
    "dc_rails_stock_limit_mv": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                                                 _P(ctypes.c_int32), _P(ctypes.c_double)]),
    "dc_rails_limits_mv": (ctypes.c_int32, [ctypes.c_void_p, _P(_LimitsMv)]),
    "dc_rails_write_records": (ctypes.c_int32, [ctypes.c_void_p, _P(_Records), _P(_WriteOutcome)]),
    "dc_rails_verify_records": (ctypes.c_int32, [ctypes.c_void_p, _P(_Records), ctypes.c_int32, ctypes.c_int32,
                                                 _P(_VerifyResult)]),
    "dc_rails_set_limits": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(_LimitRequest), ctypes.c_int32,
                                             ctypes.c_int32, _P(native._Outcome), _P(_SetterWrites)]),
    "dc_rails_set_limits_raw": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(_RawRequest), ctypes.c_int32,
                                                 ctypes.c_int32, _P(native._Outcome), _P(_SetterWrites)]),
    "dc_rails_reset_limits": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, _P(_ResetRequest),
                                               _P(native._Outcome), _P(_SetterWrites)]),
    "dc_rails_hold_record_get": (ctypes.c_int32, [ctypes.c_void_p, _P(_HoldRecord)]),
    "dc_rails_hold_record_set": (ctypes.c_int32, [ctypes.c_void_p, _P(_HoldRecord)]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the rail entry points bound; raises NativeUnavailable."""
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
            raise NativeUnavailable(f"native core has no voltage-rail getters: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


def _check(status):
    if status != DC_OK:
        native._raise(status)


def _new(struct):
    value = struct()
    value.size = ctypes.sizeof(struct)
    return value


def read_volt_rail_blocks(gpu, function, version):
    """GPU._read_volt_rail_blocks(function, version): {rail: the 8192 bytes the getter filled}."""
    if function not in FUNCTIONS:
        raise ValueError(f"unknown rail getter {function!r}")
    if not isinstance(version, int):
        raise TypeError(f"rail getter version must be an int, not {type(version).__name__}")
    if not 0 <= version <= 0xFFFFFFFF:
        raise ValueError(f"rail getter version {version!r} is outside 0..0xFFFFFFFF")
    out = _new(_BlockSet)
    _check(load().dc_rails_blocks(gpu._handle, FUNCTIONS[function], int(version), ctypes.byref(out)))
    return {rail: bytes(out.blocks[rail]) for rail in range(RAILS) if out.present >> rail & 1}


def read_volt_rail_deltas(gpu):
    """GPU._read_volt_rail_deltas(): {rail: {"type", *VOLT_LIMIT_FIELDS}}."""
    out = _new(_DeltaSet)
    _check(load().dc_rails_deltas(gpu._handle, ctypes.byref(out)))
    rows = {}
    for rail in range(RAILS):
        if out.present >> rail & 1:
            row = out.rails[rail]
            rows[rail] = dict(type=row.type, **{key: getattr(row, key) for key in VOLT_LIMIT_FIELDS})
    return rows


def read_volt_rail_state(gpu):
    """GPU.read_volt_rail_state(): {rail: {"_boost_mv", *LIVE_RAIL_FIELDS}} or None."""
    out = _new(_StateSet)
    _check(load().dc_rails_state(gpu._handle, ctypes.byref(out)))
    rows = {}
    for rail in range(RAILS):
        if out.present >> rail & 1:
            row = out.rails[rail]
            rec = {"_boost_mv": row.boost_mv}
            for key in LIVE_RAIL_FIELDS:
                rec[key] = getattr(row, key)
            rows[rail] = rec
    return rows or None


def read_rail_live_mv(gpu, rail):
    """GPU.read_rail_live_mv(rail): one rail's live millivolts, or None.

    The rail is looked up as Python's dict does (1.0 and True find rail 1; 1.5, "1" or 2**32 + 1
    find nothing, and the state is still read). An unhashable rail raises TypeError before the read;
    Python raises it after the read."""
    index = {0: 0, 1: 1}.get(rail, -1)  # only 0, 1 or -1 reach the c_int32 argument
    has, mv = ctypes.c_int32(), ctypes.c_double()
    _check(load().dc_rails_live_mv(gpu._handle, index, ctypes.byref(has), ctypes.byref(mv)))
    return mv.value if has.value else None


def caches(gpu):
    """The four rail caches, keyed by their GPU attribute names, in Python's shapes."""
    c = _new(_Caches)
    _check(load().dc_rails_caches_get(gpu._handle, ctypes.byref(c)))
    masks, retry, diagnostics, versions = {}, {}, {}, {}
    for f in (CONTROL, ABS):
        name = _NAMES[f]
        if c.has_known[f]:
            masks[name] = tuple(r for r in range(RAILS) if c.known[f] >> r & 1)
        if c.has_version[f]:
            versions[name] = c.version[f]
        for rail in range(RAILS):
            if c.has_retry[f][rail]:
                retry[(name, rail)] = c.retry_after[f][rail]
            d = c.diagnostics[f][rail]
            if d.present:
                diagnostics.setdefault(name, {})[rail] = {
                    "version": f"0x{d.version:08X}", "status": d.status, "valid": bool(d.valid),
                    "attempts": d.attempts, "error": None}
    return {"_volt_rail_masks_cache": masks, "_volt_rail_retry_after": retry,
            "_volt_rail_getter_diagnostics": diagnostics, "_volt_rail_read_versions": versions}


def refresh(gpu):
    """The four-cache reset of GPU._refresh_volt_rail_capabilities."""
    _check(load().dc_rails_refresh(gpu._handle))


# ---- increment R3: the reference capture and the readers over it ----------------------------- #

def _rail(rail):
    """The ABI's rail for a Python rail key: 0 or 1 when the key equals one (dict lookup), else -1.
    An unhashable key raises TypeError here, before any read (Python raises it after its reads)."""
    return {0: 0, 1: 1}.get(rail, -1)


def read_volt_rail_limits(gpu):
    """GPU.read_volt_rail_limits(): {rail: row} or None."""
    out = _new(_LimitSet)
    _check(load().dc_rails_limits(gpu._handle, ctypes.byref(out)))
    return _limit_rows(out)


def _limit_rows(out):
    """read_volt_rail_limits' dict from a dc_rails_limit_set (None when no rail is present)."""
    rows = {}
    for rail in range(RAILS):
        if not out.present >> rail & 1:
            continue
        e = out.rails[rail]
        row = {"type": e.type}
        row.update(zip(VOLT_LIMIT_FIELDS, e.fields))
        row["_base_mv"] = dict(zip(VOLT_LIMIT_FIELDS, e.base)) if e.has_base else {}
        row["_headroom_mv"] = None
        row["_absolute_ok"] = bool(e.absolute_ok)
        row["_boost_mv"] = e.boost_mv
        row["_effective_mv"] = e.effective_mv if e.has_effective else None
        row["_reference_kind"] = REFERENCE_KIND
        rows[rail] = row
    return rows or None


def volt_rail_profile(gpu):
    """GPU._volt_rail_profile(): the per-adapter reference and first-read record, or None."""
    p = _new(_Profile)
    _check(load().dc_rails_profile(gpu._handle, ctypes.byref(p)))
    if not p.has_profile:
        return None
    order = [p.order[i] for i in range(min(p.order_count, RAILS))]
    refs = [r for r in order if p.has_reference[r]]
    initial = [r for r in order if p.has_initial[r]]
    initial += [r for r in range(RAILS) if p.has_initial[r] and r not in initial]
    return {"bases": {r: dict(zip(VOLT_LIMIT_FIELDS, p.bases[r])) for r in refs},
            "headroom_mv": {r: None for r in refs},
            "poweron": {r: tuple(p.initial[r]) for r in initial},
            "fields": {r: VOLT_LIMIT_FIELDS for r in refs},
            "control_version": p.control_version,
            "settle_vmin": bool(p.settle_vmin),
            "min_mv": p.min_mv, "max_mv": p.max_mv}


def volt_rail_diagnostics(gpu):
    """GPU.volt_rail_diagnostics(); the writer's keys are None, [] and None (not ported yet)."""
    d = _new(_Diagnosis)
    _check(load().dc_rails_diagnostics(gpu._handle, ctypes.byref(d)))
    rails = {}
    for rail in range(RAILS):
        e = d.rails[rail]
        if e.present:
            rails[rail] = {"available": bool(e.available), "reason": e.reason.decode()}
    identity = getattr(gpu, "static", {})
    return {"available": bool(d.available),
            "reason": d.reason.decode(),
            "architecture": d.architecture if d.has_architecture else None,
            "rails": rails,
            "architecture_source": d.architecture_source.decode() if d.has_architecture_source else None,
            "adapter": {"slot": identity.get("slot"),
                        **{key: identity.get(key) for key in ("name", "driver", "vbios")}},
            "getters": caches(gpu)["_volt_rail_getter_diagnostics"],
            "last_write_error": None,
            "observed_packets": [],
            "write_protocol": None}


def volt_rail_limits_supported(gpu, rail=None):
    """GPU.volt_rail_limits_supported(rail)."""
    has_rail = rail is not None
    index = _rail(rail) if has_rail else 0
    out = ctypes.c_int32()
    _check(load().dc_rails_limits_supported(gpu._handle, 1 if has_rail else 0, index, ctypes.byref(out)))
    return bool(out.value)


def volt_rail_limit_fields(gpu, rail):
    """GPU.volt_rail_limit_fields(rail): VOLT_LIMIT_FIELDS or ()."""
    index = _rail(rail)
    count = ctypes.c_uint32()
    _check(load().dc_rails_limit_fields(gpu._handle, index, ctypes.byref(count)))
    return VOLT_LIMIT_FIELDS if count.value == len(VOLT_LIMIT_FIELDS) else ()


def stock_limit_mv(gpu, rail, key):
    """GPU.stock_limit_mv(rail, key): the estimated first-read value in mV, or None."""
    index = _rail(rail)
    field = VOLT_LIMIT_FIELDS.index(key) if key in VOLT_LIMIT_FIELDS else -1
    has, mv = ctypes.c_int32(), ctypes.c_double()
    _check(load().dc_rails_stock_limit_mv(gpu._handle, index, field, ctypes.byref(has), ctypes.byref(mv)))
    if has.value == 2:
        {}[key]  # profile["bases"][rail][key]: KeyError, or TypeError for an unhashable key
    return mv.value if has.value == 1 else None


def volt_rail_limits_mv(gpu):
    """GPU.volt_rail_limits_mv(): {rail: {field: absolute mV}} (NaN without a reference) or None."""
    out = _new(_LimitsMv)
    _check(load().dc_rails_limits_mv(gpu._handle, ctypes.byref(out)))
    if not out.present:
        return None
    return {rail: dict(zip(VOLT_LIMIT_FIELDS, out.mv[rail])) for rail in range(RAILS) if out.present >> rail & 1}


# ---- increment R4: the rail write sequence and its verification ------------------------------- #

_INT32 = (-(1 << 31), (1 << 31) - 1)
_INT64 = (-(1 << 63), (1 << 63) - 1)


def _plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _comparable(value):
    """A value as the int64 the core compares: its integer when it equals one (clamped to int64,
    which keeps anything outside int32 outside it), else INT64_MIN, which equals no int32."""
    try:
        number = int(value)
        equal = number == value
    except (TypeError, ValueError, OverflowError):
        return _INT64[0]
    if not equal:
        return _INT64[0]
    return min(max(number, _INT64[0]), _INT64[1])


def _key_kind(key):
    """(DC_RAILS_KEY_*, the TypeError `1 << key` raises or None) for a key equal to a rail."""
    if _plain_int(key):
        return KEY_PLAIN, None
    try:
        1 << key
    except TypeError as exc:
        return KEY_NO_SHIFT, exc
    return KEY_INT, None


def _records(records, *, verify=False):
    """(dc_rails_records, the TypeError Python's rail_mask would raise first, or None)."""
    out = _new(_Records)
    shift_error = None
    for key, values in records.items():
        rail = {0: 0, 1: 1}.get(key, -1)
        if rail < 0:
            out.foreign = 1  # the core never inspects a foreign key's value
            continue
        kind, error = _key_kind(key)
        if verify and kind != KEY_PLAIN:
            # Python prints the key itself ("rail True", "rail 1.0"); the core prints the rail number.
            raise TypeError("native rail verification needs plain int rail keys")
        if error is not None and shift_error is None:
            shift_error = error
        count = len(values)  # TypeError for an unsized value, as Python's len() raises it
        is_list = isinstance(values, list)  # any other sequence never equals verify's list
        if verify:
            # The core prints `wanted` itself: lists and tuples of up to four plain ints only.
            if (not isinstance(values, (list, tuple)) or count > 4
                    or any(not _plain_int(v) or not _INT64[0] <= v <= _INT64[1] for v in values)):
                raise TypeError("native rail verification needs a list or tuple of at most four integers")
        values = list(values)
        record = out.records[out.count]
        record.rail = rail
        record.key_kind = kind
        record.count = count
        record.kind = 0 if is_list else 1
        record.exact = 1 if count == 4 and all(
            _plain_int(v) and _INT32[0] <= v <= _INT32[1] for v in values) else 0
        for k, value in enumerate(values[:4]):
            record.values[k] = _comparable(value)
        out.count += 1
    return out, shift_error


def write_rail_records(gpu, records):
    """GPU._write_rail_records(records): (ok, status) and the writer's three attributes on gpu.

    _volt_rail_write_error is cleared to None first; when no locked write got past its
    preconditions the protocol and observed packets are left as they were, otherwise all three come
    from the last locked write that ran (gpu._take_rail_write_result). A key equal to a rail for
    which `1 << key` raises (1.0, a Decimal) raises that TypeError where Python's rail_mask does:
    after the preconditions and the boost read, with nothing written."""
    gpu._volt_rail_write_error = None
    request, shift_error = _records(records)
    out = _new(_WriteOutcome)
    _check(load().dc_rails_write_records(gpu._handle, ctypes.byref(request), ctypes.byref(out)))
    if out.type_error:
        raise TypeError(str(shift_error))
    if out.attributes:
        gpu._take_rail_write_result(out.result)
    return bool(out.ok), (out.status if out.has_status else None)


def verify_rail_records(gpu, records, boost):
    """GPU._verify_rail_records(records, boost): (back, None) or (None, error)."""
    request, _ = _records(records, verify=True)
    if boost is not None and (not _plain_int(boost) or not _INT32[0] <= boost <= _INT32[1]):
        raise TypeError("native rail verification needs an int32 boost or None")
    out = _new(_VerifyResult)
    _check(load().dc_rails_verify_records(gpu._handle, ctypes.byref(request), 0 if boost is None else 1,
                                          0 if boost is None else boost, ctypes.byref(out)))
    if not out.has_back:
        return None, out.error.decode()
    return _limit_rows(out.back), None


# ---- increment R5: the verified setters ------------------------------------------------------- #

VOLT_LIMIT_MIN_MV = 650.0       # DC_RAILS_LIMIT_MIN_MV
VOLT_LIMIT_MAX_MV = 1200.0      # DC_RAILS_LIMIT_MAX_MV
VOLT_LIMIT_XOC_MAX_MV = 1500.0  # DC_RAILS_LIMIT_XOC_MAX_MV
RAIL_NAME = {0: "core rail (NVVDD)", 1: "MSVDD"}  # _RAIL_NAME, as the core prints it
TEXT_MAX = 1024                 # DC_RAILS_TEXT_MAX
RAW_REFUSAL = "rail controls must be named signed 32-bit microvolt values"


def _setter_rail(rail):
    """The ABI rail of a setter's rail argument: 0 or 1 for a plain int naming one, -1 for a key that
    names no rail. A key equal to a rail that is not a plain int (True, 1.0) raises TypeError."""
    index = _rail(rail)
    if index >= 0 and not _plain_int(rail):
        raise TypeError("native rail setters need a plain int rail")
    return index


def _text(value):
    """A caller text's bytes: UTF-8 with lone surrogates carried (surrogatepass), NUL bytes kept (the
    core takes the byte length, not strlen)."""
    data = value.encode("utf-8", "surrogatepass")
    if len(data) > TEXT_MAX:
        raise ValueError(f"native rail setters accept texts of at most {TEXT_MAX} bytes")
    return data


def _put_text(request, name, value):
    """Set a present text: has_<name> 1, the pointer and the byte length (an empty text is present)."""
    data = _text(value)
    setattr(request, "has_" + name, 1)
    setattr(request, name, data)
    setattr(request, name + "_length", len(data))


def _message(outcome):
    """dc_outcome.message as the core composed it: surrogatepass undoes _text's encoding of a lone
    surrogate; anything else that does not decode is replaced."""
    try:
        return outcome.message.decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return outcome.message.decode("utf-8", "replace")


def _take_writes(gpu, writes):
    """Leave the writer's three attributes as the setter's _write_rail_records calls left them."""
    if not writes.calls:
        return
    if writes.attributes:
        gpu._take_rail_write_result(writes.result)
    if not writes.last_attributes:
        gpu._volt_rail_write_error = None


def _flags(gpu):
    return (1 if getattr(gpu, "volt_limits_write_enabled", False) else 0,
            1 if getattr(gpu, "voltage_xoc_enabled", False) else 0)


def _finish(gpu, status, outcome, writes):
    # The writer's attributes first: the core fills `writes` even when a step after a write failed,
    # and Python had set them inside _write_rail_records before any later failure.
    _take_writes(gpu, writes)
    _check(status)
    return bool(outcome.ok), _message(outcome)


def set_volt_rail_limits(gpu, rail, limits):
    """GPU.set_volt_rail_limits(rail, **limits): (ok, message)."""
    index = _setter_rail(rail)
    request = _new(_LimitRequest)
    unknown = sorted(set(limits) - set(VOLT_LIMIT_FIELDS))
    if unknown:  # the set, not the text: the keyword "" alone is unknown
        _put_text(request, "unknown", ", ".join(unknown))
    for key, value in limits.items():
        if key not in VOLT_LIMIT_FIELDS:
            continue
        field = VOLT_LIMIT_FIELDS.index(key)
        request.order[request.count] = field
        request.count += 1
        entry = request.fields[field]
        entry.passed = 1
        if value is None or unknown:  # never converted: refused before the bounds
            continue
        if not isinstance(value, (int, float)):
            raise TypeError("native rail setters need int or float millivolts")
        entry.has_value, entry.value_mv = 1, float(value)
    write_enabled, xoc = _flags(gpu)
    outcome, writes = native._Outcome(), _new(_SetterWrites)
    status = load().dc_rails_set_limits(gpu._handle, index, ctypes.byref(request), write_enabled, xoc,
                                        ctypes.byref(outcome), ctypes.byref(writes))
    return _finish(gpu, status, outcome, writes)


def set_volt_rail_limits_raw(gpu, rail, limits_uv):
    """GPU.set_volt_rail_limits_raw(rail, **limits_uv): (ok, message)."""
    if any(k not in VOLT_LIMIT_FIELDS or type(v) is not int
           or not -(1 << 31) <= v < (1 << 31) for k, v in limits_uv.items()):
        return False, RAW_REFUSAL
    index = _setter_rail(rail)
    request = _new(_RawRequest)
    for key, value in limits_uv.items():
        field = VOLT_LIMIT_FIELDS.index(key)
        request.order[request.count] = field
        request.count += 1
        request.passed[field], request.uv[field] = 1, value
    write_enabled, xoc = _flags(gpu)
    outcome, writes = native._Outcome(), _new(_SetterWrites)
    status = load().dc_rails_set_limits_raw(gpu._handle, index, ctypes.byref(request), write_enabled, xoc,
                                            ctypes.byref(outcome), ctypes.byref(writes))
    return _finish(gpu, status, outcome, writes)


def reset_volt_rail_limits(gpu, rail=None, fields=None):
    """GPU.reset_volt_rail_limits(rail, fields): (ok, message)."""
    request = _new(_ResetRequest)
    if fields:
        keys = tuple(fields)
        request.has_fields = 1
        bad = set(keys) - set(VOLT_LIMIT_FIELDS)
        if bad:  # the set, not the text: the key "" alone is bad
            _put_text(request, "bad", ", ".join(sorted(bad)))
        else:
            _put_text(request, "joined", ", ".join(keys))
        for key in keys:
            if key in VOLT_LIMIT_FIELDS:
                request.field_mask |= 1 << VOLT_LIMIT_FIELDS.index(key)
    has_rail = rail is not None
    index = _setter_rail(rail) if has_rail else 0
    outcome, writes = native._Outcome(), _new(_SetterWrites)
    status = load().dc_rails_reset_limits(gpu._handle, 1 if has_rail else 0, index, ctypes.byref(request),
                                          ctypes.byref(outcome), ctypes.byref(writes))
    return _finish(gpu, status, outcome, writes)


def hold_record(gpu):
    """The core's _hold_headroom: None or {"rail", "written_uv", "prior_uv"} (fields in
    VOLT_LIMIT_FIELDS order)."""
    out = _new(_HoldRecord)
    _check(load().dc_rails_hold_record_get(gpu._handle, ctypes.byref(out)))
    if not out.present:
        return None
    return {"rail": out.rail,
            "written_uv": {k: out.written_uv[i] for i, k in enumerate(VOLT_LIMIT_FIELDS) if out.has_written[i]},
            "prior_uv": {k: out.prior_uv[i] for i, k in enumerate(VOLT_LIMIT_FIELDS) if out.has_prior[i]}}


def set_hold_record(gpu, record):
    """Set the core's _hold_headroom from None (or {}) or a dict with the keys hold_record returns."""
    value = _new(_HoldRecord)
    if record:
        rail = record["rail"]
        if not _plain_int(rail) or not _INT32[0] <= rail <= _INT32[1]:
            raise TypeError("the hold record's rail must be a plain int32")
        value.present, value.rail = 1, rail
        for name, has, values in (("written_uv", value.has_written, value.written_uv),
                                  ("prior_uv", value.has_prior, value.prior_uv)):
            for key, uv in (record.get(name) or {}).items():
                if key not in VOLT_LIMIT_FIELDS or not _plain_int(uv) or not _INT32[0] <= uv <= _INT32[1]:
                    raise TypeError(f"the hold record's {name} must map field names to int32 values")
                index = VOLT_LIMIT_FIELDS.index(key)
                has[index], values[index] = 1, uv
    _check(load().dc_rails_hold_record_set(gpu._handle, ctypes.byref(value)))
