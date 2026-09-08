# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reset must distinguish unreadable controls from confirmed stock values."""

import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta.nvbackend import (
    GPU, NvAPI, _ClockLock, VF_LOCK_VERSION,
    VF_LOCK_MODE_POINT, VF_LOCK_MODE_FREQ,
)
from tests.test_pascal_reset import reset_gpu


class ResetReadbackTests(unittest.TestCase):
    def gpu(self):
        gpu = reset_gpu()
        gpu.clkdom_ok = Mock(return_value=True)
        gpu.clkdom_layout = Mock(return_value=SimpleNamespace(nvvdd_uv=0x104))
        gpu.read_clk_domain_offsets = Mock(return_value=({2: {"freq_khz": 0}}, None))
        gpu.read_rail_offset_mv = Mock(return_value=0)
        gpu.set_clk_domain_offset = Mock(return_value=(True, "clock reset"))
        gpu.set_rail_offset_mv = Mock(return_value=(True, "rail reset"))
        gpu._volt_rail_profile = Mock(return_value=None)
        gpu.read_volt_rail_limits = Mock()
        gpu.reset_volt_rail_limits = Mock(return_value=(True, "limits reset"))
        return gpu

    def steps(self, gpu):
        return {step.name: tuple(step) for step in gpu.reset_all()}

    def test_unreadable_offsets_report_failure_and_can_be_retried(self):
        gpu = self.gpu()
        stored = {"clock": 25000, "rail": 12.5}
        gpu.read_clk_domain_offsets.return_value = (None, "driver read failed")
        gpu.read_rail_offset_mv.return_value = None
        steps = self.steps(gpu)
        self.assertFalse(steps["clock-domain offsets"][0])
        self.assertFalse(steps["core rail offset"][0])
        gpu.set_clk_domain_offset.assert_not_called()
        gpu.set_rail_offset_mv.assert_not_called()
        self.assertEqual(stored, {"clock": 25000, "rail": 12.5})

        gpu.read_clk_domain_offsets.return_value = ({2: {"freq_khz": stored["clock"]}}, None)
        gpu.read_rail_offset_mv.return_value = stored["rail"]
        gpu.set_clk_domain_offset.side_effect = lambda domain, value: (
            stored.update(clock=value * 1000) or (True, "clock reset"))
        gpu.set_rail_offset_mv.side_effect = lambda value, domain: (
            stored.update(rail=value) or (True, "rail reset"))
        self.assertTrue(all(ok for ok, _ in self.steps(gpu).values()))
        self.assertEqual(stored, {"clock": 0, "rail": 0})

    def test_partial_or_empty_clock_read_cannot_be_treated_as_stock(self):
        for rows, error in (({}, None), ({2: {"freq_khz": 25000}}, "partial read")):
            with self.subTest(rows=rows, error=error):
                gpu = self.gpu()
                gpu.read_clk_domain_offsets.return_value = (rows, error)
                self.assertFalse(self.steps(gpu)["clock-domain offsets"][0])
                gpu.set_clk_domain_offset.assert_not_called()

    def test_confirmed_zero_offsets_require_no_write_or_failure(self):
        gpu = self.gpu()
        steps = self.steps(gpu)
        self.assertTrue(all(ok for ok, _ in steps.values()))
        self.assertNotIn("clock-domain offsets", steps)
        self.assertNotIn("core rail offset", steps)
        gpu.set_clk_domain_offset.assert_not_called()
        gpu.set_rail_offset_mv.assert_not_called()

    def test_unvalidated_private_layout_is_not_a_failed_supported_control(self):
        gpu = self.gpu()
        gpu.clkdom_layout.return_value = None
        self.assertTrue(all(ok for ok, _ in self.steps(gpu).values()))
        gpu.read_clk_domain_offsets.assert_not_called()
        gpu.read_rail_offset_mv.assert_not_called()

    def test_layout_without_nvvdd_does_not_require_a_rail_offset_read(self):
        gpu = self.gpu()
        gpu.clkdom_layout.return_value = SimpleNamespace(nvvdd_uv=None)
        self.assertTrue(all(ok for ok, _ in self.steps(gpu).values()))
        gpu.read_rail_offset_mv.assert_not_called()

    def test_known_rail_profile_with_missing_or_incomplete_read_is_a_failure(self):
        for rows in (None, {}, {1: {}}):
            with self.subTest(rows=rows):
                gpu = self.gpu()
                gpu._volt_rail_profile.return_value = {"poweron": {0: (0, 0, 0, 0)}}
                gpu.read_volt_rail_limits.return_value = rows
                self.assertFalse(self.steps(gpu)["rail limits"][0])
                gpu.reset_volt_rail_limits.assert_not_called()

    def test_unknown_rail_profile_never_reads_or_dispatches_another_cards_reset(self):
        gpu = self.gpu()
        self.assertNotIn("rail limits", self.steps(gpu))
        gpu.read_volt_rail_limits.assert_not_called()
        gpu.reset_volt_rail_limits.assert_not_called()

    def test_exact_stock_rails_skip_write_and_changed_rails_dispatch_reset(self):
        gpu = self.gpu()
        gpu._volt_rail_profile.return_value = {"poweron": {0: (0, 0, 0, 0)}}
        rows = {0: dict.fromkeys(GPU.VOLT_LIMIT_FIELDS, 0)}
        gpu.read_volt_rail_limits.return_value = rows
        self.assertNotIn("rail limits", self.steps(gpu))
        gpu.reset_volt_rail_limits.assert_not_called()
        rows[0]["vmin"] = 12.5
        self.assertTrue(self.steps(gpu)["rail limits"][0])
        gpu.reset_volt_rail_limits.assert_called_once()

    def test_unreadable_point_lock_is_not_skipped_by_reset(self):
        gpu = self.gpu()
        gpu.read_vf_lock.return_value = None
        gpu.clear_vf_lock.return_value = (False, "V/F lock getter failed")
        self.assertFalse(self.steps(gpu)[GPU.VF_LOCK_STEP][0])
        gpu.clear_vf_lock.assert_called_once()


