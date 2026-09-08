# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Blackwell UI requests stay on the grid used by the dispatched setter."""
import ctypes
import threading
from unittest.mock import Mock, patch

from druta import timings, timingwrite
from druta.druta import KnobRange
from tests.test_arch_ui_regressions import FakeUiTest
from tests.test_offset_precision import modern_gpu
from tests.test_nvml_clock_lists import ClockDriver
from tests.test_power_limit_readback import PowerDriver


class BlackwellClockUiTests(FakeUiTest):
    def test_core_uses_graphics_grid_and_reapply_preserves_the_encoded_request(self):
        for grid, request, expected in (
                (7500, 8, 8), (7500, 13, 8), (7500, 130, 128),
                (7500, -13, -15), (7500, -200, -195),
                (12000, 8, 0), (12000, 13, 12), (12000, 130, 120),
                (12000, -13, -24), (12000, -200, -192),
                (15000, 8, 0), (15000, 13, 0), (15000, 130, 120),
                (15000, -13, -15), (15000, -200, -195)):
            with self.subTest(grid=grid, request=request):
                self.app.gpu, driver = modern_gpu()
                self.app.gpu.static.update(name="NVIDIA GeForce RTX 5080",
                                           core_off_range=(-200, 300))
                self.app.gpu.clock_step_khz.return_value = grid
                self.assertTrue(self.app.gpu.clkdom_is_blackwell())
                self.assertEqual(self.app.gpu.clkdom_step_mhz(), 1)
                self.values.update(sl_core=request, in_core=request)
                self.app.apply_core(request)
                self.assertEqual(driver.current, expected)
                self.assertEqual(self.values["sl_core"], expected)
                self.assertEqual(self.values["in_core"], expected)
                self.app.apply_core(self.values["sl_core"])
                self.assertEqual(driver.current, expected)
                self.assertTrue(self.app.report.call_args.args[0][0])

    def test_private_domain_keeps_one_mhz_requests_independently_of_graphics_grid(self):
        self.app.gpu, _ = modern_gpu()
        self.app.gpu.static["name"] = "NVIDIA GeForce RTX 5080"
        self.app.gpu.clock_step_khz.return_value = 12000
        self.app.gpu.set_clk_domain_offset = Mock(return_value=(True, "stored"))
        for request in (8, 13, -13, 130):
            with self.subTest(request=request):
                self.values["sl_xbar"] = request
                self.app.apply_domain_offset(1, "xbar", request)
                self.app.gpu.set_clk_domain_offset.assert_called_with(1, request)
                self.assertEqual(self.values["sl_xbar"], request)

    def test_measured_memory_inputs_truncate_to_exact_even_driver_units(self):
        self.app.gpu, driver = modern_gpu()
        self.app.gpu.static.update(name="NVIDIA GeForce RTX 5080", mem_div=None,
                                   driver="580.97", vbios="98.03.3b.c0.6f")
        self.app.gpu.nvml.selected = {"devid": 0x2C02, "subsys": 2313031747}
        self.app._knob_sync = self.app._xoc_bounds = False
        self.app._carryover_hi = {}
        self.app._slider_ranges = {"mem": KnobRange("Memory", -1000, 3000, -1000, 3000)}
        self.assertEqual(self.app.gpu.mem_offset_scale(), (2, "MHz eff"))
        for value in (0.5, -0.5, 1.5, -1.5, 2.5, -2.5, 12.5, -12.5, 12, -12):
            with self.subTest(value=value):
                expected = int(value)
                self.values.update(in_mem=value, sl_mem=0)
                self.app.knob_typed("mem")
                self.assertEqual(self.values["in_mem"], expected)
                self.assertEqual(self.values["sl_mem"], expected)
                self.app.apply_mem(value)
                self.assertTrue(self.app.report.call_args.args[0][0])
                self.assertEqual(driver.current, expected * 2)
                self.app.apply_mem(self.values["sl_mem"])
                self.assertEqual(driver.current, expected * 2)

    def test_unmeasured_identity_keeps_existing_fractional_memory_grid(self):
        self.app.gpu, driver = modern_gpu()
        self.app.gpu.static.update(name="NVIDIA GeForce RTX 5080", mem_div=None,
                                   driver="580.97", vbios="different")
        self.app.gpu.nvml.selected = {"devid": 0x2C02, "subsys": 2313031747}
        self.app.apply_mem(12.5)
        self.assertEqual(driver.current, 25)


class ClockLockUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.driver = ClockDriver({405: [180, 885],
                                   15001: [180 + 15 * index // 2 for index in range(389)]})
        self.app.gpu = self.driver.gpu()
        self.app.gpu.static.update(mem_clocks=[405, 15001], gfx_min=180, gfx_max=3090)
        self.app.gpu.nvml.dll.nvmlDeviceResetGpuLockedClocks = Mock(return_value=0)
        self.app._clk_lock = None
        self.app._lock = threading.RLock()
        self.app._snap = None
        self.values.update(lock_min=1501, lock_max=1501, hold_info="", vf_holdline=[[]])

    def test_snapped_lock_updates_both_inputs_and_banner_to_dispatched_request(self):
        self.app.apply_lock()
        self.assertEqual(self.driver.writes, [(1500, 1500)])
        self.assertEqual((self.values["lock_min"], self.values["lock_max"]), (1500, 1500))
        self.assertIn("[1500..1500]", self.values["hold_info"])
        self.assertEqual((self.app._clk_lock["lo"], self.app._clk_lock["hi"]), (1500, 1500))
        self.app.release_lock()
        self.app.gpu.nvml.dll.nvmlDeviceResetGpuLockedClocks.assert_called_once()
        self.assertIsNone(self.app._clk_lock)

    def test_lock_max_uses_current_dispatched_clock_if_table_changes(self):
        self.driver.rows[15001].pop()
        self.app.lock_max()
        self.assertEqual(self.driver.writes, [(3082, 3082)])
        self.assertEqual((self.values["lock_min"], self.values["lock_max"]), (3082, 3082))
        self.assertIn("[3082..3082]", self.values["hold_info"])

    def test_unavailable_command_metadata_keeps_owned_hold_for_exit_without_invented_range(self):
        for value in (None, RuntimeError("metadata unavailable")):
            with self.subTest(value=value):
                self.app.gpu.last_clock_lock_request = Mock(side_effect=value) if isinstance(
                    value, Exception) else Mock(return_value=value)
                self.app.apply_lock()
                self.assertFalse(self.app._clk_lock["verified"])
                self.assertNotIn("lo", self.app._clk_lock)
                self.assertIn("submitted range is unavailable", self.values["hold_info"])
                self.assertFalse(self.app.shutdown_is_clean())
                with patch("builtins.print"):
                    self.app.release_on_exit()
                self.assertIsNone(self.app._clk_lock)
                self.assertTrue(self.app.shutdown_is_clean())

    def test_failed_new_lock_retains_previous_ownership_and_failed_ui_report_cannot_drop_it(self):
        self.app.report.side_effect = RuntimeError("log unavailable")
        with self.assertRaisesRegex(RuntimeError, "log unavailable"):
            self.app.apply_lock()
        self.assertEqual(self.app._clk_lock["lo"], 1500)
        previous = dict(self.app._clk_lock)
        self.app.report.side_effect = None
        self.app.gpu.nvml.dll.nvmlDeviceSetGpuLockedClocks = Mock(return_value=4)
        self.values.update(lock_min=1515, lock_max=1515)
        self.app.apply_lock()
        self.assertFalse(self.app.report.call_args.args[0][0])
        self.assertEqual(self.app._clk_lock, previous)
        self.app.release_lock()
        self.assertIsNone(self.app._clk_lock)


class PowerRequestUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.driver = PowerDriver()
        self.app.gpu = self.driver.gpu()
        self.app.gpu.mem_offset_scale = lambda: (2, "MHz eff")
        self.app.gpu.read_voltage_boost = lambda: None

        def read():
            result = {}
            self.app.gpu._read_power(result)
            return result

        self.app.gpu.read = read
        self.app._knob_sync = self.app._xoc_bounds = False
        self.app._carryover_hi = {}
        self.app._slider_ranges = {"pl": KnobRange("Power", 100.125, 400, 100.125, 400)}
        self.values.update(sl_pl=360, in_pl=360)

    def test_fractional_power_typing_apply_readback_and_reapply_use_requested_mw(self):
        for value in (349.2, 350.001):
            with self.subTest(value=value):
                self.values["in_pl"] = ctypes.c_float(value).value
                self.app.knob_typed("pl")
                self.assertEqual(self.values["sl_pl"], value)
                self.app.apply_pl(self.values["sl_pl"])
                self.assertEqual(self.driver.requested, round(value * 1000))
                self.assertTrue(self.app.report.call_args.args[0][0])
                self.app.sync_sliders_from_gpu()
                self.assertEqual(self.values["sl_pl"], value)
                self.assertEqual(self.values["in_pl"], value)
                self.app.sync_slider_ranges(False)
                self.assertEqual(self.values["in_pl"], value)
                self.app.apply_pl(self.values["sl_pl"])
                self.assertEqual(self.driver.requested, round(value * 1000))

    def test_lagging_enforced_limit_never_replaces_the_configured_request(self):
        self.driver.requested, self.driver.enforced = 355000, 349200
        self.app.sync_sliders_from_gpu()
        self.assertEqual(self.values["sl_pl"], 355)
        self.driver.read_status = 4
        self.app.sync_sliders_from_gpu()
        self.assertEqual(self.values["sl_pl"], 355)
        self.assertEqual(self.values["in_pl"], 355)
        self.app.apply_pl(350)
        self.assertEqual(self.driver.writes, [])

    def test_stock_power_keeps_fractional_default(self):
        self.app.gpu.static["pl_def_mw"] = 349200
        self.app._knob_cb = {"pl": self.app.apply_pl}
        self.app.stock_knob("pl")
        self.assertEqual(self.driver.requested, 349200)
        self.assertEqual(self.values["in_pl"], 349.2)


class UnknownTimingUiTests(FakeUiTest):
    def test_unknown_layout_refuses_apply_even_with_good_p0_clocks(self):
        self.app._tw_pending = {"RC": 46}
        self.app._tim = timings.Snapshot(ok=True, codename="UNKNOWN_1B3",
                                          mem_states=[405, 15001], mem_before=15001,
                                          mem_after=15001, pstate_before=0, pstate_after=0)
        self.app.gpu = Mock()
        self.app.gpu.read.return_value = {"mem": 15001, "pstate": 0, "mem_off": 0}
        with patch.object(timingwrite, "check", return_value=[]), \
                patch.object(timingwrite, "ensure_backup") as backup, \
                patch.object(timingwrite, "apply") as apply:
            self.app.tw_apply()
        backup.assert_not_called()
        apply.assert_not_called()
        self.assertIn("layout is unknown", self.values["tw_result"])

    def test_unknown_layout_refuses_stock_restore_before_external_write(self):
        self.app.gpu = Mock()
        with patch.object(timingwrite, "existing_backup_path", return_value="raw-backup.json"), \
                patch.object(timingwrite, "backup_describes", return_value=("UNKNOWN_1B3", "raw")), \
                patch.object(timingwrite, "restore") as restore:
            self.app.tw_restore()
        restore.assert_not_called()
        self.assertIn("restore refused", self.values["tw_result"])
