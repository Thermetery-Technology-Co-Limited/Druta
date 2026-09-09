"""Telemetry-only regulator profiles must never grow a voltage-write control."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dearpygui.dearpygui as dpg

from druta.druta import Druta
from druta.nvbackend import GPU


class ReadOnlyI2cUiTests(unittest.TestCase):
    def setUp(self):
        dpg.create_context()
        self.app = app = Druta.__new__(Druta)
        app.rail = SimpleNamespace(
            p=SimpleNamespace(read_only=True, regulator="MP29816", rail="NVVDD",
                              name="ASUS Astral RTX 5080", port=2,
                              hw_min_mv=None, hw_max_mv=None),
            addr7=0x30, present=Mock(return_value=True),
            read_vout=Mock(return_value=1150), telemetry=Mock(),
            plan=Mock(), set_offset_mv=Mock(), verify=Mock(), reset=Mock(),
            enable_xoc=Mock(), disable_xoc=Mock())
        app.gpu = SimpleNamespace(nvapi=object(), arch=Mock(return_value=10))
        app._gpu_gen = 0
        app.i2c_connection = lambda: (id(app.gpu), id(app.rail), app._gpu_gen)
        app._i2c_verified_for = None
        app.log, app.bind = Mock(), Mock()
        app.s = lambda n: n
        app.guard = Mock(return_value=True)
        app._i2c_busy = app._i2c_verified = False
        app._xoc_bounds = app._knob_sync = False
        app._slider_ranges, app._knob_decimals = {}, {}
        app._carryover_hi, app._knob_cb = {}, {}
        app._ctl_widgets = []

    def tearDown(self):
        dpg.destroy_context()

    def build(self):
        with dpg.window():
            with dpg.table():
                self.app.knob_cols()
                self.app.build_i2c_rail_row()

    def test_readonly_profile_shows_live_voltage_and_bus_without_write_widgets(self):
        self.build()
        self.assertEqual(dpg.get_value("live_i2crail"), "1150 mV")
        self.assertEqual(dpg.get_value("i2c_profile_status"), "Port 2 / 0x30")
        for tag in ("sl_i2crail", "in_i2crail", "go_i2crail",
                    "go_i2crail_x", "go_i2crail_x1"):
            self.assertFalse(dpg.does_item_exist(tag), tag)
        self.assertEqual(self.app._slider_ranges, {})
        self.app.rail.telemetry.assert_not_called()

    def test_readonly_callbacks_refuse_before_load_or_write(self):
        self.app._i2c_verified = True
        with patch("druta.gpuload.available") as available:
            self.app.verify_i2c_rail()
            self.app.apply_i2c_rail(50)
        available.assert_not_called()
        self.app.rail.plan.assert_not_called()
        self.app.rail.verify.assert_not_called()
        self.app.rail.set_offset_mv.assert_not_called()
        self.app.rail.present.assert_not_called()
        self.assertIn("telemetry only", self.app.i2c_gate()[1])

    def test_discovery_passes_selected_adapter_pci_identity(self):
        api = SimpleNamespace(selected={"devid": 0x2C02, "subsys": 0x89DE1043})
        self.app.gpu.nvapi = api
        with patch("druta.railctl.discover", return_value=[]) as find:
            self.app.find_rail()
        find.assert_called_once_with(api, log=self.app.log, architecture=10)

    def test_readonly_is_not_an_i2c_write_risk_even_if_checkbox_stale(self):
        with dpg.window():
            dpg.add_checkbox(tag="i2c_mode", default_value=True)
            dpg.add_checkbox(tag="xoc_mode", default_value=True)
        self.assertEqual(self.app.risk_features(), {"xoc"})
        self.app.rail.present.assert_not_called()

    def test_missing_or_failing_regulator_does_not_build_row(self):
        self.app.rail.present.side_effect = RuntimeError("adapter removed")
        self.build()
        self.assertFalse(dpg.does_item_exist("live_i2crail"))
        self.app.log.assert_called_once()

    def test_read_failure_and_gpu_swap_clear_previous_telemetry(self):
        self.build()
        self.app.rail.read_vout.side_effect = RuntimeError("adapter removed")
        self.assertIsNone(self.app.i2c_rail_text(1100))
        self.app.rail.read_vout.side_effect = None
        self.app.rail.read_vout.return_value = None
        self.assertIsNone(self.app.i2c_rail_text(1100))
        with patch("druta.railctl.discover", side_effect=RuntimeError("new GPU absent")):
            self.app.find_rail()
        self.assertIsNone(self.app.rail)
        self.assertIsNone(self.app.i2c_rail_text(1100))

    def test_readonly_does_not_imply_gpu_voltage_difference_on_unknown_rail(self):
        self.build()
        self.assertEqual(self.app.i2c_rail_text(950), "1150 mV")
        # A previous card's verifier can finish after swapping to this reader.
        self.app._i2c_busy = True
        self.assertEqual(self.app.i2c_rail_text(950), "1150 mV")

    def test_writable_mp2888_keeps_offset_controls_and_verify_gate(self):
        self.app.rail.p = SimpleNamespace(
            read_only=False, rail="NVVDD", regulator="MP2888A",
            env_min=-50, env_max=100, hw_min_mv=-800, hw_max_mv=793.75,
            rungs=(6.25, 12.5, 25, 50, 75))
        self.app.rail.telemetry.return_value = {"offset_mv": 25}
        self.build()
        for tag in ("sl_i2crail", "in_i2crail", "go_i2crail",
                    "go_i2crail_x", "go_i2crail_x1"):
            self.assertTrue(dpg.does_item_exist(tag), tag)
        self.assertEqual(dpg.get_value("sl_i2crail"), 25)
        self.assertEqual(dpg.get_item_configuration("go_i2crail_x")["label"], "Verify")
        self.assertEqual(self.app.i2c_rail_text(1100), "1150 mV")
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.assertEqual(self.app.i2c_rail_text(1100), "1150 mV  (+50 vs GPU)")
        self.assertFalse(self.app.i2c_gate()[0])

    def test_i2c_presence_does_not_suppress_private_nvvdd_limit_rows(self):
        raw = {0: {
            "type": 0, "reliability": 0.0, "alt_reliability": 0.0,
            "overvoltage": 0.0, "vmin": 0.0,
            "_base_mv": {"reliability": 1068.75,
                         "alt_reliability": 1093.75,
                         "overvoltage": 1125.0, "vmin": 650.0},
            "_headroom_mv": 25.0,
        }}
        state = {0: {
            "type": 1, "live": 1093.75, "reliability": 1093.75,
            "alt_reliability": 1093.75, "overvoltage": 1125.0,
            "effective": 1093.75, "vmin": 643.75,
        }}
        self.app.gpu = SimpleNamespace(
            read_volt_rail_limits=Mock(return_value=raw),
            read_volt_rail_state=Mock(return_value=state),
            volt_rail_limits_supported=Mock(return_value=True),
            volt_rail_limit_fields=Mock(side_effect=lambda rail:
                GPU.VOLT_LIMIT_FIELDS if rail == 0 else ()),
            VOLT_LIMIT_MIN_MV=650, VOLT_LIMIT_MAX_MV=1200,
            VOLT_LIMIT_XOC_MAX_MV=1500,
            arch=Mock(return_value=GPU.ARCH_TURING),
        )
        self.app.rail.p = SimpleNamespace(
            read_only=False, rail="NVVDD", regulator="MP2888A",
            env_min=-50, env_max=100, hw_min_mv=-800, hw_max_mv=793.75,
            rungs=(6.25, 12.5, 25, 50, 75))
        self.app.rail.telemetry.return_value = {"offset_mv": 0}

        with dpg.window():
            with dpg.table():
                self.app.knob_cols()
                self.app.build_volt_limits_rows()
                self.app.build_i2c_rail_row()

        for tag in ("sl_vlim_rel", "sl_vlim_alt", "sl_vlim_ov",
                    "sl_vlim_lo", "sl_i2crail"):
            self.assertTrue(dpg.does_item_exist(tag), tag)

    def test_link_applies_one_value_to_both_ceiling_fields_atomically(self):
        with dpg.window():
            dpg.add_checkbox(tag="vlim_link", default_value=True)
        self.app.gpu.set_volt_rail_limits = Mock(return_value=(True, "stored"))
        self.app.refresh_volt_limits = Mock()

        for field in ("reliability", "alt_reliability"):
            with self.subTest(field=field):
                self.app.gpu.set_volt_rail_limits.reset_mock()
                self.app.apply_vlim(0, **{field: 1167})
                self.app.gpu.set_volt_rail_limits.assert_called_once_with(
                    0, reliability=1167, alt_reliability=1167)

        self.assertEqual(self.app.refresh_volt_limits.call_count, 2)

    def test_mp29816_write_map_is_visibly_unverified_with_its_own_rungs(self):
        self.app.rail.p = SimpleNamespace(
            read_only=False, rail="NVVDD", regulator="MP29816",
            name="ASUS RTX 5080 Astral - MP29816 (write path unverified)",
            env_min=-20, env_max=20, hw_min_mv=-640, hw_max_mv=635,
            rungs=(5, 10, 15, 20))
        self.app.rail.telemetry.return_value = {"offset_mv": 0}
        self.build()
        self.assertIn("unverified", dpg.get_value("i2c_write_status"))
        help_text = self.app.i2c_verify_description()
        self.assertIn("+5, +10, +15, +20 mV relative to the entry offset", help_text)
        self.assertIn("exact entry register word", help_text)
        self.assertNotIn("75", help_text)
        # An idle rail/VID difference must not imply a confirmed applied offset.
        self.app.rail.read_vout.return_value = 980
        self.assertEqual(self.app.i2c_rail_text(810), "980 mV")
        with dpg.window():
            dpg.add_checkbox(tag="i2c_mode", default_value=True)
        self.app.apply_i2c_rail(5)
        self.app.rail.plan.assert_not_called()
        self.app.rail.set_offset_mv.assert_not_called()

    def test_failed_verification_does_not_arm_apply(self):
        self.app.gpu.read_vcore_mv = Mock(return_value=1040)
        self.app._i2c_busy = True
        with patch("druta.gpuload.induce", return_value={
                "result": (False, "NVAPI write rejected", [])}):
            self.app._i2c_verify_worker(self.app.gpu, self.app.rail, self.app.i2c_connection())
        self.assertFalse(self.app._i2c_verified)
        self.assertFalse(self.app._i2c_busy)

    def test_successful_verification_arms_only_the_captured_card(self):
        old_gpu, old_rail = self.app.gpu, self.app.rail
        old_gpu.read_vcore_mv = Mock(return_value=1040)
        self.app._gpu_gen = 1
        with patch("druta.gpuload.induce", return_value={"result": 1040}), \
                patch("druta.gpuload.verify_in_p0", return_value=(
                    True, "rail moved and entry restored", [])):
            # Even switching away and back to the same object invalidates it.
            self.app._i2c_verify_worker(old_gpu, old_rail, (id(old_gpu), id(old_rail), 0))
            self.assertFalse(self.app._i2c_verified)
            self.app._i2c_verify_worker(old_gpu, old_rail, self.app.i2c_connection())
            self.assertTrue(self.app._i2c_verified)

    def test_verifier_binds_adapter_before_background_dispatch(self):
        self.app.sync_lock_ui = Mock()
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.rail.p.rungs = (5, 10, 15, 20)
        self.app._gpu_gen = 3
        with patch("druta.gpuload.available", return_value=(True, "")), \
                patch("druta.druta.threading.Thread") as thread:
            self.app.verify_i2c_rail()
        self.assertEqual(thread.call_args.kwargs["args"],
                         (self.app.gpu, self.app.rail, self.app.i2c_connection(), self.app._i2c_cancel))
        self.assertFalse(thread.call_args.kwargs["daemon"])


if __name__ == "__main__":
    unittest.main()
