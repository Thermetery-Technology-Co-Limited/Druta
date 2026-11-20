# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pure rail-limit and hold-headroom helpers of the native core (drutacore/rail_limits.h).

Each function here has the signature, return values and exceptions of the ``nvbackend.GPU``
helper of the same name, so the same cases run against both. Two of the originals read an
instance attribute; here it is an explicit parameter:

    hold_headroom_architecture(arch)            arch is what ``self.arch()`` returned
    hold_headroom_bound_mv(record, active)      active is ``self.hold_headroom_record()``
    hold_headroom_allowance_mv(record, active)  (the Python evaluates ``record or active or {}``)

The core computes; this module only converts. A dict value travels with its kind (missing, None,
int, float, another object), so int + int stays an int and a None or a string raises where the
Python raises. For another object the binding passes its ``bool()`` and, when it succeeds, its
``float()``: that is the conversion the Python itself applies. Where the core reports an exception,
this module re-runs the same failing Python operation (``None + 1.0``, ``fields[key]``,
``None.get``...) so the exception type and message are the running interpreter's own.

What the core cannot represent is refused rather than approximated:

- int + int beyond +-2**53 (OverflowError): Python's ints are unbounded, the core's are doubles.
  Such an int is carried, and refused only where a helper adds it to another int; its sign, truth,
  float(), range checks and float + int (Python's own conversion to the nearest double) stay exact.
- rows, states and records that are not mappings (TypeError), and a key outside
  ``VOLT_LIMIT_FIELDS`` for ``abs_limit_mv`` (KeyError where the Python would read any key).
- an object that is not None, bool, int or float but would take part in arithmetic or an ordering
  comparison: the Python operation is re-run, and if it does not raise (a Fraction, say) the
  binding raises TypeError instead of returning a result the core did not compute. Such an object
  as a ``type`` or a ``rail`` compares unequal to every number (numpy's ints would not).
- an object whose ``float()`` raises something other than TypeError or ValueError raises here,
  while the binding marshals, even where the Python would not have reached that conversion.
"""
from __future__ import annotations

import ctypes
import math
import threading
from collections.abc import Mapping

from . import native
from .native import DC_OK, NativeUnavailable

DC_E_RL_KEY = -140
DC_E_RL_TYPE = -141
DC_E_RL_ATTRIBUTE = -142
DC_E_RL_CONVERSION = -143
DC_E_RL_BIG_INT = -144

_ABSENT, _NONE, _INT, _FLOAT, _OTHER, _BIG = 0, 1, 2, 3, 4, 5
_BASES_ABSENT, _BASES_MAPPING, _BASES_NO_GET = 0, 1, 2
_INT_LIMIT = 1 << 53
_FIELDS = 4
_HEADROOM_FIELDS = 3
_REASON_PLANNED = 0


class _Value(ctypes.Structure):  # dc_rl_value
    _fields_ = [("kind", ctypes.c_int32), ("truthy", ctypes.c_int32), ("has_float", ctypes.c_int32),
                ("reserved", ctypes.c_int32), ("f", ctypes.c_double)]


class _Row(ctypes.Structure):  # dc_rl_row
    _fields_ = [("size", ctypes.c_uint32), ("bases_kind", ctypes.c_int32), ("limit", _Value * _FIELDS),
                ("base", _Value * _FIELDS), ("boost_mv", _Value), ("effective_mv", _Value),
                ("headroom_mv", _Value), ("type", _Value)]


class _Record(ctypes.Structure):  # dc_rl_record
    _fields_ = [("size", ctypes.c_uint32), ("truthy", ctypes.c_int32), ("raised_ceiling_mv", _Value),
                ("hold_mv", _Value), ("margin_mv", _Value), ("grain_mv", _Value), ("live_offset_mv", _Value)]


class _Constants(ctypes.Structure):  # dc_rl_constants
    _fields_ = [("size", ctypes.c_uint32), ("architecture_count", ctypes.c_uint32),
                ("architectures", ctypes.c_uint32 * 8), ("max_margin_mv", ctypes.c_double),
                ("fallback_grain_mv", ctypes.c_double), ("grain_min_mv", ctypes.c_double),
                ("grain_max_mv", ctypes.c_double)]


class _Targets(ctypes.Structure):  # dc_rl_targets
    _fields_ = [("size", ctypes.c_uint32), ("reason", ctypes.c_int32), ("field", ctypes.c_uint32),
                ("has_target", ctypes.c_int32 * _HEADROOM_FIELDS), ("target_mv", ctypes.c_double * _HEADROOM_FIELDS),
                ("message", ctypes.c_char * 768)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_rl_status_text": (ctypes.c_char_p, [ctypes.c_int32]),
    "dc_rl_constants_get": (ctypes.c_int32, [_P(_Constants)]),
    "dc_rl_field_name": (ctypes.c_char_p, [ctypes.c_uint32]),
    "dc_rl_volt_rail_architecture": (ctypes.c_int32, [ctypes.c_int32, ctypes.c_int64, _P(ctypes.c_int32)]),
    "dc_rl_valid_volt_rail_state": (ctypes.c_int32, [_P(_Value), _P(_Row), _P(ctypes.c_int32)]),
    "dc_rl_volt_boost_headroom_mv": (ctypes.c_int32, [_P(_Row), _P(ctypes.c_double)]),
    "dc_rl_abs_limit_mv": (ctypes.c_int32, [_P(_Row), ctypes.c_uint32, _P(_Value)]),
    "dc_rl_rail_ceiling_mv": (ctypes.c_int32, [_P(_Row), _P(_Value)]),
    "dc_rl_rail_floor_mv": (ctypes.c_int32, [_P(_Row), _P(_Value)]),
    "dc_rl_hold_headroom_ceiling_mv": (ctypes.c_int32, [_P(_Row), _P(_Value)]),
    "dc_rl_headroom_enforced_mv": (ctypes.c_int32, [_P(_Row), _P(_Value), _P(_Value)]),
    "dc_rl_hold_headroom_targets": (ctypes.c_int32, [_P(_Row), _P(_Value), _P(_Value), _P(_Value), _P(_Targets)]),
    "dc_rl_hold_headroom_bound_mv": (ctypes.c_int32, [_P(_Record), _P(_Record), _P(ctypes.c_double)]),
    "dc_rl_hold_headroom_allowance_mv": (ctypes.c_int32, [_P(_Record), _P(_Record), _P(ctypes.c_double)]),
    "dc_rl_round_digits": (ctypes.c_int32, [ctypes.c_double, ctypes.c_int32, _P(ctypes.c_double)]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the rail helpers bound; raises NativeUnavailable."""
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
            raise NativeUnavailable(f"native core has no rail-limit helpers: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


def volt_limit_fields():
    """VOLT_LIMIT_FIELDS as the core names them."""
    lib = load()
    return tuple(lib.dc_rl_field_name(i).decode() for i in range(_FIELDS))


def hold_headroom_fields():
    """HOLD_HEADROOM_FIELDS: the first three of VOLT_LIMIT_FIELDS."""
    return volt_limit_fields()[:_HEADROOM_FIELDS]


def constants():
    """{"volt_rail_architectures", "max_margin_mv", "fallback_grain_mv", "grain_min_mv", "grain_max_mv"}.

    The fallback grain and its bounds are one card's estimate (half a TU102 V/F point), used only
    when the current card's curve cannot be read."""
    out = _Constants(size=ctypes.sizeof(_Constants))
    _check(load().dc_rl_constants_get(ctypes.byref(out)))
    return {"volt_rail_architectures": tuple(out.architectures[:out.architecture_count]),
            "max_margin_mv": out.max_margin_mv, "fallback_grain_mv": out.fallback_grain_mv,
            "grain_min_mv": out.grain_min_mv, "grain_max_mv": out.grain_max_mv}


# --------------------------------------------------------------------------- marshalling

_MISSING = object()


def _value(v=_MISSING):
    out = _Value()
    if v is _MISSING:
        out.kind = _ABSENT
    elif v is None:
        out.kind = _NONE
    elif isinstance(v, int):  # bool included, as isinstance(True, int) is in the Python
        if -_INT_LIMIT <= v <= _INT_LIMIT:
            out.kind, out.f = _INT, float(v)
        else:
            out.kind = _BIG
            try:
                out.f = float(v)
            except OverflowError:
                out.f = math.inf if v > 0 else -math.inf
    elif isinstance(v, float):
        out.kind, out.f = _FLOAT, v
    else:
        out.kind, out.truthy = _OTHER, int(bool(v))
        try:
            out.f, out.has_float = float(v), 1
        except (TypeError, ValueError):
            out.has_float = 0
    return out


def _arg(v):
    """An optional argument: None stays None (a NULL pointer)."""
    return None if v is None else ctypes.byref(_value(v))


def _mapping(obj, what):
    if not isinstance(obj, Mapping):
        raise TypeError(f"the native rail helpers take {what} as a mapping, not {type(obj).__name__}")
    return obj


def _row(fields):
    fields = _mapping(fields, "a rail row")
    row = _Row(size=ctypes.sizeof(_Row))
    names = volt_limit_fields()
    for i, key in enumerate(names):
        row.limit[i] = _value(fields.get(key, _MISSING))
    if "_base_mv" not in fields:
        row.bases_kind = _BASES_ABSENT
    else:
        bases = fields["_base_mv"]
        if isinstance(bases, Mapping):
            row.bases_kind = _BASES_MAPPING
            for i, key in enumerate(names):
                row.base[i] = _value(bases.get(key, _MISSING))
        else:
            row.bases_kind = _BASES_NO_GET
    row.boost_mv = _value(fields.get("_boost_mv", _MISSING))
    row.effective_mv = _value(fields.get("_effective_mv", _MISSING))
    row.headroom_mv = _value(fields.get("_headroom_mv", _MISSING))
    row.type = _value(fields.get("type", _MISSING))
    return row


def _record(record):
    if record is None:
        return None
    if not record:
        return ctypes.byref(_Record(size=ctypes.sizeof(_Record), truthy=0))
    record = _mapping(record, "a hold-headroom record")
    out = _Record(size=ctypes.sizeof(_Record), truthy=1)
    for key in ("raised_ceiling_mv", "hold_mv", "margin_mv", "grain_mv", "live_offset_mv"):
        setattr(out, key, _value(record.get(key, _MISSING)))
    return ctypes.byref(out)


def _result(v):
    if v.kind == _INT:
        return int(v.f)
    return v.f


def _check(status):
    if status != DC_OK:
        raise ValueError(load().dc_rl_status_text(status).decode())


# The Python expressions whose exceptions the core reports. Each is the original's own operation
# order; it runs only after the core said it raises, to raise the interpreter's own exception.

def _py_abs(fields, key):
    return fields[key] + fields.get("_base_mv", {}).get(key, float("nan"))


def _py_ceiling(fields):
    return min(_py_abs(fields, "reliability") + fields.get("_boost_mv", 0.0),
               _py_abs(fields, "alt_reliability"), _py_abs(fields, "overvoltage"))


def _py_effective(row):
    try:
        float(row.get("_effective_mv"))
    except (TypeError, ValueError):
        pass


def _raise(status, replay):
    """Re-raise what the Python raises for this status; replay re-runs its failing operation."""
    if status == DC_E_RL_BIG_INT:
        raise OverflowError("an int + int beyond +-2**53 is not computed by the native core, "
                            "or an int does not fit a float")
    if status in (DC_E_RL_KEY, DC_E_RL_TYPE, DC_E_RL_ATTRIBUTE, DC_E_RL_CONVERSION):
        replay()
        raise TypeError("the native rail helpers carry only None, bool, int and float as numbers")
    _check(status)


def _num_call(function, replay, *args):
    out = _Value()
    status = function(*args, ctypes.byref(out))
    if status != DC_OK:
        _raise(status, replay)
    return _result(out)


def _double_call(function, replay, *args):
    out = ctypes.c_double()
    status = function(*args, ctypes.byref(out))
    if status != DC_OK:
        _raise(status, replay)
    return out.value


# --------------------------------------------------------------------------- the helpers

def hold_headroom_architecture(arch):
    """GPU.hold_headroom_architecture with ``arch`` = ``self.arch()``: True, False or None."""
    if arch is None:
        return None
    if isinstance(arch, float) and arch.is_integer():
        arch = int(arch)                     # 4.0 in (4, 6, ...) is True in Python
    if not isinstance(arch, int) or not -(1 << 63) <= arch < (1 << 63):
        return False  # no such architecture number is in _VOLT_RAIL_ARCHITECTURES
    supported = ctypes.c_int32()
    _check(load().dc_rl_volt_rail_architecture(1, arch, ctypes.byref(supported)))
    return {1: True, 0: False}.get(supported.value)


def _valid_volt_rail_state(rail, state):
    state = _mapping(state, "a rail state")
    valid = ctypes.c_int32()
    status = load().dc_rl_valid_volt_rail_state(ctypes.byref(_value(rail)), ctypes.byref(_row(state)),
                                                ctypes.byref(valid))
    if status != DC_OK:
        _raise(status, lambda: None)
    return bool(valid.value)


def volt_boost_headroom_mv(fields=None):
    if not fields:
        return _double_call(load().dc_rl_volt_boost_headroom_mv, lambda: None, None)
    return _double_call(load().dc_rl_volt_boost_headroom_mv,
                        lambda: float(fields.get("_headroom_mv")), ctypes.byref(_row(fields)))


def abs_limit_mv(fields, key):
    names = volt_limit_fields()
    if key not in names:
        raise KeyError(key)
    return _num_call(load().dc_rl_abs_limit_mv, lambda: _py_abs(fields, key),
                     ctypes.byref(_row(fields)), names.index(key))


def rail_ceiling_mv(fields):
    return _num_call(load().dc_rl_rail_ceiling_mv, lambda: _py_ceiling(fields), ctypes.byref(_row(fields)))


def rail_floor_mv(fields):
    return _num_call(load().dc_rl_rail_floor_mv, lambda: _py_abs(fields, "vmin"), ctypes.byref(_row(fields)))


def hold_headroom_ceiling_mv(row):
    def replay():
        _py_ceiling(row)
        _py_effective(row)
    return _num_call(load().dc_rl_hold_headroom_ceiling_mv, replay, ctypes.byref(_row(row)))


def _headroom_enforced_mv(row, grain=None):
    def replay():
        estimate = _py_ceiling(row)
        _py_effective(row)
        if grain and grain > 0:
            estimate + grain
    return _num_call(load().dc_rl_headroom_enforced_mv, replay, ctypes.byref(_row(row)), _arg(grain))


def hold_headroom_targets(fields, hold_mv, margin_mv, maximum_mv):
    out = _Targets(size=ctypes.sizeof(_Targets))
    names = volt_limit_fields()

    def replay():
        for key in names[:_HEADROOM_FIELDS]:
            if key in fields:
                _py_abs(fields, key)
    status = load().dc_rl_hold_headroom_targets(
        ctypes.byref(_row(fields)), ctypes.byref(_value(hold_mv)), ctypes.byref(_value(margin_mv)),
        ctypes.byref(_value(maximum_mv)), ctypes.byref(out))
    if status != DC_OK:
        _raise(status, replay)
    if out.reason != _REASON_PLANNED:
        return None, out.message.decode()
    return {names[i]: out.target_mv[i] for i in range(_HEADROOM_FIELDS) if out.has_target[i]}, None


def hold_headroom_bound_mv(record=None, active=None):
    return _double_call(load().dc_rl_hold_headroom_bound_mv, lambda: None, _record(record), _record(active))


def hold_headroom_allowance_mv(record=None, active=None):
    return _double_call(load().dc_rl_hold_headroom_allowance_mv, lambda: None, _record(record), _record(active))


def round_digits(x, ndigits):
    """round(float(x), ndigits) for ndigits 2 or 4 as the core computes it (the hold-headroom grain's
    rounding): exact, ties to even, for |x| < 2**39; x itself at or above 2**53 and for 0, +-inf and
    NaN; nearbyint(x * 10**ndigits) / 10**ndigits between 2**39 and 2**53, which may differ from
    Python's round there. Another ndigits raises ValueError."""
    out = ctypes.c_double()
    if ndigits not in (2, 4) or load().dc_rl_round_digits(float(x), ndigits, ctypes.byref(out)) != DC_OK:
        raise ValueError("the core rounds to 2 or 4 digits only")
    return out.value
