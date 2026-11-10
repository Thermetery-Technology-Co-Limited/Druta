# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The voltage-rail getters of the native core (drutacore/rails.h), increments R2 to R6.

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
  ``"bases"``, as Python's does (R6 does not change that).

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
read and set the core's ``_hold_headroom`` (since R6 the fuller record, below), which the setters
adopt into and clear.

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
- ``set_hold_record``: see R6.

Increment R6, the hold-headroom runtime: ``hold_headroom_grain_mv``, ``hold_headroom_architecture``,
``hold_headroom_rail_unavailable``, ``hold_headroom_overwritten``, ``apply_hold_headroom``,
``hold_headroom_live_ok`` and ``restore_hold_headroom`` are the GPU methods of those names, run by
the core on the handle (``_headroom_unread`` is the core's own unread path). ``user_rail_limits``
and ``adopt_foreign_hold_headroom`` stay here, over ``hold_record``, ``read_volt_rail_limits`` and
``dc_rails_initial_get`` / ``_set``. The record (``hold_record`` / ``set_hold_record``) carries
apply's keys in apply's order: ``rail``, ``hold_mv``, ``margin_mv``, ``ceiling_mv``, ``boost_mv``,
``live_offset_mv``, ``grain_mv``, ``input_uv``, ``prior_uv``, ``written_uv`` and
``raised_ceiling_mv``; a record set from outside keeps exactly the keys it was given (a key mask
travels with it). ``hold_state`` / ``set_hold_state`` read and change ``_hold_headroom_grain``,
``_hold_headroom_offset_mv``, ``_hold_headroom_unread`` and ``_hold_headroom_live_trip``, which the
native.Gpu exposes as attributes; ``set_hold_state`` changes only the values it is given, in one
locked core call (a field mask), so assigning one attribute never rewrites the other three. A float
key a record set from outside lacks reads as NaN in the core, as Python's missing key does. All of it
is session state of the handle: the core never persists it, and the crash marker stays with druta.py. ``before_write(record)`` runs as a cdecl callback on
the calling thread while the core holds the handle lock, before the record is kept and before any
SET; it returns PROCEED (1), FALSE (2, the hook returned ``False`` itself), RAISED (3, an Exception,
its text copied into the core's buffer) or ABORT (4, a BaseException, re-raised once the core has
returned; the outcome is then None with ``aborted`` set). No answer is zero: 0 and any unknown
answer are refused as FALSE, so a callback that fails without answering never lets the raise go
ahead without its crash marker.

Divergences of R6:

- Native diagnostics carry no ``"error"`` key (see R2), so the unavailable detail never gets
  Python's ``diag.get("error")`` reason or a getter's " (error)" suffix.
- Python's ``except`` around ``_write_rail_records`` (the "raised" text, and the restore after it)
  is not reached through the real writer: an escape callback that raises is swallowed by ctypes on
  either side. The core has no such branch; the tests show both sides alike.
- ``before_write`` receives a new dict built from the planned record, not Python's shallow copy whose
  nested ``*_uv`` dicts are the record's own (a hook that mutates them changes nothing natively).
- ``float(hold_mv)`` / ``float(margin_mv)`` run before any read: an OverflowError (``10 ** 400``) is
  raised then, where Python raises it after its reads. TypeError / ValueError become the core's
  "must be a number" refusals at Python's point.
- ``rail`` must be a plain int (as the R5 setters): True or 1.0 raises TypeError before any read.
- An exception that ctypes swallows at the hook's entry, before the binding's ``try`` (an
  asynchronous KeyboardInterrupt), is lost where Python would raise it: the callback answers 0, which
  the core refuses as FALSE ("the crash marker could not be written, ...", unread set, nothing kept
  or written), so the caller retries.
- A plan whose written values do not all fit int32 (a corrupt reading near +-2147 V) is refused by
  the writer before any SET on both sides with the same text and no record kept, but natively the
  hook is not called for it: Python calls it first with the true int (its caller's marker is then
  dropped again), where the int32 record would have carried a wrapped value.
- ``hold_headroom_overwritten(limits)`` follows Python's order (no record: False before any row is
  looked at; ``limits[record["rail"]]`` that raises KeyError / TypeError / ValueError, or a row that
  does not support ``in``, is False; other rails' rows are never looked at). The one difference: a
  row of the record's own rail that lacks some ``VOLT_LIMIT_FIELDS`` key raises TypeError (Python
  skips the missing keys); a record rail outside 0 and 1 (possible only through ``set_hold_record``)
  is never overwritten natively.
