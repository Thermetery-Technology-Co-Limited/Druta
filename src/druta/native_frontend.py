# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Python front ends' explicit compatibility surface on the native core.

Driver operations use native helpers. Only the read-only functions named in
DELEGATES retain Python implementations; they run over the core's paired
companions, never construct a Python GPU and never install Python's RM hook.
Importing this module does not load the optional native library.
"""
from __future__ import annotations

import ctypes
import inspect
import threading

from . import nvbackend


class _RequestEnvelopes(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32)] + [
        (name, ctypes.c_double) for name in (
            "volt_limit_min_mv", "volt_limit_max_mv", "volt_limit_xoc_max_mv",
            "rail_offset_max_mv", "rail_offset_xoc_max_mv")
    ] + [("max_abs_delta_khz", ctypes.c_int64), ("hard_floor_mv", ctypes.c_double),
         ("power_limit_xoc_max_mw", ctypes.c_int64),
         ("volt_limit_fields", (ctypes.c_char * 32) * 4)]


_P = ctypes.POINTER
_SIGNATURES = {
    "dc_request_envelopes_get": (ctypes.c_int, [_P(_RequestEnvelopes)]),
    "dc_gpu_msvdd_domains_get": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint32,
                                              _P(ctypes.c_uint32), ctypes.c_size_t,
                                              _P(ctypes.c_size_t)]),
    "dc_pl_enforced_read": (ctypes.c_int, [ctypes.c_void_p, _P(ctypes.c_int32),
                                          _P(ctypes.c_uint32)]),
}


def bind(library):
    """Called by native._bind so old libraries fail before a backend is selected."""
    for name, (restype, argtypes) in _SIGNATURES.items():
        function = getattr(library, name)
        function.restype, function.argtypes = restype, argtypes


def request_envelopes():
    from . import native
    out = _RequestEnvelopes(size=ctypes.sizeof(_RequestEnvelopes))
    status = native.load().dc_request_envelopes_get(ctypes.byref(out))
    if status != native.DC_OK:
        native._raise(status)
    values = {name: getattr(out, name) for name, _ in out._fields_
              if name not in ("size", "volt_limit_fields")}
    values["volt_limit_fields"] = tuple(row.value.decode("ascii") for row in out.volt_limit_fields)
    return values


class _CoreConstant:
    """Class and instance access both read the loaded core's request envelope."""
    def __init__(self, name, source="envelope"):
        self.name, self.source = name, source

    def __get__(self, instance, owner=None):
        if self.source == "envelope":
            return request_envelopes()[self.name]
        if self.source == "rail":
            from . import native_rail_limits
            return native_rail_limits.constants()[self.name]
        from . import native_power_layouts
        return {arch: rows for arch in range(1, 11)
                if (rows := native_power_layouts.generation_policies(arch))}


# Explicit, read-only delegation. Values list the companion members each
# function uses directly; nested GPU helpers are also explicit below.
DELEGATES = {
    "_read_pstates20": ("nvapi.ok", "nvapi.Pstates20Get", "nvapi.ver", "nvapi.gpu"),
    "_pstate20_clock": (),
    "_clkdom_get": ("nvapi.ClkDomCtlGet", "nvapi.gpu"),
    "_clkdom_valid_block": (),
    "_vfp_curve_error": (),
    "clock_offset_range": ("nvml.ok", "nvml.has", "nvml.ver", "nvml.dll", "nvml.dev"),
    "read_power_arbitration": ("nvapi.selected",),
    "read_power_relationships": ("nvapi.selected",),
}

# The front end creates these optional display/session records itself. The
# three exempt methods/table stay intentionally absent (DESIGN-FRONTEND b4).
INTENTIONALLY_ABSENT = frozenset({
    "clkdom_debug_report", "clkdom_mapping_probe", "LEGACY_P0_PROFILES",
    "power_policy_names", "get", "_up9512r_mutex",
})
_miss_lock = threading.Lock()
_miss_seen = set()
_miss_pending = []


def record_missing_member(instance, name):
    """Record a missing probe once; a descriptor's own failure is an internal error."""
    if name in INTENTIONALLY_ABSENT or (name.startswith("__") and name.endswith("__")):
        return
    defined = inspect.getattr_static(type(instance), name, None) is not None
    label = f"{type(instance).__name__}.{name}"
    key = (label, defined)
    with _miss_lock:
        if key not in _miss_seen:
            _miss_seen.add(key)
            _miss_pending.append((label, defined))


