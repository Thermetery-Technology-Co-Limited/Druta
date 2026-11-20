# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pure client-power tables and geometry helpers of the native core (drutacore/client_power.h).

``constants()`` returns client_power_runtime's module constants by their Python names, shaped
exactly like the originals (dicts in the same key order, tuples), so the two compare with ``==``.
``small_native_geometry``, ``definition_stride``, ``native_geometry``, ``coupled_requests``,
``project`` and ``matches`` take and return what ``_small_native_geometry`` ... ``_matches`` do,
and raise the same exception types with the same messages.

The core works on a binding struct and byte payloads. What Python's dynamic typing decides stays
here, and each place says so: dict lookups and comparisons of non-payload values, hex decoding (a
payload that does not decode is passed as absent and its genuine exception is re-raised at the
point the core reports needing it), and values the struct cannot carry (see ``_binding``).
"""
from __future__ import annotations

import copy
import ctypes
import operator
import struct
import threading

from . import native
from .native import DC_E_FAILED, DC_E_RANGE, DC_OK, NativeUnavailable

DC_CP_COMMAND_COUNT = 7
DC_CP_SIZE_GROUP_COUNT = 7
DC_CP_LIST_MAX = 8
DC_CP_STATE_KEY_COUNT = 10
DC_CP_STATE_KEY_CHARS = 24
DC_CP_PAYLOAD_COUNT = 5
DC_CP_FIRST_PAYLOAD_KEY = 5

DC_CP_ABSENT, DC_CP_OTHER = 0, 1
DC_CP_LAYOUT_RM26, DC_CP_LAYOUT_LEGACY32 = 2, 3
DC_CP_NATIVE_POLICY124, DC_CP_NATIVE_RM26_EXTENDED = 2, 3

(DC_CP_ERROR_NONE, DC_CP_ERROR_STRUCT, DC_CP_ERROR_INDEX, DC_CP_ERROR_OVERFLOW, DC_CP_ERROR_KEY,
 DC_CP_ERROR_PAYLOAD, DC_CP_ERROR_BINDING, DC_CP_ERROR_RANGE) = range(8)
DC_CP_MW_INTEGER, DC_CP_MW_NOT_INTEGER, DC_CP_MW_OUT_OF_RANGE = range(3)
DC_CP_CMP_BYTES, DC_CP_CMP_EQUAL, DC_CP_CMP_DIFFERENT, DC_CP_CMP_MISSING = range(4)

# The size-group rows of dc_cp_constants.group_sizes, by their Python names.
SIZE_GROUP_NAMES = ("SIZES", "LEGACY_SIZES", "TURING_SIZES", "INFO216_SIZES", "RM26_SIZES",
                    "RM26_EXTENDED_SIZES", "A612_COMPACT_SIZES")
COMMAND_NAMES = ("INFO", "CONTROL", "SET", "ROLE_INFO", "NATIVE_INFO", "NATIVE_CONTROL", "NATIVE_STATUS")

_INT64_MIN, _INT64_MAX = -(1 << 63), (1 << 63) - 1


class _Constants(ctypes.Structure):  # dc_cp_constants
    _fields_ = [("size", ctypes.c_uint32),
                ("commands", ctypes.c_uint32 * DC_CP_COMMAND_COUNT),
                ("rm26_commands", ctypes.c_uint32 * DC_CP_COMMAND_COUNT),
                ("group_sizes", (ctypes.c_uint32 * DC_CP_COMMAND_COUNT) * DC_CP_SIZE_GROUP_COUNT),
                ("size_refusal_count", ctypes.c_uint32), ("size_refusals", ctypes.c_uint32 * DC_CP_LIST_MAX),
                ("info_base", ctypes.c_uint32), ("info_stride", ctypes.c_uint32),
                ("control_base", ctypes.c_uint32), ("control_stride", ctypes.c_uint32),
                ("capacity", ctypes.c_uint32),
                ("native_info_base", ctypes.c_uint32), ("native_info_stride", ctypes.c_uint32),
                ("native_control_base", ctypes.c_uint32), ("native_control_stride", ctypes.c_uint32),
                ("native_status_base", ctypes.c_uint32), ("native_status_stride", ctypes.c_uint32),
                ("native_capacity", ctypes.c_uint32),
                ("role", ctypes.c_uint32), ("client", ctypes.c_uint32), ("missing", ctypes.c_uint32),
                ("legacy_group_architecture_count", ctypes.c_uint32),
                ("legacy_group_architectures", ctypes.c_uint32 * DC_CP_LIST_MAX),
                ("rm26_architecture_count", ctypes.c_uint32),
                ("rm26_architectures", ctypes.c_uint32 * DC_CP_LIST_MAX),
                ("state_keys", (ctypes.c_char * DC_CP_STATE_KEY_CHARS) * DC_CP_STATE_KEY_COUNT)]


class _Binding(ctypes.Structure):  # dc_cp_binding
    _fields_ = [("layout", ctypes.c_int32), ("native_layout", ctypes.c_int32),
                ("has_native_info_stride", ctypes.c_int32), ("native_info_stride", ctypes.c_int64),
                ("has_native_index", ctypes.c_int32), ("native_index", ctypes.c_int64),
                ("has_client_index", ctypes.c_int32), ("client_index", ctypes.c_int64)]


class _Error(ctypes.Structure):  # dc_cp_error
    _fields_ = [("kind", ctypes.c_int32), ("key", ctypes.c_uint32), ("side", ctypes.c_int32),
                ("message", ctypes.c_char * 256)]


class _Payload(ctypes.Structure):  # dc_cp_payload
    _fields_ = [("data", ctypes.c_void_p), ("length", ctypes.c_uint32), ("present", ctypes.c_int32)]


class _Geometry(ctypes.Structure):  # dc_cp_geometry
    _fields_ = [("info_base", ctypes.c_int64), ("info_stride", ctypes.c_int64),
                ("control_base", ctypes.c_int64), ("control_stride", ctypes.c_int64),
                ("control_size", ctypes.c_int64)]


class _MatchInput(ctypes.Structure):  # dc_cp_match_input
    _fields_ = [("size", ctypes.c_uint32), ("binding", _Binding), ("binding_usable", ctypes.c_int32),
                ("compare", ctypes.c_int32 * DC_CP_STATE_KEY_COUNT), ("extra_keys", ctypes.c_int32),
                ("expected", _Payload * DC_CP_PAYLOAD_COUNT), ("observed", _Payload * DC_CP_PAYLOAD_COUNT)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_cp_constants_get": (ctypes.c_int32, [_P(_Constants)]),
    "dc_cp_small_native_geometry": (ctypes.c_int32, [_P(_Binding), _P(ctypes.c_uint32), _P(ctypes.c_uint32)]),
    "dc_cp_definition_stride": (ctypes.c_int32, [ctypes.c_int64, _P(ctypes.c_int64)]),
    "dc_cp_native_geometry": (ctypes.c_int32, [_P(_Binding), _P(_Geometry)]),
    "dc_cp_coupled_requests": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_uint32, _P(ctypes.c_int32), _P(_Error)]),
    "dc_cp_project": (ctypes.c_int32, [_P(_Binding), ctypes.c_int32, ctypes.c_int64, _P(_Payload), _P(_Payload),
                                       ctypes.c_void_p, ctypes.c_void_p, _P(_Error)]),
    "dc_cp_matches": (ctypes.c_int32, [_P(_MatchInput), _P(ctypes.c_int32), _P(_Error)]),
}

_lock = threading.Lock()
_bound = None


def load():
    """The core library with the client-power helpers bound; raises NativeUnavailable."""
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
            raise NativeUnavailable(f"native core has no client-power helpers: {exc}") from None
        _bound = library
        return library


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


# ---- constants ------------------------------------------------------------ #

def constants():
    """client_power_runtime's module constants (lines 22-128) by name."""
    raw = _Constants()
    raw.size = ctypes.sizeof(_Constants)
    status = load().dc_cp_constants_get(ctypes.byref(raw))
    if status != DC_OK:
        raise RuntimeError(f"dc_cp_constants_get: status {status}")
    commands = tuple(raw.commands)
    out = dict(zip(COMMAND_NAMES, commands, strict=True))
    for name, row in zip(SIZE_GROUP_NAMES, raw.group_sizes, strict=True):
        out[name] = dict(zip(commands, row, strict=True))
    out["SIZE_REFUSALS"] = tuple(raw.size_refusals[:raw.size_refusal_count])
    out["_RM26_COMMANDS"] = dict(zip(commands, raw.rm26_commands, strict=True))
    for name in ("info_base", "info_stride", "control_base", "control_stride", "capacity",
                 "native_info_base", "native_info_stride", "native_control_base", "native_control_stride",
                 "native_capacity", "role", "client", "missing"):
        out[name.upper()] = getattr(raw, name)
    out["_LAYOUT"] = {
        "info": (raw.native_info_base, raw.native_info_stride),
        "control": (raw.native_control_base, raw.native_control_stride),
        "status": (raw.native_status_base, raw.native_status_stride),
        "sizes": {k: out["SIZES"][k] for k in commands[4:7]},
    }
    out["LEGACY_GROUP_ARCHITECTURES"] = tuple(raw.legacy_group_architectures[:raw.legacy_group_architecture_count])
    out["RM26_ARCHITECTURES"] = tuple(raw.rm26_architectures[:raw.rm26_architecture_count])
    out["_STATE_KEYS"] = tuple(key.value.decode("ascii") for key in raw.state_keys)
    return out


