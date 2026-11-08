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
import math
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
DC_E_UNAVAILABLE = -8


class NativeUnavailable(ImportError):
    """The native core could not be loaded; the reason is the message."""


class _RailLayout(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "packet_size", "params_size", "get_command", "set_command", "mask_word", "boost_word",
        "record0", "record_stride", "record_type")] + [("valid_word", ctypes.c_int32)]


class _RailRecord(ctypes.Structure):
    _fields_ = [("rail", ctypes.c_uint32), ("values", ctypes.c_int32 * 4)]


class _GpuEntry(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("slot", ctypes.c_char * 24), ("name", ctypes.c_char * 96),
                ("uuid", ctypes.c_char * 96), ("has_devid", ctypes.c_int32), ("devid", ctypes.c_uint32),
                ("subsys", ctypes.c_uint32), ("nvml_index", ctypes.c_int32), ("has_nvapi", ctypes.c_int32)]


class _GpuIdentity(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("nvapi_ok", ctypes.c_int32), ("nvml_ok", ctypes.c_int32),
                ("nvapi_error", ctypes.c_char * 512), ("nvml_error", ctypes.c_char * 1024),
                ("nvml_path", ctypes.c_char * 512), ("pairing_error", ctypes.c_char * 512),
                ("requested_slot", ctypes.c_char * 24), ("slot", ctypes.c_char * 24),
                ("nvapi_selected", ctypes.c_int32), ("nvapi_slot", ctypes.c_char * 24),
                ("nvapi_devid", ctypes.c_uint32), ("nvapi_subsys", ctypes.c_uint32),
                ("nvml_selected", ctypes.c_int32), ("nvml_slot", ctypes.c_char * 24),
                ("nvml_has_devid", ctypes.c_int32), ("nvml_devid", ctypes.c_uint32),
                ("nvml_subsys", ctypes.c_uint32), ("nvml_index", ctypes.c_int32),
                ("name", ctypes.c_char * 96), ("driver", ctypes.c_char * 96), ("vbios", ctypes.c_char * 96),
                ("uuid", ctypes.c_char * 96), ("admin", ctypes.c_int32)]


class _FanReading(ctypes.Structure):
    _fields_ = [("has_duty", ctypes.c_int32), ("duty", ctypes.c_uint32), ("has_rpm", ctypes.c_int32),
                ("rpm", ctypes.c_uint32)]


class _TypedValue(ctypes.Structure):
    _fields_ = [("present", ctypes.c_int32), ("type", ctypes.c_uint32), ("d", ctypes.c_double),
                ("u", ctypes.c_uint64), ("s", ctypes.c_int64)]


class _Telemetry(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint64),
                ("core", ctypes.c_uint32), ("mem", ctypes.c_uint32), ("video", ctypes.c_uint32), ("xbar", ctypes.c_uint32),
                ("core_off", ctypes.c_int32), ("mem_off", ctypes.c_int32),
                ("priv_clocks", ctypes.c_uint32 * 288),
                ("temp_edge", ctypes.c_int32), ("temp_hotspot", ctypes.c_double),
                ("temp_sensor_count", ctypes.c_uint32), ("temp_sensors", ctypes.c_double * 32),
                ("temp_delta", ctypes.c_double),
                ("pl_requested_mw", ctypes.c_uint32), ("power_w", ctypes.c_double), ("pl_now_mw", ctypes.c_uint32),
                ("pwr_gpu_pct", ctypes.c_double), ("pwr_board_pct", ctypes.c_double),
                ("pl_target_pct", ctypes.c_double), ("vcore_mv", ctypes.c_double),
                ("num_fans", ctypes.c_uint32), ("fan_count", ctypes.c_uint32), ("fans", _FanReading * 32),
                ("util_gpu", ctypes.c_uint32), ("util_fb", ctypes.c_uint32), ("util_vid", ctypes.c_uint32),
                ("util_bus", ctypes.c_uint32),
                ("event_mask", ctypes.c_uint64), ("perf_decrease", ctypes.c_uint32),
                ("pcie_gen", ctypes.c_uint32), ("pcie_width", ctypes.c_uint32), ("pcie_err", _TypedValue * 11),
                ("pcie_err_total", ctypes.c_double), ("pcie_err_since", ctypes.c_double),
                ("pstate", ctypes.c_uint32), ("vboost_pct", ctypes.c_int32),
                ("core_p0max", ctypes.c_uint32), ("mem_p0max", ctypes.c_uint32), ("energy_j", ctypes.c_double),
                ("vf_locked_count", ctypes.c_uint32), ("vf_locked_domains", ctypes.c_uint32 * 32),
                ("vf_lock_mv", ctypes.c_double),
                ("clk_lock_min_mhz", ctypes.c_uint32), ("clk_lock_max_mhz", ctypes.c_uint32)]