- ``hold_headroom_live_ok`` converts an explicit record's numbers in Python's order (see the
  function); an unhashable rail is no rail (-1) rather than Python's caught read error, and
  ``live_mv`` is converted with ``float()``.
- ``adopt_foreign_hold_headroom`` skips a field whose prior is not a plain int32 (a corrupt marker):
  that field keeps its first-read value and is not in the returned list, and nothing raises. Python
  stores any prior in the first-read dict (and its later reset is refused by the writer).
- ``set_hold_record`` accepts None (or an empty dict) or a dict with only the keys above, a plain
  int32 ``rail``, numbers for the float keys, None or a number for ``grain_mv`` and field names to
  int32 values in the ``*_uv`` dicts; anything else raises TypeError. ``hold_headroom_record`` returns
  a new dict each call, always in apply's key order (an outside record's own order is not kept).
- The error text a hook's exception carries is NUL-terminated in the core's buffer, so a NUL in it
  shortens the printed text (at most ``HOLD_ERROR_MAX`` - 1 UTF-8 bytes are carried).
- The grain's ``round(x, 2)`` (the pure helpers' ``round_digits``, ``dc_rl_round_digits``) is exact
  half-even below 2**39 and ``nearbyint`` between 2**39 and 2**53 (the grain is bounded far below
  either).
