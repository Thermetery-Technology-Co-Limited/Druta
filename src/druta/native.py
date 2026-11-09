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
DC_E_FAILED = -9

DC_ERR_NONE, DC_ERR_TRANSIENT, DC_ERR_INVALID, DC_ERR_UNSUPPORTED = 0, 1, 2, 3


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
                ("uuid", ctypes.c_char * 96), ("admin", ctypes.c_int32), ("nvapi_handle", ctypes.c_uint64)]


class _Target(ctypes.Structure):  # dc_target
    _fields_ = [("size", ctypes.c_uint32), ("slot", ctypes.c_char * 24), ("architecture", ctypes.c_uint32),
                ("device_id", ctypes.c_uint32), ("subsystem_id", ctypes.c_uint32), ("handle", ctypes.c_uint64)]


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
# dc_cl_confirm_fn: the read_live_power_request branch of _current_limit_state (DC_CALL is cdecl).
CL_CONFIRM = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                              ctypes.c_uint32)

class _ClockDomainRow(ctypes.Structure):
    _fields_ = [("domain", ctypes.c_uint32), ("name", ctypes.c_char * 24), ("grade", ctypes.c_char * 16),
                ("kind", ctypes.c_char * 12), ("prog_khz", ctypes.c_uint32), ("meas_khz", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("srcid", ctypes.c_uint32), ("scale", ctypes.c_uint32),
                ("has_prog_mhz", ctypes.c_int32), ("has_meas_mhz", ctypes.c_int32), ("has_delta_mhz", ctypes.c_int32),
                ("prog_mhz", ctypes.c_double), ("meas_mhz", ctypes.c_double), ("delta_mhz", ctypes.c_double)]


class _ClockDomains(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("count", ctypes.c_uint32), ("rows", _ClockDomainRow * 32)]



class _ClkDomLayout(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("name", ctypes.c_char * 16), ("version", ctypes.c_uint32),
                ("block_size", ctypes.c_uint32), ("header", ctypes.c_uint32), ("stride", ctypes.c_uint32),
                ("mask_dword", ctypes.c_uint32), ("mode", ctypes.c_uint32), ("freq_khz", ctypes.c_uint32),
                ("has_nvvdd_uv", ctypes.c_int32), ("nvvdd_uv", ctypes.c_uint32), ("has_msvdd_uv", ctypes.c_int32),
                ("msvdd_uv", ctypes.c_uint32)]


class _ClkDomError(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("has_error", ctypes.c_int32), ("kind", ctypes.c_char * 32),
                ("text", ctypes.c_char * 256)]


class _ClkDomRequest(ctypes.Structure):
    _fields_ = [("domain", ctypes.c_uint32), ("mode", ctypes.c_int32), ("freq_khz", ctypes.c_int32),
                ("has_msvdd_uv", ctypes.c_int32), ("msvdd_uv", ctypes.c_int32)]


class _ClkDomRequests(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("count", ctypes.c_uint32), ("rows", _ClkDomRequest * 32),
                ("error", ctypes.c_char * 256)]


class _ClkDomPair(ctypes.Structure):
    _fields_ = [("control", ctypes.c_uint32), ("domain", ctypes.c_uint32)]


class _ClkDomMeasurement(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 16), ("has_value", ctypes.c_int32), ("value_khz", ctypes.c_uint32)]


class _ClkDomProbeError(ctypes.Structure):
    _fields_ = [("domain", ctypes.c_uint32), ("text", ctypes.c_char * 64)]


class _RailOffsetCapability(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("available", ctypes.c_int32), ("has_domain", ctypes.c_int32),
                ("domain", ctypes.c_int64), ("has_value", ctypes.c_int32), ("value_mv", ctypes.c_double),
                ("experimental", ctypes.c_int32), ("reason", ctypes.c_char * 256)]



class _CurrentRow(ctypes.Structure):  # dc_current_row
    _fields_ = [("size", ctypes.c_uint32), ("policy", ctypes.c_uint32), ("label", ctypes.c_char * 64),
                ("channel", ctypes.c_uint32), ("minimum_ma", ctypes.c_uint32), ("maximum_ma", ctypes.c_uint32),
                ("normal_maximum_ma", ctypes.c_uint32), ("default_ma", ctypes.c_uint32), ("limit_ma", ctypes.c_uint32),
                ("requested_ma", ctypes.c_uint32), ("value_ma", ctypes.c_uint32)]


class _CurrentUnavailable(ctypes.Structure):  # dc_current_unavailable
    _fields_ = [("size", ctypes.c_uint32), ("policy", ctypes.c_uint32), ("label", ctypes.c_char * 64),
                ("present", ctypes.c_int32), ("unsupported", ctypes.c_int32), ("error", ctypes.c_char * 256)]


class _Error(ctypes.Structure):  # dc_error
    _fields_ = [("size", ctypes.c_uint32), ("kind", ctypes.c_int32), ("fixed", ctypes.c_int32),
                ("has_ntstatus", ctypes.c_int32), ("ntstatus", ctypes.c_int32), ("rm_status", ctypes.c_uint32),
                ("message", ctypes.c_char * 1024)]


class _EscapeCapability(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32)] + [(name, ctypes.c_int32) for name in (
        "gdi32_loaded", "escape", "query_adapter_info", "close_adapter", "enum_adapters2", "enum_adapters",
        "open_from_display_name", "open_from_hdc", "complete", "method")] + [
        ("patch_bytes", ctypes.c_uint32), ("detail", ctypes.c_char * 256)]


class _EscapeFields(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ("hAdapter", "hDevice", "Type", "Flags", "hContext")]


class _RmTransport(ctypes.Structure):  # dc_rm_transport
    _fields_ = [("size", ctypes.c_uint32), ("captured", ctypes.c_int32), ("header_count", ctypes.c_uint32),
                ("header", ctypes.c_uint32 * 17), ("fields", _EscapeFields)]


class _RmObserved(ctypes.Structure):  # dc_rm_observed
    _fields_ = [("size", ctypes.c_uint32), ("seen", ctypes.c_int32), ("command_id", ctypes.c_uint32),
                ("packet_bytes", ctypes.c_uint32), ("parameter_bytes", ctypes.c_uint32)]


class _RailWriteRequest(ctypes.Structure):  # dc_rail_write_request
    _fields_ = [("size", ctypes.c_uint32), ("control_version", ctypes.c_uint32), ("rail_mask", ctypes.c_uint32),
                ("records", _RailRecord * 2), ("record_count", ctypes.c_uint32), ("boost", ctypes.c_uint32)]


class _RailWriteResult(ctypes.Structure):  # dc_rail_write_result
    _fields_ = [("size", ctypes.c_uint32), ("done", ctypes.c_int32), ("has_status", ctypes.c_int32),
                ("status", ctypes.c_uint32), ("error", ctypes.c_char * 512), ("has_protocol", ctypes.c_int32),
                ("get_command", ctypes.c_uint32), ("set_command", ctypes.c_uint32), ("packet_size", ctypes.c_uint32),
                ("getter_version", ctypes.c_uint32), ("observed_count", ctypes.c_uint32),
                ("observed", (ctypes.c_char * 256) * 8)]


