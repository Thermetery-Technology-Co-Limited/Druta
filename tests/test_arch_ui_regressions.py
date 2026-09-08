# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Architecture UI and callback regressions with fake DPG and driver transports."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from druta import druta, timings, timingwrite
from druta.druta import Druta
from druta.nvbackend import GPU, ResetStep
from tests.test_offset_precision import modern_gpu
from tests.test_timingwrite_results import COMMIT, DRY_RUN


class FakeUiTest(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.log = Mock()
        self.app.autosave_before = Mock()
        self.app.report = Mock()
        self.app._gpu_gen = 0
        self.app._slider_ranges = {}
        self.values = {"unlock": True}
        self.ui = MagicMock()
        self.ui.does_item_exist.side_effect = lambda tag: tag in self.values
        self.ui.get_value.side_effect = self.values.get
        self.ui.set_value.side_effect = self.values.__setitem__
        self.ui.is_item_active.return_value = False
        self.ui.is_item_focused.return_value = False
        self.ui.run_callbacks = druta.dpg.run_callbacks  # Pure Python dispatcher.
        self.ui.is_dearpygui_running.return_value = True
        patcher = patch.object(druta, "dpg", self.ui)
        patcher.start()
        self.addCleanup(patcher.stop)


class CoreGridUiTests(FakeUiTest):
    def test_pascal_requests_and_reapply_use_the_same_exact_hardware_bin(self):
        for requested, expected in ((130, 127), (127, 127), (26, 26), (13, 13),
                                    (12, 0), (-13, -25), (-126, -126),
                                    (-200, -189), (300, 292), (0, 0)):
            with self.subTest(requested=requested):
                gpu, driver = modern_gpu()
                gpu.clock_step_khz.return_value = 12657
                gpu.clkdom_is_blackwell = lambda: False
                gpu.static["core_off_range"] = (-200, 300)
                self.app.gpu = gpu
                self.values.update(sl_core=requested, in_core=requested)
                self.app.apply_core(requested)
                self.assertTrue(self.app.report.call_args.args[0][0])
                self.assertEqual(self.values["sl_core"], expected)
                self.assertEqual(self.values["in_core"], expected)
                self.assertEqual(driver.current, expected)
                self.app.apply_core(self.values["sl_core"])
                self.assertEqual(driver.writes, [(0, 0, expected), (0, 0, expected)])

    def test_integral_clock_grids_keep_existing_floor_and_driver_minimum(self):
        for step, requested, expected in ((13000, 130, 130), (13000, 26, 26),
                                         (15000, 130, 120), (15000, -200, -195)):
            with self.subTest(step=step, requested=requested):
                gpu, driver = modern_gpu()
                gpu.clock_step_khz.return_value = step
                gpu.clkdom_is_blackwell = lambda: False
                gpu.static["core_off_range"] = (-200, 300)
                self.app.gpu = gpu
                self.values.update(sl_core=requested, in_core=requested)
                self.app.apply_core(requested)
                self.assertEqual(driver.current, expected)
                self.assertEqual(self.values["in_core"], expected)

    def test_locked_apply_does_not_touch_transport_or_widgets(self):
        self.app.gpu, driver = modern_gpu()
        self.values.update(unlock=False, sl_core=130, in_core=130)
        self.app.apply_core(130)
        self.assertEqual(driver.writes, [])
        self.assertEqual(self.values["sl_core"], 130)
        self.app.autosave_before.assert_not_called()


class CallbackBatchTests(FakeUiTest):
    def test_switch_discards_outgoing_callbacks_before_they_reach_new_card(self):
        def card():
            return SimpleNamespace(set_clock_offset=Mock(return_value=(True, "set")),
                                   mem_offset_scale=lambda: (4, "MHz true"))

        old, new = card(), card()
        self.app.gpu = old

        def switch():
            self.app.gpu = new
            self.app._gpu_gen += 1

        late = lambda: self.app.apply_mem(12.5)
        self.ui.get_callback_queue.return_value = [[switch], [late]]
        self.app.dispatch_callbacks()
        self.assertIs(self.app.gpu, new)
        old.set_clock_offset.assert_not_called()
        new.set_clock_offset.assert_not_called()
        # A fresh callback obtained on the next frame belongs to the new card.
        self.ui.get_callback_queue.return_value = [[late]]
        self.app.dispatch_callbacks()
        new.set_clock_offset.assert_called_once_with(2, 12.5)

    def test_unchanged_generation_runs_every_callback_in_order(self):
        calls = []
        self.ui.get_callback_queue.return_value = [
            [lambda: calls.append("first")], [lambda: calls.append("second")]]
        self.app.dispatch_callbacks()
        self.assertEqual(calls, ["first", "second"])

    def test_exit_callback_discards_later_writes_in_same_batch(self):
        late = Mock()

        def close():
            self.ui.is_dearpygui_running.return_value = False

        self.ui.get_callback_queue.return_value = [[close], [late]]
        self.app.dispatch_callbacks()
        late.assert_not_called()

    def test_empty_queue_needs_no_callbacks(self):
        self.ui.get_callback_queue.return_value = None
        self.app.dispatch_callbacks()


class ResetReadbackUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app._reset_armed = True
        self.app._clk_lock = None
        self.app.rail = None
        self.app.refresh_volt_limits = Mock()
        self.app.vf_read = Mock()
        self.app.gpu = SimpleNamespace(
            reset_all=Mock(return_value=[]), read=Mock(return_value={}),
            mem_offset_scale=lambda: (8, "MHz true"),
            read_voltage_boost=Mock(return_value=None),
            read_rail_offset_mv=Mock(return_value=None),
            read_clk_domain_offsets=Mock(return_value=(None, "read failed")),
            read_fan_control_state=Mock(return_value=None))
        self.knob = self.app.DOMAIN_KNOBS[0]
        for key, value in (("core", 127), ("mem", 12.5), ("pl", 200),
                           ("volt", 25), ("rail", 12), ("fan", 70),
                           (self.knob.key, 25)):
            self.values["sl_" + key] = self.values["in_" + key] = value
            self.app._slider_ranges[key] = None

    def test_failed_reset_and_missing_readers_preserve_requests_and_hold(self):
        before = dict(self.values)
        self.app._clk_lock = {"kind": Druta.LOCK_VF, "idx": 10}
        self.app.gpu.reset_all.return_value = [
            ResetStep("clock-domain offsets", (False, "domain read failed")),
            ResetStep("core rail offset", (False, "rail read failed")),
            ResetStep(GPU.VF_LOCK_STEP, (False, "lock read failed"))]
        self.app.reset_all()
        self.assertEqual(self.values, before)
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_VF)
        self.assertIn("reset incomplete", self.app.log.call_args.args[0])

    def test_missing_release_verdict_never_clears_held_banner_or_claims_stock(self):
        self.app._clk_lock = {"kind": Druta.LOCK_VF, "idx": 10}
        self.app.reset_all()
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_VF)
        self.assertIn("reset incomplete", self.app.log.call_args.args[0])

    def test_fresh_readbacks_replace_requests_and_confirmed_release_clears_hold(self):
        self.app._clk_lock = {"kind": Druta.LOCK_VF, "idx": 10}
        self.app.gpu.reset_all.return_value = [ResetStep(GPU.VF_LOCK_STEP, (True, "released"))]
        self.app.gpu.read.return_value = {"core_off": 0, "mem_off": 0,
                                          "pl_requested_mw": 180000, "pl_now_mw": 200000}
        self.app.gpu.read_voltage_boost.return_value = 0
        self.app.gpu.read_rail_offset_mv.return_value = 0
        self.app.gpu.read_clk_domain_offsets.return_value = ({self.knob.ctrl: {"freq_khz": 0}}, "")
        self.app.gpu.read_fan_control_state.return_value = {"fans": [{"manual": True, "level": 65}]}
        self.app.reset_all()
        self.assertIsNone(self.app._clk_lock)
        for key, expected in (("core", 0), ("mem", 0), ("pl", 180), ("volt", 0),
                              ("rail", 0), (self.knob.key, 0), ("fan", 65)):
            self.assertEqual(self.values["sl_" + key], expected, key)
            self.assertEqual(self.values["in_" + key], expected, key)


class TimingWriteUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app._tw_pending = {"RC": 46}
        self.app._tim = timings.Snapshot(
            ok=True, codename="GM107", mem_states=[405, 900], mem_before=900,
            mem_after=900, pstate_before=0, pstate_after=0)
        self.app.timings_capture = Mock()
        self.app.gpu = SimpleNamespace(
            read=Mock(return_value={"mem": 900, "pstate": 0, "mem_off": 0}),
            slot=lambda: "0000:02:00.0")
        self.values["tw_force"] = False
        self.backup = Mock(return_value=("mock-backup.json", False, None))
        # Existing field validation has its own tests. Exercise the UI's write
        # gate and the real plan/commit flow with a fake external transport.
        for name, replacement in (("check", Mock(return_value=[])),
                                  ("ensure_backup", self.backup)):
            patcher = patch.object(timingwrite, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_locked_busy_pending_or_closing_never_reaches_external_helper(self):
        for blocked in ("unlock", "_i2c_busy", "_profile_pending", "_closing"):
            with self.subTest(blocked=blocked):
                self.values["unlock"] = blocked != "unlock"
                for name in ("_i2c_busy", "_profile_pending", "_closing"):
                    setattr(self.app, name, name == blocked)
                with patch.object(timingwrite, "_run") as run:
                    self.app.tw_apply()
                run.assert_not_called()
                self.backup.assert_not_called()

    def test_cached_top_band_cannot_authorize_idle_unknown_or_failed_live_read(self):
        cases = ({"mem": 405, "pstate": 8, "mem_off": 0},
                 {"mem": 900, "pstate": 8, "mem_off": 0},
                 {"mem": 405, "pstate": 0, "mem_off": 0},
                 {"mem": None, "pstate": 0, "mem_off": 0},
                 {"mem": 900, "pstate": None, "mem_off": 0},
                 {"mem": 900, "pstate": 0, "mem_off": None},
                 RuntimeError("read failed"))
        for live in cases:
            with self.subTest(live=live):
                self.app.gpu.read.side_effect = [live]
                with patch.object(timingwrite, "_run") as run:
                    self.app.tw_apply()
                run.assert_not_called()
                self.backup.assert_not_called()
                self.assertIn("not applied", self.values["tw_result"])

    def test_known_pascal_p2_and_negative_offset_top_band_remain_writable(self):
        for codename, states, live in (
                ("GM107", [405, 900], {"mem": 875, "pstate": 0, "mem_off": -50}),
                ("GP102", [405, 810, 5505, 5705],
                 {"mem": 5505, "pstate": 2, "mem_off": 0})):
            with self.subTest(codename=codename):
                self.app._tim.codename, self.app._tim.mem_states = codename, states
                self.app.gpu.read.side_effect = [live, live]
                with patch.object(timingwrite, "_run", side_effect=[
                        ("RC=45", 0), (DRY_RUN, 0), (COMMIT, 0), ("RC=46", 0)]) as run:
                    self.app.tw_apply()
                self.assertTrue(any("--commit" in c.args[0] for c in run.call_args_list))
                self.assertIn("written and verified", self.values["tw_result"])

    def test_clock_drop_after_dryrun_refuses_before_commit(self):
        self.app.gpu.read.side_effect = [
            {"mem": 900, "pstate": 0, "mem_off": 0},
            {"mem": 405, "pstate": 8, "mem_off": 0}]
        with patch.object(timingwrite, "_run", side_effect=[("RC=45", 0), (DRY_RUN, 0)]) as run:
            self.app.tw_apply()
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any("--commit" in c.args[0] for c in run.call_args_list))
        self.assertIn("refused before hardware write", self.values["tw_result"])
        self.assertIn("no timing write", self.values["tw_result"])

    def test_permission_revoked_during_dryrun_refuses_before_commit(self):
        def run(args, *_unused, **_kwargs):
            if args[0] == "get":
                return "RC=45", 0
            self.values["unlock"] = False
            return DRY_RUN, 0

        with patch.object(timingwrite, "_run", side_effect=run) as external:
            self.app.tw_apply()
        self.assertEqual(external.call_count, 2)
        self.assertFalse(any("--commit" in c.args[0] for c in external.call_args_list))
        self.assertIn("controls are locked", self.values["tw_result"])

    def test_restore_stock_remains_an_unlock_exception(self):
        self.values["unlock"] = False
        self.app.tw_plan = Mock()
        with patch.object(timingwrite, "existing_backup_path", return_value="mock-backup.json"), \
                patch.object(timingwrite, "backup_describes", return_value=("GM107", "mock")), \
                patch.object(timingwrite, "restore", return_value=(True, "restored")) as restore:
            self.app.tw_restore()
        restore.assert_called_once_with("mock-backup.json", "0000:02:00.0")


if __name__ == "__main__":
    unittest.main()