"""

from __future__ import annotations

import ctypes
import math
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
                ("has_prior", ctypes.c_int32 * 4), ("prior_uv", ctypes.c_int32 * 4),
                ("has_input", ctypes.c_int32 * 3), ("input_uv", ctypes.c_int32 * 3),
                ("has_grain", ctypes.c_int32), ("has_raised_ceiling", ctypes.c_int32), ("keys", ctypes.c_uint32),
                ("hold_mv", ctypes.c_double), ("margin_mv", ctypes.c_double), ("ceiling_mv", ctypes.c_double),
                ("boost_mv", ctypes.c_double), ("live_offset_mv", ctypes.c_double), ("grain_mv", ctypes.c_double),
                ("raised_ceiling_mv", ctypes.c_double)]


class _HoldState(ctypes.Structure):  # dc_rails_hold_state
    _fields_ = [("size", ctypes.c_uint32), ("has_grain", ctypes.c_int32), ("has_offset", ctypes.c_int32),
                ("unread", ctypes.c_int32), ("live_trip", ctypes.c_int32), ("reserved", ctypes.c_int32),
                ("grain", ctypes.c_double), ("offset_mv", ctypes.c_double)]


HOLD_MESSAGE_MAX = 8192  # DC_RAILS_HOLD_MESSAGE_MAX
HOLD_ERROR_MAX = 2048    # DC_RAILS_HOLD_ERROR_MAX


class _HoldOutcome(ctypes.Structure):  # dc_rails_hold_outcome
    _fields_ = [("size", ctypes.c_uint32), ("ok", ctypes.c_int32), ("key_error", ctypes.c_int32),
                ("aborted", ctypes.c_int32), ("reserved", ctypes.c_int32),
                ("message", ctypes.c_char * HOLD_MESSAGE_MAX)]


# dc_rails_before_write_fn: cdecl (DC_CALL), int32 (void* context, const record*, char* error, size_t cap)
BEFORE_WRITE = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(_HoldRecord), ctypes.c_void_p,
                                ctypes.c_size_t)


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
    "dc_rails_hold_state_get": (ctypes.c_int32, [ctypes.c_void_p, _P(_HoldState)]),
    "dc_rails_hold_state_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, _P(_HoldState)]),
    "dc_rails_hold_apply": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_uint32,
                                             ctypes.c_int32, ctypes.c_int32, BEFORE_WRITE, ctypes.c_void_p,
                                             _P(_HoldOutcome), _P(_SetterWrites)]),
    "dc_rails_hold_restore": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(_HoldOutcome),
                                               _P(_SetterWrites)]),
    "dc_rails_hold_live_ok": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(_HoldRecord), ctypes.c_int32,
                                               ctypes.c_double, _P(native._Outcome)]),
    "dc_rails_hold_grain_mv": (ctypes.c_int32, [ctypes.c_void_p, _P(ctypes.c_int32), _P(ctypes.c_double)]),
    "dc_rails_hold_architecture": (ctypes.c_int32, [ctypes.c_void_p, _P(ctypes.c_int32)]),
    "dc_rails_hold_rail_unavailable": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(ctypes.c_int32),
                                                        _P(ctypes.c_int32), ctypes.c_char_p, ctypes.c_size_t,
                                                        _P(ctypes.c_size_t)]),
    "dc_rails_hold_overwritten": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(_LimitSet),
                                                   _P(ctypes.c_int32)]),
    "dc_rails_initial_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(ctypes.c_int32),
                                              _P(ctypes.c_int32)]),
    "dc_rails_initial_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, _P(ctypes.c_int32),
                                              _P(ctypes.c_int32)]),
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


# ---- increment R6: the hold-headroom runtime ------------------------------------------------- #

HOLD_HEADROOM_FIELDS = VOLT_LIMIT_FIELDS[:3]
HOLD_HEADROOM_FALLBACK_GRAIN_MV = 6.25
_HOLD_FLOATS = ("hold_mv", "margin_mv", "ceiling_mv", "boost_mv", "live_offset_mv")
# DC_RAILS_HOLD_KEY_*: the record's keys besides "rail" and "raised_ceiling_mv", in apply's order
_HOLD_KEYS = {"hold_mv": 0x001, "margin_mv": 0x002, "ceiling_mv": 0x004, "boost_mv": 0x008,
              "live_offset_mv": 0x010, "grain_mv": 0x020, "input_uv": 0x040, "prior_uv": 0x080,
              "written_uv": 0x100}
_HOLD_DICTS = {"input_uv": ("has_input", "input_uv", HOLD_HEADROOM_FIELDS),
               "prior_uv": ("has_prior", "prior_uv", VOLT_LIMIT_FIELDS),
               "written_uv": ("has_written", "written_uv", VOLT_LIMIT_FIELDS)}
_HOLD_OK = {1: True, 0: None, -1: False}
_KEY_ERRORS = {5: "written_uv", 6: "prior_uv"}  # DC_RAILS_HOLD_KEY_ERROR_*; 1 + k is VOLT_LIMIT_FIELDS[k]
_HOOK_PROCEED, _HOOK_FALSE, _HOOK_RAISED, _HOOK_ABORT = 1, 2, 3, 4  # DC_RAILS_HOLD_HOOK_*: 0 is never proceed
_BAD_HOLD, _BAD_MARGIN = 1, 2


def _record_dict(out):
    """_hold_headroom from a dc_rails_hold_record: None, or the dict with the keys it was given in
    apply_hold_headroom's order (ints for "rail" and the *_uv values, floats otherwise)."""
    if not out.present:
        return None
    record = {"rail": out.rail}
    for key, bit in _HOLD_KEYS.items():
        if not out.keys & bit:
            continue
        if key in _HOLD_FLOATS:
            record[key] = getattr(out, key)
        elif key == "grain_mv":
            record[key] = out.grain_mv if out.has_grain else None
        else:
            has, values, names = _HOLD_DICTS[key]
            has, values = getattr(out, has), getattr(out, values)
            record[key] = {k: values[i] for i, k in enumerate(names) if has[i]}
    if out.has_raised_ceiling:
        record["raised_ceiling_mv"] = out.raised_ceiling_mv
    return record


