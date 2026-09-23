"""NCT3933U controls stay native current-DAC UI, never voltage controls."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dearpygui.dearpygui as dpg

from druta import druta
from druta.druta import Druta
from tests.test_arch_ui_regressions import FakeUiTest


def current_dac():
    profile = SimpleNamespace(
        regulator="Nuvoton NCT3933U", read_only=False,
        name="NCT3933U test controller", rail="OUT1/OUT2/OUT3",
        port=4, addr7=0x68)
    return SimpleNamespace(
        current_dac=True, p=profile, addr7=0x68,
        present=Mock(return_value=True),
        telemetry=Mock(return_value={
            "outputs": [0x80, 0x05, 0xFF], "configuration": 0x02,
            "currents_ua": [0, -50, 1270],
        }),
        capture_control=Mock(return_value={
            "outputs": [0x80, 0x05, 0xFF], "configuration": 0x02}),
        validate_control=Mock(),
        set_current_ua=Mock(return_value=(True, "OUT1 stored and read back")),
        zero_outputs=Mock(return_value=(True, "all outputs zeroed and read back")),
        reset=Mock(return_value=(True, "all outputs zeroed and read back")),
    )


class Nct3933RenderTests(unittest.TestCase):
    def setUp(self):
        dpg.create_context()
        self.app = Druta.__new__(Druta)
        self.app.rail = current_dac()
        self.app.s = lambda value: value
        self.app.log = Mock()
        self.app.bind = Mock()

    def tearDown(self):
        dpg.destroy_context()

    def test_three_output_panel_uses_native_current_register_readback(self):
        with dpg.window():
            self.app.build_current_dac_controls()

        for channel, expected in enumerate((0, -50, 1270), start=1):
            self.assertTrue(dpg.does_item_exist(f"nct_current_{channel}"))
            self.assertTrue(dpg.does_item_exist(f"nct_apply_{channel}"))
            self.assertTrue(dpg.does_item_exist(f"nct_zero_{channel}"))
            self.assertEqual(dpg.get_value(f"nct_current_{channel}"), expected)
        self.assertTrue(dpg.does_item_exist("nct_read_settings"))
        self.assertTrue(dpg.does_item_exist("nct_zero_all"))
        self.assertEqual(dpg.get_value("nct_config"), "configuration 0x02")
        self.assertIn("physical voltage requires meter", dpg.get_value("nct_status"))
        self.assertFalse(dpg.does_item_exist("sl_i2crail"))
        self.assertFalse(dpg.does_item_exist("live_i2crail"))
        self.app.rail.telemetry.assert_called_once_with()

    def test_configuration_sets_native_steps_bounds_and_power_saving_status(self):
        self.app.rail.telemetry.return_value = {
            "outputs": [0x80, 0x05, 0xFF], "configuration": 0x55,
            "currents_ua": [0, -100, 2540],
        }
        with dpg.window():
            self.app.build_current_dac_controls()
        for channel in range(1, 4):
            config = dpg.get_item_configuration(f"nct_current_{channel}")
            self.assertEqual(config["step"], 20)
            self.assertEqual(config["min_value"], -2540)
            self.assertEqual(config["max_value"], 2540)
            self.assertFalse(config["min_clamped"])
            self.assertFalse(config["max_clamped"])
            self.assertIn("20 µA step, ±2540 µA", dpg.get_value(f"nct_range_{channel}"))
        self.assertIn("outputs disabled by power saving", dpg.get_value("nct_config"))
        self.assertIn("Outputs disabled by power saving", dpg.get_value("nct_status"))

    def test_unlock_gate_disables_every_dac_write_but_keeps_read_settings_available(self):
        self.app.gpu = SimpleNamespace(
            fan_capabilities=lambda: {"manual": True, "auto": True}, arch=lambda: 0)
        self.app._i2c_busy = False
        self.app._profile_pending = None
        self.app._ctl_widgets = []
        with dpg.window():
            dpg.add_checkbox(tag="unlock", default_value=False)
            self.app.build_current_dac_controls()
        self.app.sync_lock_ui()
        for tag in ("nct_current_1", "nct_apply_1", "nct_zero_1", "nct_zero_all"):
            self.assertFalse(dpg.get_item_configuration(tag)["enabled"], tag)
        self.assertTrue(dpg.get_item_configuration("nct_read_settings")["enabled"])

    def test_candidate_panel_branches_before_generic_voltage_table(self):
        self.app._rail_candidates = [self.app.rail]
        self.app._i2c_discovery_complete = True
        self.app._i2c_scan_busy = False
        self.app._i2c_scan_work = (0, 0, "")
        self.app._i2c_scan_status = "done"
        self.app.update_i2c_scan_ui = Mock()
        self.app.knob_cols = Mock(side_effect=AssertionError("generic voltage UI"))
        self.app.build_i2c_rail_row = Mock(side_effect=AssertionError("generic voltage UI"))
        self.app.i2c_candidate_label = lambda _rail: "1: Nuvoton NCT3933U"
        with dpg.window():
            self.app.build_i2c_candidates()
        self.assertTrue(dpg.does_item_exist("nct_current_1"))
        self.app.knob_cols.assert_not_called()
        self.app.build_i2c_rail_row.assert_not_called()


class Nct3933ActionTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.rail = current_dac()
        self.app.guard = Mock(return_value=True)
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.i2c_connection = lambda: ("same-adapter", "same-dac")
        self.app.refresh_current_dac_settings = Mock(return_value=True)
        self.app._i2c_busy = False
        self.app._tim_busy = False
        self.values.update(nct_current_1=50, i2c_mode=True)

    def test_apply_captures_exact_control_before_autosave_then_writes_native_ua(self):
        self.app.apply_current_dac(1)

        self.app.rail.capture_control.assert_called_once_with()
        self.app.rail.validate_control.assert_called_once_with(
            {"outputs": [0x80, 0x05, 0xFF], "configuration": 0x02}, xoc=None)
        self.app.autosave_before.assert_called_once_with("i2c-current-dac-out1")
        self.app.rail.set_current_ua.assert_called_once_with(
            1, 50, acknowledged=True,
            expected={"outputs": [0x80, 0x05, 0xFF], "configuration": 0x02})
        self.app.report.assert_called_once_with((True, "OUT1 stored and read back"))
        self.app.refresh_current_dac_settings.assert_called_once_with(
            log_failure=False, preserve_status=False)

    def test_zero_all_uses_controller_operation_and_never_a_voltage_setter(self):
        self.app.apply_i2c_rail(25)
        self.app.rail.set_current_ua.assert_not_called()
        self.assertIn("native current controls", self.app.log.call_args.args[0])

        self.app.log.reset_mock()
        self.app.zero_current_dac_outputs()
        self.app.rail.zero_outputs.assert_called_once_with(
            acknowledged=True, expected={"outputs": [0x80, 0x05, 0xFF], "configuration": 0x02})
        self.app.autosave_before.assert_called_once_with("i2c-current-dac-zero-all")
        self.app.report.assert_called_once_with((True, "all outputs zeroed and read back"))

    def test_invalid_current_is_refused_before_capture_autosave_or_write(self):
        self.values["nct_current_1"] = 12.5
        self.app.apply_current_dac(1)
        self.app.rail.capture_control.assert_not_called()
        self.app.autosave_before.assert_not_called()
        self.app.rail.set_current_ua.assert_not_called()
        self.assertIn("integer number of", self.app.log.call_args.args[0])

    def test_failed_rollback_stays_sticky_and_readback_does_not_replace_failure(self):
        self.app.rail.set_current_ua.return_value = (False, "write rejected; RESTORE FAILED")
        self.app.rail._verification_restore_ok = False
        self.app.rail._verification_restore_error = "OUT1: restore readback mismatch"
        self.app.apply_current_dac(1)
        self.assertTrue(self.app._i2c_restore_failed)
        self.assertEqual(self.app._i2c_restore_error, "OUT1: restore readback mismatch")
        self.app.refresh_current_dac_settings.assert_called_once_with(
            log_failure=False, preserve_status=True)

    def test_current_dac_never_calls_generic_vout_reader(self):
        self.values["live_i2crail"] = "old value"
        self.app.rail.read_vout = Mock(side_effect=AssertionError("must not read VOUT"))
        self.assertIsNone(self.app.i2c_rail_text(1000))
        self.app.rail.read_vout.assert_not_called()

    def test_read_settings_refuses_while_i2c_is_busy(self):
        self.app._i2c_busy = True
        self.assertFalse(Druta.refresh_current_dac_settings(self.app))
        self.app.rail.telemetry.assert_not_called()
        self.assertIn("wait for I2C", self.app.log.call_args.args[0])

    def test_readback_verification_never_starts_voltage_staircase(self):
        self.app.refresh_current_dac_settings = Druta.refresh_current_dac_settings.__get__(self.app, Druta)
        with patch.object(druta.gpuload, "available") as available, \
                patch.object(druta.threading, "Thread") as thread:
            self.app.verify_i2c_rail()
        available.assert_not_called()
        thread.assert_not_called()
        self.assertTrue(self.app._i2c_verified)
        self.assertEqual(self.app._i2c_verified_for, ("same-adapter", "same-dac"))
        self.assertIn("physical voltage requires meter", self.app.log.call_args.args[0])

    def test_profile_load_continues_after_synchronous_dac_readback(self):
        self.app.gpu = SimpleNamespace()
        self.app._i2c_discovery_complete = True
        self.app._profile_pending = None
        self.app.rail_for_profile = Mock(return_value=self.app.rail)
        self.app.autosave_before = Mock(return_value=True)
        self.app.sync_risk_ui = Mock()
        self.app.verify_i2c_rail = Mock()
        self.app.i2c_verified = Mock(side_effect=(False, True))
        self.app.finish_profile_load = Mock()
        state = {"i2c": {"format": "i2c.current_dac_control"}, "schema": 1}
        with patch.object(druta.profiles, "preflight", return_value=None), \
                patch.object(druta.profiles, "summarize", return_value="DAC outputs"):
            self.app.begin_profile_load("dac-profile", state)
        self.app.finish_profile_load.assert_called_once_with("dac-profile", state, False)
        self.assertIsNone(self.app._profile_pending)
        self.assertNotIn("under load", " ".join(
            str(call.args[0]) for call in self.app.log.call_args_list))

    def test_profile_restore_rollback_failure_becomes_session_sticky(self):
        self.app.rail._verification_restore_ok = False
        self.app.rail._verification_restore_error = "OUT2: restore write rejected"
        self.app.i2c_verified = Mock(return_value=True)
        self.app.sync_sliders_from_gpu = Mock()
        self.app.sync_profile_rail_sliders = Mock()
        self.app.refresh_volt_limits = Mock()
        self.app.vf_read = Mock()
        self.app.profile_failure = Mock()
        with patch.object(druta.profiles, "restore", return_value=[(False, "DAC restore failed")]):
            self.app.finish_profile_load("dac-profile", {"i2c": {}}, False)
        self.assertTrue(self.app._i2c_restore_failed)
        self.assertEqual(self.app._i2c_restore_error, "OUT2: restore write rejected")
        self.app.profile_failure.assert_called_once()

    def test_reset_path_uses_current_dac_reset_without_session_staircase_flag(self):
        self.app._i2c_busy = False
        self.app._i2c_discovery_complete = True
        self.app._i2c_verified = False
        ok, message = self.app.reset_i2c_rail()
        self.assertTrue(ok)
        self.assertIn("zeroed", message)
        self.app.rail.reset.assert_called_once_with()
        self.app.rail.capture_control.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
