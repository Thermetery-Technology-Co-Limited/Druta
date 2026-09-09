"""Board-independent references, partial rails, exact restoration and refresh."""
import copy
import math
import unittest
from unittest.mock import Mock

from druta.nvbackend import GPU
from tests.test_volt_rails import fake_gpu
from tests.test_arch_ui_regressions import FakeUiTest


class RuntimeRailReferenceTests(unittest.TestCase):
    def test_different_board_bases_and_nonzero_initial_values_are_not_stock(self):
        gpu = fake_gpu("blackwell", subsys=123, vbios="different")
        gpu.nvapi.bases = (987625, 1087125, 1173375, 612500)
        gpu.nvapi.control = {0: [23000, -12000, 9100, 47000],
                             1: [-77000, 5500, -19000, 31500]}
        gpu.nvapi.sync_live()
        initial = copy.deepcopy(gpu.nvapi.control)
        raw = gpu.read_volt_rail_limits()
        self.assertEqual(raw[0]["_base_mv"]["reliability"], 987.625)
        self.assertEqual(gpu.stock_limit_mv(1, "reliability"), 910.625)
        gpu.volt_limits_write_enabled = True
        self.assertTrue(gpu.set_volt_rail_limits(0, reliability=1050.125)[0])
        self.assertEqual(gpu.nvapi.control[0][0], 62500)
        ok, message = gpu.reset_volt_rail_limits()
        self.assertTrue(ok, message)
        self.assertEqual(gpu.nvapi.control, initial)
        self.assertIn("not factory", message)

    def test_initial_floor_below_software_default_survives_reapply_and_raw_restore(self):
        gpu = fake_gpu()
        gpu.nvapi.bases = (1000000, 1050000, 1150000, 600000)
        gpu.nvapi.sync_live()
        self.assertEqual(gpu.stock_limit_mv(0, "vmin"), 600)
        gpu.volt_limits_write_enabled = True
        self.assertTrue(gpu.set_volt_rail_limits(0, vmin=600)[0])
        self.assertTrue(gpu.set_volt_rail_limits(0, vmin=700)[0])
        self.assertTrue(gpu.set_volt_rail_limits_raw(0, vmin=0)[0])
        self.assertEqual(gpu.nvapi.control[0][3], 0)
        self.assertFalse(gpu.set_volt_rail_limits(0, vmin=599)[0])

    def test_unrelated_bad_rail_does_not_hide_or_enter_selected_write(self):
        gpu = fake_gpu("blackwell")
        gpu.nvapi.live[1][0] = 42
        self.assertTrue(gpu.volt_rail_limits_supported(0))
        self.assertFalse(gpu.volt_rail_limits_supported(1))
        gpu.volt_limits_write_enabled = True
        self.assertTrue(gpu.set_volt_rail_limits(0, reliability=1000)[0])
        self.assertEqual(set(gpu._write_rail_records.call_args.args[0]), {0})
        self.assertFalse(gpu.set_volt_rail_limits(1, reliability=1000)[0])

    def test_partial_reset_reports_failed_previous_rail_and_restores_valid_one(self):
        gpu = fake_gpu("blackwell")
        gpu.read_volt_rail_limits()
        gpu.nvapi.control[0][0] = 20000
        gpu.nvapi.control[1][0] = -20000
        gpu.nvapi.sync_live()
        gpu.nvapi.live[1][0] = 42
        ok, message = gpu.reset_volt_rail_limits()
        self.assertFalse(ok, message)
        self.assertEqual(gpu.nvapi.control[0][0], 0)
        self.assertEqual(gpu.nvapi.control[1][0], -20000)
        self.assertEqual(set(gpu._write_rail_records.call_args.args[0]), {0})

    def test_telemetry_skew_after_capture_does_not_rebase_requests(self):
        gpu = fake_gpu()
        before = gpu.read_volt_rail_limits()[0]["_base_mv"]
        gpu.nvapi.live[0][2] += 6250
        self.assertEqual(gpu.read_volt_rail_limits()[0]["_base_mv"], before)
        gpu.volt_limits_write_enabled = True
        self.assertTrue(gpu.set_volt_rail_limits_raw(0, reliability=12345)[0])
        self.assertEqual(gpu.nvapi.control[0][0], 12345)

    def test_initial_missing_rail_can_be_discovered_without_changing_initial_other_rail(self):
        gpu = fake_gpu("blackwell")
        saved = gpu.nvapi.control.pop(1)
        gpu.nvapi.live.pop(1)
        self.assertTrue(gpu.volt_rail_limits_supported(0))
        initial = gpu._volt_rail_initial_uv[0]
        gpu.nvapi.control[0][0] = 12500
        gpu.nvapi.control[1] = saved
        gpu.nvapi.live[1] = [3, 800000, 0, 0, 0, 0, 0]
        gpu.nvapi.sync_live()
        gpu._refresh_volt_rail_capabilities()
        self.assertTrue(gpu.volt_rail_limits_supported(1))
        self.assertEqual(gpu._volt_rail_initial_uv[0], initial)

    def test_status_change_during_pairing_does_not_create_a_reference(self):
        gpu = fake_gpu()
        reader = gpu.read_volt_rail_state
        n = [0]
        def changing():
            n[0] += 1
            result = reader()
            result[0]["reliability"] += n[0]
            return result
        gpu.read_volt_rail_state = changing
        raw = gpu.read_volt_rail_limits()
        self.assertFalse(raw[0]["_base_mv"])
        self.assertFalse(gpu._volt_rail_initial_uv)

    def test_missing_reference_has_no_generation_fallback(self):
        raw = dict(reliability=0, alt_reliability=0, overvoltage=0, vmin=0)
        self.assertTrue(math.isnan(GPU.abs_limit_mv(raw, "reliability")))
        self.assertTrue(math.isnan(GPU.volt_boost_headroom_mv(raw)))


class CapabilityRefreshUiTests(FakeUiTest):
    def test_refresh_retains_input_modes_and_existing_hold_without_writes(self):
        self.values.update(in_core=130, sl_core=130, xoc_mode=True, vcap=1093.75)
        self.app._slider_ranges = {"core": object()}
        self.app.gpu = Mock()
        self.app.vf_points = None
        hold = self.app._clk_lock = {"kind": "legacy", "verified": True}
        self.app.build_ui = Mock(side_effect=lambda **kw: self.values.update(
            in_core=0, sl_core=0, xoc_mode=False, vcap=1000))
        self.app.sync_risk_ui = Mock()
        self.app.sync_lock_ui = Mock()
        self.app.relayout = Mock()
        self.app.refresh_capabilities()
        self.app.gpu.refresh_capabilities.assert_called_once_with()
        self.assertEqual(self.values["in_core"], 130)
        self.assertTrue(self.values["xoc_mode"])
        self.assertEqual(self.values["vcap"], 1093.75)
        self.assertIs(self.app._clk_lock, hold)
        self.assertFalse(self.app._rebuilding)
        self.assertEqual(len(self.app.gpu.mock_calls), 1)

    def test_busy_and_staged_editor_refuse_rebuild(self):
        self.app.gpu = Mock()
        self.app.build_ui = Mock()
        for fields in ({"_i2c_busy": True}, {"_tim_busy": True},
                       {"vf_orig": {1: 0}, "vf_work": {1: 15}},
                       {"_tw_pending": {"timing": 1}}):
            for key, value in fields.items():
                setattr(self.app, key, value)
            self.app.refresh_capabilities()
            self.app.gpu.refresh_capabilities.assert_not_called()
            self.app.build_ui.assert_not_called()
            for key in fields:
                delattr(self.app, key)
