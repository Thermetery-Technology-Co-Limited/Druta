# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ada uses measured semantics with the current adapter's validated getters."""
import ctypes
import struct
from unittest.mock import Mock

import pytest

from druta import nvbackend as n
from tests.test_clock_capability_refresh import clock_gpu
from tests.test_current_limits import fixture as current_gpu
from tests.test_volt_rails import fake_gpu


def telemetry():
    return [dict(domain=domain, kind=n.PRIV_FREQ, prog_khz=mhz * 1000,
                 meas_khz=mhz * 1000, prog_mhz=mhz, meas_mhz=mhz)
            for domain, mhz in ((0, 1500), (1, 1530), (2, 1320), (3, 405),
                                (4, 11251), (5, 1350), (21, 1800), (22, 0))]


def test_names_confirm_gpu_z_correlations_but_keep_ltc_uncertain_and_empty_rows_unnamed():
    rows = {row["domain"]: row for row in n.classify_domain_names(
        telemetry(), 1500, 11251, architecture=n.GPU.ARCH_ADA)}
    assert (rows[0]["name"], rows[0]["grade"]) == ("GPC", n.PRIV_CONFIRMED)
    assert (rows[4]["name"], rows[4]["grade"]) == ("MEM", n.PRIV_CONFIRMED)
    for domain, name in ((1, "Crossbar"), (2, "SYS"), (21, "Video")):
        assert (rows[domain]["name"], rows[domain]["grade"]) == (name, n.PRIV_CONFIRMED)
    assert (rows[5]["name"], rows[5]["grade"]) == ("LTC", n.PRIV_LIKELY)
    assert rows[3]["name"] == ""
    assert rows[3]["grade"] == n.PRIV_UNNAMED
    assert rows[22]["name"] == ""
    assert rows[22]["grade"] == n.PRIV_UNPOPULATED


def test_clock_controls_use_explicit_routing_without_optional_telemetry():
    gpu, state, layout = clock_gpu(n.GPU.ARCH_ADA)
    assert gpu.clkdom_layout() == n.CLKDOM_LAYOUT_TURING
    assert gpu.clkdom_controls_for_ui(rows=[]) == [1, 2, 3, 5, 9]
    assert gpu.clkdom_pairing(rows=[]) == {1: 1, 2: 4, 3: 2, 5: 21, 9: 5}
    assert [gpu.clkdom_control_label(i) for i in (1, 2, 3, 5, 9)] == [
        "Crossbar", "Memory", "SYS", "Video", "LTC?"]
    gpu.read.assert_not_called()
    gpu._clk_measure_freq = Mock(side_effect=AssertionError("counter identity is unvalidated"))
    assert gpu.clkdom_measurements() == {}
    gpu._clk_measure_freq.assert_not_called()
    assert state["writes"] == []


def test_partial_clock_capabilities_recover_without_erasing_established_state():
    gpu, state, _ = clock_gpu(n.GPU.ARCH_ADA, domains=(0, 1, 2, 3, 5, 9))
    state["accept"] = {1, 2}
    assert gpu.clkdom_controls_for_ui() == [1, 2]
    reference, initial = {0: object()}, {0: object()}
    gpu._volt_rail_reference, gpu._volt_rail_initial_uv = reference, initial
    assert not gpu.retry_clock_capabilities(now=10)
    state["accept"] = {1, 2, 3, 5, 9}
    prior_reads = len(state["gets"])
    assert gpu.retry_clock_capabilities(now=11)
    assert state["gets"][prior_reads:] == [1 << i for i in (3, 5, 9)]
    assert gpu.clkdom_controls_for_ui() == [1, 2, 3, 5, 9]
    assert gpu._volt_rail_reference is reference
    assert gpu._volt_rail_initial_uv is initial
    assert state["writes"] == []