PRIV_CLK_WORDS = 288


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
    "dc_gpu_clkdom_ok": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_gpu_clkdom_is_blackwell": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_gpu_clkdom_domains": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
                                               ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_clkdom_layout": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClkDomLayout)]),
    "dc_gpu_clkdom_capability_error": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClkDomError)]),
    "dc_gpu_clkdom_probe_errors": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClkDomProbeError),
                                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_clkdom_offset_read_pending": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_gpu_clkdom_controls_for_ui": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
                                                       ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_clkdom_control_polarity": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64,
                                                        ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_clkdom_control_label": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.c_char_p,
                                                     ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_clkdom_step_mhz": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
    "dc_gpu_clkdom_delta_inert": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64,
                                                   ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_clkdom_delta_clears_ceiling": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64,
                                                            ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_clkdom_offset_slider_offered": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64]),
    "dc_gpu_clkdom_pairing": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
                                               ctypes.POINTER(_ClkDomPair), ctypes.c_size_t,
                                               ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_clkdom_measurements": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClkDomMeasurement),
                                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]),
    "dc_gpu_clkdom_requests": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClkDomRequests)]),
    "dc_gpu_clkdom_xbar_offset_mhz": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)]),
    "dc_gpu_clkdom_offset_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.c_double,
                                                  ctypes.POINTER(_Outcome)]),
    "dc_gpu_rail_offset_capability": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int32,
                                                       ctypes.c_int64, ctypes.POINTER(_RailOffsetCapability)]),
    "dc_gpu_rail_offset_read": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int64,
                                                 ctypes.POINTER(ctypes.c_double)]),
    "dc_gpu_rail_offset_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_double, ctypes.c_int64, ctypes.c_int64,
                                                ctypes.POINTER(_Outcome)]),
    "dc_gpu_msvdd_offset_reset": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int64, ctypes.POINTER(_Outcome)]),
    "dc_gpu_clkdom_flags_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32]),
    "dc_gpu_clkdom_flags_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32),
                                                 ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_retry_clock_capabilities": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_double,
                                                         ctypes.POINTER(ctypes.c_int32)]),
    "dc_gpu_refresh_capabilities": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_ClkDomError)]),
    "dc_escape_capability_get": (ctypes.c_int32, [ctypes.POINTER(_EscapeCapability)]),
    "dc_rm_escape_raw": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.POINTER(_EscapeFields), ctypes.POINTER(ctypes.c_int32),
                                          ctypes.POINTER(_Error)]),
    "dc_rm_transport_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_RmTransport)]),
    "dc_rm_transport_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_RmTransport)]),
    "dc_rm_transport_reset": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32]),
    "dc_rm_observed_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_RmObserved)]),
    "dc_rm_layout_choice_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32),
                                                 ctypes.POINTER(ctypes.c_uint32)]),
    "dc_rm_layout_choice_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32]),
    "dc_rm_capture": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_RmTransport)]),
    "dc_rm_profile_supported": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]),
    "dc_rm_power_transport": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_RmTransport),
                                               ctypes.POINTER(_Error)]),
    "dc_rm_power_layouts": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_Error)]),
    "dc_legacy_clk_records": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                               ctypes.POINTER(ctypes.c_int32)]),
    "dc_rail_write_locked": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_RailWriteRequest),
                                              ctypes.POINTER(_RailWriteResult)]),
    "dc_cl_max_words": (ctypes.c_uint32, []),
    "dc_cl_capacity": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
    "dc_cl_abi": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                   ctypes.POINTER(_Error)]),
    "dc_cl_probe_info": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_Error)]),
    "dc_cl_rm": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
                                  ctypes.c_int32, ctypes.c_uint32, ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32),
                                  ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_Error)]),
    "dc_cl_policy_table": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_Error)]),
    "dc_cl_table_layout": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]),
    "dc_cl_table_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_uint32),
                                         ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int32)]),
    "dc_cl_table_reset": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_cl_policy_mask_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
    "dc_cl_policy_mask_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32]),
    "dc_gpu_target_bind": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(_Target),
                                            ctypes.POINTER(_Error)]),
    "dc_gpu_target_reset": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_nvapi_interface_present": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32]),
    "dc_cl_state": (ctypes.c_int32, [ctypes.c_void_p, CL_CONFIRM, ctypes.c_void_p, ctypes.POINTER(_CurrentRow),
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int32),
                                     ctypes.POINTER(_Error)]),
    "dc_cl_unavailable": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_CurrentUnavailable), ctypes.c_uint32,
                                           ctypes.POINTER(ctypes.c_uint32)]),
    "dc_cl_get_current_limits": (ctypes.c_int32, [ctypes.c_void_p, CL_CONFIRM, ctypes.c_void_p,
                                                  ctypes.POINTER(_CurrentRow), ctypes.c_uint32,
                                                  ctypes.POINTER(ctypes.c_uint32)]),
    "dc_cl_last_rows": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_CurrentRow), ctypes.c_uint32,
                                         ctypes.POINTER(ctypes.c_uint32)]),
    "dc_cl_error_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_char_p, ctypes.c_size_t,
                                         ctypes.POINTER(ctypes.c_size_t)]),
    "dc_cl_established_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]),
    "dc_cl_established_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32]),
    "dc_cl_set_current_limit_ma": (ctypes.c_int32, [ctypes.c_void_p, CL_CONFIRM, ctypes.c_void_p, ctypes.c_int64,
                                                    ctypes.c_int64, ctypes.c_int32, ctypes.POINTER(ctypes.c_int32),
                                                    ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]),
    "dc_cl_pins_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int64),
                                        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]),
    "dc_cl_pins_set": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int64),
                                        ctypes.c_uint32]),
    "dc_hook_test_entry": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_hook_test_state": (ctypes.c_int32, [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32),
                                            ctypes.POINTER(ctypes.c_uint32)]),
    "dc_hook_test_fail_protect": (None, [ctypes.c_int32]),
    "dc_hook_thunk": (ctypes.c_int32, [ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32,
                                       ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_uint32)]),
    "dc_gpu_clock_domains": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int32,
                                              ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32,
                                              ctypes.POINTER(_ClockDomains)]),
    "dc_clock_domains_classify": (ctypes.c_int32, [ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t, ctypes.c_int32,
                                                   ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32, ctypes.c_int32,
                                                   ctypes.c_uint32, ctypes.POINTER(_ClockDomains)]),
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
    # client-power controller (increment A3); dc_cpc_state is Gpu._CpcState, passed by address
    "dc_cpc_transport": (ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(_RmTransport), ctypes.POINTER(_Error)]),
    "dc_cpc_exchange": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                                         ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(_Error)]),
    "dc_cpc_reset_transport": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_cpc_reset": (ctypes.c_int32, [ctypes.c_void_p]),
    "dc_cpc_state_get": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p]),
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