def hold_record(gpu):
    """GPU.hold_headroom_record(): the core's _hold_headroom, None or a new dict each call."""
    out = _new(_HoldRecord)
    _check(load().dc_rails_hold_record_get(gpu._handle, ctypes.byref(out)))
    return _record_dict(out)


def _number(value, what):
    if not isinstance(value, (int, float)):
        raise TypeError(f"the hold record's {what} must be a number")
    return float(value)


def set_hold_record(gpu, record):
    """Set the core's _hold_headroom from None (or {}) or a dict with keys hold_record returns: "rail"
    (a plain int32), the five floats and "raised_ceiling_mv" (int or float), "grain_mv" (None or a
    number) and the *_uv dicts (field names to int32 values). The keys it has are kept."""
    value = _new(_HoldRecord)
    if record:
        rail = record["rail"]
        if not _plain_int(rail) or not _INT32[0] <= rail <= _INT32[1]:
            raise TypeError("the hold record's rail must be a plain int32")
        value.present, value.rail = 1, rail
        for key in record:
            if key not in _HOLD_KEYS and key not in ("rail", "raised_ceiling_mv"):
                raise TypeError(f"the hold record has no key {key!r} natively")
        for key, bit in _HOLD_KEYS.items():
            if key not in record:
                continue
            value.keys |= bit
            item = record[key]
            if key in _HOLD_FLOATS:
                setattr(value, key, _number(item, key))
            elif key == "grain_mv":
                if item is not None:
                    value.has_grain, value.grain_mv = 1, _number(item, key)
            else:
                has, values, names = _HOLD_DICTS[key]
                has, values = getattr(value, has), getattr(value, values)
                for name, uv in (item or {}).items():
                    if name not in names or not _plain_int(uv) or not _INT32[0] <= uv <= _INT32[1]:
                        raise TypeError(f"the hold record's {key} must map field names to int32 values")
                    index = names.index(name)
                    has[index], values[index] = 1, uv
        if "raised_ceiling_mv" in record:
            value.has_raised_ceiling = 1
            value.raised_ceiling_mv = _number(record["raised_ceiling_mv"], "raised_ceiling_mv")
    _check(load().dc_rails_hold_record_set(gpu._handle, ctypes.byref(value)))


def hold_state(gpu):
    """The session state besides the record: {"_hold_headroom_grain", "_hold_headroom_offset_mv",
    "_hold_headroom_unread", "_hold_headroom_live_trip"} (None / False when never set)."""
    out = _new(_HoldState)
    _check(load().dc_rails_hold_state_get(gpu._handle, ctypes.byref(out)))
    return {"_hold_headroom_grain": out.grain if out.has_grain else None,
            "_hold_headroom_offset_mv": out.offset_mv if out.has_offset else None,
            "_hold_headroom_unread": bool(out.unread),
            "_hold_headroom_live_trip": bool(out.live_trip)}


_STATE_BITS = {"_hold_headroom_grain": 0x1, "_hold_headroom_offset_mv": 0x2, "_hold_headroom_unread": 0x4,
               "_hold_headroom_live_trip": 0x8}  # DC_RAILS_HOLD_STATE_*


def set_hold_state(gpu, **changes):
    """Change some of hold_state's four values (the grain and offset: None or a number; the flags are
    stored as bool()). Only the named values change, in one locked core call: nothing is read first,
    so another thread's change to the others is never written back over."""
    unknown = set(changes) - set(_STATE_BITS)
    if unknown:
        raise TypeError(f"unknown hold-headroom state {sorted(unknown)!r}")
    value, mask = _new(_HoldState), 0
    for name in changes:
        mask |= _STATE_BITS[name]
    grain = changes.get("_hold_headroom_grain")
    offset = changes.get("_hold_headroom_offset_mv")
    if grain is not None:
        value.has_grain, value.grain = 1, _number(grain, "grain")
    if offset is not None:
        value.has_offset, value.offset_mv = 1, _number(offset, "offset")
    value.unread = 1 if changes.get("_hold_headroom_unread") else 0
    value.live_trip = 1 if changes.get("_hold_headroom_live_trip") else 0
    _check(load().dc_rails_hold_state_set(gpu._handle, mask, ctypes.byref(value)))