_STATE_KEYS = ("schema", "stable_reads", "identity", "binding", "requested_mw", "info_hex", "control_hex",
               "role_info_hex", "native_info_hex", "native_control_hex")
_PAYLOAD_KEYS = _STATE_KEYS[DC_CP_FIRST_PAYLOAD_KEY:]
_ABSENT = object()


# ---- binding conversion (Python's dynamic part) ---------------------------- #

def _clamped(value):
    """An int as int64. An int beyond int64 is clamped: every offset that multiplies it by a
    positive constant then leaves int64 in the same direction, as Python's does. One case is not
    exact: a native_info_stride below int64 multiplied by row 1 alone lands back inside int64, so a
    recalculated peer in native row 1 reports struct.error ("offset ... out of range") where Python
    raises OverflowError. The messages assume a 64-bit Python (ssize_t = int64)."""
    return max(_INT64_MIN, min(_INT64_MAX, int(value)))


def _int_field(binding, key):
    """(present, value) of an index field the core uses in arithmetic. The struct carries ints only:
    a present non-int (which Python would combine dynamically, e.g. str * int) raises TypeError
    here instead of where Python fails, and this message is not Python's."""
    value = binding.get(key, _ABSENT)
    if value is _ABSENT:
        return 0, 0
    if not isinstance(value, int):
        raise TypeError(f"binding[{key!r}] must be an int for the native core, not {type(value).__name__}")
    return 1, _clamped(value)


