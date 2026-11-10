# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The legacy P0 hold and the verification holds of the native core (drutacore/legacy_p0.h and the
V/F lock recovery accessors of drutacore.h), increment G2.

On a ``druta.native.Gpu``: ``legacy_p0_supported(gpu)``, ``legacy_p0_owned(gpu)``,
``hold_legacy_p0(gpu)`` and ``release_legacy_p0(gpu)`` are the ``nvbackend.GPU`` methods of the same
names (the last two return ``(ok, message)``); ``verification_legacy_p0(gpu)`` and
``verification_p0(gpu, voltage_mv=None)`` are the context managers, with Python's control flow and
``RuntimeError`` texts; ``vf_lock_recovery(gpu)`` is ``GPU._vf_lock_recovery`` in Python's dict shape
(``{"domain", "previous": (mode, uv), "requested": (mode, uv)}``, plus ``"verification": True`` only
when a verification hold owns it), or None; ``set_vf_lock_recovery(gpu, record)`` replaces it
(None clears). This is a parallel implementation for the parity tests; the Python module stays the
one the backend uses.

``verification_p0`` reads the loaded voltage with ``gpu.read_vcore_mv()`` (dc_gpu_read_vcore_mv)
when ``voltage_mv`` is None. Both context managers serialize their request and restoration on ``gpu._lock``, as Python does on ``GPU._lock``.
On a ``native.Gpu`` that lock is the handle mutex every core entry takes (one lock per card), so each entry
inside a ``with gpu._lock`` block only adds a level of the same recursive mutex. ``hold_legacy_p0(gpu)`` takes
no ``gpu._lock`` of its own: the core takes the handle mutex around the request, each sample and the release
and drops it across the 100 ms waits, where nvbackend's ``_synchronized`` hold keeps its lock throughout; a
caller that needs Python's behaviour wraps the call in ``with gpu._lock`` (native/README.md).

Not reachable natively: Python's ``except Exception`` branches of ``hold_legacy_p0`` /
``release_legacy_p0`` (a ctypes OSError from ForcePstate, an exception from ``read()``). The core's
telemetry read does not throw and a faulting export is not guarded, so those texts never appear.
"""
from __future__ import annotations

import ctypes
import math
import threading
from contextlib import contextmanager

from . import native
from .native import DC_E_UNAVAILABLE, DC_OK, NativeUnavailable

ARCH_KEPLER = 2       # GPU.ARCH_KEPLER
VF_LOCK_DOMAIN = 6    # nvbackend.VF_LOCK_DOMAIN
VF_LOCK_MODE_OFF = 0  # nvbackend.VF_LOCK_MODE_OFF
VF_LOCK_MODE_POINT = 3  # nvbackend.VF_LOCK_MODE_POINT


class _Recovery(ctypes.Structure):  # dc_vf_lock_recovery
    _fields_ = [("size", ctypes.c_uint32), ("pending", ctypes.c_int32), ("domain", ctypes.c_int64),
                ("previous_mode", ctypes.c_uint32), ("previous_uv", ctypes.c_uint32),
                ("requested_mode", ctypes.c_uint32), ("requested_uv", ctypes.c_uint32),
                ("verification", ctypes.c_int32)]


class _PointLock(ctypes.Structure):  # dc_vf_point_lock
    _fields_ = [("domain", ctypes.c_uint32), ("lock_mode", ctypes.c_uint32), ("volt_uv", ctypes.c_uint32),
                ("count", ctypes.c_uint32)]


_P = ctypes.POINTER
_OUTCOME = _P(native._Outcome)
_I32 = _P(ctypes.c_int32)
_SIGNATURES = {
    "dc_legacy_p0_supported": (ctypes.c_int32, [ctypes.c_void_p, _I32]),
    "dc_legacy_p0_owned": (ctypes.c_int32, [ctypes.c_void_p, _I32]),
    "dc_legacy_p0_hold": (ctypes.c_int32, [ctypes.c_void_p, _OUTCOME]),
    "dc_legacy_p0_release": (ctypes.c_int32, [ctypes.c_void_p, _OUTCOME]),
    "dc_legacy_p0_request": (ctypes.c_int32, [ctypes.c_void_p, _I32, _OUTCOME]),
    "dc_gpu_vf_lock_recovery_get": (ctypes.c_int32, [ctypes.c_void_p, _P(_Recovery)]),
    "dc_gpu_vf_lock_recovery_set": (ctypes.c_int32, [ctypes.c_void_p, _P(_Recovery)]),
    "dc_gpu_vf_lock_entry_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, _P(_PointLock)]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the legacy P0 and recovery entry points bound; raises NativeUnavailable."""
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
            raise NativeUnavailable(f"native core has no legacy P0 hold: {exc}") from None
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