_T_BITS = ["core", "mem", "video", "xbar", "core_off", "mem_off", "priv_clocks", "temp_edge", "temp_hotspot",
           "temp_delta", "pl_requested_mw", "power_w", "pl_now_mw", "pwr_gpu_pct", "pwr_board_pct", "pl_target_pct",
           "vcore_mv", "num_fans", "util_gpu", "util_fb", "util_vid", "util_bus", "event_mask", "perf_decrease",
           "pcie_gen", "pcie_width", "pcie_err", "pstate", "vboost_pct", "core_p0max", "mem_p0max", "energy_j",
           "vf_locked_domains", "vf_lock_mv", "clk_lock_mhz"]
PCIE_ERR_NAMES = ["correctable", "naks_rx", "receiver", "bad_tlp", "naks_tx", "bad_dllp", "non_fatal", "fatal",
                  "unsupp_req", "lcrc", "lane"]


class _OffsetRange(ctypes.Structure):
    _fields_ = [("present", ctypes.c_int32), ("low", ctypes.c_int32), ("high", ctypes.c_int32),
                ("current", ctypes.c_int32)]


class _Capabilities(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("present", ctypes.c_uint32),
                ("pl_min_mw", ctypes.c_uint32), ("pl_max_mw", ctypes.c_uint32), ("pl_def_mw", ctypes.c_uint32),
                ("mem_clock_count", ctypes.c_uint32), ("mem_clocks", ctypes.c_uint32 * 512),
                ("gfx_min", ctypes.c_uint32), ("gfx_max", ctypes.c_uint32),
                ("fan_min", ctypes.c_uint32), ("fan_max", ctypes.c_uint32),
                ("mem_type_id", ctypes.c_uint32), ("mem_type", ctypes.c_char * 32), ("mem_div", ctypes.c_uint32),
                ("core_off_range", _OffsetRange), ("mem_off_range", _OffsetRange)]


_C_BITS = ["pl_min_mw", "pl_max_mw", "pl_def_mw", "gfx_min", "gfx_max", "fan_min", "fan_max", "mem_type_id", "mem_div"]


class _FanRow(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("level", ctypes.c_uint32), ("policy", ctypes.c_uint32),
                ("default_policy", ctypes.c_uint32), ("manual", ctypes.c_int32), ("min", ctypes.c_uint32),
                ("max", ctypes.c_uint32), ("has_rpm", ctypes.c_int32), ("rpm", ctypes.c_uint32)]


class _FanState(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("source", ctypes.c_char * 16), ("count", ctypes.c_uint32),
                ("fans", _FanRow * 32)]


class _FanCapabilities(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("manual", ctypes.c_int32), ("automatic", ctypes.c_int32),
                ("source", ctypes.c_char * 16), ("min", ctypes.c_uint32), ("max", ctypes.c_uint32)]


class _Outcome(ctypes.Structure):
    _fields_ = [("ok", ctypes.c_int32), ("message", ctypes.c_char * 4096)]


class _FanSnapshotRow(ctypes.Structure):
    _fields_ = [("has_manual", ctypes.c_int32), ("manual", ctypes.c_int32), ("has_id", ctypes.c_int32),
                ("id", ctypes.c_uint32), ("level_valid", ctypes.c_int32), ("level", ctypes.c_int32),
                ("has_policy", ctypes.c_int32), ("policy", ctypes.c_uint32)]


class _FanSnapshot(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("is_mapping", ctypes.c_int32), ("source", ctypes.c_char * 16),
                ("count", ctypes.c_uint32), ("fans", _FanSnapshotRow * 32)]


class _VfPointLock(ctypes.Structure):
    _fields_ = [("domain", ctypes.c_uint32), ("lock_mode", ctypes.c_uint32), ("volt_uv", ctypes.c_uint32),
                ("count", ctypes.c_uint32)]


class _VfLockStatus(ctypes.Structure):
    _fields_ = [("locked", ctypes.c_int32), ("lock", _VfPointLock), ("error", ctypes.c_char * 256)]


class _ClkLockStatus(ctypes.Structure):
    _fields_ = [("locked", ctypes.c_int32), ("min_mhz", ctypes.c_uint32), ("max_mhz", ctypes.c_uint32),
                ("error", ctypes.c_char * 256)]


VFP_POINTS = 128


