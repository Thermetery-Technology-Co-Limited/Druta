# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The frontend facade over a real core and the shared fake card (G4e/f)."""
import inspect
import math
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from druta import native, nvbackend
from druta import native_frontend as frontend
from tests.fake_driver import card

BUILT = native.available()
needs_core = pytest.mark.skipif(not BUILT, reason="native core not built")


def canonical(value):
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items() if key != "captured_at"}
    if isinstance(value, (tuple, list)):
        return type(value)(canonical(item) for item in value)
    return value


def compare(action, **options):
    results = []
    for kind in ("python", "native"):
        with card(kind, **options) as session:
            session.gpu.static.get("pl_min_mw")
            session.gpu.arch()
            result = action(session)
            assert session.driver.errors == []
            results.append(canonical(result))
    assert results[0] == results[1]
    return results[1]


def test_import_and_python_selection_do_not_require_native_library():
    code = """
from unittest.mock import patch
from druta import gpu_backend, native
with patch.object(native, 'load', side_effect=AssertionError('optional DLL loaded')):
    from druta import native_frontend
    assert native_frontend.missing_members() == ()
    with patch('druta.nvbackend.GPU', return_value='python-card'):
        assert gpu_backend.open_gpu(kind='python') == 'python-card'
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env={**os.environ, "DRUTA_NATIVE_LIB": "/missing/drutacore.dll"})
    assert result.returncode == 0, result.stderr


def test_gate_reports_missing_required_names_without_loading():
    class Incomplete:
        pass
    with patch.object(native, "load", side_effect=AssertionError("must not load")):
        assert set(frontend.missing_members(Incomplete)) == frontend.REQUIRED_MEMBERS
        assert frontend.missing_members() == ()


def test_missing_and_internal_errors_are_distinct_and_getter_runs_once():
    class Broken(frontend.FrontendSurface):
        calls = 0

        @property
        def broken_property(self):
            self.calls += 1
            raise AttributeError("the nested value is missing")

    frontend.reset_missing_members_for_tests()
    obj = Broken()
    assert getattr(obj, "unknown_feature", 3) == 3
    assert getattr(obj, "unknown_feature", 4) == 4
    for name in frontend.INTENTIONALLY_ABSENT:
        getattr(obj, name, None)
    with pytest.raises(AttributeError, match="the nested value is missing"):
        _ = obj.broken_property
    assert obj.calls == 1
    assert frontend.drain_missing_members() == [
        "native backend: front end requested missing member Broken.unknown_feature",
        "native backend: internal error reading Broken.broken_property",
    ]
    assert frontend.drain_missing_members() == []
    assert getattr(obj, "unknown_feature", 5) == 5
    assert frontend.drain_missing_members() == []
    frontend.reset_missing_members_for_tests()


@needs_core
def test_envelope_descriptors_report_the_core_values_on_class_and_instance():
    with card("native") as session:
        for name, value in frontend.request_envelopes().items():
            name = name.upper()
            assert isinstance(inspect.getattr_static(native.Gpu, name), frontend._CoreConstant)
            assert getattr(native.Gpu, name) == value == getattr(session.gpu, name)
            assert value == getattr(nvbackend.GPU, name)
        for name in ("HOLD_HEADROOM_MAX_MARGIN_MV", "HOLD_HEADROOM_FALLBACK_GRAIN_MV",
                     "CURRENT_LIMIT_GENERATION_POLICIES"):
            assert getattr(session.gpu, name) == getattr(nvbackend.GPU, name)


@needs_core
def test_rail_read_surface_and_refresh_keep_the_same_reference():
    def action(s):
        g = s.gpu
        before = [g.read_volt_rail_limits(), g.read_volt_rail_state(), g._read_volt_rail_deltas(),
                  g._volt_rail_profile(), g.volt_rail_diagnostics(), g.volt_rail_limits_mv()]
        per_rail = [(g.read_rail_live_mv(rail), g.volt_rail_limits_supported(rail),
                     g.volt_rail_limit_fields(rail), g.stock_limit_mv(rail, "reliability"))
                    for rail in (0, 1)]
        g._refresh_volt_rail_capabilities()
        return before, per_rail, g.read_volt_rail_limits()
    result = compare(action)
    assert result[0][0] == result[2]
    assert result[1][0][1] is True


@needs_core
@pytest.mark.parametrize("name,args", [
    ("peak_info", ()), ("resolve_vf_point", (900.0,)),
    ("compute_deflatten", (900.0,)), ("compute_hard_deflatten", (800.0, 1500000)),
    ("compute_ramp", (800.0, 925.0)),
])
def test_curve_planners_on_the_gpu_class(name, args):
    points = [{"idx": i, "volt_mv": 800.0 + 25 * i, "freq_mhz": 1500.0 + 15 * (i // 2), "delta_khz": 0}
              for i in range(8)]
    assert canonical(getattr(native.Gpu, name)(points, *args)) == canonical(
        getattr(nvbackend.GPU, name)(points, *args))


@needs_core
def test_remaining_curve_planners_and_headroom_helpers():
    points = [{"idx": i, "volt_mv": 800.0 + 25 * i, "freq_mhz": 1500.0 + 15 * i, "delta_khz": 0}
              for i in range(4)]
    cases = [
        ("evaluate_curve_law", ([1500000, 1515000, 1530000, 1545000],)),
        ("predict_curve", (points, [1500000, 1515000, 1530000, 1545000], {1: 1530000})),
        ("cascade_meta", (points, [1500000, 1515000, 1530000, 1545000], [1500000, 1530000, 1530000, 1545000],
                          {0: 0, 1: 1, 2: 2, 3: 3}, [1])),
        ("compute_rephase", ({0: 17000, 1: -17000}, 15000)),
    ]
    for name, args in cases:
        assert canonical(getattr(native.Gpu, name)(*args)) == canonical(getattr(nvbackend.GPU, name)(*args))
    row = {"reliability": 0.0, "alt_reliability": 0.0, "overvoltage": 0.0, "vmin": 0.0,
           "_base_mv": {"reliability": 1068.75, "alt_reliability": 1093.75, "overvoltage": 1125., "vmin": 650.},
           "_boost_mv": 25., "_effective_mv": 1087.5}
    for name, args in [
        ("abs_limit_mv", (row, "reliability")), ("rail_ceiling_mv", (row,)), ("rail_floor_mv", (row,)),
        ("volt_boost_headroom_mv", (row,)), ("hold_headroom_ceiling_mv", (row,)),
        ("hold_headroom_targets", (row, 1100., 25., 1200.)),
    ]:
        assert canonical(getattr(native.Gpu, name)(*args)) == canonical(getattr(nvbackend.GPU, name)(*args))
    record = {"hold_mv": 1000.0, "margin_mv": 25.0, "grain_mv": 6.25, "live_offset_mv": 2.0}
    assert compare(lambda s: (s.gpu.hold_headroom_bound_mv(record),
                              s.gpu.hold_headroom_allowance_mv(record))) == (1025., 5.125)


@needs_core
def test_legacy_p0_methods_and_verification_contexts():
    def action(s):
        g = s.gpu
        out = [g.legacy_p0_supported(), g.legacy_p0_owned(), g.hold_legacy_p0(), g.legacy_p0_owned(),
               g.release_legacy_p0(), g.legacy_p0_owned()]
        for method in (g.verification_p0, g.verification_legacy_p0):
            with method() as check:
                check()
                out.append(g.legacy_p0_owned())
            out.append(g.legacy_p0_owned())
        return out
    result = compare(action, architecture="kepler")
    assert result[:2] == [True, False]
    assert result[-4:] == [True, False, True, False]
    with card("native", architecture="kepler") as session:
        assert session.gpu.legacy_p0_profile() is None


@needs_core
def test_vf_lock_recovery_round_trips_without_python_shadow_state():
    record = {"domain": 6, "previous": (0, 0), "requested": (3, 900000), "verification": True}
    with card("native") as session:
        g = session.gpu
        assert g._vf_lock_recovery is None
        g._vf_lock_recovery = record
        assert g._vf_lock_recovery == record
        assert "_vf_lock_recovery" not in g.__dict__
        g._vf_lock_recovery = None
        assert g._vf_lock_recovery is None


@needs_core
def test_public_offset_ranges_are_fresh_and_keep_nvapi_fallback():
    def action(s):
        g = s.gpu
        first = g.clock_offset_range(0), g.clock_offset_range(2), g.clock_offset_range(1)
        s.driver.parts["clock"].offsets[0]["mx"] = 777
        return first, g.clock_offset_range(0)
    result = compare(action)
    assert result[0][0] == (-1000, 1000, 0)
    assert result[1] == (-1000, 777, 0)
    result = compare(lambda s: (s.gpu.clock_offset_range(0), s.gpu.clock_offset_range(2)),
                     absent=("nvmlDeviceGetClockOffsets",))
    assert result[0] == (-1000, 1000, 0)
    assert result[1] == (-1001, 3001, 0)


@needs_core
def test_public_msvdd_domains_capture_written_and_seen_this_session():
    def action(s):
        g = s.gpu
        initial = g.msvdd_offset_domains()
        g.rail_offset_capability(1, 3)
        seen = g.msvdd_offset_domains()
        g.msvdd_write_enabled = True
        written = g.set_rail_offset_mv(10, 3, rail=1)
        return initial, seen, written, g.msvdd_offset_domains()
    initial, seen, written, final = compare(action)
    assert initial == (frozenset(), frozenset())
    assert seen == (frozenset(), frozenset({3}))
    assert written[0] is True
    assert final == (frozenset({3}), frozenset({3}))


@needs_core
def test_profile_refuses_a_300_mv_limit_using_each_backends_reported_envelope():
    from druta import profiles

    def action(s):
        return profiles.preflight(s.gpu, {
            "device": dict(s.gpu.static), "xoc": False,
            "rail_limits_mv": {"0": {"vmin": 300.0}},
        })
    assert compare(action) == "rail 0 vmin is outside the saved mode's voltage bounds"


@needs_core
def test_enforced_power_is_one_fresh_read_and_failed_read_does_not_cache():
    def action(s):
        mark = s.driver.mark()
        first = s.gpu.read_enforced_power_limit_mw()
        calls = s.driver.since(mark)
        s.driver.fail_next("nvmlDeviceGetEnforcedPowerLimit", 999)
        failed = s.gpu.read_enforced_power_limit_mw()
        return first, calls, failed, s.gpu.read_enforced_power_limit_mw()
    first, calls, failed, retried = compare(action)
    assert isinstance(first, int) and retried == first
    assert calls == [("nvml", "nvmlDeviceGetEnforcedPowerLimit")]
    assert failed is None
    assert compare(lambda s: s.gpu.read_enforced_power_limit_mw(),
                   absent=("nvmlDeviceGetEnforcedPowerLimit",)) is None


@needs_core
def test_private_delegates_read_through_paired_companions():
    def action(s):
        g = s.gpu
        info, error = g._read_pstates20()
        clock = g._pstate20_clock(info, 0)
        status, block = g._clkdom_get(1)
        return error, (clock.domain, clock.delta.minimum, clock.delta.maximum), status, bytes(block), \
            g._clkdom_valid_block(block, g.clkdom_layout(), 1)
    result = compare(action)
    assert result[0] is None and result[2] == 0 and result[-1] is True


@needs_core
def test_diagnostics_never_call_python_capture_or_write_hooks():
    for method in ("read_power_arbitration", "read_power_relationships"):
        with patch.object(nvbackend.GPU, "_capture_current_limit_transport", side_effect=AssertionError("Python hook")), \
             patch.object(nvbackend.GPU, "_capture_legacy_clk_transport", side_effect=AssertionError("Python hook")), \
             patch.object(nvbackend.GPU, "_write_rail_records", side_effect=AssertionError("Python writer")):
            result = compare(lambda s, name=method: getattr(s.gpu, name)())
            assert result[0] is not None
