# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Request-field callbacks and capability refresh through the real UI methods."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

from druta.druta import KnobRange
from tests.test_arch_ui_regressions import FakeUiTest
from tests.test_clock_capability_refresh import clock_gpu
from tests.test_pascal_reset import reset_gpu


class MsvddUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.gpu, self.transport, _ = clock_gpu()
        self.app._msvdd_domain = 1
        self.app.rail = None
        self.app._knob_sync = self.app._xoc_bounds = False
        self.app._carryover_hi = {}
        self.app._slider_ranges["msvdd"] = KnobRange("MSVDD request", -500, 500, -500, 500)
        self.values.update(sl_msvdd=12.5, in_msvdd=12.5)

    def test_fractional_apply_and_readback_stay_bound_to_selected_request_field(self):
        self.app.gpu.msvdd_write_enabled = True
        self.app.apply_msvdd(ctypes.c_float(25.001).value)
        self.assertTrue(self.app.report.call_args.args[0][0])
        self.assertEqual(self.app.gpu.read_rail_offset_mv(1, rail=1), 25.001)
        self.assertEqual(self.app.gpu.read_rail_offset_mv(0, rail=1), 12.5)
        self.assertEqual(self.app.gpu.read_rail_offset_mv(1), 42)
        self.assertEqual(self.values["sl_msvdd"], 25.001)
        self.assertEqual(self.values["in_msvdd"], 25.001)
        self.app.autosave_before.assert_called_once_with("msvdd-request-offset")

    def test_no_opt_in_or_locked_callback_cannot_write(self):
        self.app.apply_msvdd(25)
        self.assertFalse(self.app.report.call_args.args[0][0])
        self.assertEqual(self.transport["writes"], [])
        self.app.autosave_before.reset_mock()
        self.app.gpu.msvdd_write_enabled = True
        for blocked in ("unlock", "_i2c_busy", "_profile_pending", "_closing"):
            with self.subTest(blocked=blocked):
                self.values["unlock"] = blocked != "unlock"
                for name in ("_i2c_busy", "_profile_pending", "_closing"):
                    setattr(self.app, name, name == blocked)
                self.app.apply_msvdd(25)
                self.app.zero_msvdd()
                self.assertEqual(self.transport["writes"], [])
                self.app.autosave_before.assert_not_called()

    def test_zero_remains_available_without_xoc_and_takes_undo_first(self):
        order = []
        self.app.autosave_before.side_effect = lambda label: order.append("snapshot")
        reset = self.app.gpu.reset_msvdd_offset_mv
        self.app.gpu.reset_msvdd_offset_mv = lambda domain: (order.append("reset") or reset(domain))
        self.app.zero_msvdd()
        self.assertEqual(order, ["snapshot", "reset"])
        self.assertTrue(self.app.report.call_args.args[0][0])
        self.assertEqual(self.values["sl_msvdd"], 0)
        self.assertEqual(self.app.gpu.read_rail_offset_mv(0, rail=1), 12.5)

    def test_bad_inputs_and_missing_selected_record_cannot_write(self):
        self.app.gpu.msvdd_write_enabled = True
        for value in (True, float("nan"), float("inf"), "not a number"):
            self.app.apply_msvdd(value)
        self.app._msvdd_domain = None
        self.app.apply_msvdd(25)
        self.app.zero_msvdd()
        self.assertEqual(self.transport["writes"], [])
        self.app.autosave_before.assert_not_called()

    def test_msvdd_row_uses_float_widgets(self):
        self.app._knob_cb = {}
        self.app._ctl_widgets = []
        self.app.bind = Mock()
        self.app.s = lambda value: value
        self.app.slider_row("msvdd", "MSVDD request", -500, 500, 12.5,
                            self.app.apply_msvdd, extra=("Zero", self.app.zero_msvdd))
        self.ui.add_slider_float.assert_called_once()
        self.ui.add_input_float.assert_called_once()
        self.assertEqual(self.ui.add_input_float.call_args.kwargs["default_value"], 12.5)


class CapabilityRefreshUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.gpu, self.transport, _ = clock_gpu()
        self.app.vf_points = []
        self.app.vf_work = {}
        self.app.vf_orig = {}
        self.app._tw_pending = {}
        self.app._clk_lock = {"kind": "legacy_p0", "verified": True}
        self.app.sync_risk_ui = Mock()
        self.app.sync_lock_ui = Mock()
        self.app.relayout = Mock()
        self.app.vf_redraw = Mock()
        self.app.insert_recovered_clock_controls = Mock(return_value=1)
        self.app._slider_ranges = {"msvdd": KnobRange("MSVDD request", -500, 500, -500, 500)}
        self.values.update(xoc_mode=True, sl_msvdd=25.001, in_msvdd=25.001, rfloor=1068.75)
        self.app.build_ui = Mock(side_effect=lambda **_: self.values.update(
            xoc_mode=False, sl_msvdd=0, in_msvdd=0, rfloor=800))

    def test_refresh_preserves_staged_slider_precision_and_hold_and_ramp(self):
        hold = self.app._clk_lock
        generation = getattr(self.app, "_ui_gen", 0)
        gpu_generation = self.app._gpu_gen
        self.app.refresh_capabilities()
        self.app.build_ui.assert_called_once_with(rebuild=True)
        self.assertEqual(self.values["sl_msvdd"], 25.001)
        self.assertEqual(self.values["in_msvdd"], 25.001)
        self.assertEqual(self.values["rfloor"], 1068.75)
        self.assertTrue(self.values["xoc_mode"])
        self.assertIs(self.app._clk_lock, hold)
        self.assertGreater(self.app._ui_gen, generation)
        self.assertEqual(self.app._gpu_gen, gpu_generation)
        self.assertFalse(self.app._rebuilding)
        self.assertEqual(self.transport["writes"], [])

    def test_refresh_discards_queued_callbacks_from_old_controls(self):
        late = Mock()
        self.ui.get_callback_queue.return_value = [[lambda: self.app.refresh_capabilities()], [late]]
        self.app.dispatch_callbacks()
        late.assert_not_called()

    def test_busy_and_pending_curve_or_timing_edits_keep_current_controls(self):
        for attribute, value in (("_i2c_busy", True), ("_tim_busy", True),
                                 ("_profile_pending", object()), ("_closing", True),
                                 ("vf_work", {1: 25}), ("_tw_pending", {"RC": 45})):
            with self.subTest(attribute=attribute):
                old = getattr(self.app, attribute, None)
                setattr(self.app, attribute, value)
                self.app.refresh_capabilities()
                self.app.build_ui.assert_not_called()
                self.assertEqual(self.transport["writes"], [])
                setattr(self.app, attribute, old)

    def test_automatic_recovery_inserts_once_and_keeps_visible_requests(self):
        self.app._clock_capability_recovery = (self.app._gpu_gen, self.app.gpu, 0)
        self.app._clock_capability_recovery_token = 0
        self.app._clock_capability_recovery_logged = False
        self.assertTrue(self.app.consume_clock_capability_recovery())
        self.app.insert_recovered_clock_controls.assert_called_once_with()
        self.app.build_ui.assert_not_called()
        self.app.sync_risk_ui.assert_called_once_with()
        self.app.sync_lock_ui.assert_called_once_with()
        self.assertEqual(self.values["sl_msvdd"], 25.001)
        self.assertEqual(self.values["in_msvdd"], 25.001)
        self.assertEqual(self.values["rfloor"], 1068.75)
        self.assertTrue(self.values["xoc_mode"])
        self.assertIsNone(self.app._clock_capability_recovery)
        self.assertEqual(self.transport["writes"], [])
        self.assertFalse(self.app.consume_clock_capability_recovery())
        self.assertEqual(self.app.insert_recovered_clock_controls.call_count, 1)

    def test_targeted_insertion_uses_fresh_domain_rows_without_manual_refresh(self):
        self.values["clock_offset_table"] = object()
        self.app.gpu.refresh_capabilities = Mock()
        self.app.gpu.static["core_off_range"] = (-1000, 1000, 0)
        self.app.insert_recovered_clock_controls = (
            type(self.app).insert_recovered_clock_controls.__get__(self.app))
        self.app.build_domain_offset_rows = Mock(return_value=2)
        self.assertEqual(self.app.insert_recovered_clock_controls(), 2)
        self.ui.push_container_stack.assert_called_once_with("clock_offset_table")
        self.ui.pop_container_stack.assert_called_once_with()
        self.app.build_domain_offset_rows.assert_called_once_with(
            self.app.gpu.static, 2, -1000, 1000, only_missing=True)
        self.app.gpu.refresh_capabilities.assert_not_called()

    def test_domain_row_builder_uses_validated_current_request_when_telemetry_fails(self):
        knob = self.app.DOMAIN_KNOBS[0]
        self.app.gpu = SimpleNamespace(
            clkdom_layout=lambda: object(),
            read_clk_domain_offsets=lambda: ({knob.ctrl: {"freq_khz": 125000}}, ""),
            read=lambda: (_ for _ in ()).throw(RuntimeError("telemetry")),
            clkdom_controls_for_ui=lambda _rows: [knob.ctrl],
            clkdom_domains=lambda: [knob.ctrl])
        self.app.domain_knob_label = Mock(return_value=("Recovered XBAR", (1, 2, 3), True))
        self.app.slider_row = Mock()
        self.assertEqual(self.app.build_domain_offset_rows(
            {"core_off_range": (-1000, 1000, 0)}, 1, -1000, 1000,
            only_missing=True), 1)
        self.assertEqual(self.app.slider_row.call_args.args[0], knob.key)
        self.assertEqual(self.app.slider_row.call_args.args[4], 125)

    def test_domain_row_builder_skips_unreadable_current_request_and_logs_once(self):
        knob = self.app.DOMAIN_KNOBS[0]
        self.app.gpu = SimpleNamespace(
            clkdom_layout=lambda: object(),
            read_clk_domain_offsets=lambda: (None, "unreadable"))
        self.app.log_once = Mock()
        self.app.slider_row = Mock()
        self.assertEqual(self.app.build_domain_offset_rows({}, 1, -1000, 1000,
                                                           only_missing=True), 0)
        self.app.slider_row.assert_not_called()
        self.app.log_once.assert_called_once()

    def test_automatic_recovery_waits_for_edits_and_discards_stale_result(self):
        self.app._clock_capability_recovery = (self.app._gpu_gen, self.app.gpu, 0)
        self.app._clock_capability_recovery_token = 0
        self.app.vf_work = {1: 25}
        self.app.vf_orig = {1: 0}
        self.assertFalse(self.app.consume_clock_capability_recovery())
        self.app.build_ui.assert_not_called()
        self.assertIsNotNone(self.app._clock_capability_recovery)
        self.app.vf_work = self.app.vf_orig = {}
        self.app._clock_capability_recovery = (self.app._gpu_gen - 1, self.app.gpu, 0)
        self.assertFalse(self.app.consume_clock_capability_recovery())
        self.assertIsNone(self.app._clock_capability_recovery)