class _Static(dict):
    """nvbackend.GPU.static: the identity keys at construction, the capability keys
    (pl_min_mw, gfx_min, mem_clocks, ...) merged in from capabilities() on their first use.
    nvbackend reads them all in __init__; the core reads the capabilities once, when first
    asked, so construction issues no NVML capability calls and the parity call logs hold."""

    def __init__(self, gpu, identity):
        super().__init__(identity)
        self._gpu, self._merged = gpu, False

    def _merge(self, key):
        if not self._merged and key not in dict.keys(self):
            self._merged = True
            for name, value in self._gpu.capabilities().items():
                dict.setdefault(self, name, value)

    def __getitem__(self, key):
        self._merge(key)
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self._merge(key)
        return dict.__contains__(self, key)

    def get(self, key, default=None):
        self._merge(key)
        return dict.get(self, key, default)


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
                              "subsys": identity.nvapi_subsys, "handle": identity.nvapi_handle}
        nvml_selected = None
        if identity.nvml_selected:
            nvml_selected = {"slot": _text(identity.nvml_slot), "nvml_index": identity.nvml_index,
                             "devid": identity.nvml_devid if identity.nvml_has_devid else None,
                             "subsys": identity.nvml_subsys if identity.nvml_has_devid else None,
                             "uuid": _text(identity.uuid)}
        self.nvapi = _Half(bool(identity.nvapi_ok), _text(identity.nvapi_error), nvapi_selected)
        # NvAPI.gpu: the selected handle while the half is live; NvAPI.disable (a pairing failure) clears it
        # but keeps the selected record, as nvbackend.GPU._pair does.
        self.nvapi.gpu = (identity.nvapi_handle or None) if identity.nvapi_ok else None
        self.nvml = _Half(bool(identity.nvml_ok), _text(identity.nvml_error), nvml_selected)
        self.nvml.dll_path = _text(identity.nvml_path)
        self.pairing_error = _text(identity.pairing_error)
        self.static = _Static(self, {"name": _text(identity.name), "driver": _text(identity.driver),
                                     "vbios": _text(identity.vbios), "admin": bool(identity.admin),
                                     "slot": _text(identity.slot), "uuid": _text(identity.uuid)})
        # nvbackend.GPU._lock: the front end and the profiles serialize their own sequences on it.
        # The core keeps its own re-entrant mutex per handle; this one exists for those callers.
        self._lock = threading.RLock()

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

    # ---- the paired target binding (perf_diagnostics.PerfDiagnosticsReader._binding) ---------- #

    _ERROR_KINDS = {1: "transient", 2: "invalid", 3: "unsupported", 4: "identity", 5: "malformed"}

    def _target_bind(self, client_power):
        target, error = _Target(), _Error()
        target.size, error.size = ctypes.sizeof(_Target), ctypes.sizeof(_Error)
        status = load().dc_gpu_target_bind(self._handle, 1 if client_power else 0, ctypes.byref(target),
                                           ctypes.byref(error))
        if status == DC_E_FAILED:
            return None, (self._ERROR_KINDS.get(error.kind, "transient"), _text(error.message))
        if status != DC_OK:
            _raise(status)
        return target, None

    def _target_binding(self):
        """PerfDiagnosticsReader._binding on this handle's shared binding: (api, slot, architecture),
        or the PerfDiagnosticError it raises. One sticky identity per handle, reset with
        _target_binding_reset() (a fresh reader)."""
        from .perf_diagnostics import PerfDiagnosticError
        target, failure = self._target_bind(False)
        if failure:
            raise PerfDiagnosticError(*failure)
        return self.nvapi, _text(target.slot), target.architecture

    def _client_power_binding(self):
        """ClientPowerController._binding: the identity dict, or the ClientPowerError it raises."""
        from .client_power_runtime import ClientPowerError
        target, failure = self._target_bind(True)
        if failure:
            raise ClientPowerError(*failure)
        return {"slot": _text(target.slot), "architecture": target.architecture,
                "device_id": target.device_id, "subsystem_id": target.subsystem_id}

    def _target_binding_reset(self):
        status = load().dc_gpu_target_reset(self._handle)
        if status != DC_OK:
            _raise(status)

    def nvapi_interface_present(self, ident):
        """NvAPI._i(ident) is not None: the binder resolves the interface id."""
        return bool(load().dc_nvapi_interface_present(self._handle, int(ident) & 0xFFFFFFFF))

    # ---- client-power controller (increment A3) ------------------------------------------------- #
    # ClientPowerController._transport / _exchange / _reset_transport on this handle's one sticky
    # controller (dc_cpc_*); _cp_reset() stands for constructing a fresh controller.

    class _CpcState(ctypes.Structure):  # dc_cpc_state
        _fields_ = [("size", ctypes.c_uint32)] + [(name, ctypes.c_int32) for name in (
            "sizes", "legacy", "native_policy124", "role_info216", "rm26_records", "rm26", "rm26_extended",
            "has_transport_key")] + [("transport_key", ctypes.c_uint32 * 3)]

    # dc_cpc_state.sizes: the DC_CP_*_SIZES group, by its client_power_runtime name.
    _CP_SIZE_GROUPS = ("SIZES", "LEGACY_SIZES", "TURING_SIZES", "INFO216_SIZES", "RM26_SIZES",
                       "RM26_EXTENDED_SIZES", "A612_COMPACT_SIZES")

    def _cp_raise(self, error):
        """The ClientPowerError a dc_error stands for; DC_ERR_INVALID is the one bare ValueError."""
        from .client_power_runtime import ClientPowerError
        message = _text(error.message)
        if error.kind == DC_ERR_INVALID:
            raise ValueError(message)
        raise ClientPowerError(self._ERROR_KINDS.get(error.kind, "transient"), message,
                               rm_status=error.rm_status if error.rm_status else None)

    def _cp_transport(self):
        """ClientPowerController._transport: (header tuple, fields dict), or ClientPowerError."""
        t, error = _RmTransport(), _Error()
        t.size, error.size = ctypes.sizeof(_RmTransport), ctypes.sizeof(_Error)
        status = load().dc_cpc_transport(self._handle, ctypes.byref(t), ctypes.byref(error))
        if status == DC_E_FAILED:
            self._cp_raise(error)
        if status != DC_OK:
            _raise(status)
        header, fields = _transport_value(t)
        return tuple(header), fields

    def _cp_exchange(self, command, payload, *, write=None):
        """ClientPowerController._exchange: the response bytes, or ClientPowerError. Sets
        write.dispatched as Python does: on any write object passed, once the packet reaches the
        escape."""
        from .client_power_runtime import SET, ClientPowerError, _Write
        # Python's dynamic precondition, in its order, before any call. A command that compares equal
        # to a known one without being an int (2080A630.0) passes Python's `in` and fails later with a
        # TypeError; it is refused here as malformed instead.
        sizes = self._cp_state()["sizes"]
        if (command not in sizes or len(payload) != sizes[command] or (command == SET) != isinstance(write, _Write)
                or type(command) is not int):
            raise ClientPowerError("malformed", "Client-power command or packet geometry is not understood")
        data = bytes(payload)
        out = (ctypes.c_ubyte * max(len(data), 1))()
        dispatched, error = ctypes.c_int32(), _Error()
        error.size = ctypes.sizeof(_Error)
        status = load().dc_cpc_exchange(self._handle, command, data, len(data), 1 if isinstance(write, _Write) else 0,
                                        ctypes.addressof(out), len(data), ctypes.byref(dispatched), ctypes.byref(error))
        if write is not None and dispatched.value:
            write.dispatched = True
        if status == DC_E_FAILED:
            self._cp_raise(error)
        if status != DC_OK:
            _raise(status)
        return bytes(out)[:len(data)]

    def _cp_reset_transport(self):
        """ClientPowerController._reset_transport: clears _current_limit_transport and the layout choice."""
        status = load().dc_cpc_reset_transport(self._handle)
        if status != DC_OK:
            _raise(status)

    def _cp_reset(self):
        """A fresh controller: every attribute back to __init__, the sticky identity included."""
        status = load().dc_cpc_reset(self._handle)
        if status != DC_OK:
            _raise(status)

    def _cp_state(self):
        """The controller's attributes; "sizes" is the very client_power_runtime table object."""
        from . import client_power_runtime
        s = self._CpcState()
        s.size = ctypes.sizeof(self._CpcState)
        status = load().dc_cpc_state_get(self._handle, ctypes.addressof(s))
        if status != DC_OK:
            _raise(status)
        return {"sizes": getattr(client_power_runtime, self._CP_SIZE_GROUPS[s.sizes]),
                "legacy": bool(s.legacy), "native_policy124": bool(s.native_policy124),
                "role_info216": bool(s.role_info216), "rm26": bool(s.rm26),
                "rm26_records": bool(s.rm26_records), "rm26_extended": bool(s.rm26_extended),
                "transport_key": tuple(s.transport_key) if s.has_transport_key else None}

    def arch_name(self):
        from .nvbackend import GPU
        return GPU.ARCH_NAMES.get(self.arch() or -1)

    def vf_curve_applicable(self):
        """Kepler uses ordinary clock offsets, not the V/F table (nvbackend.GPU.vf_curve_applicable)."""
        from .nvbackend import GPU
        return self.arch() != GPU.ARCH_KEPLER

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
        the live-power fallback of pl_requested_mw, the Turing legacy
        clock-lock fallback, nvvdd_live_mv.
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
            d["clk_domains"], d["clk_domains_err"] = self.read_clock_domains(
                d["priv_clocks"], core_mhz=d.get("core"), mem_nvml=d.get("mem"))
        else:
            d["clk_domains"], d["clk_domains_err"] = None, PRIV_UNAVAIL
        if self.clkdom_is_blackwell():
            d["clkdom_offsets"], d["clkdom_offsets_err"] = self.read_clk_domain_offsets()
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

    # ---- RM escape transport, layer 0 (nvbackend.GPU._legacy_clk_escape) ----------------------- #

    _ESCAPE_FIELDS = ("hAdapter", "hDevice", "Type", "Flags", "hContext")

    def _legacy_clk_escape(self, packet, fields):
        """Issue one raw escape with `packet` (a ctypes buffer, used in place) and the captured
        `fields` (hAdapter is replaced by the adapter at the paired PCI slot). The NTSTATUS, or
        None when no escape was issued; `last_escape_error` then says why."""
        f = _EscapeFields(*(int(fields.get(name, 0)) for name in self._ESCAPE_FIELDS))
        status, error = ctypes.c_int32(), _Error()
        error.size = ctypes.sizeof(_Error)
        rc = load().dc_rm_escape_raw(self._handle, ctypes.addressof(packet), ctypes.sizeof(packet), ctypes.byref(f),
                                     ctypes.byref(status), ctypes.byref(error))
        if rc == DC_E_UNAVAILABLE:
            self.last_escape_error = _error_record(error)
            return None
        if rc != DC_OK:
            _raise(rc)
        self.last_escape_error = None
        return status.value

    # ---- RM escape transport, layers 1 and 2 (the hook captures and the transport cache) ------- #

    _TRANSPORT_POWER, _TRANSPORT_LEGACY_CLK = 1, 2

    def _transport_get(self, kind):
        t = _RmTransport()
        t.size = ctypes.sizeof(_RmTransport)
        status = load().dc_rm_transport_get(self._handle, kind, ctypes.byref(t))
        if status != DC_OK:
            _raise(status)
        return _transport_value(t)

    def _transport_set(self, kind, value):
        status = load().dc_rm_transport_set(self._handle, kind, ctypes.byref(_transport_struct(value)))
        if status != DC_OK:
            _raise(status)

    @property
    def _current_limit_transport(self):
        """The cached power transport (header, fields) or None. Assignment stores it unvalidated,
        as a fixture does; the next use validates it."""
        return self._transport_get(self._TRANSPORT_POWER)

    @_current_limit_transport.setter
    def _current_limit_transport(self, value):
        self._transport_set(self._TRANSPORT_POWER, value)

    @property
    def _legacy_clk_transport(self):
        return self._transport_get(self._TRANSPORT_LEGACY_CLK)

    @_legacy_clk_transport.setter
    def _legacy_clk_transport(self, value):
        self._transport_set(self._TRANSPORT_LEGACY_CLK, value)

    @property
    def _current_limit_layout_choice(self):
        has_choice, choice = ctypes.c_int32(), ctypes.c_uint32()
        status = load().dc_rm_layout_choice_get(self._handle, ctypes.byref(has_choice), ctypes.byref(choice))
        if status != DC_OK:
            _raise(status)
        return choice.value if has_choice.value else None

    @_current_limit_layout_choice.setter
    def _current_limit_layout_choice(self, value):
        status = load().dc_rm_layout_choice_set(self._handle, 0 if value is None else 1,
                                                0 if value is None else int(value))
        if status != DC_OK:
            _raise(status)

    @property
    def _current_limit_observed_transport(self):
        """What the last power capture saw: {} or the command / sizes record."""
        o = _RmObserved()
        o.size = ctypes.sizeof(_RmObserved)
        status = load().dc_rm_observed_get(self._handle, ctypes.byref(o))
        if status != DC_OK:
            _raise(status)
        if not o.seen:
            return {}
        return {"command": f"0x{o.command_id:08X}", "command_id": o.command_id,
                "packet_bytes": o.packet_bytes, "parameter_bytes": o.parameter_bytes}

    def _capture(self, kind):
        t = _RmTransport()
        t.size = ctypes.sizeof(_RmTransport)
        status = load().dc_rm_capture(self._handle, kind, ctypes.byref(t))
        if status != DC_OK:
            _raise(status)
        return _transport_value(t)

    def _capture_current_limit_transport(self):
        """One PowerPolInfo call under the hook: (header, fields) of its power GET, or None."""
        return self._capture(self._TRANSPORT_POWER)

    def _capture_legacy_clk_transport(self):
        """One VoltRailsCtlGet call under the hook: (header, fields) of its escape, or None."""
        return self._capture(self._TRANSPORT_LEGACY_CLK)

    def _current_limit_profile_supported(self):
        supported = ctypes.c_int32()
        status = load().dc_rm_profile_supported(self._handle, ctypes.byref(supported))
        if status != DC_OK:
            _raise(status)
        return bool(supported.value)

    def _current_limit_generation_policies(self):
        from . import native_power_layouts
        return native_power_layouts.generation_policies(self.arch())

    def _rm_power_transport(self):
        """The cached power transport or a fresh capture, validated; raises as nvbackend does."""
        t, error = _RmTransport(), _Error()
        t.size, error.size = ctypes.sizeof(_RmTransport), ctypes.sizeof(_Error)
        status = load().dc_rm_power_transport(self._handle, ctypes.byref(t), ctypes.byref(error))
        if status == DC_E_FAILED:
            _raise_error(error)
        if status != DC_OK:
            _raise(status)
        return _transport_value(t)

    def _current_limit_transport_layouts(self):
        """(layout, *fallbacks) for the captured power GET; raises as nvbackend does."""
        from . import native_power_layouts as tables
        rows = (tables._PowerLayout * tables.DC_POWER_LAYOUTS_MAX)()
        for row in rows:
            row.size = ctypes.sizeof(tables._PowerLayout)
        count, error = ctypes.c_uint32(), _Error()
        error.size = ctypes.sizeof(_Error)
        status = load().dc_rm_power_layouts(self._handle, ctypes.cast(rows, ctypes.c_void_p),
                                            tables.DC_POWER_LAYOUTS_MAX, ctypes.byref(count), ctypes.byref(error))
        if status == DC_E_FAILED:
            _raise_error(error)
        if status != DC_OK:
            _raise(status)
        return tuple(tables._layout_dict(rows[i]) for i in range(min(count.value, tables.DC_POWER_LAYOUTS_MAX)))

    def _legacy_clk_limit_records(self):
        """RM GET 0x20802077 through the legacy transport: two 82-word records, or None."""
        records, available = (ctypes.c_uint32 * (2 * 82))(), ctypes.c_int32()
        status = load().dc_legacy_clk_records(self._handle, records, ctypes.byref(available))
        if status != DC_OK:
            _raise(status)
        if not available.value:
            return None
        return [list(records[0:82]), list(records[82:164])]

    def _rail_write_locked(self, control_version, rail_mask, records, boost):
        """nvbackend.GPU._write_rail_records_locked after its Python preconditions (the profile,
        the supported rails, the boost read): one VoltRailsCtlGet under the hook, the recognized
        GET read back, exactly one SET with the prepared records. Returns (done, status) and sets
        _volt_rail_write_error, _volt_rail_write_protocol and _volt_rail_observed_packets as the
        Python does."""
        request = _RailWriteRequest()
        request.size = ctypes.sizeof(_RailWriteRequest)
        request.control_version, request.rail_mask, request.boost = int(control_version), int(rail_mask), int(boost)
        items = list(records.items())
        if len(items) > 2:
            raise ValueError("requested_mask must exactly match the record rails")
        request.record_count = len(items)
        for rail, values in items:
            if len(tuple(values)) != 4:  # the Python precondition: (False, None), attributes untouched
                return False, None
        for position, (rail, values) in enumerate(items):
            record = tuple(values)
            # The ABI record carries int32 values and a c_int32 array wraps silently, so the range
            # check of rail_transport._validated_records happens here. The refusal is the one
            # prepare_write would give inside the hook, but no GET is issued for it.
            if any(not _is_plain_int(value) or not -(1 << 31) <= value <= (1 << 31) - 1 for value in record):
                self._volt_rail_observed_packets = []
                self._volt_rail_write_protocol = None
                self._volt_rail_write_error = ("Could not preserve the current native rail record: "
                                               f"rail {rail} record values must be signed int32 integers")
                return False, None
            request.records[position].rail = int(rail)
            request.records[position].values[:] = record
        result = _RailWriteResult()
        result.size = ctypes.sizeof(_RailWriteResult)
        status = load().dc_rail_write_locked(self._handle, ctypes.byref(request), ctypes.byref(result))
        if status != DC_OK:
            _raise(status)
        self._volt_rail_observed_packets = [result.observed[i].value.decode(errors="replace")
                                            for i in range(min(result.observed_count, 8))]
        self._volt_rail_write_error = _text(result.error) or None
        self._volt_rail_write_protocol = None
        if result.has_protocol:
            self._volt_rail_write_protocol = {"get_command": f"0x{result.get_command:08X}",
                                              "set_command": f"0x{result.set_command:08X}",
                                              "packet_size": result.packet_size,
                                              "getter_version": f"0x{result.getter_version:08X}"}
        return bool(result.done), (result.status if result.has_status else None)

    # ---- current-policy operations on the transport (nvbackend.GPU._current_limit_*) --------- #

    _CL_COMMANDS = (0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B)

    @staticmethod
    def _current_limit_capacity(layout):
        """Indices bounded by the known buffers and their 32-bit occupancy mask."""
        from . import native_power_layouts as tables
        row, capacity = _layout_struct(layout), ctypes.c_uint32()
        status = load().dc_cl_capacity(ctypes.byref(row), ctypes.byref(capacity))
        if status != DC_OK:
            _raise(status)
        return capacity.value

    def _cl_words(self, name, *args):
        """Call a word-returning dc_cl_* function; the words as a list, or the Python exception."""
        library = load()
        out = (ctypes.c_uint32 * _cl_max_words())()
        count, error = ctypes.c_uint32(), _Error()
        error.size = ctypes.sizeof(_Error)
        status = getattr(library, name)(self._handle, *args, out, len(out), ctypes.byref(count), ctypes.byref(error))
        if status == DC_E_FAILED:
            _raise_error(error)
        if status != DC_OK:
            _raise(status)
        return list(out[:min(count.value, len(out))])

    def _current_limit_abi(self):
        """The current-policy layout: the stored choice, the only candidate, or the first the driver's
        read-only info probe accepts; raises as nvbackend does."""
        from . import native_power_layouts as tables
        row, index, error = tables._PowerLayout(), ctypes.c_uint32(), _Error()
        row.size, error.size = ctypes.sizeof(tables._PowerLayout), ctypes.sizeof(_Error)
        status = load().dc_cl_abi(self._handle, ctypes.byref(row), ctypes.byref(index), ctypes.byref(error))
        if status == DC_E_FAILED:
            _raise_error(error)
        if status != DC_OK:
            _raise(status)
        return tables._layout_dict(row)

    def _current_limit_probe_info(self, layout):
        """Read-only info GET used to choose between same-sized transports."""
        row = _layout_struct(layout)
        return self._cl_words("dc_cl_probe_info", ctypes.byref(row))

    def _current_limit_rm(self, command, params=None, policy_mask=None, *, any_live_policy=False):
        """Only the four measured policy operations, on this live GPU client."""
        if params is not None:
            params = list(params)
            # nvbackend packs the words into a c_uint32 array when it builds the packet: a float or
            # a str is refused there with this TypeError. An out-of-range int is refused here
            # instead of wrapped, so no word the caller did not mean reaches the driver.
            for word in params:
                if not _is_plain_int(word):
                    raise TypeError(f"'{type(word).__name__}' object cannot be interpreted as an integer")
            if any(not 0 <= word <= 0xFFFFFFFF for word in params):
                raise ValueError("current-policy parameters are 32-bit words")
            buffer = (ctypes.c_uint32 * max(len(params), 1))(*params)
        else:
            buffer = None
        has_mask = type(policy_mask) is int and 0 <= policy_mask <= 0xFFFFFFFF
        return self._cl_words("dc_cl_rm", int(command) & 0xFFFFFFFF, buffer, 0 if params is None else len(params),
                              1 if has_mask else 0, policy_mask if has_mask else 0, 1 if any_live_policy else 0)

    def _power_policy_table(self):
        """(layout, info, control, status) from one validated read of the driver's whole policy table;
        control and status are None when the mask is empty."""
        from . import native_power_layouts as tables
        row, error = tables._PowerLayout(), _Error()
        row.size, error.size = ctypes.sizeof(tables._PowerLayout), ctypes.sizeof(_Error)
        status = load().dc_cl_policy_table(self._handle, ctypes.byref(row), ctypes.byref(error))
        if status == DC_E_FAILED:
            _raise_error(error)
        if status != DC_OK:
            _raise(status)
        return self._last_policy_table

    @property
    def _last_policy_table(self):
        from . import native_power_layouts as tables
        library = load()
        row, present = tables._PowerLayout(), ctypes.c_int32()
        row.size = ctypes.sizeof(tables._PowerLayout)
        status = library.dc_cl_table_layout(self._handle, ctypes.byref(row), ctypes.byref(present))
        if status != DC_OK:
            _raise(status)
        if not present.value:
            return None
        blocks = []
        for block in range(3):
            out = (ctypes.c_uint32 * _cl_max_words())()
            count, has = ctypes.c_uint32(), ctypes.c_int32()
            status = library.dc_cl_table_get(self._handle, block, out, len(out), ctypes.byref(count), ctypes.byref(has))
            if status != DC_OK:
                _raise(status)
            blocks.append(list(out[:min(count.value, len(out))]) if has.value else None)
        return (tables._layout_dict(row), blocks[0], blocks[1], blocks[2])

    @_last_policy_table.setter
    def _last_policy_table(self, value):
        if value is not None:
            raise ValueError("the last policy table is read from the driver; only None can be assigned")
        status = load().dc_cl_table_reset(self._handle)
        if status != DC_OK:
            _raise(status)

    @property
    def _power_policy_mask(self):
        mask = ctypes.c_uint32()
        status = load().dc_cl_policy_mask_get(self._handle, ctypes.byref(mask))
        if status != DC_OK:
            _raise(status)
        return mask.value

    @_power_policy_mask.setter
    def _power_policy_mask(self, value):
        status = load().dc_cl_policy_mask_set(self._handle, int(value or 0) & 0xFFFFFFFF)
        if status != DC_OK:
            _raise(status)

    # ---- the policy rows, the getter and the verified setter (nvbackend.GPU._current_limit_state and up) #

    _CL_ERROR, _CL_READ_ERROR = 0, 1
    _CL_MAX_ROWS = 32  # one row per policy bit at most

    def read_live_power_request(self):
        """nvbackend.GPU.read_live_power_request is the client-power runtime, which is not ported:
        natively a derived clamp cannot be confirmed yet and stays an invalid record. The parity
        tests patch this on the instance, as they mock it on the Python twin."""
        return None, "the live power request is not ported"

    def _confirm_derived_clamp(self):
        """The dc_cl_confirm_fn: the read_live_power_request branch of _current_limit_state, on the
        info and control words the core hands over. The caller keeps the returned object alive for
        the duration of the call it is passed to."""
        def confirm(_context, _policy, _requested_ma, _effective_ma, info, info_count, control, control_count):
            try:
                observed, error = self.read_live_power_request()
                if error or observed is None:
                    return 0
                binding = observed.get("binding", {})
                return int(bytes.fromhex(observed.get("role_info_hex", ""))[4:5] == b"\x30"
                           and type(observed.get("requested_mw")) is int
                           and type(binding.get("maximum_mw")) is int
                           and observed["requested_mw"] > binding["maximum_mw"]
                           and observed.get("native_info_hex") == ctypes.string_at(info, info_count * 4).hex()
                           and observed.get("native_control_hex") == ctypes.string_at(control, control_count * 4).hex())
            except Exception:
                # nvbackend lets such an exception escape _current_limit_state (get_current_limits
                # then reports its text); inside the callback it would vanish, so it refuses instead
                return 0
        return CL_CONFIRM(confirm)

    def _cl_rows(self, name, *args):
        out = (_CurrentRow * self._CL_MAX_ROWS)()
        for row in out:
            row.size = ctypes.sizeof(_CurrentRow)
        count = ctypes.c_uint32()
        status = getattr(load(), name)(self._handle, *args, out, len(out), ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        return [_row_dict(out[i]) for i in range(min(count.value, len(out)))]

    def _current_limit_state(self):
        """(rows, control) of one validated table read; raises as nvbackend does. Resets
        _current_limit_unavailable first either way."""
        confirm = self._confirm_derived_clamp()
        rows = (_CurrentRow * self._CL_MAX_ROWS)()
        for row in rows:
            row.size = ctypes.sizeof(_CurrentRow)
        control = (ctypes.c_uint32 * _cl_max_words())()
        count, control_count, has_control = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_int32()
        error = _Error()
        error.size = ctypes.sizeof(_Error)
        status = load().dc_cl_state(self._handle, confirm, None, rows, len(rows), ctypes.byref(count), control,
                                    len(control), ctypes.byref(control_count), ctypes.byref(has_control),
                                    ctypes.byref(error))
        if status == DC_E_FAILED:
            _raise_error(error)
        if status != DC_OK:
            _raise(status)
        return ([_row_dict(rows[i]) for i in range(min(count.value, len(rows)))],
                list(control[:min(control_count.value, len(control))]) if has_control.value else None)

    @property
    def _current_limit_unavailable(self):
        out = (_CurrentUnavailable * self._CL_MAX_ROWS)()
        for item in out:
            item.size = ctypes.sizeof(_CurrentUnavailable)
        count = ctypes.c_uint32()
        status = load().dc_cl_unavailable(self._handle, out, len(out), ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        items = []
        for i in range(min(count.value, len(out))):
            item = {"policy": out[i].policy, "label": _text(out[i].label), "present": bool(out[i].present)}
            if out[i].present:
                item["unsupported"] = bool(out[i].unsupported)
            item["error"] = _text(out[i].error)
            items.append(item)
        return items

    def get_current_limits(self):
        """Supported current policies in mA; empty on an unvalidated adapter."""
        confirm = self._confirm_derived_clamp()
        return self._cl_rows("dc_cl_get_current_limits", confirm, None)

    @property
    def _current_limit_last_rows(self):
        return self._cl_rows("dc_cl_last_rows")

    def _cl_text(self, which):
        library, needed = load(), ctypes.c_size_t()
        status = library.dc_cl_error_get(self._handle, which, None, 0, ctypes.byref(needed))
        if status != DC_OK:
            _raise(status)
        buffer = ctypes.create_string_buffer(max(needed.value, 1))
        status = library.dc_cl_error_get(self._handle, which, buffer, len(buffer), ctypes.byref(needed))
        if status != DC_OK:
            _raise(status)
        return buffer.value.decode(errors="replace")

    @property
    def _current_limit_error(self):
        return self._cl_text(self._CL_ERROR)

    @property
    def _current_limit_read_error(self):
        return self._cl_text(self._CL_READ_ERROR)

    @property
    def _current_limit_established(self):
        mask = ctypes.c_uint32()
        status = load().dc_cl_established_get(self._handle, ctypes.byref(mask))
        if status != DC_OK:
            _raise(status)
        return {policy for policy in range(32) if mask.value >> policy & 1}

    @_current_limit_established.setter
    def _current_limit_established(self, value):
        policies = set(value or ())
        if any(not _is_plain_int(policy) or not 0 <= policy < 32 for policy in policies):
            raise ValueError("established current policies are policy indices below 32")
        status = load().dc_cl_established_set(self._handle, sum(1 << policy for policy in policies))
        if status != DC_OK:
            _raise(status)

    def current_limit_diagnostics(self):
        """Last GET outcome only; contains no client handles and performs no writes."""
        choice, layout = self._current_limit_layout_choice, None
        if choice is not None and self._current_limit_transport is not None:
            try:
                candidates = self._current_limit_transport_layouts()
                layout = candidates[choice] if choice < len(candidates) else None
            except ValueError:
                layout = None
        return {
            "build": "current-visibility-v3",
            "driver": self.static.get("driver"),
            "vbios": self.static.get("vbios"),
            "generation": self.arch(),
            "expected_policies": self._current_limit_generation_policies(),
            "transport": self._current_limit_observed_transport,
            # which same-sized layout the info probe chose, by its info size
            "layout_info_bytes": (layout or {}).get("sizes", {}).get(0x2080A618),
            "error": self._current_limit_error,
            "read_error": self._current_limit_read_error,
            "policies": self._current_limit_last_rows,
            "unavailable_policies": self._current_limit_unavailable,
        }

    def set_current_limit_ma(self, policy, ma, *, pin=True):
        """Change one policy and verify stored AND effective limits; (ok, message) as nvbackend's."""
        if type(policy) is not int or policy not in self._current_limit_generation_policies():
            return False, "not a supported current policy"
        if type(ma) is not int:
            return False, "current limit must be an integer number of milliamps"

        def int64(value):  # beyond int64 is beyond every policy range: the core's refusal is the same
            return max(-(1 << 63), min(value, (1 << 63) - 1))
        confirm = self._confirm_derived_clamp()
        ok, needed = ctypes.c_int32(), ctypes.c_size_t()
        message = ctypes.create_string_buffer(4096)
        status = load().dc_cl_set_current_limit_ma(self._handle, confirm, None, int64(policy), int64(ma),
                                                   1 if pin else 0, ctypes.byref(ok), message, len(message),
                                                   ctypes.byref(needed))
        if status != DC_OK:
            _raise(status)
        return bool(ok.value), message.value.decode(errors="replace")

    def _set_current_limit_ma(self, policy, ma):
        return self.set_current_limit_ma(policy, ma, pin=False)

    @property
    def _power_policy_pins(self):
        policies, values = (ctypes.c_uint32 * 32)(), (ctypes.c_int64 * 32)()
        count = ctypes.c_uint32()
        status = load().dc_cl_pins_get(self._handle, policies, values, 32, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        return {policies[i]: values[i] for i in range(min(count.value, 32))}

    @_power_policy_pins.setter
    def _power_policy_pins(self, value):
        items = list((value or {}).items())
        if any(not _is_plain_int(policy) or not 0 <= policy < 32 or not _is_plain_int(ma)
               or not -(1 << 63) <= ma < (1 << 63) for policy, ma in items):
            raise ValueError("power policy pins map policy indices below 32 to integer milliamps")
        policies = (ctypes.c_uint32 * max(len(items), 1))(*[policy for policy, _ in items])
        values = (ctypes.c_int64 * max(len(items), 1))(*[ma for _, ma in items])
        status = load().dc_cl_pins_set(self._handle, policies, values, len(items))
        if status != DC_OK:
            _raise(status)

    # ---- per-domain clock-offset control block (nvbackend.GPU.clkdom_* and the rail offsets) --- #

    def _list_u32(self, function):
        count = ctypes.c_size_t()
        status = function(self._handle, None, 0, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        values = (ctypes.c_uint32 * max(1, count.value))()
        status = function(self._handle, values, count.value, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        return list(values[:count.value])

    def clkdom_ok(self):
        return bool(load().dc_gpu_clkdom_ok(self._handle))

    def clkdom_is_blackwell(self):
        return bool(load().dc_gpu_clkdom_is_blackwell(self._handle))

    def clkdom_domains(self):
        """Accepted control indices, probed one bit at a time and cached until refresh."""
        return self._list_u32(load().dc_gpu_clkdom_domains)

    def clkdom_layout(self):
        """The validated nvbackend.ClkDomLayout for this card, or None."""
        from .nvbackend import ClkDomLayout
        layout = _ClkDomLayout()
        layout.size = ctypes.sizeof(_ClkDomLayout)
        status = load().dc_gpu_clkdom_layout(self._handle, ctypes.byref(layout))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return ClkDomLayout(name=_text(layout.name), version=layout.version, size=layout.block_size,
                            header=layout.header, stride=layout.stride, mask_dword=layout.mask_dword,
                            mode=layout.mode, freq_khz=layout.freq_khz,
                            nvvdd_uv=layout.nvvdd_uv if layout.has_nvvdd_uv else None,
                            msvdd_uv=layout.msvdd_uv if layout.has_msvdd_uv else None)

    @staticmethod
    def _error_tuple(error):
        return (_text(error.kind), _text(error.text)) if error.has_error else None

    @property
    def _clkdom_capability_error(self):
        error = _ClkDomError()
        error.size = ctypes.sizeof(_ClkDomError)
        status = load().dc_gpu_clkdom_capability_error(self._handle, ctypes.byref(error))
        if status != DC_OK:
            _raise(status)
        return self._error_tuple(error)

    @property
    def _clkdom_probe_errors(self):
        count = ctypes.c_size_t()
        status = load().dc_gpu_clkdom_probe_errors(self._handle, None, 0, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        rows = (_ClkDomProbeError * max(1, count.value))()
        status = load().dc_gpu_clkdom_probe_errors(self._handle, rows, count.value, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        return {row.domain: _text(row.text) for row in rows[:count.value]}

    @property
    def _clkdom_offset_read_pending(self):
        return bool(load().dc_gpu_clkdom_offset_read_pending(self._handle))

    def clkdom_controls_for_ui(self, rows=None):
        return self._list_u32(load().dc_gpu_clkdom_controls_for_ui)

    def clkdom_control_polarity(self, control):
        value = ctypes.c_int32()
        status = load().dc_gpu_clkdom_control_polarity(self._handle, int(control), ctypes.byref(value))
        if status != DC_OK:
            _raise(status)
        return value.value

    @staticmethod
    def _table_key(value):
        """A Python dict lookup key that equals an int, as an int; None for any other key
        (nvbackend looks these tables up with the raw argument, so 2.0 finds 2 and "2" does not)."""
        if isinstance(value, bool) or _is_plain_int(value):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None

    def clkdom_control_label(self, control):
        key = self._table_key(control)
        if key is None:
            return f"{'control' if self.clkdom_is_blackwell() else 'domain'} {control}"
        buffer = ctypes.create_string_buffer(64)
        needed = ctypes.c_size_t()
        status = load().dc_gpu_clkdom_control_label(self._handle, key, buffer, 64, ctypes.byref(needed))
        if status != DC_OK:
            _raise(status)
        return buffer.value.decode()

    def clkdom_step_mhz(self):
        value = ctypes.c_uint32()
        status = load().dc_gpu_clkdom_step_mhz(self._handle, ctypes.byref(value))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return value.value

    def clkdom_delta_inert(self, domain=None):
        key = None if domain is None else self._table_key(domain)
        if domain is not None and key is None:
            return None
        value = ctypes.c_int32()
        has = 0 if domain is None else 1
        status = load().dc_gpu_clkdom_delta_inert(self._handle, has, 0 if domain is None else key,
                                                  ctypes.byref(value))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return bool(value.value)

    def clkdom_offset_slider_offered(self, control):
        return bool(load().dc_gpu_clkdom_offset_slider_offered(self._handle, int(control)))

    def clkdom_delta_clears_ceiling(self, domain):
        key = self._table_key(domain)
        if key is None:
            return None
        value = ctypes.c_int32()
        status = load().dc_gpu_clkdom_delta_clears_ceiling(self._handle, key, ctypes.byref(value))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return bool(value.value)

    def clkdom_pairing(self, rows=None):
        """{control index: private domain} for THIS card, or {} if unknown."""
        has_rows = 0 if rows is None else 1
        live = [r["domain"] for r in (rows or []) if r.get("prog_mhz")]
        live_array = (ctypes.c_uint32 * max(1, len(live)))(*live)
        function = load().dc_gpu_clkdom_pairing
        count = ctypes.c_size_t()
        status = function(self._handle, has_rows, live_array, len(live), None, 0, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        pairs = (_ClkDomPair * max(1, count.value))()
        status = function(self._handle, has_rows, live_array, len(live), pairs, count.value, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        return {pair.control: pair.domain for pair in pairs[:count.value]}

    def clkdom_measurements(self):
        """Read-only Blackwell physical clock measurements in kHz ({} elsewhere)."""
        rows = (_ClkDomMeasurement * 8)()
        count = ctypes.c_size_t()
        status = load().dc_gpu_clkdom_measurements(self._handle, rows, 8, ctypes.byref(count))
        if status != DC_OK:
            _raise(status)
        return {_text(row.name): (row.value_khz if row.has_value else None) for row in rows[:count.value]}

    def read_clk_domain_offsets(self):
        """({domain: {mode, freq_khz, msvdd_uv}}, err)."""
        out = _ClkDomRequests()
        out.size = ctypes.sizeof(_ClkDomRequests)
        status = load().dc_gpu_clkdom_requests(self._handle, ctypes.byref(out))
        if status == DC_E_UNAVAILABLE:
            return None, _text(out.error)
        if status != DC_OK:
            _raise(status)
        return {row.domain: {"mode": row.mode, "freq_khz": row.freq_khz,
                             "msvdd_uv": row.msvdd_uv if row.has_msvdd_uv else None}
                for row in out.rows[:out.count]}, None

    def read_xbar_offset_mhz(self):
        value = ctypes.c_double()
        status = load().dc_gpu_clkdom_xbar_offset_mhz(self._handle, ctypes.byref(value))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return value.value

    def set_clk_domain_offset(self, domain, mhz):
        """Read-modify-write exactly one dword of the control block (Python-type checks here)."""
        if type(domain) is not int or not 0 <= domain < 32:
            return False, "clock domain must be an integer control index"
        if type(mhz) not in (int, float):
            return False, "clock-domain offset must be a finite number"
        try:
            value = float(mhz)
        except OverflowError:
            return False, "clock-domain offset exceeds the signed 32-bit kHz range"
        outcome = _Outcome()
        status = load().dc_gpu_clkdom_offset_set(self._handle, domain, value, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def rail_offset_capability(self, rail=1, domain=None):
        """A readable request field, not evidence of a physical rail response."""
        if type(rail) is not int or rail not in (0, 1):
            return {"available": False, "domain": domain, "value_mv": None, "experimental": rail == 1,
                    "reason": "unknown rail offset index"}
        if domain is not None and (type(domain) is not int or not 0 <= domain < 32):
            return {"available": False, "domain": domain, "value_mv": None, "experimental": rail == 1,
                    "reason": "invalid clock-control index"}
        out = _RailOffsetCapability()
        out.size = ctypes.sizeof(_RailOffsetCapability)
        status = load().dc_gpu_rail_offset_capability(self._handle, rail, 0 if domain is None else 1,
                                                      0 if domain is None else domain, ctypes.byref(out))
        if status != DC_OK:
            _raise(status)
        return {"available": bool(out.available), "domain": out.domain if out.has_domain else None,
                "value_mv": out.value_mv if out.has_value else None, "experimental": bool(out.experimental),
                "reason": _text(out.reason)}

    def read_rail_offset_mv(self, domain=0, rail=0):
        """The selected rail's stored request in mV, not measured VOUT."""
        if rail != 0:
            capability = self.rail_offset_capability(rail, domain)
            return capability["value_mv"] if capability["available"] else None
        if type(domain) is not int or not 0 <= domain < 32:
            return None
        value = ctypes.c_double()
        status = load().dc_gpu_rail_offset_read(self._handle, domain, rail, ctypes.byref(value))
        if status == DC_E_UNAVAILABLE:
            return None
        if status != DC_OK:
            _raise(status)
        return value.value

    def set_rail_offset_mv(self, mv, domain=0, rail=0):
        """Offset the core rail (rail 0) or store an MSVDD request (rail 1, opt-in)."""
        if rail == 1:
            return self._set_msvdd_offset_mv(mv, domain)
        if type(rail) is not int or rail != 0:
            return False, "unknown rail offset index"
        if type(domain) is not int or not 0 <= domain < 32:
            return False, "clock-control index must be an integer from 0 to 31"
        if type(mv) not in (int, float):
            return False, "NVVDD offset must be a finite number"
        try:
            value = float(mv)
        except OverflowError:
            return False, "NVVDD offset exceeds the signed 32-bit microvolt range"
        outcome = _Outcome()
        status = load().dc_gpu_rail_offset_set(self._handle, value, domain, rail, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def _set_msvdd_offset_mv(self, mv, domain, *, reset=False):
        if type(domain) is not int or not 0 <= domain < 32:
            return False, "clock-control index must be an integer from 0 to 31"
        if type(mv) not in (int, float):
            return False, "MSVDD offset must be a finite number"
        try:
            value = float(mv)
        except OverflowError:
            return False, "MSVDD offset exceeds the signed 32-bit microvolt range"
        outcome = _Outcome()
        if reset:
            status = load().dc_gpu_msvdd_offset_reset(self._handle, domain, ctypes.byref(outcome))
        else:
            status = load().dc_gpu_rail_offset_set(self._handle, value, domain, 1, ctypes.byref(outcome))
        if status != DC_OK:
            _raise(status)
        return self._outcome(outcome)

    def reset_msvdd_offset_mv(self, domain=0):
        """Explicitly clear a stored MSVDD request, including after XOC is off."""
        return self._set_msvdd_offset_mv(0, domain, reset=True)

    @property
    def msvdd_write_enabled(self):
        return self._clkdom_flags()[0]

    @msvdd_write_enabled.setter
    def msvdd_write_enabled(self, value):
        self._set_clkdom_flags(bool(value), self.voltage_xoc_enabled)

    @property
    def voltage_xoc_enabled(self):
        return self._clkdom_flags()[1]

    @voltage_xoc_enabled.setter
    def voltage_xoc_enabled(self, value):
        self._set_clkdom_flags(self.msvdd_write_enabled, bool(value))

    def _clkdom_flags(self):
        msvdd, xoc = ctypes.c_int32(), ctypes.c_int32()
        status = load().dc_gpu_clkdom_flags_get(self._handle, ctypes.byref(msvdd), ctypes.byref(xoc))
        if status != DC_OK:
            _raise(status)
        return bool(msvdd.value), bool(xoc.value)

    def _set_clkdom_flags(self, msvdd, xoc):
        status = load().dc_gpu_clkdom_flags_set(self._handle, 1 if msvdd else 0, 1 if xoc else 0)
        if status != DC_OK:
            _raise(status)

    def retry_clock_capabilities(self, *, now=None):
        """At most two delayed, read-only retries of failed clock discovery; True on recovery."""
        if now is not None:
            now = float(now)
            if not math.isfinite(now):
                raise ValueError("clock capability retry time must be finite")
        changed = ctypes.c_int32()
        status = load().dc_gpu_retry_clock_capabilities(self._handle, 0 if now is None else 1,
                                                        0.0 if now is None else now, ctypes.byref(changed))
        if status != DC_OK:
            _raise(status)
        return bool(changed.value)

    def refresh_capabilities(self):
        """Release the clock-control discovery caches for one deliberate, read-only retry."""
        previous = _ClkDomError()
        previous.size = ctypes.sizeof(_ClkDomError)
        status = load().dc_gpu_refresh_capabilities(self._handle, ctypes.byref(previous))
        if status != DC_OK:
            _raise(status)
        return {"previous_clock_error": self._error_tuple(previous), "retry_pending": True}

    # ---- private clock-domain rows (nvbackend.GPU.read_clock_domains) --------------------------- #

    def read_clock_domains(self, pc=None, core_mhz=None, mem_nvml=None):
        """(rows, err): every populated domain of the private getter, named
        against this card. `pc` is a payload already in hand (the ``priv_clocks``
        list of read(), or nvbackend's _AllClocksPriv) or None to read one now."""
        words = _priv_words(pc)
        has_core, core = _optional_u32(core_mhz)
        has_mem, mem = _optional_u32(mem_nvml)
        out = _ClockDomains()
        out.size = ctypes.sizeof(_ClockDomains)
        status = load().dc_gpu_clock_domains(self._handle, words, has_core, core, has_mem, mem, ctypes.byref(out))
        if status == DC_E_UNAVAILABLE:
            return None, PRIV_UNAVAIL
        if status != DC_OK:
            _raise(status)
        return _clock_domain_rows(out), None


def _error_record(error):
    """A dc_error as a dict with the Python exception's kind and text."""
    return {"kind": ("none", "transient", "invalid", "unsupported", "identity", "malformed")[error.kind],
            "fixed": bool(error.fixed),
            "ntstatus": error.ntstatus if error.has_ntstatus else None, "rm_status": error.rm_status,
            "message": _text(error.message)}


def _raise_error(error):
    """Raise the exception a dc_error stands for (DESIGN-TRANSPORT.md 3.6): CurrentLimitUnsupported
    for a fixed property, ValueError (with .fixed) for a failed read."""
    from .nvbackend import CurrentLimitUnsupported
    message = _text(error.message)
    if error.kind == DC_ERR_UNSUPPORTED:
        raise CurrentLimitUnsupported(message)
    exc = ValueError(message)
    if error.fixed:  # nvbackend sets .fixed only on the rejections that are a property of the layout
        exc.fixed = True
    raise exc


def _row_dict(row):
    """A dc_current_row as nvbackend's get_current_limits returns it."""
    return {"policy": row.policy, "label": _text(row.label), "channel": row.channel,
            "minimum_ma": row.minimum_ma, "maximum_ma": row.maximum_ma, "normal_maximum_ma": row.normal_maximum_ma,
            "default_ma": row.default_ma, "limit_ma": row.limit_ma, "requested_ma": row.requested_ma,
            "value_ma": row.value_ma}


def _transport_value(t):
    """A dc_rm_transport as nvbackend keeps it: (header words, fields dict), or None."""
    if not t.captured:
        return None
    header = list(t.header[:min(t.header_count, 17)])
    return header, {name: getattr(t.fields, name) for name in Gpu._ESCAPE_FIELDS}


def _transport_struct(value):
    """The dc_rm_transport for a (header, fields) pair or None. A header of another length is
    stored with its length so the next use rejects it as Python does; its words beyond 17 are
    not kept."""
    t = _RmTransport()
    t.size = ctypes.sizeof(_RmTransport)
    if value is None:
        return t
    header, fields = value
    header = [int(word) for word in header]
    if any(not 0 <= word <= 0xFFFFFFFF for word in header):
        raise ValueError("a transport header holds 32-bit packet words")
    t.captured = 1
    t.header_count = min(len(header), 0xFFFFFFFF)
    for index, word in enumerate(header[:17]):
        t.header[index] = word
    for name in Gpu._ESCAPE_FIELDS:
        setattr(t.fields, name, int(fields.get(name, 0)))
    return t


def _cl_max_words():
    """The largest policy packet any layout sizes, in words (dc_cl_max_words)."""
    return load().dc_cl_max_words()


def _layout_struct(layout):
    """A Python layout dict (the _CURRENT_LIMIT_LAYOUTS shape) as a dc_power_layout row."""
    from . import native_power_layouts as tables
    row = tables._PowerLayout()
    row.size = ctypes.sizeof(tables._PowerLayout)
    sizes = layout["sizes"]
    for index, command in enumerate(tables.POWER_GET_COMMANDS):
        row.sizes[index] = int(sizes.get(command, 0))
    if "commands" in layout:
        row.has_commands = 1
        for index, command in enumerate(tables.POWER_GET_COMMANDS):
            row.commands[index] = int(layout["commands"].get(command, 0))
    row.info_base, row.info_stride = layout["info"]
    row.status_base, row.status_stride = layout["status"]
    row.control_base, row.control_stride = layout["control"]
    if "status_mask_word" in layout:
        row.has_status_mask_word, row.status_mask_word = 1, int(layout["status_mask_word"])
    if "control_mask_word" in layout:
        row.has_control_mask_word, row.control_mask_word = 1, int(layout["control_mask_word"])
    return row


def hook_test_entry(escape_address):
    """The thunk body (dc_hook_test_entry): forwards to the served D3DKMTEscape; its NTSTATUS."""
    return load().dc_hook_test_entry(escape_address)


def hook_test_state():
    """{installed, patched, calls} of the process-wide hook session."""
    installed, patched, calls = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_uint32()
    status = load().dc_hook_test_state(ctypes.byref(installed), ctypes.byref(patched), ctypes.byref(calls))
    if status != DC_OK:
        _raise(status)
    return {"installed": bool(installed.value), "patched": bool(patched.value), "calls": calls.value}


def hook_test_fail_protect(fail):
    """Make the buffer patcher's begin() fail (Python's VirtualProtect failure)."""
    load().dc_hook_test_fail_protect(1 if fail else 0)


def hook_thunk(address, target, pointer_bytes):
    """The hook's thunk bytes for a pointer width (pure)."""
    out, size = (ctypes.c_uint8 * 14)(), ctypes.c_uint32()
    status = load().dc_hook_thunk(address, target, pointer_bytes, out, ctypes.byref(size))
    if status != DC_OK:
        _raise(status)
    return bytes(out[:size.value])


def escape_capability():
    """gdi32's D3DKMT* exports as a capability record (nvbackend.GPU._legacy_clk_gdi, but
    reporting WHAT is missing instead of None). Never raises for a missing capability."""
    c = _EscapeCapability()
    c.size = ctypes.sizeof(_EscapeCapability)
    status = load().dc_escape_capability_get(ctypes.byref(c))
    if status != DC_OK:
        _raise(status)
    out = {name: bool(getattr(c, name)) for name in (
        "gdi32_loaded", "escape", "query_adapter_info", "close_adapter", "enum_adapters2", "enum_adapters",
        "open_from_display_name", "open_from_hdc", "complete")}
    out["method"] = ("none", "enum2", "enum", "gdi_display_name", "hdc")[c.method]
    out["patch_bytes"] = c.patch_bytes
    out["detail"] = _text(c.detail)
    return out


PRIV_UNAVAIL = "private NvAPI_GPU_GetAllClocks (0x1BD69F49) did not answer"


def _optional_u32(value):
    """(has, value) for a Python int-or-None argument that the ABI takes as a flag and a u32."""
    if value is None:
        return 0, 0
    if not _is_plain_int(value) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("expected None or an unsigned 32-bit integer")
    return 1, value


def _priv_words(pc):
    """A (c_uint32 * 288) array for the ABI, or None to let the core read the getter."""
    if pc is None:
        return None
    if hasattr(pc, "w"):  # nvbackend._AllClocksPriv
        pc = list(pc.w)
    words = list(pc)
    if len(words) != PRIV_CLK_WORDS:
        raise ValueError(f"private clock payload must hold {PRIV_CLK_WORDS} words")
    return (ctypes.c_uint32 * PRIV_CLK_WORDS)(*words)


def _clock_domain_rows(out):
    rows = []
    for row in out.rows[:out.count]:
        rows.append({"domain": row.domain, "name": row.name.decode(), "grade": row.grade.decode(),
                     "kind": row.kind.decode(), "prog_khz": row.prog_khz, "meas_khz": row.meas_khz,
                     "flags": row.flags, "srcid": row.srcid, "scale": row.scale,
                     "prog_mhz": row.prog_mhz if row.has_prog_mhz else None,
                     "meas_mhz": row.meas_mhz if row.has_meas_mhz else None,
                     "delta_mhz": row.delta_mhz if row.has_delta_mhz else None})
    return rows


def classify_clock_domains(words, core_mhz=None, mem_nvml=None, architecture=None):
    """read_clock_domains' row loop over `words` followed by
    nvbackend.classify_domain_names(rows, core_mhz, mem_nvml, blackwell=architecture == 10, architecture),
    without a GPU. Returns the rows as dicts with the Python keys."""
    payload = _priv_words(words)
    if payload is None:
        raise ValueError("a private clock payload is required")
    has_core, core = _optional_u32(core_mhz)
    has_mem, mem = _optional_u32(mem_nvml)
    has_arch, arch = _optional_u32(architecture)
    out = _ClockDomains()
    out.size = ctypes.sizeof(_ClockDomains)
    status = load().dc_clock_domains_classify(payload, PRIV_CLK_WORDS, has_core, core, has_mem, mem, has_arch, arch,
                                              ctypes.byref(out))
    if status != DC_OK:
        _raise(status)
    return _clock_domain_rows(out)


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
