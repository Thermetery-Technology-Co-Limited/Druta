# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pure V/F curve planners of the native core (drutacore/vfplan.h).

Each function here has the signature, return shape and exceptions of the
``nvbackend.GPU`` static method of the same name, so the same cases run
against both. The core computes; this module only converts. Values the Python
returns straight from a point (``idx``, ``volt_mv``, a peak ``freq_mhz``, the
resolved point itself) are echoed from the caller's own objects by position,
so their types and identity survive the trip.

Where the Python raises, the core returns a status and this module re-runs the
same failing Python operation (``int(nan)``, ``x // 0``, ``None + 0.01``...),
so the exception type and message are the running interpreter's own.

Inputs the core cannot represent are refused here rather than approximated:
more than 128 points or deltas (ValueError), integers beyond +-2**53
(OverflowError), point fields or scalars that are not int, float (or None where
the Python accepts None), a non-int ``delta_khz`` (TypeError), and NaN voltages
that Python would have to sort (ValueError: Python's order there is an artefact
of its sort algorithm).
"""
from __future__ import annotations

import ctypes
import threading

from . import native
from .native import DC_OK, DC_E_UNAVAILABLE, NativeUnavailable

VFP_POINTS = 128
INT_LIMIT = 1 << 53

DC_E_VFP_FLOORDIV_ZERO = -100
DC_E_VFP_MOD_ZERO = -101
DC_E_VFP_NAN_INT = -102
DC_E_VFP_INF_INT = -103
DC_E_VFP_NONE_INT = -104
DC_E_VFP_NONE_ADD = -105
DC_E_VFP_NONE_SUB = -106
DC_E_VFP_EMPTY_INDEX = -107
DC_E_VFP_EMPTY_MIN = -108
DC_E_VFP_FLOAT_SLICE = -109
DC_E_VFP_UNORDERED = -110
DC_E_VFP_BIG_INT = -111
DC_E_VFP_MISSING = -112

_NONE, _INT, _FLOAT = 0, 1, 2


class _Num(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_int32), ("reserved", ctypes.c_int32), ("i", ctypes.c_int64),
                ("f", ctypes.c_double)]


class _VfPoint(ctypes.Structure):  # dc_vf_point
    _fields_ = [("idx", ctypes.c_uint32), ("volt_mv", ctypes.c_double), ("freq_mhz", ctypes.c_double),
                ("delta_khz", ctypes.c_int32)]


class _Change(ctypes.Structure):
    _fields_ = [("pos", ctypes.c_uint32), ("idx", ctypes.c_uint32), ("volt_mv", ctypes.c_double),
                ("old_mhz", ctypes.c_double), ("new_mhz", ctypes.c_double), ("delta_khz", _Num)]


class _Peak(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("found", ctypes.c_int32), ("peak_pos", ctypes.c_uint32),
                ("park_pos", ctypes.c_uint32), ("park_idx", ctypes.c_uint32), ("n_at_peak", ctypes.c_uint32),
                ("peak_mhz", ctypes.c_double), ("park_mv", ctypes.c_double)]


class _Deflatten(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("change_count", ctypes.c_uint32), ("changes", _Change * VFP_POINTS),
                ("ceil_before_mhz", ctypes.c_double), ("ceil_after_mhz", ctypes.c_double),
                ("full_meta", ctypes.c_int32), ("clamped", ctypes.c_int32), ("boundary_pos", ctypes.c_uint32),
                ("boundary_idx", ctypes.c_uint32), ("unique", ctypes.c_int32), ("reserved", ctypes.c_int32),
                ("lowered", ctypes.c_int64), ("lowered_by_mhz", ctypes.c_double)]


class _CascadeRow(ctypes.Structure):
    _fields_ = [("real", _Num), ("khz", _Num), ("has_volt", ctypes.c_int32), ("reserved", ctypes.c_int32),
                ("volt_mv", ctypes.c_double)]


class _Cascade(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("lifted_below", ctypes.c_uint32), ("lift_max_mhz", ctypes.c_double),
                ("has_lowest", ctypes.c_int32), ("lowest_row", ctypes.c_uint32), ("lift_lowest_mv", ctypes.c_double),
                ("missing_row", ctypes.c_uint32), ("reserved", ctypes.c_uint32)]


class _Hard(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("change_count", ctypes.c_uint32), ("changes", _Change * VFP_POINTS),
                ("floor_before_mhz", ctypes.c_double), ("target_after_mhz", ctypes.c_double),
                ("target_mhz", ctypes.c_double), ("has_band", ctypes.c_int32), ("n_flat", ctypes.c_uint32),
                ("floor_pos", ctypes.c_uint32), ("floor_idx", ctypes.c_uint32), ("floor_mv", ctypes.c_double),
                ("park_pos", ctypes.c_uint32), ("park_idx", ctypes.c_uint32), ("park_mv", ctypes.c_double),
                ("park_mhz", ctypes.c_double), ("parks_at_floor", ctypes.c_int32),
                ("lifted_below", ctypes.c_uint32), ("lift_max_mhz", ctypes.c_double),
                ("has_lift_lowest", ctypes.c_int32), ("lift_lowest_pos", ctypes.c_uint32),
                ("lift_lowest_mv", ctypes.c_double)]


class _Ramp(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("change_count", ctypes.c_uint32), ("changes", _Change * VFP_POINTS),
                ("ceil_before_mhz", ctypes.c_double), ("ceil_after_mhz", ctypes.c_double),
                ("has_band", ctypes.c_int32), ("clamped", ctypes.c_int32), ("unique", ctypes.c_int32),
                ("boundary_pos", ctypes.c_uint32), ("boundary_idx", ctypes.c_uint32), ("lo_pos", ctypes.c_uint32),
                ("lo_idx", ctypes.c_uint32), ("rungs", ctypes.c_uint32), ("lo_mv", ctypes.c_double),
                ("cap_mv", ctypes.c_double), ("top_mhz", ctypes.c_double), ("floor_before_mhz", ctypes.c_double),
                ("floor_after_mhz", ctypes.c_double), ("floor_cost_mhz", ctypes.c_double),
                ("leveled_above", ctypes.c_uint32), ("has_under_band", ctypes.c_int32),
                ("under_band_mhz", ctypes.c_double), ("shadowed", ctypes.c_uint32), ("delivered", ctypes.c_uint32),
                ("lifted_below", ctypes.c_uint32), ("has_lift_lowest", ctypes.c_int32),
                ("lift_max_mhz", ctypes.c_double), ("lift_lowest_pos", ctypes.c_uint32),
                ("dropped_rungs", ctypes.c_uint32), ("lift_lowest_mv", ctypes.c_double),
                ("dropped_reason", ctypes.c_char * 64)]


class _RephaseChange(ctypes.Structure):
    _fields_ = [("pos", ctypes.c_uint32), ("reserved", ctypes.c_uint32), ("delta_khz", ctypes.c_int64)]


class _Rephase(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("change_count", ctypes.c_uint32), ("phase", ctypes.c_int64),
                ("changes", _RephaseChange * VFP_POINTS)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_vfp_status_text": (ctypes.c_char_p, [ctypes.c_int32]),
    "dc_vfp_peak_info": (ctypes.c_int32, [_P(_VfPoint), ctypes.c_size_t, _P(_Num), _P(_Peak)]),
    "dc_vfp_resolve_vf_point": (ctypes.c_int32, [_P(_VfPoint), ctypes.c_size_t, _P(_Num), _P(ctypes.c_uint32)]),
    "dc_vfp_compute_deflatten": (ctypes.c_int32, [_P(_VfPoint), ctypes.c_size_t, _P(_Num), _P(_Num), _P(_Num),
                                                  _P(_Num), _P(_Deflatten)]),
    "dc_vfp_evaluate_curve_law": (ctypes.c_int32, [_P(_Num), ctypes.c_size_t, _P(_Num), _P(ctypes.c_int64),
                                                   ctypes.c_size_t]),
    "dc_vfp_predict_curve": (ctypes.c_int32, [_P(_VfPoint), ctypes.c_size_t, _P(_Num), _P(_Num),
                                              _P(ctypes.c_int64), _P(ctypes.c_uint32), ctypes.c_size_t]),
    "dc_vfp_cascade_meta": (ctypes.c_int32, [_P(_CascadeRow), ctypes.c_size_t, _P(_Cascade)]),
    "dc_vfp_compute_hard_deflatten": (ctypes.c_int32, [_P(_VfPoint), ctypes.c_size_t, _P(_Num), _P(_Num),
                                                       _P(_Num), _P(_Hard)]),
    "dc_vfp_compute_ramp": (ctypes.c_int32, [_P(_VfPoint), ctypes.c_size_t, _P(_Num), _P(_Num), _P(_Num),
                                             _P(_Num), _P(_Ramp)]),
    "dc_vfp_compute_rephase": (ctypes.c_int32, [_P(_Num), ctypes.c_size_t, _P(_Num), _P(_Rephase)]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the planner functions bound; raises NativeUnavailable."""
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
            raise NativeUnavailable(f"native core has no V/F planners: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


# ---- errors: re-run the Python operation the core stood in for ---------------------------------

def _floordiv_zero():
    one, zero = 1, 0
    return one // zero


def _mod_zero():
    one, zero = 1, 0
    return one % zero


def _int_of(value):
    return int(value)


def _none_plus():
    bound = None
    return bound + 0.01


def _none_minus():
    bound = None
    return bound - 0.01


def _first_of_nothing():
    nothing = []
    return nothing[0]


def _min_of_nothing():
    return min([], key=lambda point: point)


def _float_slice():
    boundary = 0.5
    return [][:boundary]


_PYTHON_ERRORS = {
    DC_E_VFP_FLOORDIV_ZERO: _floordiv_zero,
    DC_E_VFP_MOD_ZERO: _mod_zero,
    DC_E_VFP_NAN_INT: lambda: _int_of(float("nan")),
    DC_E_VFP_INF_INT: lambda: _int_of(float("inf")),
    DC_E_VFP_NONE_INT: lambda: _int_of(None),
    DC_E_VFP_NONE_ADD: _none_plus,
    DC_E_VFP_NONE_SUB: _none_minus,
    DC_E_VFP_EMPTY_INDEX: _first_of_nothing,
    DC_E_VFP_EMPTY_MIN: _min_of_nothing,
    DC_E_VFP_FLOAT_SLICE: _float_slice,
}

_BIG = "an integer beyond +-2**53 is outside the native planners' range"


def _raise(status):
    action = _PYTHON_ERRORS.get(status)
    if action is not None:
        action()
        raise AssertionError(f"status {status} did not raise")  # pragma: no cover
    text = load().dc_vfp_status_text(status).decode()
    if status == DC_E_VFP_BIG_INT:
        raise OverflowError(_BIG)
    if status == DC_E_VFP_UNORDERED:
        raise ValueError(text + ": Python's order would be an artefact of its sort, so the core refuses")
    native._raise(status)


def _check(status):
    if status != DC_OK:
        _raise(status)


# ---- conversions -------------------------------------------------------------------------------

def _num(value, what="value"):
    out = _Num()
    if value is None:
        out.kind = _NONE
    elif isinstance(value, int):  # bool included, as Python's arithmetic treats it
        if not -INT_LIMIT <= value <= INT_LIMIT:
            raise OverflowError(_BIG)
        out.kind, out.i = _INT, int(value)
    elif isinstance(value, float):
        out.kind, out.f = _FLOAT, value
    else:
        raise TypeError(f"{what} must be an int, a float or None for the native planners, "
                        f"not {type(value).__name__}")
    return out


def _value(num):
    if num.kind == _INT:
        return num.i
    if num.kind == _FLOAT:
        return num.f
    return None


def _real(value, what, limit=INT_LIMIT):
    if isinstance(value, int):
        if not -limit <= value <= limit:
            raise OverflowError(_BIG)
        return float(value)
    if isinstance(value, float):
        return value
    raise TypeError(f"{what} must be an int or a float for the native planners, not {type(value).__name__}")


def _points(points):
    count = len(points)
    if count > VFP_POINTS:
        raise ValueError(f"the native planners take at most {VFP_POINTS} points, not {count}")
    array = (_VfPoint * max(count, 1))()
    for k in range(count):
        point = points[k]
        idx, volt, freq, delta = point["idx"], point["volt_mv"], point["freq_mhz"], point["delta_khz"]
        row = array[k]
        row.volt_mv = _real(volt, "volt_mv")
        # freq_mhz * 1000 must stay exact in a double when freq_mhz is an int
        row.freq_mhz = _real(freq, "freq_mhz", INT_LIMIT // 1000)
        if not isinstance(delta, int):
            raise TypeError(f"delta_khz must be an int for the native planners, not {type(delta).__name__}")
        if not -(1 << 31) <= delta < (1 << 31):
            raise OverflowError("delta_khz must fit a signed 32-bit integer")
        row.delta_khz = delta
        # the core only carries idx; the caller's own value is what comes back
        row.idx = idx if isinstance(idx, int) and 0 <= idx <= 0xFFFFFFFF else 0
    return array, count


def _nums(values, what):
    values = list(values)
    if len(values) > VFP_POINTS:
        raise ValueError(f"the native planners take at most {VFP_POINTS} {what}, not {len(values)}")
    array = (_Num * max(len(values), 1))()
    for k, value in enumerate(values):
        array[k] = _num(value, what)
    return array, len(values)


def _changes(points, result):
    out = []
    for change in result.changes[:result.change_count]:
        point = points[change.pos]
        out.append((point["idx"], point["volt_mv"], change.old_mhz, change.new_mhz, _value(change.delta_khz)))
    return out


# ---- the planners ------------------------------------------------------------------------------

def peak_info(points, cap_mv=None):
    array, count = _points(points)
    cap = _num(cap_mv, "cap_mv")
    out = _Peak(size=ctypes.sizeof(_Peak))
    _check(load().dc_vfp_peak_info(array, count, ctypes.byref(cap), ctypes.byref(out)))
    if not out.found:
        return 0.0, None, 0.0, 0
    park = points[out.park_pos]
    return points[out.peak_pos]["freq_mhz"], park["idx"], park["volt_mv"], out.n_at_peak


def resolve_vf_point(points, volt_mv):
    array, count = _points(points)
    volt = _num(volt_mv, "volt_mv")
    pos = ctypes.c_uint32()
    status = load().dc_vfp_resolve_vf_point(array, count, ctypes.byref(volt), ctypes.byref(pos))
    if status == DC_E_UNAVAILABLE:
        return None
    _check(status)
    return points[pos.value]


def compute_deflatten(points, vcap_mv, max_khz=None, extra_points_above=None, step_khz=None):
    array, count = _points(points)
    args = [_num(vcap_mv, "vcap_mv"), _num(max_khz, "max_khz"),
            _num(extra_points_above, "extra_points_above"), _num(step_khz, "step_khz")]
    out = _Deflatten(size=ctypes.sizeof(_Deflatten))
    _check(load().dc_vfp_compute_deflatten(array, count, *map(ctypes.byref, args), ctypes.byref(out)))
    if not out.full_meta:
        meta = {"clamped": False, "boundary_idx": None, "unique": False}
    else:
        meta = {"clamped": bool(out.clamped), "boundary_idx": points[out.boundary_pos]["idx"],
                "unique": bool(out.unique), "lowered": out.lowered, "lowered_by_mhz": out.lowered_by_mhz}
    return _changes(points, out), out.ceil_before_mhz, out.ceil_after_mhz, meta


def evaluate_curve_law(khz, max_rise_khz=None):
    array, count = _nums(khz, "khz values")
    rise = _num(max_rise_khz, "max_rise_khz")
    out = (ctypes.c_int64 * max(count, 1))()
    _check(load().dc_vfp_evaluate_curve_law(array, count, ctypes.byref(rise), out, count))
    return list(out[:count])


def predict_curve(points, khz, new, max_rise_khz=None):
    array, count = _points(points)
    values = (_Num * max(count, 1))()
    for i in range(count):
        values[i] = _num(new.get(i, khz[i]), "planned kHz")
    rise = _num(max_rise_khz, "max_rise_khz")
    real = (ctypes.c_int64 * max(count, 1))()
    order = (ctypes.c_uint32 * max(count, 1))()
    _check(load().dc_vfp_predict_curve(array, count, values, ctypes.byref(rise), real, order, count))
    return list(real[:count]), {order[k]: k for k in range(count)}


def cascade_meta(points, khz, real, pos, idxs):
    idxs = list(idxs)
    if len(idxs) > VFP_POINTS:
        raise ValueError(f"the native planners take at most {VFP_POINTS} indices, not {len(idxs)}")
    rows = (_CascadeRow * max(len(idxs), 1))()
    for r, i in enumerate(idxs):
        rows[r].real = _num(real[pos[i]], "real")
        rows[r].khz = _num(khz[i], "khz")
    for r, i in enumerate(idxs):
        # looked up only if the core needs it, which is when Python's min() would
        try:
            rows[r].volt_mv = _real(points[i]["volt_mv"], "volt_mv")
            rows[r].has_volt = 1
        except Exception:  # noqa: BLE001 - re-raised below exactly when Python would raise it
            rows[r].has_volt = 0
    out = _Cascade(size=ctypes.sizeof(_Cascade))
    status = load().dc_vfp_cascade_meta(rows, len(idxs), ctypes.byref(out))
    if status == DC_E_VFP_MISSING:
        _real(points[idxs[out.missing_row]]["volt_mv"], "volt_mv")  # raises the lookup's own error
    _check(status)
    return {"lifted_below": out.lifted_below,
            "lift_max_mhz": out.lift_max_mhz,
            "lift_lowest_mv": points[idxs[out.lowest_row]]["volt_mv"] if out.has_lowest else None}


def compute_hard_deflatten(points, floor_mv, target_khz, step_khz=None):
    array, count = _points(points)
    args = [_num(floor_mv, "floor_mv"), _num(target_khz, "target_khz"), _num(step_khz, "step_khz")]
    out = _Hard(size=ctypes.sizeof(_Hard))
    _check(load().dc_vfp_compute_hard_deflatten(array, count, *map(ctypes.byref, args), ctypes.byref(out)))
    band = bool(out.has_band)
    floor = points[out.floor_pos] if band else None
    park = points[out.park_pos] if band else None
    meta = {"floor_idx": floor["idx"] if band else None,
            "floor_mv": floor["volt_mv"] if band else None,
            "target_mhz": out.target_mhz,
            "n_flat": out.n_flat,
            "park_idx": park["idx"] if band else None,
            "park_mv": park["volt_mv"] if band else None,
            "park_mhz": out.park_mhz,
            "parks_at_floor": bool(out.parks_at_floor),
            "lifted_below": out.lifted_below,
            "lift_max_mhz": out.lift_max_mhz,
            "lift_lowest_mv": points[out.lift_lowest_pos]["volt_mv"] if out.has_lift_lowest else None}
    return _changes(points, out), out.floor_before_mhz, out.target_after_mhz, meta


def compute_ramp(points, lo_mv, cap_mv, max_khz=None, step_khz=None):
    array, count = _points(points)
    args = [_num(lo_mv, "lo_mv"), _num(cap_mv, "cap_mv"), _num(max_khz, "max_khz"), _num(step_khz, "step_khz")]
    out = _Ramp(size=ctypes.sizeof(_Ramp))
    _check(load().dc_vfp_compute_ramp(array, count, *map(ctypes.byref, args), ctypes.byref(out)))
    band = bool(out.has_band)
    cap = points[out.boundary_pos] if band else None
    lo = points[out.lo_pos] if band else None
    meta = {"clamped": bool(out.clamped),
            "boundary_idx": cap["idx"] if band else None,
            "unique": bool(out.unique),
            "lo_idx": lo["idx"] if band else None,
            "cap_idx": cap["idx"] if band else None,
            "lo_mv": lo["volt_mv"] if band else None,
            "cap_mv": cap["volt_mv"] if band else None,
            "rungs": out.rungs,
            "top_mhz": out.top_mhz,
            "floor_before_mhz": out.floor_before_mhz,
            "floor_after_mhz": out.floor_after_mhz,
            "floor_cost_mhz": out.floor_cost_mhz,
            "leveled_above": out.leveled_above,
            "under_band_mhz": out.under_band_mhz if out.has_under_band else None,
            "shadowed": out.shadowed,
            "delivered": out.delivered,
            "lifted_below": out.lifted_below,
            "lift_max_mhz": out.lift_max_mhz,
            "lift_lowest_mv": points[out.lift_lowest_pos]["volt_mv"] if out.has_lift_lowest else None,
            "dropped_rungs": out.dropped_rungs,
            "dropped_reason": out.dropped_reason.decode()}
    return _changes(points, out), out.ceil_before_mhz, out.ceil_after_mhz, meta


def compute_rephase(deltas, step_khz):
    keys = list(deltas)
    array, count = _nums((deltas[key] for key in keys), "deltas")
    step = _num(step_khz, "step_khz")
    out = _Rephase(size=ctypes.sizeof(_Rephase))
    _check(load().dc_vfp_compute_rephase(array, count, ctypes.byref(step), ctypes.byref(out)))
    return {keys[change.pos]: change.delta_khz for change in out.changes[:out.change_count]}, out.phase
