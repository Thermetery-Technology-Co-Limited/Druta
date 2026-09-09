"""Current-limit controls preserve driver readback and the XOC request envelope."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg

from druta.druta import Druta


def rows(core=300000, other=120000, maximum=5001000):
    return [dict(policy=p, label=label, minimum_ma=1, maximum_ma=maximum,
                 normal_maximum_ma=normal, default_ma=default,
                 limit_ma=current, value_ma=draw)
            for p, label, normal, default, current, draw in (
                (13, "Core current", 500000, 300000, core, 32500),
                (14, "Other rail current", 200000, 120000, other, 4567))]


class CurrentLimitUiTests(unittest.TestCase):
    def setUp(self):
        dpg.create_context()
        self.app = app = Druta.__new__(Druta)
        self.rows = rows()
        app.gpu = SimpleNamespace(
            get_current_limits=Mock(side_effect=lambda: copy.deepcopy(self.rows)),
            set_current_limit_ma=Mock(side_effect=self.write),
            voltage_xoc_enabled=False,
            read=Mock(return_value={}), mem_offset_scale=Mock(return_value=(1, "MHz")),
            read_voltage_boost=Mock(return_value=None))
        app._slider_ranges, app._knob_decimals = {}, {}
        app._carryover_hi, app._current_limits, app._knob_cb = {}, {}, {}
        app._ctl_widgets = []
        app._xoc_bounds = app._knob_sync = False
        app.log, app.bind = Mock(), Mock()
        app.s = lambda n: n
        app.guard = Mock(return_value=True)
        app._tim_lock = threading.Lock()

    def tearDown(self):
        dpg.destroy_context()

    def write(self, policy, milliamps):
        row = next(r for r in self.rows if r["policy"] == policy)
        row["limit_ma"] = milliamps
        return True, "current limit applied"

    def build(self):
        with dpg.window():
            with dpg.table():
                self.app.knob_cols()
                self.app.build_current_limits_rows()

    def maximum(self, key):
        return dpg.get_item_configuration(f"sl_{key}")["max_value"]

    def test_normal_caps_and_xoc_use_api_maximum_without_writes(self):
        self.build()
        self.assertEqual(self.maximum("current13"), 500)
        self.assertEqual(self.maximum("current14"), 200)
        self.app.sync_slider_ranges(True)
        self.assertEqual(self.maximum("current13"), 5001)
        self.assertEqual(self.maximum("current14"), 5001)
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.maximum("current13"), 500)
        self.assertEqual(self.maximum("current14"), 200)
        self.app.gpu.set_current_limit_ma.assert_not_called()

    def test_normal_and_xoc_bounds_never_exceed_a_smaller_api_maximum(self):
        self.rows = rows(core=100000, other=100000, maximum=150000)
        self.build()
        for xoc in (False, True):
            self.app.sync_slider_ranges(xoc)
            self.assertEqual(self.maximum("current13"), 150)
            self.assertEqual(self.maximum("current14"), 150)

    def test_malformed_or_unavailable_policies_do_not_create_controls(self):
        self.rows = [{"policy": 12, "label": "unvalidated"}]
        self.build()
        self.assertFalse(dpg.does_item_exist("sl_current13"))
        self.assertFalse(dpg.does_item_exist("sl_current14"))
        self.app.gpu.get_current_limits.side_effect = RuntimeError("driver unavailable")
        self.assertEqual(self.app.read_current_limit_rows(), [])

    def test_backend_generation_descriptor_controls_policy_label_and_cap(self):
        self.rows = [dict(self.rows[0], policy=12, label="Generation rail",
                          normal_maximum_ma=390000, maximum_ma=390000)]
        self.build()
        self.assertTrue(dpg.does_item_exist("sl_current12"))
        self.assertEqual(self.maximum("current12"), 390)

    def test_only_supported_policy_gets_a_row(self):
        self.rows = self.rows[:1]
        self.build()
        self.assertTrue(dpg.does_item_exist("sl_current13"))
        self.assertFalse(dpg.does_item_exist("sl_current14"))

    def test_live_above_normal_is_preserved_when_xoc_is_disabled(self):
        self.rows = rows(core=600125, other=250000)
        self.build()
        self.assertEqual(self.maximum("current13"), 600.125)
        self.assertEqual(self.maximum("current14"), 250)
        self.app.sync_slider_ranges(True)
        dpg.set_value("sl_current13", 700.0)
        self.app.sync_slider_ranges(False)
        self.assertEqual(dpg.get_value("sl_current13"), 600.125)
        self.assertEqual(dpg.get_value("in_current13"), 600.125)
        self.assertEqual(self.rows[0]["limit_ma"], 600125)
        self.app.gpu.set_current_limit_ma.assert_not_called()

    def test_apply_converts_amperes_to_milliamps_and_reads_back(self):
        self.build()
        self.app.apply_current_limit(13, 305.001)
        self.app.gpu.set_current_limit_ma.assert_called_once_with(13, 305001)
        self.assertAlmostEqual(dpg.get_value("sl_current13"), 305.001, places=3)
        self.assertEqual(dpg.get_value("sl_current14"), 120)

    def test_refused_apply_shows_the_actual_limit(self):
        self.build()
        self.app.gpu.set_current_limit_ma.side_effect = lambda *_: (False, "refused")
        dpg.set_value("sl_current13", 400.0)
        self.app.apply_current_limit(13, 400)
        self.assertEqual(dpg.get_value("sl_current13"), 300)
        self.assertEqual(self.app.log.call_args.args, ("refused", False))

    def test_stock_uses_policy_default_and_keeps_other_policy(self):
        self.rows = rows(core=450000, other=180000)
        self.build()
        self.app.stock_knob("current14")
        self.app.gpu.set_current_limit_ma.assert_called_once_with(14, 120000)
        self.assertEqual(dpg.get_value("sl_current14"), 120)
        self.assertEqual(dpg.get_value("sl_current13"), 450)

    def test_refresh_keeps_staged_inputs_but_restore_sync_uses_fresh_limits(self):
        self.build()
        dpg.set_value("sl_current13", 400.0)
        self.app.refresh_current_limits()
        self.assertEqual(dpg.get_value("sl_current13"), 400)
        self.assertEqual(dpg.get_value("live_current13"), "32.5 A\n≤300 A")
        self.rows[0]["limit_ma"] = 325125
        self.app.sync_sliders_from_gpu()
        self.assertEqual(dpg.get_value("sl_current13"), 325.125)
        self.assertEqual(dpg.get_value("in_current13"), 325.125)

    def test_unreadable_refresh_clears_live_without_inventing_a_limit(self):
        self.rows = rows(core=600000)
        self.build()
        self.app.gpu.get_current_limits.side_effect = RuntimeError("driver reset")
        self.app.refresh_current_limits(sync=True)
        self.assertEqual(dpg.get_value("live_current13"), "unavailable")
        self.assertEqual(dpg.get_value("sl_current13"), 600)
        self.assertEqual(self.maximum("current13"), 600)
        self.app.stock_knob("current13")
        self.app.gpu.set_current_limit_ma.assert_not_called()

    def test_lowered_limit_removes_old_xoc_carryover(self):
        self.rows = rows(core=600000)
        self.build()
        self.app.apply_current_limit(13, 450)
        self.assertNotIn("current13", self.app._carryover_hi)
        self.assertEqual(self.maximum("current13"), 500)
        self.assertEqual(dpg.get_value("sl_current13"), 450)

    def test_finite_inputs_and_write_gate_are_enforced_before_call(self):
        self.build()
        for value in (float("inf"), float("nan"), "invalid"):
            self.app.apply_current_limit(13, value)
        self.app.guard.return_value = False
        self.app.apply_current_limit(13, 400)
        self.app.gpu.set_current_limit_ma.assert_not_called()

    def test_fractional_amperes_survive_both_edit_directions(self):
        self.build()
        dpg.set_value("in_current13", 301.125)
        self.app.knob_typed("current13")
        self.assertEqual(dpg.get_value("sl_current13"), 301.125)
        dpg.set_value("sl_current13", 302.375)
        self.app.knob_dragged("current13")
        self.assertEqual(dpg.get_value("in_current13"), 302.375)
        self.assertEqual(self.app.knob_value("pl", 450.75), 450.75)

    def test_live_ceiling_keeps_all_milliamps_at_the_upper_end(self):
        self.rows = rows(core=5000999)
        self.build()
        self.assertEqual(dpg.get_value("live_current13"), "32.5 A\n≤5000.999 A")
        self.rows[0]["limit_ma"] = 5001000
        self.app.refresh_current_limits()
        self.assertEqual(dpg.get_value("live_current13"), "32.5 A\n≤5001 A")

    def test_card_switch_drops_current_policy_state_and_carryover(self):
        self.rows = rows(core=600000)
        self.build()
        self.app.reset_card_state()
        self.assertEqual(self.app._current_limits, {})
        self.assertEqual(self.app._carryover_hi, {})


if __name__ == "__main__":
    unittest.main()