def _outcome(outcome):
    return bool(outcome.ok), outcome.message.decode(errors="replace")


def _flag(entry, gpu):
    value = ctypes.c_int32()
    _check(getattr(load(), entry)(gpu._handle, ctypes.byref(value)))
    return bool(value.value)


# ---- the legacy P0 hold ---------------------------------------------------------------------- #

def legacy_p0_supported(gpu):
    """GPU.legacy_p0_supported(): Kepler and Maxwell probe P0 at run time, independent of board ID."""
    return _flag("dc_legacy_p0_supported", gpu)


def legacy_p0_owned(gpu):
    """GPU.legacy_p0_owned(): session ownership, not a claim to read another tuner's force state."""
    return _flag("dc_legacy_p0_owned", gpu)


def hold_legacy_p0(gpu):
    """GPU.hold_legacy_p0(): (ok, message)."""
    outcome = native._Outcome()
    _check(load().dc_legacy_p0_hold(gpu._handle, ctypes.byref(outcome)))
    return _outcome(outcome)


def release_legacy_p0(gpu):
    """GPU.release_legacy_p0(): (ok, message)."""
    outcome = native._Outcome()
    _check(load().dc_legacy_p0_release(gpu._handle, ctypes.byref(outcome)))
    return _outcome(outcome)


def _request_legacy_p0(gpu, took=None):
    """The request half of verification_legacy_p0: (acquired, ok, message).

    took: an optional list; the acquired flag is appended to it whatever the entry's status, before a
    failed status raises, so a caller learns of ownership the core took even when the entry failed
    after taking it (dc_legacy_p0_request writes *acquired on DC_E_FAILED too)."""
    acquired, outcome = ctypes.c_int32(), native._Outcome()
    try:
        status = load().dc_legacy_p0_request(gpu._handle, ctypes.byref(acquired), ctypes.byref(outcome))
    finally:
        if took is not None:
            took.append(bool(acquired.value))
    _check(status)
    return bool(acquired.value), *_outcome(outcome)


# ---- the V/F lock recovery record and entry -------------------------------------------------- #

def vf_lock_recovery(gpu):
    """GPU._vf_lock_recovery in Python's dict shape, or None."""
    r = _Recovery()
    r.size = ctypes.sizeof(_Recovery)
    _check(load().dc_gpu_vf_lock_recovery_get(gpu._handle, ctypes.byref(r)))
    if not r.pending:
        return None
    record = {"domain": r.domain, "previous": (r.previous_mode, r.previous_uv),
              "requested": (r.requested_mode, r.requested_uv)}
    if r.verification:
        record["verification"] = True
    return record


def set_vf_lock_recovery(gpu, record):
    """Replace GPU._vf_lock_recovery with record (None clears); nothing is written to the driver."""
    r = _Recovery()
    r.size = ctypes.sizeof(_Recovery)
    if record is not None:
        r.pending = 1
        r.domain = int(record["domain"])
        r.previous_mode, r.previous_uv = (int(v) for v in record["previous"])
        r.requested_mode, r.requested_uv = (int(v) for v in record["requested"])
        r.verification = 1 if record.get("verification") else 0
    _check(load().dc_gpu_vf_lock_recovery_set(gpu._handle, ctypes.byref(r)))