def _binding(binding, *, stride_used=True, selected=False):
    """binding (a dict, read with .get as Python does) as a dc_cp_binding.

    layout / native_layout are compared with ``==`` to the understood names here, exactly as
    Python compares them. native_info_stride keeps its presence; a falsy value is passed as 0,
    which the core replaces with the layout default as ``stride or default`` does. With
    ``selected``, native_index is reduced to what ``_matches`` asks of it (``i == selected`` for
    i in range(32)): that i, or -1 when no row equals it.
    """
    out = _Binding()
    layout = binding.get("layout")
    native_layout = binding.get("native_layout")
    out.layout = (DC_CP_ABSENT if layout is None and "layout" not in binding
                  else DC_CP_LAYOUT_RM26 if layout == "rm26"
                  else DC_CP_LAYOUT_LEGACY32 if layout == "legacy32" else DC_CP_OTHER)
    out.native_layout = (DC_CP_ABSENT if native_layout is None and "native_layout" not in binding
                         else DC_CP_NATIVE_POLICY124 if native_layout == "policy124"
                         else DC_CP_NATIVE_RM26_EXTENDED if native_layout == "rm26_extended" else DC_CP_OTHER)
    stride = binding.get("native_info_stride", _ABSENT)
    if stride is not _ABSENT:
        out.has_native_info_stride = 1
        if not stride:
            out.native_info_stride = 0
        elif isinstance(stride, int):
            out.native_info_stride = _clamped(stride)
        elif stride_used:
            raise TypeError(f"binding['native_info_stride'] must be an int for the native core, "
                            f"not {type(stride).__name__}")
    if selected:
        index = binding.get("native_index", _ABSENT)
        if index is not _ABSENT:
            out.has_native_index = 1
            out.native_index = next((i for i in range(32) if i == index), -1)
    else:
        out.has_native_index, out.native_index = _int_field(binding, "native_index")
        out.has_client_index, out.client_index = _int_field(binding, "client_index")
    return out