class _VfLayout(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("n_entries", ctypes.c_uint32), ("gpu_count", ctypes.c_uint32),
                ("gpu_idx", ctypes.c_uint32 * VFP_POINTS), ("other_count", ctypes.c_uint32),
                ("other_idx", ctypes.c_uint32 * VFP_POINTS), ("freq_div", ctypes.c_uint32),
                ("notes", ctypes.c_char * 256), ("error", ctypes.c_char * 256)]


class _VfPoint(ctypes.Structure):
    _fields_ = [("idx", ctypes.c_uint32), ("volt_mv", ctypes.c_double), ("freq_mhz", ctypes.c_double),
                ("delta_khz", ctypes.c_int32)]


class _VfCurve(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("count", ctypes.c_uint32), ("points", _VfPoint * VFP_POINTS),
                ("error", ctypes.c_char * 256)]


class _VfOtherRow(ctypes.Structure):
    _fields_ = [("idx", ctypes.c_uint32), ("volt_mv", ctypes.c_double), ("value", ctypes.c_double),
                ("memory", ctypes.c_int32)]


class _VfOtherRows(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("count", ctypes.c_uint32), ("rows", _VfOtherRow * VFP_POINTS)]


class _VfDelta(ctypes.Structure):
    _fields_ = [("idx", ctypes.c_int64), ("delta_khz", ctypes.c_int64)]


class _VfDeltaSnapshot(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("freq_div", ctypes.c_uint32), ("count", ctypes.c_uint32),
                ("deltas", _VfDelta * VFP_POINTS)]


class _MemOffsetScale(ctypes.Structure):
    _fields_ = [("scale", ctypes.c_uint32), ("unit", ctypes.c_char * 16)]


class _ClockRow(ctypes.Structure):
    _fields_ = [("mem_mhz", ctypes.c_uint32), ("count", ctypes.c_uint32), ("graphics", ctypes.c_uint32 * 512)]


class VfpLayout:
    """nvbackend.VfpLayout's shape, filled from the core."""

    __slots__ = ("n_entries", "gpu_idx", "other_idx", "freq_div", "notes")

    def __init__(self, n_entries, gpu_idx, other_idx, freq_div, notes):
        self.n_entries, self.gpu_idx, self.other_idx = n_entries, gpu_idx, other_idx
        self.freq_div, self.notes = freq_div, notes

    @property
    def n_gpu(self):
        return len(self.gpu_idx)


# resolve(context, library, name) -> function pointer or NULL (the test seam)
RESOLVER = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p)
SLEEP_HOOK = ctypes.CFUNCTYPE(None, ctypes.c_uint32)

_SIGNATURES = {
    "dc_set_export_resolver": (None, [RESOLVER, ctypes.c_void_p]),
    "dc_enumerate_gpus": (ctypes.c_int32, [ctypes.POINTER(_GpuEntry), ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_open": (ctypes.c_int32, [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]),
    "dc_gpu_close": (None, [ctypes.c_void_p]),
    "dc_gpu_identity_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_GpuIdentity)]),
    "dc_gpu_status_line": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_architecture": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
    "dc_gpu_read_telemetry": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_Telemetry)]),
    "dc_gpu_capabilities_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_Capabilities)]),
    "dc_gpu_fan_state_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_FanState)]),
    "dc_gpu_fan_manual_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_fan_capabilities_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_FanCapabilities)]),
    "dc_gpu_fan_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_Outcome)]),
    "dc_gpu_fan_reset": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_Outcome)]),
    "dc_gpu_fan_restore": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_FanSnapshot), ctypes.POINTER(_Outcome)]),
    "dc_set_sleep_hook": (None, [SLEEP_HOOK]),
    "dc_gpu_voltage_boost_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_voltage_boost_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.POINTER(_Outcome)]),
    "dc_gpu_vf_lock_status": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64,
                                               ctypes.POINTER(_VfLockStatus)]),
    "dc_gpu_clk_lock_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                             ctypes.POINTER(ctypes.c_uint32)]),
    "dc_gpu_clk_lock_status": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClkLockStatus)]),
    "dc_gpu_vf_lock_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64,
                                            ctypes.POINTER(_Outcome)]),
    "dc_gpu_vf_lock_recovery_pending": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_gpu_vf_lock_recover": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_Outcome)]),
    "dc_gpu_vf_lock_clear": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64, ctypes.c_int32,
                                              ctypes.c_int64, ctypes.POINTER(_Outcome)]),
    "dc_gpu_vf_lock_self_test": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_Outcome)]),
    "dc_gpu_vf_layout": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_VfLayout)]),
    "dc_gpu_vf_curve_read": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_VfCurve)]),
    "dc_gpu_vf_other_rows": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_VfOtherRows)]),
    "dc_gpu_vf_deltas_apply": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_VfDelta), ctypes.c_size_t,
                                                ctypes.POINTER(_Outcome)]),
    "dc_gpu_vf_curve_reset": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_Outcome)]),
    "dc_gpu_vf_delta_snapshot": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_VfDeltaSnapshot)]),
    "dc_gpu_mem_offset_scale": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_MemOffsetScale)]),
    "dc_gpu_memory_offset_step_units": (ctypes.c_uint32, [ctypes.c_void_p]),
    "dc_gpu_clock_step_khz": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                               ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_max_rise_khz": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
    "dc_gpu_lockable_clocks": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClockRow), ctypes.c_size_t,
                                                ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_clock_offset_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double, ctypes.c_int32,
                                                 ctypes.POINTER(_Outcome)]),
    "dc_gpu_clocks_lock": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int64, ctypes.POINTER(_Outcome)]),
    "dc_gpu_clocks_last_lock": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                                 ctypes.POINTER(ctypes.c_uint32)]),
    "dc_gpu_clocks_reset": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_Outcome)]),
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


