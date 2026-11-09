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
raises ValueError (Python would look it up on the NVAPI object). A diagnostic's ``"error"`` is
always None: Python records ``str(exc)`` when the getter raises, and a C getter cannot raise. Dict
keys come out in sorted order; Python's insertion order is not tracked (the contents are equal).
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
    out = _new(_BlockSet)
    _check(load().dc_rails_blocks(gpu._handle, FUNCTIONS[function], int(version) & 0xFFFFFFFF,
                                  ctypes.byref(out)))
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
    """GPU.read_rail_live_mv(rail): one rail's live millivolts, or None."""
    has, mv = ctypes.c_int32(), ctypes.c_double()
    _check(load().dc_rails_live_mv(gpu._handle, int(rail), ctypes.byref(has), ctypes.byref(mv)))
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