def hold_headroom_grain_mv(gpu):
    """GPU.hold_headroom_grain_mv(): this card's modal V/F voltage step (bounded), or None."""
    has, grain = ctypes.c_int32(), ctypes.c_double()
    _check(load().dc_rails_hold_grain_mv(gpu._handle, ctypes.byref(has), ctypes.byref(grain)))
    return grain.value if has.value else None


def hold_headroom_architecture(gpu):
    """GPU.hold_headroom_architecture(): True, False, or None when arch() could not be read."""
    supported = ctypes.c_int32()
    _check(load().dc_rails_hold_architecture(gpu._handle, ctypes.byref(supported)))
    return {1: True, 0: False}.get(supported.value)


def hold_headroom_rail_unavailable(gpu, rail=0):
    """GPU.hold_headroom_rail_unavailable(rail): None, or ("transient" | "unanswered", detail)."""
    index = _rail(rail)
    unavailable, kind, needed = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_size_t()
    detail = ctypes.create_string_buffer(4096)
    _check(load().dc_rails_hold_rail_unavailable(gpu._handle, index, ctypes.byref(unavailable), ctypes.byref(kind),
                                                 detail, len(detail), ctypes.byref(needed)))
    if not unavailable.value:
        return None
    text = detail.raw[:min(needed.value, len(detail) - 1)]
    if needed.value >= len(detail):  # cannot happen with the texts the core composes; read it whole
        detail = ctypes.create_string_buffer(needed.value + 1)
        _check(load().dc_rails_hold_rail_unavailable(gpu._handle, index, ctypes.byref(unavailable),
                                                     ctypes.byref(kind), detail, len(detail), ctypes.byref(needed)))
        text = detail.raw[:needed.value]
    return ("unanswered" if kind.value == 1 else "transient"), text.decode("utf-8", "replace")


def _limit_row(rail, row):
    """The record's rail's row of a caller's limits as a dc_rails_limit_set holding that row only (a
    non-number field is NaN, which hold_headroom_overwritten answers False for, as Python does)."""
    if not all(k in row for k in VOLT_LIMIT_FIELDS):
        raise TypeError("native hold_headroom_overwritten needs every VOLT_LIMIT_FIELDS key in the record's row")
    value = _new(_LimitSet)
    if 0 <= rail < RAILS:
        value.present |= 1 << rail
        e = value.rails[rail]
        for i, k in enumerate(VOLT_LIMIT_FIELDS):
            item = row[k]
            e.fields[i] = float(item) if isinstance(item, (int, float)) else float("nan")
    return value


def hold_headroom_overwritten(gpu, limits=None):
    """GPU.hold_headroom_overwritten(limits): True when the plan's inputs changed under the raise.
    In Python's order: no record is False before any row is looked at; limits[record["rail"]] that
    raises KeyError / TypeError / ValueError, or a row that does not support `in` (None), is False;
    rows of other rails are never looked at. Only the record's rail's row reaches the core."""
    rows = None
    if limits is not None:
        record = hold_record(gpu)
        if not record:
            return False
        try:
            row = limits[record["rail"]]
            for k in VOLT_LIMIT_FIELDS:
                k in row  # noqa: B015 - Python's `if k in row` raises TypeError for None
        except (KeyError, TypeError, ValueError):
            return False
        rows = _limit_row(record["rail"], row)
    out = ctypes.c_int32()
    _check(load().dc_rails_hold_overwritten(gpu._handle, 1 if rows is not None else 0,
                                            ctypes.byref(rows) if rows is not None else None, ctypes.byref(out)))
    if out.value == 2:
        raise OverflowError("cannot convert float infinity to integer")
    return bool(out.value)