@pytest.mark.parametrize("bad_field", ["version", "mask"])
def test_success_status_with_invalid_echo_does_not_enable_ada_controls(bad_field):
    gpu, state, _ = clock_gpu(n.GPU.ARCH_ADA)
    if bad_field == "version":
        ctypes.cast(state["wire"], ctypes.POINTER(n.u32))[0] = 0
    else:
        state["bad_echo"] = True
    assert gpu.clkdom_controls_for_ui() == []
    assert not gpu.set_clk_domain_offset(1, -60)[0]
    assert state["writes"] == []


@pytest.mark.parametrize("control, delta", [(1, -60), (2, -64), (3, -60), (5, -60), (9, -60)])
def test_ada_clock_write_preserves_unknown_bytes_and_restores(control, delta):
    gpu, state, layout = clock_gpu(n.GPU.ARCH_ADA)
    original = bytes(state["wire"])[:layout.size]
    expected = bytearray(original)
    struct.pack_into("<I", expected, layout.mask_dword * 4, 1 << control)
    offset = layout.header + control * layout.stride + layout.freq_khz
    struct.pack_into("<i", expected, offset, delta * 1000)
    assert gpu.set_clk_domain_offset(control, delta)[0]
    assert state["writes"] == [bytes(expected)]
    assert gpu.read_clk_domain_offsets()[0][control]["freq_khz"] == delta * 1000
    assert gpu.set_clk_domain_offset(control, 0)[0]
    struct.pack_into("<i", expected, offset, 0)
    assert state["writes"][-1] == bytes(expected)


def test_ada_rail_reference_and_bounds_come_from_another_adapter():
    gpu = fake_gpu(architecture=n.GPU.ARCH_ADA, name="Other PCB", devid=0xFFFF,
                   subsys=0, driver="another branch", vbios="different")
    gpu.nvapi.bases = (987625, 1087125, 1173375, 612500)
    gpu.nvapi.sync_live()
    row = gpu.read_volt_rail_limits()[0]
    assert row["_base_mv"] == dict(zip(gpu.VOLT_LIMIT_FIELDS, (987.625, 1087.125, 1173.375, 612.5)))
    assert gpu.volt_rail_limits_supported(0)
    assert not gpu.volt_rail_limits_supported(1)
    assert gpu.volt_rail_limit_fields(1) == ()
    assert gpu.hold_headroom_architecture()
    gpu.volt_limits_write_enabled = True
    assert gpu.set_volt_rail_limits(0, reliability=982.625)[0]
    assert gpu.nvapi.control[0] == [-5000, 0, 0, 0]
    assert gpu.reset_volt_rail_limits()[0]
    assert gpu.nvapi.control[0] == [0, 0, 0, 0]


def test_ada_rail_transient_get_retries_without_inventing_a_second_rail():
    gpu = fake_gpu(architecture=n.GPU.ARCH_ADA)
    getter = gpu.nvapi.VoltRailsCtlGet
    attempts = []

    def flaky(handle, buf):
        attempts.append(ctypes.cast(buf, ctypes.POINTER(n.u32))[1])
        return -1 if len(attempts) == 1 else getter(handle, buf)

    gpu.nvapi.VoltRailsCtlGet = flaky
    assert gpu.volt_rail_limits_supported(0)
    assert attempts[:2] == [1, 1]
    assert not gpu.volt_rail_limits_supported(1)
    gpu._write_rail_records.assert_not_called()


@pytest.mark.parametrize("newer", [False, True])
def test_ada_current_semantics_accept_older_validated_abis_and_live_ranges(newer):
    gpu, state = current_gpu(n.GPU.ARCH_AMPERE, newer=newer)
    gpu.arch.return_value = n.GPU.ARCH_ADA
    gpu.static.update(driver="unrelated", vbios="different")
    gpu.nvapi.selected["devid"] = 0xFFFF
    row = gpu.get_current_limits()[0]
    assert row["default_ma"] == 243000
    assert row["maximum_ma"] == 270000
    original = state.control.copy()
    assert gpu.set_current_limit_ma(13, 242000)[0]
    assert gpu.set_current_limit_ma(13, 243000)[0]
    assert state.control == original
