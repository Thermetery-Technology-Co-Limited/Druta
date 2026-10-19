# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""A current-policy interface Druta does not understand has no control, so it
must not make profiles incomplete or Reset all fail; a failed READ still must.

Runs the real current-limit backend on the repo's modelled driver
(tests/test_current_limits.fixture). Nothing touches NVAPI or hardware."""
import unittest
from unittest.mock import Mock

from druta import nvbackend as n
from druta import profiles
from tests.test_current_limits import fixture

A618 = 0x2080A618
TURING_13 = (0x14 + 13 * 0xC4) // 4          # control record of policy 13, 610.88 layout


class ProfileView:
    """What profiles.capture reads, with the current-limit members of the real
    backend forwarded live."""

    def __init__(self, gpu):
        self._gpu = gpu
        self.static = gpu.static

    def read(self):
        return {}

    def mem_offset_scale(self):
        return 2, "MHz eff"

    def vf_curve_applicable(self):
        return False

    def read_voltage_boost(self):
        return None

    def __getattr__(self, name):
        if name.startswith("_current_limit") or name == "get_current_limits":
            return getattr(self._gpu, name)
        raise AttributeError(name)


def capture_problems(gpu):
    return [m for m in profiles.incomplete(profiles.capture(ProfileView(gpu)))
            if "Current limits" in m]


def reset_steps(gpu):
    gpu.nvapi.VoltCtrlGet = None
    gpu.nvapi.BoostTableSet = None
    gpu._volt_rail_profile = Mock(return_value=None)
    for name in ("set_clock_offset", "set_power_limit_mw", "_reset_gpu_clocks", "reset_fan"):
        setattr(gpu, name, Mock(return_value=(True, "ok")))
    for name in ("clkdom_ok", "volt_rail_limits_supported", "_vf_lock_available"):
        setattr(gpu, name, Mock(return_value=False))
    return [r for r in gpu.reset_all() if not r[0] and "current limits" in r[1]]


def unknown_transport(gpu, packet_bytes=30000):
    gpu._capture_current_limit_transport = Mock(return_value=None)
    gpu._current_limit_observed_transport = {"command": "0x2080A612", "packet_bytes": packet_bytes,
                                             "parameter_bytes": packet_bytes - 68}


def info_rm(gpu, code_for_size):
    """Make the modelled RM answer the info GET with code_for_size(size)."""
    inner = gpu._legacy_clk_escape.side_effect

    def escape(packet, fields):
        if packet[14] == A618:
            code = code_for_size(packet[15])
            if code:
                packet[16] = code
                return 0
        return inner(packet, fields)
    gpu._legacy_clk_escape = Mock(side_effect=escape)


class NotUnderstoodCase(unittest.TestCase):
    def assert_not_blocking(self, gpu, reason):
        self.assertEqual(gpu.get_current_limits(), [])
        self.assertIn(reason, gpu._current_limit_error)       # still shown and in Copy info
        self.assertEqual(gpu._current_limit_read_error, "")
        self.assertEqual(capture_problems(gpu), [])
        self.assertEqual(reset_steps(gpu), [])


class NotUnderstoodIsNotAFailedReadTests(NotUnderstoodCase):
    def test_ampere_policy_13_of_another_record_type(self):
        gpu, state = fixture(ampere_layout=True)
        meta = (0x58 + 13 * 0xE4) // 4
        state.info[meta + 1] = (state.info[meta + 1] & ~0xFF) | 0x0B
        self.assert_not_blocking(gpu, "invalid current record")
        self.assertTrue(gpu._current_limit_unavailable[0]["unsupported"])

    def test_power_get_of_unrecognized_geometry(self):
        gpu, _ = fixture(ampere_layout=True)
        unknown_transport(gpu)
        self.assert_not_blocking(gpu, "30000/29932 bytes")

    def test_every_candidate_info_size_rejected_by_the_driver(self):
        gpu, _ = fixture(ampere_layout=True)
        info_rm(gpu, lambda size: 0x1F)
        self.assert_not_blocking(gpu, "no known policy-info layout fits this driver")
        self.assertIn("20000-byte info", gpu._current_limit_error)
        self.assertIn("8632-byte info", gpu._current_limit_error)

    def test_blackwell_other_rail_not_understood_keeps_core_current(self):
        gpu, state = fixture()                                      # policies 13 and 14
        meta = (0x58 + 14 * 0xE4) // 4
        state.info[meta + 1] = (state.info[meta + 1] & ~0xFF) | 0x07
        self.assertEqual([r["policy"] for r in gpu.get_current_limits()], [13])
        self.assertEqual(gpu._current_limit_read_error, "")
        snap = profiles.capture(ProfileView(gpu))
        self.assertEqual(list(snap["current_limits_ma"]), ["13"])
        self.assertEqual([m for m in profiles.incomplete(snap) if "Current" in m], [])


class MoreNotUnderstoodTests(NotUnderstoodCase):
    def test_single_layout_drivers_that_reject_the_info_size(self):
        for arch in (n.GPU.ARCH_TURING, n.GPU.ARCH_PASCAL, 10):
            with self.subTest(arch=arch):
                gpu, _ = fixture(arch)                        # 11748-byte transport: one layout
                info_rm(gpu, lambda size: 0x1F)
                self.assert_not_blocking(gpu, "size 8620 rejected (RM 0x1F)")

    def test_control_and_status_headers_outside_the_measured_layout(self):
        gpu, state = fixture(n.GPU.ARCH_TURING, newer=True)
        state.control[3] = 0                                  # control[:5] should be 0,0,0,255,mask
        self.assert_not_blocking(gpu, "layout or mask differs")

    def test_a_mask_wider_than_the_layout_holds(self):
        gpu, _ = fixture(n.GPU.ARCH_TURING)
        gpu._current_limit_capacity = lambda layout: 8        # mask 0xEBBF has bits above 7
        self.assert_not_blocking(gpu, "mask exceeds the validated ABI capacity")

    def test_every_candidate_rejected_for_size_or_a_mask_it_cannot_hold(self):
        gpu, _ = fixture(ampere_layout=True)                  # 20000 rejected (RM 0x1F)
        gpu._current_limit_capacity = lambda layout: 8        # 8632 answers, mask 1 << 13
        self.assert_not_blocking(gpu, "mask does not fit this info layout")

    def test_a_captured_header_of_unknown_geometry(self):
        gpu, _ = fixture(n.GPU.ARCH_TURING)
        header, _fields = gpu._capture_current_limit_transport.return_value
        header[2], header[15] = 30000, 29932
        self.assert_not_blocking(gpu, "unvalidated current-policy transport geometry")

    def test_the_profile_says_why_it_holds_no_current_limits(self):
        gpu, _ = fixture(ampere_layout=True)
        unknown_transport(gpu)
        snap = profiles.capture(ProfileView(gpu))
        self.assertIn("30000/29932 bytes", snap["current_limits_note"])
        self.assertIn("current limits not readable on this card", profiles.summarize(snap))
        self.assertEqual(profiles.incomplete(snap), [])


class FailedReadsStillBlockTests(unittest.TestCase):
    def assert_blocking(self, gpu, reason):
        gpu.get_current_limits()
        self.assertIn(reason, gpu._current_limit_read_error)
        self.assertTrue(capture_problems(gpu))
        self.assertTrue(reset_steps(gpu))

    def test_transport_failure_on_turing(self):
        gpu, _ = fixture(n.GPU.ARCH_TURING, newer=True)
        info_rm(gpu, lambda size: None)
        inner = gpu._legacy_clk_escape.side_effect
        gpu._legacy_clk_escape = Mock(side_effect=lambda p, f: 0xC0000001 if p[14] == A618 else inner(p, f))
        self.assert_blocking(gpu, "NTSTATUS")

    def test_nothing_captured_may_be_a_missed_capture(self):
        gpu, _ = fixture(ampere_layout=True)
        gpu._capture_current_limit_transport = Mock(return_value=None)
        self.assert_blocking(gpu, "no recognized power GET captured")

    def test_one_off_rm_error_on_the_right_layout_is_reported_and_blocks(self):
        gpu, _ = fixture(n.GPU.ARCH_TURING, newer=True)            # 610.88: 20000 right, 8632 not
        info_rm(gpu, lambda size: 0x3 if size == 20000 else 0x1F)
        self.assert_blocking(gpu, "20000-byte info: policy info layout rejected (RM 0x3)")

    def test_reading_outside_an_understood_records_range(self):
        gpu, state = fixture(n.GPU.ARCH_TURING, newer=True)
        state.control[TURING_13 + 1] = 390001                      # above the record's maximum
        self.assert_blocking(gpu, "invalid current record")
        self.assertFalse(gpu._current_limit_unavailable[0]["unsupported"])

    def test_a_policy_already_read_this_session_then_not_understood(self):
        gpu, _ = fixture(n.GPU.ARCH_TURING, newer=True)
        self.assertEqual([r["policy"] for r in gpu.get_current_limits()], [13])
        gpu._current_limit_transport = None                        # lost, re-captured differently
        unknown_transport(gpu)
        self.assert_blocking(gpu, "30000/29932 bytes")

    def test_a_known_power_get_whose_capture_then_failed(self):
        # the hook records every power GET it sees, recognized ones included;
        # a getter that fails after issuing one is a failed read
        gpu, _ = fixture(n.GPU.ARCH_TURING, newer=True)
        unknown_transport(gpu, packet_bytes=54420)                 # 54420/54352 is a known layout
        self.assert_blocking(gpu, "54420/54352 bytes, but the capture did not complete")

    def test_a_record_left_by_an_earlier_capture_is_not_this_attempts(self):
        gpu, _ = fixture(ampere_layout=True)
        del gpu._capture_current_limit_transport                   # the real capture
        gpu._current_limit_observed_transport = {"command": "0x2080A612", "packet_bytes": 30000,
                                                 "parameter_bytes": 29932}
        gpu.nvapi.PowerPolInfo = None                              # this attempt sees nothing
        self.assert_blocking(gpu, "no recognized power GET captured")

    def test_a_policy_already_read_whose_record_type_changes(self):
        gpu, state = fixture(n.GPU.ARCH_TURING, newer=True)
        gpu.get_current_limits()
        meta = (0xCC + 13 * 0xFC) // 4
        state.info[meta + 1] = (state.info[meta + 1] & ~0xFF) | 0x0F
        self.assert_blocking(gpu, "policy 13: invalid current record (type=0xF")

    def test_blackwell_keeps_core_current_and_blocks_on_a_changed_other_rail(self):
        gpu, state = fixture()
        self.assertEqual([r["policy"] for r in gpu.get_current_limits()], [13, 14])
        meta = (0x58 + 14 * 0xE4) // 4
        state.info[meta + 1] = (state.info[meta + 1] & ~0xFF) | 0x07
        self.assertEqual([r["policy"] for r in gpu.get_current_limits()], [13])
        self.assert_blocking(gpu, "policy 14: invalid current record")

    def test_a_policy_already_read_that_the_mask_stops_listing(self):
        gpu, state = fixture(n.GPU.ARCH_TURING, newer=True)
        gpu.get_current_limits()
        state.info[1] = 0
        self.assert_blocking(gpu, "policy 13 was read earlier this session")


class UnchangedTests(unittest.TestCase):
    def test_supported_cards_capture_and_reset_as_before(self):
        for arch, newer in ((n.GPU.ARCH_TURING, True), (n.GPU.ARCH_TURING, False),
                            (n.GPU.ARCH_PASCAL, False), (10, False), (n.GPU.ARCH_AMPERE, True)):
            with self.subTest(arch=arch, newer=newer):
                gpu, _ = (fixture(ampere_layout=True) if arch == n.GPU.ARCH_AMPERE
                          else fixture(arch, newer=newer))
                self.assertTrue(gpu.get_current_limits())
                self.assertEqual((gpu._current_limit_error, gpu._current_limit_read_error), ("", ""))
                self.assertEqual(capture_problems(gpu), [])
                self.assertEqual(reset_steps(gpu), [])

    def test_diagnostics_name_the_chosen_layout_and_the_read_error(self):
        gpu, _ = fixture(n.GPU.ARCH_TURING, newer=True)
        gpu.get_current_limits()
        diagnostics = gpu.current_limit_diagnostics()
        self.assertEqual(diagnostics["layout_info_bytes"], 20000)
        self.assertEqual(diagnostics["read_error"], "")
        ampere, _ = fixture(ampere_layout=True)
        ampere.get_current_limits()
        self.assertEqual(ampere.current_limit_diagnostics()["layout_info_bytes"], 8632)


if __name__ == "__main__":
    unittest.main()