def _float_or_nan(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


_NO_ALLOWANCE = ("no active raise", "no bound to check against", "no live rail reading")


def hold_headroom_live_ok(gpu, record=None, live_mv=None):
    """GPU.hold_headroom_live_ok(record, live_mv): (ok, message). An explicit record's numbers are
    converted here in Python's order: raised_ceiling_mv first (hold_headroom_bound_mv); hold_mv and
    margin_mv only when that is not finite; grain_mv and live_offset_mv in hold_headroom_allowance_mv's
    one try, whose OverflowError is raised only once the core has consulted the allowance."""
    value, has_live, live, pending = None, 0, 0.0, None
    nan = float("nan")
    if record:
        value = _new(_HoldRecord)
        value.present, value.keys = 1, 0x1FF
        bound = _float_or_nan(record.get("raised_ceiling_mv"))  # OverflowError propagates, as in Python
        if "raised_ceiling_mv" in record:
            value.has_raised_ceiling, value.raised_ceiling_mv = 1, bound
        hold = margin = nan
        if not math.isfinite(bound):
            try:
                hold = float(record["hold_mv"])
                margin = float(record["margin_mv"])
            except (KeyError, TypeError, ValueError):
                hold = margin = nan
        value.hold_mv, value.margin_mv = hold, margin
        try:
            grain = float(record.get("grain_mv") or HOLD_HEADROOM_FALLBACK_GRAIN_MV)
            offset = float(record.get("live_offset_mv") or 0.0)
        except (TypeError, ValueError):
            grain, offset = HOLD_HEADROOM_FALLBACK_GRAIN_MV, 0.0
        except OverflowError as exc:  # Python raises it only when the allowance is computed
            pending, grain, offset = exc, HOLD_HEADROOM_FALLBACK_GRAIN_MV, 0.0
        value.has_grain, value.grain_mv, value.live_offset_mv = 1, grain, offset
        if "rail" in record:
            try:
                value.rail = _rail(record["rail"])
            except TypeError:  # unhashable: Python's read raises after the state read, then None
                value.rail = -1
        else:
            has_live, live = 1, 0.0  # record["rail"] raises KeyError: no read, no live reading
    if live_mv is not None:
        has_live = 1
        live = float(live_mv) if isinstance(live_mv, (int, float)) else 0.0  # not a number: no reading
    outcome = native._Outcome()
    _check(load().dc_rails_hold_live_ok(gpu._handle, 1 if value is not None else 0,
                                        ctypes.byref(value) if value is not None else None, has_live, live,
                                        ctypes.byref(outcome)))
    message = _message(outcome)
    if pending is not None and message not in _NO_ALLOWANCE:
        raise pending
    return bool(outcome.ok), message


def _utf8_prefix(text, cap):
    """text's UTF-8 bytes (surrogatepass), cut to at most cap bytes at a character boundary."""
    data = text.encode("utf-8", "surrogatepass")
    if len(data) <= cap:
        return data
    data = data[:cap]
    while data and (data[-1] & 0xC0) == 0x80:
        data = data[:-1]
    if data and data[-1] >= 0xC0:  # a lead byte whose continuation was cut
        data = data[:-1]
    return data


def _hold_finish(gpu, status, outcome, writes, failure):
    _take_writes(gpu, writes)
    _check(status)
    if failure:
        raise failure[0]
    if outcome.key_error:
        name = _KEY_ERRORS.get(outcome.key_error) or VOLT_LIMIT_FIELDS[outcome.key_error - 1]
        raise KeyError(name)
    return _HOLD_OK[outcome.ok], _message(outcome)


def apply_hold_headroom(gpu, hold_mv, margin_mv, rail=0, before_write=None):
    """GPU.apply_hold_headroom(hold_mv, margin_mv, rail, before_write): (True | None | False, message).
    before_write(record) runs on this thread while the core holds the handle lock."""
    index = _setter_rail(rail)
    invalid, hold, margin = 0, 0.0, 0.0
    try:
        hold = float(hold_mv)
    except (TypeError, ValueError):
        invalid |= _BAD_HOLD
    try:
        margin = float(margin_mv)
    except (TypeError, ValueError):
        invalid |= _BAD_MARGIN
    failure = []
    callback = None
    if before_write is not None:
        def hook(_context, planned, error, cap):
            try:
                try:
                    tracked = before_write(_record_dict(planned.contents))
                except Exception as exc:                                # noqa: BLE001 - Python's
                    text = _utf8_prefix(str(exc), cap - 1)
                    ctypes.memmove(error, text + b"\0", len(text) + 1)
                    return _HOOK_RAISED
                return _HOOK_FALSE if tracked is False else _HOOK_PROCEED
            except BaseException as exc:                                # noqa: BLE001 - re-raised below
                failure.append(exc)
                return _HOOK_ABORT
        callback = BEFORE_WRITE(hook)
    xoc = 1 if getattr(gpu, "voltage_xoc_enabled", False) else 0
    outcome, writes = _new(_HoldOutcome), _new(_SetterWrites)
    status = load().dc_rails_hold_apply(gpu._handle, hold, margin, invalid, index, xoc,
                                        callback if callback is not None else BEFORE_WRITE(),
                                        None, ctypes.byref(outcome), ctypes.byref(writes))
    return _hold_finish(gpu, status, outcome, writes, failure)


def restore_hold_headroom(gpu, force=False):
    """GPU.restore_hold_headroom(force): (True | False, message)."""
    outcome, writes = _new(_HoldOutcome), _new(_SetterWrites)
    status = load().dc_rails_hold_restore(gpu._handle, 1 if force else 0, ctypes.byref(outcome),
                                          ctypes.byref(writes))
    return _hold_finish(gpu, status, outcome, writes, [])


def user_rail_limits(gpu, cur=None):
    """GPU.user_rail_limits(cur): read_volt_rail_limits() with this window's raise (the core's record)
    and another window's (gpu._foreign_hold_headroom) taken out."""
    if cur is None:
        cur = read_volt_rail_limits(gpu)
    records = [r for r in (hold_record(gpu), getattr(gpu, "_foreign_hold_headroom", None))
               if isinstance(r, dict)]
    if cur is None or not records:
        return cur
    out = {r: dict(row) for r, row in cur.items()}
    for record in records:
        row = out.get(record.get("rail"))
        if row is None:
            continue
        for key, written in (record.get("written_uv") or {}).items():
            if key in row and key in (record.get("prior_uv") or {}) \
                    and int(round(row[key] * 1000)) == written:
                row[key] = record["prior_uv"][key] / 1000.0
    return out


def adopt_foreign_hold_headroom(gpu, record):
    """GPU.adopt_foreign_hold_headroom(record): another window's priors in place of its raised values in
    this session's first-read restore target, where they still match. Returns the changed fields."""
    try:
        rail, written, prior = record["rail"], record["written_uv"], record["prior_uv"]
    except (KeyError, TypeError):
        return []
    index = _rail(rail)
    if index < 0:
        return []  # (initial or {}).get(rail) is None
    has, current = ctypes.c_int32(), (ctypes.c_int32 * 4)()
    _check(load().dc_rails_initial_get(gpu._handle, index, ctypes.byref(has), current))
    if not has.value:
        return []
    values, changed = list(current), []
    for key, uv in written.items():
        if key in VOLT_LIMIT_FIELDS and key in prior:
            i = VOLT_LIMIT_FIELDS.index(key)
            value = prior[key]
            # a prior the first-read record cannot hold (not a plain int32: a corrupt marker) is not
            # adopted, and nothing raises; Python would store it and refuse its later reset
            if values[i] == uv and _plain_int(value) and _INT32[0] <= value <= _INT32[1]:
                values[i] = value
                changed.append(key)
    if changed:
        replaced = ctypes.c_int32()
        _check(load().dc_rails_initial_set(gpu._handle, index, (ctypes.c_int32 * 4)(*values),
                                           ctypes.byref(replaced)))
    return changed
