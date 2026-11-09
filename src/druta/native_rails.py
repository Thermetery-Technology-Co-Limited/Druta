# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The voltage-rail getters of the native core (drutacore/rails.h), increment R2.

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
