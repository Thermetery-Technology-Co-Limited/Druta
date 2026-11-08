# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native core (drutacore) through its C ABI.

The core is replacing the Python backend one feature at a time. Each feature
exposed here keeps the signature and the error behaviour of the Python module
it replaces, so the same tests run against both. Python-side type checks
(bytes versus bytearray, bool versus int) stay here; the core validates the
numbers and the packets.

The library is found, in order, at ``DRUTA_NATIVE_LIB``, beside a frozen
executable, or in a source checkout's ``native/build/linux`` tree. It is
optional: :func:`available` says whether it loaded, and nothing in the
application requires it yet.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import threading

from . import rail_transport as _py_rail

ABI_VERSION = 1

DC_OK = 0
DC_E_ARGUMENT = -1
DC_E_BUFFER = -2
DC_E_PARSE = -3
DC_E_UNRECOGNIZED = -4
DC_E_MASK = -5
DC_E_RECORDS = -6
DC_E_RANGE = -7


class NativeUnavailable(ImportError):
    """The native core could not be loaded; the reason is the message."""


class _RailLayout(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "packet_size", "params_size", "get_command", "set_command", "mask_word", "boost_word",
        "record0", "record_stride", "record_type")] + [("valid_word", ctypes.c_int32)]


class _RailRecord(ctypes.Structure):
    _fields_ = [("rail", ctypes.c_uint32), ("values", ctypes.c_int32 * 4)]


_SIGNATURES = {
    "dc_abi_version": (ctypes.c_uint32, []),
    "dc_status_text": (ctypes.c_char_p, [ctypes.c_int32]),
    "dc_parse_slot": (ctypes.c_int32, [ctypes.c_char_p] + [ctypes.POINTER(ctypes.c_uint32)] * 4),
    "dc_format_slot": (ctypes.c_int32, [ctypes.c_uint32] * 4 + [ctypes.c_char_p, ctypes.c_size_t,
                                                                 ctypes.POINTER(ctypes.c_size_t)]),
    "dc_same_slot": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_char_p]),
    "dc_rail_layout_count": (ctypes.c_uint32, []),
    "dc_rail_layout_at": (ctypes.c_int32, [ctypes.c_uint32, ctypes.POINTER(_RailLayout)]),
    "dc_rail_recognize": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32,
                                           ctypes.POINTER(ctypes.c_uint32)]),
    "dc_rail_prepare_write": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32,
                                               ctypes.POINTER(_RailRecord), ctypes.c_size_t, ctypes.c_uint32,
                                               ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]),
    "dc_rail_packet_summary": (ctypes.c_int32, [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
                                                ctypes.POINTER(ctypes.c_size_t)]),
}

_lock = threading.Lock()
_library = None
_error = None


def _library_name():
    if sys.platform == "win32":
        return "drutacore.dll"
    if sys.platform == "darwin":
        return "libdrutacore.dylib"
    return "libdrutacore.so"


def candidate_paths(environ=None):
    """Where the core may be, most specific first. Only absolute paths."""
    environ = os.environ if environ is None else environ
    out = []
    override = environ.get("DRUTA_NATIVE_LIB")
    if override:
        out.append(Path(override))
    name = _library_name()
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).resolve().parent / name)
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            out.append(Path(bundle) / name)
    else:
        from .paths import source_root
        root = source_root()
        if root is not None:
            for config in ("linux", "msvc", "mingw-x86_64", "mingw-i686"):
                out.append(root / "native" / "build" / config / name)
    return [path for path in out if path.is_absolute()]


def _bind(library):
    for name, (restype, argtypes) in _SIGNATURES.items():
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes
    return library


def load():
    """The bound library; raises NativeUnavailable with the reason."""
    global _library, _error
    with _lock:
        if _library is not None:
            return _library
        if _error is not None:
            raise NativeUnavailable(_error)
        failures = []
        for path in candidate_paths():
            if not path.is_file():
                failures.append(f"{path}: not present")
                continue
            try:
                library = _bind(ctypes.CDLL(str(path)))
            except (OSError, AttributeError) as exc:
                failures.append(f"{path}: {exc}")
                continue
            version = library.dc_abi_version()
            if version != ABI_VERSION:
                failures.append(f"{path}: ABI {version}, this build needs {ABI_VERSION}")
                continue
            _library = library
            library.path = str(path)
            return library
        _error = "native core not loaded: " + ("; ".join(failures) or "no candidate location")
        raise NativeUnavailable(_error)


def available():
    try:
        load()
    except NativeUnavailable:
        return False
    return True


def _raise(status):
    text = load().dc_status_text(status).decode()
    if status == DC_E_RANGE:
        text = "boost must be an integer from 0 through 100"
    raise ValueError(text)


# ---- PCI slot (nvbackend.format_slot / parse_slot / same_slot) ----------------------------------