def _vf_lock_entry(gpu, domain):
    """(lockMode, volt_uV) of domain in the driver's own table, or None (unreadable or absent)."""
    e = _PointLock()
    status = load().dc_gpu_vf_lock_entry_get(gpu._handle, int(domain), ctypes.byref(e))
    if status == DC_E_UNAVAILABLE:
        return None
    _check(status)
    return e.lock_mode, e.volt_uv


# ---- the verification holds ------------------------------------------------------------------ #

@contextmanager
def verification_legacy_p0(gpu):
    """GPU.verification_legacy_p0(): own a temporary Kepler P0 request; the caller confirms
    physical P0. A hold this session already owns is preserved; otherwise return to automatic."""
    if gpu.arch() != ARCH_KEPLER or not legacy_p0_supported(gpu):
        raise RuntimeError("Kepler P0 request is unavailable; verification cannot write")
    took = []
    try:
        with gpu._lock:
            # The core sets ownership before its call: a failed request still owns, and is released,
            # also when the entry itself failed after taking it.
            _, ok, message = _request_legacy_p0(gpu, took)
            if not ok:
                raise RuntimeError(message)

        def check_hold():
            if not legacy_p0_owned(gpu):
                raise RuntimeError("verification's Kepler P0 request was released")
        yield check_hold
    finally:
        if took and took[0]:
            with gpu._lock:
                ok, message = release_legacy_p0(gpu)
            if not ok:
                raise RuntimeError("verification P0 hold RELEASE FAILED: " + message)


@contextmanager
def verification_p0(gpu, voltage_mv=None):
    """GPU.verification_p0(): temporarily hold a loaded V/F point and restore the exact prior target.
    An existing point hold is reused without taking ownership; the caller still confirms P0."""
    if gpu.arch() == ARCH_KEPLER:
        with verification_legacy_p0(gpu) as check_hold:
            yield check_hold
        return
    with gpu._lock:
        if gpu.vf_lock_recovery_pending():
            raise RuntimeError("V/F lock recovery is pending")
        held, error = gpu.read_vf_lock_status()
        if error:
            raise RuntimeError(error)
        pending = None
        if held is None:
            voltage = gpu.read_vcore_mv() if voltage_mv is None else voltage_mv
            points, error = gpu.read_vf_curve()
            if error or not points or voltage is None or not math.isfinite(voltage):
                raise RuntimeError("cannot select a loaded V/F point for verification")
            under = [p for p in points if p["volt_mv"] <= voltage]
            if not under:
                raise RuntimeError("no V/F point at or below the loaded voltage")
            requested = int(round(max(under, key=lambda p: p["volt_mv"])["volt_mv"] * 1000))
            target = _vf_lock_entry(gpu, VF_LOCK_DOMAIN)
            if target is None or target[0] != VF_LOCK_MODE_OFF:
                raise RuntimeError("verification V/F target is unavailable or already in use")
            pending = {"domain": VF_LOCK_DOMAIN, "verification": True,
                       "previous": (int(target[0]), int(target[1])),
                       "requested": (VF_LOCK_MODE_POINT, requested)}
            ok, message = gpu.set_vf_lock(requested, domain=VF_LOCK_DOMAIN)
            if not ok:
                record = vf_lock_recovery(gpu)
                if record is not None:
                    record["verification"] = True
                    set_vf_lock_recovery(gpu, record)
                raise RuntimeError(message)
            held = {"domain": VF_LOCK_DOMAIN, "volt_uV": requested}

    def check_hold():
        current, error = gpu.read_vf_lock_status(domain=held["domain"])
        if error or current is None or current["volt_uV"] != held["volt_uV"]:
            raise RuntimeError("verification V/F hold changed or became unreadable")

    try:
        check_hold()
        yield check_hold
    finally:
        with gpu._lock:
            if pending is not None:
                set_vf_lock_recovery(gpu, pending)
                ok, message = gpu.recover_vf_lock()
                if not ok:
                    raise RuntimeError("verification P0 hold RESTORE FAILED: " + message)