# ---- GPU identity (nvbackend.Nvml / NvAPI / enumerate_gpus / GPU) ------------------------------

_resolver_keepalive = None


def set_export_resolver(resolve):
    """Serve driver exports from Python (tests). resolve(library, name) returns a
    ctypes function pointer address or None; name "" asks whether the library
    loads. None restores the platform loader."""
    global _resolver_keepalive
    library = load()
    if resolve is None:
        library.dc_set_export_resolver(RESOLVER(0), None)
        _resolver_keepalive = None
        return

    def trampoline(_context, lib, name):
        pointer = resolve(lib.decode(), name.decode())
        return pointer or 0

    _resolver_keepalive = RESOLVER(trampoline)
    library.dc_set_export_resolver(_resolver_keepalive, None)


_sleep_keepalive = None


def set_sleep_hook(hook):
    """Replace the core's bounded wait inside readback loops (tests). hook(ms)
    is called instead of sleeping; None restores the real sleep."""
    global _sleep_keepalive
    library = load()
    if hook is None:
        library.dc_set_sleep_hook(SLEEP_HOOK(0))
        _sleep_keepalive = None
        return
    _sleep_keepalive = SLEEP_HOOK(hook)
    library.dc_set_sleep_hook(_sleep_keepalive)


def _text(field):
    return field.decode(errors="replace")


def enumerate_gpus():
    """Same keys and order as nvbackend.enumerate_gpus()."""
    library = load()
    capacity = 8
    while True:
        entries = (_GpuEntry * capacity)()
        for entry in entries:
            entry.size = ctypes.sizeof(_GpuEntry)
        count = ctypes.c_size_t()
        status = library.dc_enumerate_gpus(entries, capacity, ctypes.byref(count))
        if status == DC_E_BUFFER:
            capacity = max(count.value, capacity * 2)
            continue
        if status != DC_OK:
            _raise(status)
        return [{
            "slot": _text(e.slot), "name": _text(e.name), "uuid": _text(e.uuid),
            "devid": e.devid if e.has_devid else None, "subsys": e.subsys if e.has_devid else None,
            "nvml_index": None if e.nvml_index < 0 else e.nvml_index, "has_nvapi": bool(e.has_nvapi),
        } for e in entries[:count.value]]


class _Half:
    """The .ok / .err_detail / .selected shape of nvbackend.NvAPI and Nvml."""

    def __init__(self, ok, err_detail, selected):
        self.ok, self.err_detail, self.selected = ok, err_detail, selected