def _stride_object(binding):
    """The stride Python's _native_geometry returns when the core cannot: a truthy native_info_stride
    that is not a plain int64 int (``stride or default`` returns the object itself)."""
    stride = binding.get("native_info_stride")
    if stride and (type(stride) is not int or not _INT64_MIN <= stride <= _INT64_MAX):
        return stride
    return _ABSENT


def _decode(text):
    try:
        return bytes.fromhex(text)
    except Exception:  # re-raised from the text itself where the core needs it (_raise)
        return None


def _payload(data):
    payload = _Payload()
    if data is not None:
        payload.present = 1
        payload.length = len(data)
        payload.data = ctypes.cast(ctypes.c_char_p(data), ctypes.c_void_p) if data else None
    return payload


def _raise(error, payload_text=None, binding_get=None):
    message = error.message.decode("utf-8")
    if error.kind == DC_CP_ERROR_STRUCT:
        raise struct.error(message)
    if error.kind == DC_CP_ERROR_INDEX:
        raise IndexError(message)
    if error.kind in (DC_CP_ERROR_OVERFLOW, DC_CP_ERROR_RANGE):
        raise OverflowError(message)
    if error.kind == DC_CP_ERROR_KEY:
        raise KeyError(message)
    if error.kind == DC_CP_ERROR_PAYLOAD and payload_text is not None:
        bytes.fromhex(payload_text(error.key, error.side))  # raises Python's own exception
    if error.kind == DC_CP_ERROR_BINDING and binding_get is not None:
        binding_get()  # raises Python's own AttributeError
    raise RuntimeError(f"native client-power helper: unexpected error {error.kind}: {message}")


# ---- the helpers ------------------------------------------------------------ #

def small_native_geometry(binding):
    """_small_native_geometry(binding)."""
    size, stride = ctypes.c_uint32(), ctypes.c_uint32()
    status = load().dc_cp_small_native_geometry(ctypes.byref(_binding(binding, stride_used=False)),
                                                ctypes.byref(size), ctypes.byref(stride))
    if status != DC_OK:
        raise RuntimeError(f"dc_cp_small_native_geometry: status {status}")
    return size.value, stride.value


def definition_stride(role_info_size):
    """_definition_stride(role_info_size) for an int64 size. Python subtracts any number; the
    core takes ints only (TypeError here otherwise) and refuses a result outside int64
    (OverflowError, where Python returns the big int)."""
    if not isinstance(role_info_size, int):
        raise TypeError(f"role_info_size must be an int for the native core, not {type(role_info_size).__name__}")
    if not _INT64_MIN <= role_info_size <= _INT64_MAX:
        raise OverflowError("role_info_size does not fit int64")
    out = ctypes.c_int64()
    status = load().dc_cp_definition_stride(role_info_size, ctypes.byref(out))
    if status == DC_E_RANGE:
        raise OverflowError("definition stride does not fit int64")
    if status != DC_OK:
        raise RuntimeError(f"dc_cp_definition_stride: status {status}")
    return out.value


def native_geometry(binding):
    """_native_geometry(binding)."""
    out = _Geometry()
    status = load().dc_cp_native_geometry(ctypes.byref(_binding(binding, stride_used=False)), ctypes.byref(out))
    if status != DC_OK:
        raise RuntimeError(f"dc_cp_native_geometry: status {status}")
    stride = _stride_object(binding)
    return (out.info_base, out.info_stride if stride is _ABSENT else stride,
            out.control_base, out.control_stride, out.control_size)


def coupled_requests(observation):
    """_coupled_requests(observation)."""
    role = bytes.fromhex(observation["role_info_hex"])  # Python's own KeyError / ValueError
    coupled, error = ctypes.c_int32(), _Error()
    status = load().dc_cp_coupled_requests(role, len(role), ctypes.byref(coupled), ctypes.byref(error))
    if status == DC_E_FAILED:
        _raise(error)
    if status != DC_OK:
        raise RuntimeError(f"dc_cp_coupled_requests: status {status}")
    return bool(coupled.value)