class MsvddResetAllTests(FakeUiTest):
    def test_reset_tracks_earlier_domain_when_default_discovery_changes(self):
        gpu = reset_gpu()
        gpu._volt_rail_profile = Mock(return_value=None)
        gpu._msvdd_offset_domains_written = {1}
        gpu._msvdd_offset_domains_seen = {9}
        gpu.rail_offset_capability = Mock(return_value={"available": True, "domain": 3})
        values = {1: 25, 3: 12.5, 9: 6.25}
        gpu.read_rail_offset_mv = Mock(side_effect=lambda domain, rail=0: values[domain])
        gpu.reset_msvdd_offset_mv = Mock(side_effect=lambda domain: (
            values.update({domain: 0}) or (True, "request zeroed")))
        steps = {step.name: tuple(step) for step in gpu.reset_all()}
        self.assertEqual(values, {1: 0, 3: 0, 9: 0})
        self.assertTrue(steps["MSVDD request 1"][0])
        self.assertTrue(steps["MSVDD request 3"][0])
        self.assertTrue(steps["MSVDD request 9"][0])

    def test_unreadable_owned_request_is_a_failed_reset_even_if_discovery_is_empty(self):
        gpu = reset_gpu()
        gpu._volt_rail_profile = Mock(return_value=None)
        gpu._msvdd_offset_domains_written = {1}
        gpu.rail_offset_capability = Mock(return_value={"available": False})
        gpu.read_rail_offset_mv = Mock(return_value=None)
        gpu.reset_msvdd_offset_mv = Mock(return_value=(True, "request zeroed"))
        steps = {step.name: tuple(step) for step in gpu.reset_all()}
        self.assertFalse(steps["MSVDD request 1"][0])
        gpu.reset_msvdd_offset_mv.assert_not_called()