class PointLockReadbackTests(unittest.TestCase):
    def lock(self, held=True):
        lock = _ClockLock(version=NvAPI.ver(_ClockLock, VF_LOCK_VERSION))
        lock.count = 3
        for index, entry in enumerate(lock.locks[:3]):
            entry.domain = index
            entry.volt_uV = 900000 + index * 100000
        lock.locks[0].lockMode = VF_LOCK_MODE_POINT if held else 0
        lock.locks[1].lockMode = VF_LOCK_MODE_FREQ
        return lock

    def gpu(self, first, back):
        gpu = GPU.__new__(GPU)
        gpu._lock = threading.RLock()
        gpu.nvapi = SimpleNamespace(gpu=object(), ver=NvAPI.ver,
                                    VfLockSet=Mock(return_value=0))
        gpu._vf_lock_available = Mock(return_value=True)
        gpu._vf_lock_read_raw = Mock(side_effect=[first, back])
        return gpu

    def test_failed_initial_read_refuses_every_write(self):
        gpu = self.gpu(None, None)
        ok, message = gpu.clear_vf_lock()
        self.assertFalse(ok)
        self.assertIn("getter failed", message)
        gpu.nvapi.VfLockSet.assert_not_called()

    def test_failed_readback_after_successful_setter_never_reports_release(self):
        gpu = self.gpu(self.lock(), None)
        ok, message = gpu.clear_vf_lock()
        self.assertFalse(ok)
        self.assertIn("verification read failed", message)
        gpu.nvapi.VfLockSet.assert_called_once()

    def test_successful_release_changes_only_point_modes_and_preserves_frequency_lock(self):
        original = self.lock()
        expected = _ClockLock.from_buffer_copy(bytes(original))
        expected.locks[0].lockMode = 0
        gpu = self.gpu(original, _ClockLock.from_buffer_copy(bytes(expected)))
        submitted = []
        gpu.nvapi.VfLockSet.side_effect = lambda handle, ptr: (
            submitted.append(ctypes.string_at(ptr, ctypes.sizeof(_ClockLock))) or 0)
        self.assertTrue(gpu.clear_vf_lock()[0])
        self.assertEqual(submitted, [bytes(expected)])

    def test_lock_reasserted_by_another_tool_is_still_reported(self):
        gpu = self.gpu(self.lock(), self.lock())
        self.assertFalse(gpu.clear_vf_lock()[0])

    def test_confirmed_no_point_lock_is_a_noop_without_touching_frequency_lock(self):
        gpu = self.gpu(self.lock(held=False), None)
        self.assertTrue(gpu.clear_vf_lock()[0])
        gpu.nvapi.VfLockSet.assert_not_called()
        self.assertEqual(gpu._vf_lock_read_raw.call_count, 1)


if __name__ == "__main__":
    unittest.main()