class Gpu:
    """One card through the native core: nvbackend.GPU's identity surface."""

    def __init__(self, slot=None):
        library = load()
        handle = ctypes.c_void_p()
        status = library.dc_gpu_open((slot or "").encode(), ctypes.byref(handle))
        if status != DC_OK:
            _raise(status)
        self._handle = handle
        identity = _GpuIdentity()
        identity.size = ctypes.sizeof(_GpuIdentity)
        status = library.dc_gpu_identity_get(handle, ctypes.byref(identity))
        if status != DC_OK:
            self.close()
            _raise(status)
        self.requested_slot = _text(identity.requested_slot)
        nvapi_selected = None
        if identity.nvapi_selected:
            nvapi_selected = {"slot": _text(identity.nvapi_slot), "devid": identity.nvapi_devid,
                              "subsys": identity.nvapi_subsys}
        nvml_selected = None
        if identity.nvml_selected:
            nvml_selected = {"slot": _text(identity.nvml_slot), "nvml_index": identity.nvml_index,
                             "devid": identity.nvml_devid if identity.nvml_has_devid else None,
                             "subsys": identity.nvml_subsys if identity.nvml_has_devid else None,
                             "uuid": _text(identity.uuid)}
        self.nvapi = _Half(bool(identity.nvapi_ok), _text(identity.nvapi_error), nvapi_selected)
        self.nvml = _Half(bool(identity.nvml_ok), _text(identity.nvml_error), nvml_selected)
        self.nvml.dll_path = _text(identity.nvml_path)
        self.pairing_error = _text(identity.pairing_error)
        self.static = {"name": _text(identity.name), "driver": _text(identity.driver),
                       "vbios": _text(identity.vbios), "admin": bool(identity.admin),
                       "slot": _text(identity.slot), "uuid": _text(identity.uuid)}

    def close(self):
        handle, self._handle = self._handle, None
        if handle:
            load().dc_gpu_close(handle)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def available(self):
        return self.nvapi.ok or self.nvml.ok

    def slot(self):
        return self.static["slot"]

    def status_line(self):
        buffer = ctypes.create_string_buffer(4096)
        needed = ctypes.c_size_t()
        status = load().dc_gpu_status_line(self._handle, buffer, 4096, ctypes.byref(needed))
        if status != DC_OK:
            _raise(status)
        return buffer.value.decode(errors="replace")

    def arch(self):
        """NVML architecture enum, or None (nvbackend.GPU.arch)."""
        value = ctypes.c_uint32()
        status = load().dc_gpu_architecture(self._handle, ctypes.byref(value))
        if status == DC_E_UNRECOGNIZED:
            return None
        if status != DC_OK:
            _raise(status)
        return value.value

    def capabilities(self):
        """The non-identity keys of nvbackend.GPU.static."""
        c = _Capabilities()
        c.size = ctypes.sizeof(_Capabilities)
        status = load().dc_gpu_capabilities_get(self._handle, ctypes.byref(c))
        if status != DC_OK:
            _raise(status)
        s = {}
        for bit, key in enumerate(_C_BITS):
            if c.present & (1 << bit):
                s[key] = getattr(c, key)
        if c.mem_clock_count:
            s["mem_clocks"] = list(c.mem_clocks[:min(c.mem_clock_count, 512)])
        s["mem_type"] = _text(c.mem_type)
        s.setdefault("mem_div", None)
        for key in ("core_off_range", "mem_off_range"):
            r = getattr(c, key)
            s[key] = (r.low, r.high, r.current) if r.present else None
        return s

    def read(self):
        """One telemetry tick with nvbackend.GPU.read()'s keys.

        Not yet provided by the core (the Python backend still reads them):
        clk_domains / clkdom_offsets, the live-power fallback of
        pl_requested_mw, the Turing legacy clock-lock fallback, nvvdd_live_mv.
        """
        t = _Telemetry()
        t.size = ctypes.sizeof(_Telemetry)
        status = load().dc_gpu_read_telemetry(self._handle, ctypes.byref(t))
        if status != DC_OK:
            _raise(status)
        present = {key for bit, key in enumerate(_T_BITS) if t.present & (1 << bit)}
        d = {}
        for key in ("core", "mem", "video", "xbar", "core_off", "mem_off", "temp_edge", "pl_requested_mw",
                    "power_w", "pl_now_mw", "pwr_gpu_pct", "pwr_board_pct", "pl_target_pct", "vcore_mv", "num_fans",
                    "util_gpu", "util_fb", "util_vid", "util_bus", "event_mask", "perf_decrease", "pcie_gen",
                    "pcie_width", "pstate", "vboost_pct", "core_p0max", "mem_p0max", "energy_j", "vf_lock_mv",
                    "temp_delta"):
            if key in present:
                d[key] = getattr(t, key)
        if "temp_hotspot" in present:
            d["temp_hotspot"] = t.temp_hotspot
            d["temp_sensors"] = list(t.temp_sensors[:t.temp_sensor_count])
        if "temp_delta" in present:
            # Python: hotspot (float) - edge (int)
            d["temp_delta"] = t.temp_delta
        d["fans"] = [(f.duty if f.has_duty else None, f.rpm if f.has_rpm else None) for f in t.fans[:t.fan_count]]
        if "pcie_err" in present:
            per = {}
            for name, value in zip(PCIE_ERR_NAMES, t.pcie_err):
                if value.present:
                    per[name] = value.d if value.type == 0 else (value.s if value.type in (4, 5) else value.u)
            total = per.get("correctable", 0) + per.get("non_fatal", 0) + per.get("fatal", 0)
            d["pcie_err_total"] = total
            d["pcie_err"] = per
            since = t.pcie_err_since
            d["pcie_err_since"] = int(since) if isinstance(total, int) and float(since).is_integer() else since
        if "vf_locked_domains" in present:
            d["vf_locked_domains"] = list(t.vf_locked_domains[:t.vf_locked_count])
        if "clk_lock_mhz" in present:
            d["clk_lock_mhz"] = (t.clk_lock_min_mhz, t.clk_lock_max_mhz)
        if "priv_clocks" in present:
            d["priv_clocks"] = list(t.priv_clocks)
        return d

    # ---- fan control (nvbackend.GPU.read_fan_control_state .. restore_fan_control_state) -------- #

    def read_fan_control_state(self):
        """Requested levels/policies per fan, separate from spinning RPM, or None."""
        state = _FanState()
        state.size = ctypes.sizeof(_FanState)
        status = load().dc_gpu_fan_state_get(self._handle, ctypes.byref(state))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        fans = []
        for row in state.fans[:state.count]:
            fan = {"id": row.id, "level": row.level, "policy": row.policy, "default_policy": row.default_policy,
                   "manual": bool(row.manual), "min": row.min, "max": row.max}
            if row.has_rpm:
                fan["rpm"] = row.rpm
            fans.append(fan)
        return {"source": _text(state.source), "fans": fans}

    def read_fan_manual(self):
        """True/False when every fan shares that policy; None for unknown/mixed."""
        value = ctypes.c_int32()
        status = load().dc_gpu_fan_manual_get(self._handle, ctypes.byref(value))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return bool(value.value)

    def fan_capabilities(self):
        """Capabilities shared by UI, profiles, and the writer entry points."""
        c = _FanCapabilities()
        c.size = ctypes.sizeof(_FanCapabilities)
        status = load().dc_gpu_fan_capabilities_get(self._handle, ctypes.byref(c))
        if status != DC_OK:
            _raise(status)
        return {"manual": bool(c.manual), "auto": bool(c.automatic), "source": _text(c.source),
                "min": c.min, "max": c.max}

    @staticmethod
    def _outcome(outcome):
        return bool(outcome.ok), outcome.message.decode(errors="replace")

    def set_fan(self, pct):
        outcome = _Outcome()
        status = load().dc_gpu_fan_set(self._handle, int(pct), ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def reset_fan(self):
        outcome = _Outcome()
        status = load().dc_gpu_fan_reset(self._handle, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def restore_fan_control_state(self, state):
        """Restore each fan's requested duty and policy using current bindings.

        The snapshot is nvbackend's dict; its shape checks travel as flags so
        the core refuses with the same text, in the same order."""
        snapshot = _FanSnapshot()
        snapshot.size = ctypes.sizeof(_FanSnapshot)
        snapshot.is_mapping = isinstance(state, dict)
        if snapshot.is_mapping:
            source = state.get("source")
            snapshot.source = source.encode() if isinstance(source, str) else b""
            wanted = state.get("fans")
            if isinstance(wanted, list):
                snapshot.count = len(wanted)
                for row, old in zip(snapshot.fans, wanted):
                    _fill_snapshot_row(row, old)
        outcome = _Outcome()
        status = load().dc_gpu_fan_restore(self._handle, ctypes.byref(snapshot), ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    # ---- voltage boost (nvbackend.GPU.read_voltage_boost / set_voltage_boost) ------------------ #

    def read_voltage_boost(self):
        value = ctypes.c_int32()
        status = load().dc_gpu_voltage_boost_get(self._handle, ctypes.byref(value))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return value.value

    def set_voltage_boost(self, pct):
        """0..100 % of the VBIOS over-voltage headroom; read-modify-write."""
        outcome = _Outcome()
        status = load().dc_gpu_voltage_boost_set(self._handle, int(pct), ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    # ---- V/F point lock and frequency lock (nvbackend.GPU.*vf_lock* / *clk_lock*) -------------- #

    @staticmethod
    def _optional_int(value):
        return (1, int(value)) if value is not None else (0, 0)

    def read_vf_lock_status(self, domain=None):
        """(point lock, error), distinguishing unlocked from unreadable."""
        s = _VfLockStatus()
        has_domain, domain = self._optional_int(domain)
        status = load().dc_gpu_vf_lock_status(self._handle, has_domain, domain, ctypes.byref(s))
        if status != DC_OK:
            _raise(status)
        error = _text(s.error) or None
        if not s.locked:
            return None, error
        lock = s.lock
        return {"domain": lock.domain, "lockMode": lock.lock_mode, "volt_uV": lock.volt_uv,
                "volt_mv": lock.volt_uv / 1000.0, "count": lock.count}, None

    def read_vf_lock(self):
        return self.read_vf_lock_status()[0]

    def read_clk_lock(self):
        """The driver's current NVML frequency lock as (min_mhz, max_mhz), or None."""
        low, high = ctypes.c_uint32(), ctypes.c_uint32()
        status = load().dc_gpu_clk_lock_get(self._handle, ctypes.byref(low), ctypes.byref(high))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return (low.value, high.value)

    def read_clk_lock_status(self):
        """(frequency range, error), distinguishing unlocked from unknown."""
        s = _ClkLockStatus()
        status = load().dc_gpu_clk_lock_status(self._handle, ctypes.byref(s))
        if status != DC_OK:
            _raise(status)
        error = _text(s.error) or None
        return ((s.min_mhz, s.max_mhz) if s.locked else None), error

    def set_vf_lock(self, volt_uv, domain=None):
        outcome = _Outcome()
        has_domain, domain = self._optional_int(domain)
        status = load().dc_gpu_vf_lock_set(self._handle, int(volt_uv), has_domain, domain, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def vf_lock_recovery_pending(self):
        return bool(load().dc_gpu_vf_lock_recovery_pending(self._handle))

    def recover_vf_lock(self):
        outcome = _Outcome()
        status = load().dc_gpu_vf_lock_recover(self._handle, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def clear_vf_lock(self, domain=None, expected_uv=None):
        outcome = _Outcome()
        has_domain, domain = self._optional_int(domain)
        has_expected, expected_uv = self._optional_int(expected_uv)
        status = load().dc_gpu_vf_lock_clear(self._handle, has_domain, domain, has_expected, expected_uv,
                                             ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def vf_lock_self_test(self):
        outcome = _Outcome()
        status = load().dc_gpu_vf_lock_self_test(self._handle, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    # ---- V/F curve (nvbackend.GPU.vfp_layout .. _vf_delta_snapshot) ---------------------------- #

    def vfp_layout(self, force=False):
        """This card's probed V/F table shape (VfpLayout), or None."""
        layout = _VfLayout()
        layout.size = ctypes.sizeof(_VfLayout)
        status = load().dc_gpu_vf_layout(self._handle, 1 if force else 0, ctypes.byref(layout))
        if status == DC_E_UNAVAILABLE:
            self._vfp_layout_error = _text(layout.error)
            return None
        if status != DC_OK:
            _raise(status)
        self._vfp_layout_error = ""
        return VfpLayout(layout.n_entries, list(layout.gpu_idx[:layout.gpu_count]),
                         list(layout.other_idx[:layout.other_count]), layout.freq_div, _text(layout.notes))

    def read_vf_curve(self):
        """(points, err): the GPU points as {idx, volt_mv, freq_mhz, delta_khz} dicts, or an error."""
        curve = _VfCurve()
        curve.size = ctypes.sizeof(_VfCurve)
        status = load().dc_gpu_vf_curve_read(self._handle, ctypes.byref(curve))
        if status != DC_OK:
            _raise(status)
        if not curve.count:
            return None, _text(curve.error)
        return [{"idx": p.idx, "volt_mv": p.volt_mv, "freq_mhz": p.freq_mhz, "delta_khz": p.delta_khz}
                for p in curve.points[:curve.count]], None

    def read_vfp_other_rows(self):
        rows = _VfOtherRows()
        rows.size = ctypes.sizeof(_VfOtherRows)
        status = load().dc_gpu_vf_other_rows(self._handle, ctypes.byref(rows))
        if status != DC_OK:
            _raise(status)
        return [{"idx": r.idx, "volt_mv": r.volt_mv, "value": r.value, "kind": "memory" if r.memory else "unknown"}
                for r in rows.rows[:rows.count]]

    def apply_vf_deltas(self, new_deltas):
        """new_deltas maps idx -> absolute delta_khz; only differing GPU rows are written."""
        items = [(int(idx), int(delta)) for idx, delta in new_deltas.items()]
        array = (_VfDelta * max(1, len(items)))()
        for k, (idx, delta) in enumerate(items):
            array[k].idx, array[k].delta_khz = idx, delta
        outcome = _Outcome()
        status = load().dc_gpu_vf_deltas_apply(self._handle, array, len(items), ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def reset_vf_curve(self):
        outcome = _Outcome()
        status = load().dc_gpu_vf_curve_reset(self._handle, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def vf_delta_snapshot(self):
        """{"freq_div", "deltas": {idx: delta_khz}} or None (nvbackend.GPU._vf_delta_snapshot)."""
        snapshot = _VfDeltaSnapshot()
        snapshot.size = ctypes.sizeof(_VfDeltaSnapshot)
        status = load().dc_gpu_vf_delta_snapshot(self._handle, ctypes.byref(snapshot))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return {"freq_div": snapshot.freq_div,
                "deltas": {d.idx: d.delta_khz for d in snapshot.deltas[:snapshot.count]}}

    # ---- clock offsets, frequency lock, clock grid (nvbackend.GPU.set_clock_offset ..) --------- #

    def mem_offset_scale(self):
        """(scale, unit): NVML mem-offset units per slider unit and the slider's unit."""
        s = _MemOffsetScale()
        status = load().dc_gpu_mem_offset_scale(self._handle, ctypes.byref(s))
        if status != DC_OK:
            _raise(status)
        return s.scale, _text(s.unit)

    def memory_offset_step_units(self):
        return load().dc_gpu_memory_offset_step_units(self._handle)

    def clock_step_khz(self):
        """This card's core-clock grid in kHz (VF_STEP_KHZ when it cannot be measured)."""
        step, measured = ctypes.c_uint32(), ctypes.c_int32()
        status = load().dc_gpu_clock_step_khz(self._handle, ctypes.byref(step), ctypes.byref(measured))
        if status != DC_OK:
            _raise(status)
        return step.value

    def step_is_measured(self):
        step, measured = ctypes.c_uint32(), ctypes.c_int32()
        status = load().dc_gpu_clock_step_khz(self._handle, ctypes.byref(step), ctypes.byref(measured))
        if status != DC_OK:
            _raise(status)
        return bool(measured.value)

    def max_rise_khz(self):
        khz = ctypes.c_uint32()
        status = load().dc_gpu_max_rise_khz(self._handle, ctypes.byref(khz))
        if status != DC_OK:
            _raise(status)
        return khz.value

    def lockable_clocks_by_mem(self):
        """[(mem_mhz, [graphics clocks])], one row per memory clock that answers."""
        count = ctypes.c_size_t()
        status = load().dc_gpu_lockable_clocks(self._handle, None, 0, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        rows = (_ClockRow * max(1, count.value))()
        status = load().dc_gpu_lockable_clocks(self._handle, rows, count.value, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        return [(row.mem_mhz, list(row.graphics[:min(row.count, 512)])) for row in rows[:count.value]]

    def set_clock_offset(self, ctype, mhz, preserve_curve=True):
        """ctype 0 = core (MHz, snapped to this card's grid), 2 = memory (true MHz for a known GDDR
        type, else effective). The Python-type checks stay here; the core does the rest."""
        if type(preserve_curve) is not bool:
            return False, "preserve_curve must be a bool"
        if type(ctype) is not int or ctype not in (0, 2):
            return False, "clock offset type must be 0 (core) or 2 (memory)"
        if type(mhz) not in (int, float):
            return False, "clock offset must be a finite number, not a bool or string"
        try:
            finite = math.isfinite(mhz)
        except OverflowError:
            finite = False
        if not finite:
            return False, "clock offset must be a finite number"
        if not -(1 << 31) <= mhz < (1 << 31):
            return False, "clock offset exceeds the driver's signed 32-bit range"
        outcome = _Outcome()
        status = load().dc_gpu_clock_offset_set(self._handle, ctype, float(mhz), 1 if preserve_curve else 0,
                                                ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def lock_gpu_clocks(self, mn_mhz, mx_mhz):
        outcome = _Outcome()
        status = load().dc_gpu_clocks_lock(self._handle, int(mn_mhz), int(mx_mhz), ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def last_clock_lock_request(self):
        low, high = ctypes.c_uint32(), ctypes.c_uint32()
        status = load().dc_gpu_clocks_last_lock(self._handle, ctypes.byref(low), ctypes.byref(high))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return (low.value, high.value)

    def reset_gpu_clocks(self):
        return self._reset_gpu_clocks()

    def _reset_gpu_clocks(self, allow_pascal_noop=False):
        outcome = _Outcome()
        status = load().dc_gpu_clocks_reset(self._handle, 1 if allow_pascal_noop else 0, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)


def _fill_snapshot_row(row, old):
    if not isinstance(old, dict):
        return
    manual = old.get("manual")
    row.has_manual = isinstance(manual, bool)
    row.manual = bool(manual) if row.has_manual else 0
    fan_id = old.get("id")
    row.has_id = _is_plain_int(fan_id) and 0 <= fan_id <= 0xFFFFFFFF
    row.id = fan_id if row.has_id else 0
    level = old.get("level")
    row.level_valid = (not isinstance(level, bool) and isinstance(level, (int, float))
                       and float(level).is_integer() and 0 <= level <= 100)
    row.level = int(level) if row.level_valid else 0
    policy = old.get("policy")
    row.has_policy = _is_plain_int(policy) and 0 <= policy <= 0xFFFFFFFF
    row.policy = policy if row.has_policy else 0
