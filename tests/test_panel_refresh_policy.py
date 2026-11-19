# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hidden panels retain control evidence without painting or probing hardware."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta.druta import Druta, WARN
from tests.test_real_clock_ui import row
from tests.test_timing_profiles import capture, table


class HiddenClockEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(static={"mem_div": 2, "mem_type": "GDDR6"})
        self.app.step_khz = Mock(return_value=15000)
        self.app._stale = False
        self.ui = MagicMock()
        patcher = patch("druta.druta.dpg", self.ui)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def sample(i=0):
        return {"core": 2115, "util_gpu": 99, "pstate": 0,
                "clk_domains": [row(0, "GPC", 2115000, 2087100 + (i % 2) * 8)]}

    def test_hidden_samples_build_evidence_without_painting_or_changing_painted_colours(self):
        self.app._real_col = {"core": "dim"}
        self.app._real_sub_base = "last painted subtitle"
        for i in range(10):
            self.app.refresh_real_clocks(self.sample(i), now=(i + 1) * 1.05, paint=False)

        self.assertEqual(self.app._real.last("core").samples, 10)
        self.assertEqual(self.app._real_col, {"core": "dim"})
        self.assertEqual(self.app._real_sub_base, "last painted subtitle")
        self.ui.set_value.assert_not_called()
        self.ui.configure_item.assert_not_called()

        sample = self.app._real_last_d
        self.app.refresh_real_clocks(sample, now=10.5)
        self.assertEqual(self.app._real.last("core").samples, 10)
        self.ui.set_value.assert_any_call("r_core", "measured 2087  Δ -27.9")
        self.ui.configure_item.assert_any_call("r_core", color=WARN)

    def test_repeated_hidden_snapshot_is_one_reading_and_needs_no_painted_cache(self):
        sample = self.sample()
        for i in range(20):
            self.app.refresh_real_clocks(sample, now=i, paint=False)
        self.assertEqual(self.app._real.last("core").samples, 1)
        self.assertFalse(hasattr(self.app, "_real_col"))
        self.ui.set_value.assert_not_called()
        self.ui.configure_item.assert_not_called()

    def test_hidden_stale_and_transient_failed_reads_do_not_pollute_evidence(self):
        self.app.refresh_real_clocks(self.sample(), now=1, paint=False)
        self.app._stale = True
        self.app.refresh_real_clocks(self.sample(1), now=2, paint=False)
        self.app._stale = False
        self.app.refresh_real_clocks({"core": 2115, "clk_domains": None}, now=3,
                                     paint=False)
        self.assertEqual(self.app._real.last("core").samples, 1)
        self.ui.set_value.assert_not_called()
        self.ui.configure_item.assert_not_called()


class HiddenTimingCaptureTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(static={"mem_div": 2},
                                       slot=lambda: "0000:01:00.0")
        self.app._gpu_gen = self.app._ui_gen = 0
        self.app._tim_lock = threading.Lock()
        self.app._tim = capture()
        self.app._tim_avail = SimpleNamespace(ok=True, exe="test-nvtune.exe")
        self.app._tim_ft = None
        self.app._tim_new = True
        self.app._tim_busy = False
        self.app._tim_caps = {7001: self.app._tim}
        self.app._tim_note = self.app._tim_what = ""
        self.app._tw_pending = {"RC": 45, "FAW": 25}
        self.app.timings_p0_watch = Mock()
        self.app.draw_timings = Mock()
        self.ui = MagicMock()
        self.ui.does_item_exist.return_value = True
        for name, value in (("druta.druta.dpg", self.ui),
                            ("druta.druta.timings.field_table", Mock(return_value=table()))):
            patcher = patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
            if name.endswith("field_table"):
                self.field_table = value

    def test_hidden_capture_reconciles_confirmed_edit_and_preserves_pending_paint(self):
        self.app.refresh_timings({"mem": 7001, "pstate": 0}, paint=False)
        self.assertEqual(self.app._tw_pending, {"FAW": 25})
        self.assertTrue(self.app._tim_new)
        self.app.timings_p0_watch.assert_called_once_with(7001, 0, False)
        self.app.draw_timings.assert_not_called()
        self.ui.set_value.assert_not_called()
        self.ui.configure_item.assert_not_called()

    def test_pending_capture_can_reconcile_before_queued_apply_without_painting(self):
        self.app._tw_pending = {"RC": 45}
        self.app.guard = Mock(return_value=True)
        with patch("druta.druta.timingwrite.apply") as commit:
            self.app.refresh_timing_capture()
            self.app.tw_apply()
        self.assertEqual(self.app._tw_pending, {})
        self.assertTrue(self.app._tim_new)
        commit.assert_not_called()
        self.ui.set_value.assert_not_called()
        self.ui.configure_item.assert_not_called()

    def test_first_visible_tick_draws_pending_capture_once(self):
        self.app.refresh_timings({}, paint=False)
        self.app.refresh_timings({})
        self.app.draw_timings.assert_called_once_with(
            self.app._tim, self.app._tim_avail, self.app._tim_caps, "")
        self.assertFalse(self.app._tim_new)
        self.app.refresh_timings({})
        self.assertEqual(self.app.draw_timings.call_count, 1)
        self.assertEqual(self.app.timings_p0_watch.call_count, 3)
        self.field_table.assert_called_once_with(exe="test-nvtune.exe")

    def test_field_table_is_looked_up_once_per_capture_and_observes_cache_refresh(self):
        self.app.refresh_timings({}, paint=False)
        for _ in range(5):
            self.app.refresh_timings({}, paint=False)
        # A newly staged edit is not retired against a previously processed capture.
        self.app._tw_pending["RC"] = 45
        self.app.refresh_timing_capture()
        self.assertIn("RC", self.app._tw_pending)
        self.field_table.assert_called_once_with(exe="test-nvtune.exe")
        refreshed_table = table()
        self.field_table.return_value = refreshed_table
        self.app._tim = capture()
        self.app.refresh_timing_capture()
        self.assertNotIn("RC", self.app._tw_pending)
        self.assertEqual(self.field_table.call_count, 2)
        self.field_table.assert_called_with(exe="test-nvtune.exe")
        self.assertIs(self.app._tim_ft, refreshed_table)

    def test_helper_change_or_failed_lookup_does_not_reuse_previous_field_table(self):
        self.app.refresh_timing_capture()
        self.app._tim_avail.exe = "another-nvtune.exe"
        self.app._tim = capture()
        self.app._tw_pending["RC"] = 45
        self.field_table.side_effect = OSError("helper unavailable")
        self.app.refresh_timing_capture()
        self.assertIsNone(self.app._tim_ft)
        self.assertIn("RC", self.app._tw_pending)
        for _ in range(5):
            self.app.refresh_timing_capture()
        self.assertEqual(self.field_table.call_count, 2)
        self.field_table.side_effect = None
        self.app._tim = capture()
        self.app.refresh_timing_capture()
        self.assertEqual(self.field_table.call_count, 3)
        self.assertNotIn("RC", self.app._tw_pending)

    def test_failed_capture_does_not_retire_edits_or_consume_pending_paint(self):
        self.app._tim.ok = False
        self.app.refresh_timings({}, paint=False)
        self.assertEqual(self.app._tw_pending, {"RC": 45, "FAW": 25})
        self.assertTrue(self.app._tim_new)
        self.field_table.assert_not_called()
        self.app.draw_timings.assert_not_called()

    def test_hidden_armed_p0_watcher_captures_once_per_entry(self):
        del self.app.timings_p0_watch
        self.app.gpu.static["mem_clocks"] = [405, 6801, 7001]
        self.app._tim_p0_in = False
        self.app.auto_wanted = Mock(return_value=True)
        self.app.log = Mock()
        self.app.timings_capture = Mock()
        self.ui.get_value.side_effect = lambda tag: tag == "tim_auto"
        sample = {"mem": 7001, "pstate": 0}
        self.app.refresh_timings(sample, paint=False)
        self.app.refresh_timings(sample, paint=False)
        self.app.timings_capture.assert_called_once_with()
        self.app.refresh_timings({"mem": 405, "pstate": 8}, paint=False)
        self.app.refresh_timings(sample, paint=False)
        self.assertEqual(self.app.timings_capture.call_count, 2)
        self.assertTrue(self.app._tim_new)
        self.ui.set_value.assert_not_called()
        self.ui.configure_item.assert_not_called()


class PeriodicControlRefreshTests(unittest.TestCase):
    def test_periodic_control_skips_advanced_policy_probe_but_explicit_refresh_keeps_it(self):
        app = Druta.__new__(Druta)
        app.gpu = SimpleNamespace(static={}, clkdom_is_blackwell=lambda: False,
                                  clkdom_pairing=lambda rows: {})
        app._slider_ranges = {}
        app.power_request_carryover = Mock(return_value=False)
        app.refresh_power_request_status = Mock()
        app.native_power_limit_for_ui = Mock(return_value=None)
        app.mem_fmt = Mock(return_value=("--", "MHz"))
        app.refresh_rail_live = Mock()
        app.refresh_current_limits = Mock()
        app.refresh_power_policies = Mock()
        app.i2c_rail_text = Mock(return_value=None)
        app.sync_knob_boxes = Mock()
        with patch("druta.druta.dpg") as ui:
            ui.does_item_exist.return_value = False
            app.refresh_control({}, periodic=True)
            app.refresh_power_policies.assert_not_called()
            app.refresh_control({})
        app.refresh_power_policies.assert_called_once_with()
        self.assertEqual(app.refresh_rail_live.call_count, 2)
        self.assertEqual(app.refresh_current_limits.call_count, 2)
        self.assertEqual(app.sync_knob_boxes.call_count, 2)


if __name__ == "__main__":
    unittest.main()