def format_slot(domain, bus, device, function=0):
    buffer = ctypes.create_string_buffer(64)
    needed = ctypes.c_size_t()
    status = load().dc_format_slot(domain, bus, device, function, buffer, 64, ctypes.byref(needed))
    if status != DC_OK:
        _raise(status)
    return buffer.value.decode()


def parse_slot(text):
    if not text:
        return None
    values = [ctypes.c_uint32() for _ in range(4)]
    # str.strip() removes Unicode whitespace; the core strips ASCII only.
    status = load().dc_parse_slot(str(text).strip().encode("utf-8", "replace"), *map(ctypes.byref, values))
    if status == DC_E_PARSE:
        return None
    if status != DC_OK:
        _raise(status)
    return tuple(value.value for value in values)


def same_slot(a, b):
    if not a or not b:
        return False
    return bool(load().dc_same_slot(str(a).strip().encode("utf-8", "replace"),
                                    str(b).strip().encode("utf-8", "replace")))


# ---- voltage-rail packets (rail_transport) ------------------------------------------------------

def _is_plain_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def rail_known_layouts():
    """The Python module's own layout objects, checked against the core's table."""
    library = load()
    count = library.dc_rail_layout_count()
    if count != len(_py_rail.KNOWN_LAYOUTS):
        raise NativeUnavailable(f"native core knows {count} rail layouts, Python {len(_py_rail.KNOWN_LAYOUTS)}")
    for index, expected in enumerate(_py_rail.KNOWN_LAYOUTS):
        layout = _RailLayout()
        if library.dc_rail_layout_at(index, ctypes.byref(layout)) != DC_OK:
            raise NativeUnavailable(f"native core cannot describe rail layout {index}")
        valid = None if layout.valid_word < 0 else layout.valid_word
        actual = type(expected)(layout.packet_size, layout.params_size, layout.get_command, layout.set_command,
                                layout.mask_word, layout.boost_word, layout.record0, layout.record_stride,
                                layout.record_type, valid)
        if actual != expected:
            raise NativeUnavailable(f"native rail layout {index} differs from Python: {actual} != {expected}")
    return _py_rail.KNOWN_LAYOUTS


def rail_recognize_packet(payload, requested_mask):
    if not isinstance(payload, bytes) or not _is_plain_int(requested_mask) or not 0 <= requested_mask <= 0xFFFFFFFF:
        return None
    index = ctypes.c_uint32()
    status = load().dc_rail_recognize(payload, len(payload), requested_mask, ctypes.byref(index))
    if status == DC_E_UNRECOGNIZED:
        return None
    if status != DC_OK:
        _raise(status)
    return rail_known_layouts()[index.value]


def rail_prepare_write(payload, requested_mask, records, boost):
    # The Python-type checks of rail_transport._validated_records, in its order.
    if not _is_plain_int(requested_mask) or not _py_rail._valid_requested_mask(requested_mask):
        raise ValueError("requested_mask must select rail 0, rail 1, or both")
    if not hasattr(records, "keys") or not hasattr(records, "__getitem__"):
        raise ValueError("records must be a mapping")
    expected_rails = {rail for rail in range(2) if requested_mask & (1 << rail)}
    if any(not _is_plain_int(rail) for rail in records) or set(records) != expected_rails:
        raise ValueError("requested_mask must exactly match the record rails")
    native = (_RailRecord * max(len(records), 1))()
    for position, (rail, values) in enumerate(records.items()):
        try:
            record = tuple(values)
        except TypeError as exc:
            raise ValueError(f"rail {rail} record must contain four integers") from exc
        if len(record) != 4:
            raise ValueError(f"rail {rail} record must contain four integers")
        if any(not _is_plain_int(value) or not -(1 << 31) <= value <= (1 << 31) - 1 for value in record):
            raise ValueError(f"rail {rail} record values must be signed int32 integers")
        native[position].rail = rail
        native[position].values[:] = record
    if not _is_plain_int(boost) or not 0 <= boost <= 100:
        raise ValueError("boost must be an integer from 0 through 100")
    if not isinstance(payload, bytes):
        raise ValueError("payload is not an exact known rail GET packet")
    out = ctypes.create_string_buffer(len(payload))
    needed = ctypes.c_size_t()
    status = load().dc_rail_prepare_write(payload, len(payload), requested_mask, native, len(records), boost,
                                          out, len(payload), ctypes.byref(needed))
    if status != DC_OK:
        _raise(status)
    return out.raw[:needed.value]


def rail_packet_summary(payload):
    if not isinstance(payload, bytes):
        return "rail packet invalid: payload is not bytes"
    buffer = ctypes.create_string_buffer(256)
    needed = ctypes.c_size_t()
    status = load().dc_rail_packet_summary(payload, len(payload), buffer, 256, ctypes.byref(needed))
    if status == DC_E_BUFFER:
        buffer = ctypes.create_string_buffer(needed.value)
        status = load().dc_rail_packet_summary(payload, len(payload), buffer, needed.value, ctypes.byref(needed))
    if status != DC_OK:
        _raise(status)
    return buffer.value.decode()