def drain_missing_members():
    """Drain human-readable diagnostics, retaining the once-per-process deduplication."""
    with _miss_lock:
        pending, _miss_pending[:] = list(_miss_pending), []
    return [f"native backend: {'internal error reading' if internal else 'front end requested missing member'} {name}"
            for name, internal in pending]


def reset_missing_members_for_tests():
    with _miss_lock:
        _miss_seen.clear()
        _miss_pending.clear()


class FrontendSurface:
    """Only named, parity-tested additions to native.Gpu; no catch-all delegation."""
    ARCH_KEPLER = nvbackend.GPU.ARCH_KEPLER
    ARCH_MAXWELL = nvbackend.GPU.ARCH_MAXWELL
    ARCH_PASCAL = nvbackend.GPU.ARCH_PASCAL
    ARCH_TURING = nvbackend.GPU.ARCH_TURING
    ARCH_AMPERE = nvbackend.GPU.ARCH_AMPERE
    ARCH_ADA = nvbackend.GPU.ARCH_ADA
    ARCH_NAMES = dict(nvbackend.GPU.ARCH_NAMES)
    CLKDOM_DELTA_APPLIES = dict(nvbackend.GPU.CLKDOM_DELTA_APPLIES)
    CLKDOM_DELTA_CLEARS_CEILING = dict(nvbackend.GPU.CLKDOM_DELTA_CLEARS_CEILING)
    EXTRA_POINTS_ABOVE_CAP = nvbackend.GPU.EXTRA_POINTS_ABOVE_CAP
    LIVE_RAIL_VER = nvbackend.GPU.LIVE_RAIL_VER
    LIVE_RAIL_BASE = nvbackend.GPU.LIVE_RAIL_BASE
    LIVE_RAIL_STRIDE = nvbackend.GPU.LIVE_RAIL_STRIDE
    LIVE_RAIL_FIELDS = nvbackend.GPU.LIVE_RAIL_FIELDS
    VOLT_LIMIT_BASE_MV = {}
    VOLT_LIMIT_POWERON = {}
    HOLD_HEADROOM_FIELDS = ("reliability", "alt_reliability", "overvoltage")
    volt_limits_write_enabled = False
    _CLKDOM_BUF = nvbackend.GPU._CLKDOM_BUF

    VOLT_LIMIT_MIN_MV = _CoreConstant("volt_limit_min_mv")
    VOLT_LIMIT_MAX_MV = _CoreConstant("volt_limit_max_mv")
    VOLT_LIMIT_XOC_MAX_MV = _CoreConstant("volt_limit_xoc_max_mv")
    RAIL_OFFSET_MAX_MV = _CoreConstant("rail_offset_max_mv")
    RAIL_OFFSET_XOC_MAX_MV = _CoreConstant("rail_offset_xoc_max_mv")
    MAX_ABS_DELTA_KHZ = _CoreConstant("max_abs_delta_khz")
    HARD_FLOOR_MV = _CoreConstant("hard_floor_mv")
    POWER_LIMIT_XOC_MAX_MW = _CoreConstant("power_limit_xoc_max_mw")
    VOLT_LIMIT_FIELDS = _CoreConstant("volt_limit_fields")
    HOLD_HEADROOM_MAX_MARGIN_MV = _CoreConstant("max_margin_mv", "rail")
    HOLD_HEADROOM_FALLBACK_GRAIN_MV = _CoreConstant("fallback_grain_mv", "rail")
    CURRENT_LIMIT_GENERATION_POLICIES = _CoreConstant("generation_policies", "policies")

    _read_pstates20 = nvbackend.GPU._read_pstates20
    _pstate20_clock = staticmethod(nvbackend.GPU._pstate20_clock)
    _clkdom_get = nvbackend.GPU._clkdom_get
    _clkdom_valid_block = staticmethod(nvbackend.GPU._clkdom_valid_block)
    _vfp_curve_error = staticmethod(nvbackend.GPU._vfp_curve_error)
    read_power_arbitration = nvbackend.GPU.read_power_arbitration
    read_power_relationships = nvbackend.GPU.read_power_relationships

    def __getattribute__(self, name):
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            # Observe the failure where it occurs, before __getattr__ would
            # lose the descriptor's exception. Never run a getter twice.
            record_missing_member(self, name)
            raise

    def clock_offset_range(self, ctype):
        return nvbackend.GPU._offset_range(self, ctype)

    def msvdd_offset_domains(self):
        from . import native
        library = native.load()
        sets = []
        with self._lock:
            for which in (0, 1):
                count = ctypes.c_size_t()
                status = library.dc_gpu_msvdd_domains_get(self._handle, which, None, 0, ctypes.byref(count))
                if status not in (native.DC_OK, native.DC_E_BUFFER):
                    native._raise(status)
                values = (ctypes.c_uint32 * count.value)()
                status = library.dc_gpu_msvdd_domains_get(
                    self._handle, which, values, len(values), ctypes.byref(count))
                if status != native.DC_OK:
                    native._raise(status)
                sets.append(frozenset(values[:count.value]))
        return tuple(sets)

    def read_enforced_power_limit_mw(self):
        from . import native
        present, mw = ctypes.c_int32(), ctypes.c_uint32()
        status = native.load().dc_pl_enforced_read(self._handle, ctypes.byref(present), ctypes.byref(mw))
        if status != native.DC_OK:
            native._raise(status)
        return mw.value if present.value else None

    def legacy_p0_profile(self):
        # No developer-board allowlist: the UI uses its generic runtime tooltip.
        return None

    def read_volt_rail_limits(self):
        from . import native_rails
        return native_rails.read_volt_rail_limits(self)

    def read_volt_rail_state(self):
        from . import native_rails
        return native_rails.read_volt_rail_state(self)

    def read_rail_live_mv(self, rail):
        from . import native_rails
        return native_rails.read_rail_live_mv(self, rail)

    def _volt_rail_profile(self):
        from . import native_rails
        return native_rails.volt_rail_profile(self)

    def volt_rail_diagnostics(self):
        from . import native_rails
        return native_rails.volt_rail_diagnostics(self)

    def volt_rail_limits_supported(self, rail=None):
        from . import native_rails
        return native_rails.volt_rail_limits_supported(self, rail)

    def volt_rail_limit_fields(self, rail):
        from . import native_rails
        return native_rails.volt_rail_limit_fields(self, rail)

    def stock_limit_mv(self, rail, key):
        from . import native_rails
        return native_rails.stock_limit_mv(self, rail, key)

    def volt_rail_limits_mv(self):
        from . import native_rails
        return native_rails.volt_rail_limits_mv(self)

    def _read_volt_rail_deltas(self):
        from . import native_rails
        return native_rails.read_volt_rail_deltas(self)

    def _refresh_volt_rail_capabilities(self):
        from . import native_rails
        return native_rails.refresh(self)

    def legacy_p0_supported(self):
        from . import native_legacy_p0
        return native_legacy_p0.legacy_p0_supported(self)

    def legacy_p0_owned(self):
        from . import native_legacy_p0
        return native_legacy_p0.legacy_p0_owned(self)

    def hold_legacy_p0(self):
        from . import native_legacy_p0
        return native_legacy_p0.hold_legacy_p0(self)

    def release_legacy_p0(self):
        from . import native_legacy_p0
        return native_legacy_p0.release_legacy_p0(self)

    def verification_p0(self, voltage_mv=None):
        from . import native_legacy_p0
        return native_legacy_p0.verification_p0(self, voltage_mv)

    def verification_legacy_p0(self):
        from . import native_legacy_p0
        return native_legacy_p0.verification_legacy_p0(self)

    @property
    def _vf_lock_recovery(self):
        from . import native_legacy_p0
        return native_legacy_p0.vf_lock_recovery(self)

    @_vf_lock_recovery.setter
    def _vf_lock_recovery(self, value):
        from . import native_legacy_p0
        native_legacy_p0.set_vf_lock_recovery(self, value)

    @staticmethod
    def peak_info(points, cap_mv=None):
        from . import native_vfplan
        return native_vfplan.peak_info(points, cap_mv)

    @staticmethod
    def resolve_vf_point(points, volt_mv):
        from . import native_vfplan
        return native_vfplan.resolve_vf_point(points, volt_mv)

    @staticmethod
    def compute_deflatten(points, vcap_mv, max_khz=None, extra_points_above=None, step_khz=None):
        from . import native_vfplan
        return native_vfplan.compute_deflatten(points, vcap_mv, max_khz, extra_points_above, step_khz)

    @staticmethod
    def evaluate_curve_law(khz, max_rise_khz=None):
        from . import native_vfplan
        return native_vfplan.evaluate_curve_law(khz, max_rise_khz)

    @staticmethod
    def predict_curve(points, khz, new, max_rise_khz=None):
        from . import native_vfplan
        return native_vfplan.predict_curve(points, khz, new, max_rise_khz)

    @staticmethod
    def cascade_meta(points, khz, real, pos, idxs):
        from . import native_vfplan
        return native_vfplan.cascade_meta(points, khz, real, pos, idxs)

    @staticmethod
    def compute_hard_deflatten(points, floor_mv, target_khz, step_khz=None):
        from . import native_vfplan
        return native_vfplan.compute_hard_deflatten(points, floor_mv, target_khz, step_khz)

    @staticmethod
    def compute_ramp(points, lo_mv, cap_mv, max_khz=None, step_khz=None):
        from . import native_vfplan
        return native_vfplan.compute_ramp(points, lo_mv, cap_mv, max_khz, step_khz)

    @staticmethod
    def compute_rephase(deltas, step_khz):
        from . import native_vfplan
        return native_vfplan.compute_rephase(deltas, step_khz)

    @classmethod
    def abs_limit_mv(cls, fields, key):
        from . import native_rail_limits
        return native_rail_limits.abs_limit_mv(fields, key)

    @classmethod
    def rail_ceiling_mv(cls, fields):
        from . import native_rail_limits
        return native_rail_limits.rail_ceiling_mv(fields)

    @classmethod
    def rail_floor_mv(cls, fields):
        from . import native_rail_limits
        return native_rail_limits.rail_floor_mv(fields)

    @classmethod
    def volt_boost_headroom_mv(cls, fields=None):
        from . import native_rail_limits
        return native_rail_limits.volt_boost_headroom_mv(fields)

    @classmethod
    def hold_headroom_ceiling_mv(cls, row):
        from . import native_rail_limits
        return native_rail_limits.hold_headroom_ceiling_mv(row)

    @classmethod
    def hold_headroom_targets(cls, fields, hold_mv, margin_mv, maximum_mv):
        from . import native_rail_limits
        return native_rail_limits.hold_headroom_targets(fields, hold_mv, margin_mv, maximum_mv)

    def hold_headroom_bound_mv(self, record=None):
        from . import native_rail_limits
        return native_rail_limits.hold_headroom_bound_mv(record, None if record else self.hold_headroom_record())

    def hold_headroom_allowance_mv(self, record=None):
        from . import native_rail_limits
        return native_rail_limits.hold_headroom_allowance_mv(record, None if record else self.hold_headroom_record())