def _mw(mw):
    """How struct.pack_into("<I", ...) sees mw: it converts through __index__."""
    try:
        value = operator.index(mw)
    except TypeError:
        return DC_CP_MW_NOT_INTEGER, 0
    if not _INT64_MIN <= value <= _INT64_MAX:
        return DC_CP_MW_OUT_OF_RANGE, 0
    return DC_CP_MW_INTEGER, value


def project(before, mw):
    """_project(before, mw)."""
    result = copy.deepcopy({key: before[key] for key in _STATE_KEYS})
    result["requested_mw"] = mw
    # Python's _project reads binding with .get and [] only; the struct carries what it reads. The
    # INFO stride does not enter _project, so its type is not checked here.
    binding = _binding(before["binding"], stride_used=False)
    control, native_control = _decode(before["control_hex"]), _decode(before["native_control_hex"])
    kind, value = _mw(mw)
    control_out = ctypes.create_string_buffer(len(control) if control else 1)
    native_out = ctypes.create_string_buffer(len(native_control) if native_control else 1)
    error = _Error()
    status = load().dc_cp_project(ctypes.byref(binding), kind, value, ctypes.byref(_payload(control)),
                                  ctypes.byref(_payload(native_control)), control_out, native_out,
                                  ctypes.byref(error))
    if status == DC_E_FAILED:
        _raise(error, payload_text=lambda key, side: before[_STATE_KEYS[key]])
    if status != DC_OK:
        raise RuntimeError(f"dc_cp_project: status {status}")
    result["control_hex"] = control_out.raw[:len(control)].hex()
    result["native_control_hex"] = native_out.raw[:len(native_control)].hex()
    return result


def matches(expected, observed):
    """_matches(expected, observed).

    The comparisons of non-payload values (schema, identity, binding, ...) are Python's ``!=`` and
    stay here; a payload pair goes to the core as bytes when both texts are canonical lower-case
    hex (so equal bytes mean equal text), and is otherwise compared here as text, as Python does.
    One difference is not reproduced: Python's uncoupled path uses dict ``==`` (identity first)
    and its coupled path ``!=``; for a value whose ``==`` and ``!=`` disagree (a NaN, say) this
    binding uses ``!=`` on both paths.
    """
    actual = {key: observed[key] for key in _STATE_KEYS}  # _state(observed): Python's KeyError
    request = _MatchInput()
    request.size = ctypes.sizeof(_MatchInput)
    request.extra_keys = int(any(key not in _STATE_KEYS for key in expected))
    keep = []
    for k, key in enumerate(_STATE_KEYS):
        if key not in expected:
            request.compare[k] = DC_CP_CMP_MISSING
            if key in _PAYLOAD_KEYS:
                request.observed[k - DC_CP_FIRST_PAYLOAD_KEY] = _payload(_decode(actual[key]))
                keep.append(request.observed[k - DC_CP_FIRST_PAYLOAD_KEY])
            continue
        if key in _PAYLOAD_KEYS:
            i = k - DC_CP_FIRST_PAYLOAD_KEY
            old, new = _decode(expected[key]), _decode(actual[key])
            keep += [old, new]
            request.expected[i], request.observed[i] = _payload(old), _payload(new)
            canonical = (old is not None and new is not None and isinstance(expected[key], str)
                         and isinstance(actual[key], str) and old.hex() == expected[key] and new.hex() == actual[key])
            if canonical:
                request.compare[k] = DC_CP_CMP_BYTES
                continue
        request.compare[k] = DC_CP_CMP_DIFFERENT if actual[key] != expected[key] else DC_CP_CMP_EQUAL
    raw_binding = expected.get("binding", _ABSENT)
    if raw_binding is not _ABSENT and callable(getattr(raw_binding, "get", None)):
        request.binding_usable = 1
        request.binding = _binding(raw_binding, selected=True)
    out, error = ctypes.c_int32(), _Error()
    status = load().dc_cp_matches(ctypes.byref(request), ctypes.byref(out), ctypes.byref(error))
    if status == DC_E_FAILED:
        _raise(error, payload_text=lambda key, side: (expected, actual)[side][_STATE_KEYS[key]],
               binding_get=lambda: expected["binding"].get("native_info_stride"))
    if status != DC_OK:
        raise RuntimeError(f"dc_cp_matches: status {status}")
    return bool(out.value)