# Frozen front-end contract, checked independently against the source AST by
# test_backend_conformance. Do not discover members by loading drivers.
REQUIRED_MEMBERS = frozenset("""
    ARCH_ADA ARCH_AMPERE ARCH_KEPLER ARCH_MAXWELL
    ARCH_NAMES ARCH_PASCAL ARCH_TURING CLKDOM_DELTA_APPLIES
    CLKDOM_DELTA_CLEARS_CEILING CURRENT_LIMIT_GENERATION_POLICIES EXTRA_POINTS_ABOVE_CAP HARD_FLOOR_MV
    HOLD_HEADROOM_FALLBACK_GRAIN_MV HOLD_HEADROOM_FIELDS HOLD_HEADROOM_MAX_MARGIN_MV LIVE_RAIL_BASE
    LIVE_RAIL_FIELDS LIVE_RAIL_STRIDE LIVE_RAIL_VER LOCK_STEP
    MAX_ABS_DELTA_KHZ P0_LOCK_STEP POWER_LIMIT_XOC_MAX_MW RAIL_OFFSET_MAX_MV
    RAIL_OFFSET_XOC_MAX_MV VF_LOCK_STEP VOLT_LIMIT_BASE_MV VOLT_LIMIT_FIELDS
    VOLT_LIMIT_MAX_MV VOLT_LIMIT_MIN_MV VOLT_LIMIT_POWERON VOLT_LIMIT_XOC_MAX_MV
    _clkdom_get _clkdom_valid_block _current_limit_abi _current_limit_error
    _current_limit_generation_policies _current_limit_layout_choice _current_limit_read_error _current_limit_rm
    _current_limit_transport _current_limit_transport_layouts _foreign_hold_headroom _hold_headroom_live_trip
    _hold_headroom_unread _legacy_clk_escape _policy_amount _power_policy_mask
    _power_policy_pins _power_policy_table _pstate20_clock _read_pstates20
    _read_volt_rail_deltas _refresh_volt_rail_capabilities _rm_power_transport _vf_lock_recovery
    _vfp_curve_error _volt_rail_profile _write_policy_limit abs_limit_mv
    adopt_foreign_hold_headroom apply_hold_headroom apply_vf_deltas arch
    arch_name available cascade_meta clear_vf_lock
    clkdom_control_label clkdom_control_polarity clkdom_controls_for_ui clkdom_delta_clears_ceiling
    clkdom_delta_inert clkdom_domains clkdom_is_blackwell clkdom_layout
    clkdom_measurements clkdom_offset_slider_offered clkdom_ok clkdom_pairing
    clkdom_step_mhz clock_offset_range clock_step_khz compute_deflatten
    compute_hard_deflatten compute_ramp compute_rephase current_limit_diagnostics
    evaluate_curve_law fan_capabilities get_current_limits hold_headroom_allowance_mv
    hold_headroom_architecture hold_headroom_bound_mv hold_headroom_ceiling_mv hold_headroom_grain_mv
    hold_headroom_live_ok hold_headroom_overwritten hold_headroom_rail_unavailable hold_headroom_record
    hold_headroom_targets hold_legacy_p0 last_clock_lock_request legacy_p0_owned
    legacy_p0_profile legacy_p0_supported lock_gpu_clocks lockable_clocks_by_mem
    max_all_power_policies max_rise_khz mem_offset_scale memory_offset_step_units
    msvdd_offset_domains msvdd_write_enabled peak_info power_limit_bounds_mw
    power_policy_unwritable_reason predict_curve rail_ceiling_mv rail_floor_mv
    rail_offset_capability read read_clk_domain_offsets read_clk_lock
    read_clk_lock_status read_clock_domains read_enforced_power_limit_mw read_fan_control_state
    read_fan_manual read_live_power_request read_native_power_limit_mw read_power_arbitration
    read_power_limit_mw read_power_policies read_power_relationships read_rail_live_mv
    read_rail_offset_mv read_vcore_mv read_vf_curve read_vf_lock
    read_vf_lock_status read_vfp_other_rows read_volt_rail_limits read_volt_rail_state
    read_voltage_boost read_xbar_offset_mhz recover_vf_lock refresh_capabilities
    release_legacy_p0 rephase_deltas reset_all reset_fan
    reset_gpu_clocks reset_msvdd_offset_mv reset_vf_curve reset_volt_rail_limits
    resolve_vf_point restore_fan_control_state restore_hold_headroom retry_clock_capabilities
    set_clk_domain_offset set_clock_offset set_current_limit_ma set_fan
    set_power_limit_mw set_power_policy set_rail_offset_mv set_vf_lock
    set_volt_rail_limits set_volt_rail_limits_raw set_voltage_boost slot
    status_line step_is_measured stock_limit_mv stock_power_policy
    user_rail_limits verification_legacy_p0 verification_p0 vf_curve_applicable
    vf_lock_recovery_pending vf_lock_self_test vfp_layout volt_boost_headroom_mv
    volt_limits_write_enabled volt_rail_diagnostics volt_rail_limit_fields volt_rail_limits_mv
    volt_rail_limits_supported voltage_xoc_enabled
""".split())


def missing_members(gpu_class=None):
    """Missing class members, without constructing a GPU or loading its DLL."""
    if gpu_class is None:
        from .native import Gpu
        gpu_class = Gpu
    return tuple(sorted(REQUIRED_MEMBERS - set(dir(gpu_class))))
