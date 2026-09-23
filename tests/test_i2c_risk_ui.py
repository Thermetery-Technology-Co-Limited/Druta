"""Risk tint reflects an enabled selected I2C write path, not a probe result."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import druta
from druta.druta import Druta
from tests.test_arch_ui_regressions import FakeUiTest


class I2cRiskUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.rail = SimpleNamespace(
            p=SimpleNamespace(read_only=False, regulator="Nuvoton NCT3933U",
                              name="NCT3933U", port=1),
            addr7=0x15, current_dac=True, present=Mock(return_value=False),
            enable_xoc=Mock(return_value=(True, "on")),
            disable_xoc=Mock(return_value=(True, "off")),
            capture_control=Mock(), set_current_ua=Mock())
        self.app.rail = self.rail
        self.app.gpu = SimpleNamespace(
            read_volt_rail_limits=Mock(return_value={}),
            msvdd_write_enabled=False, voltage_xoc_enabled=False,
            volt_limits_write_enabled=False)
        self.app._rail_writable = {0}
        self.app._risk_themes = {}
        self.app._ctl_widgets = []
        self.app._knob_cb = {}
        self.app._i2c_busy = False
        self.app._i2c_scan_busy = False
        self.app._profile_pending = None
        self.app._profile_applying = False
        self.app._i2c_discovery_complete = True
        self.app._i2c_verified = False
        self.app._i2c_verified_for = None
        self.app._gpu_gen = 1
        self.app.ov_carryover = Mock()
        self.app.refresh_current_limits = Mock()
        self.app.sync_slider_ranges = Mock()
        self.values.update(xoc_mode=False, i2c_mode=True, vlim_mode=True,
                           risk_banner="", tab_control=True)

    def _banner_text(self):
        calls = [call for call in self.ui.configure_item.call_args_list
                 if call.args and call.args[0] == "risk_banner" and "default_value" in call.kwargs]
        return calls[-1].kwargs["default_value"]

    def test_selected_writable_nct_is_crimson_without_calling_present(self):
        self.assertEqual(self.app.risk_features(), {"i2c", "volt_limits"})
        self.assertEqual(self.app.risk_score(), 3)
        self.app.sync_risk_ui()

        self.rail.present.assert_not_called()
        self.assertIn("CRIMSON (risk 3)", self._banner_text())
        self.assertIn(druta.RISK_FEATURE_TEXT["i2c"], self._banner_text())
        self.ui.bind_item_theme.assert_any_call(
            "tab_control", self.app._risk_themes[druta.RISK_CRIMSON])
        self.assertTrue(self.app.gpu.volt_limits_write_enabled)

    def test_transient_or_raising_presence_probe_cannot_downgrade_visual_risk(self):
        for failure in (False, RuntimeError("temporary I2C read failure")):
            with self.subTest(failure=failure):
                self.rail.present.reset_mock()
                self.rail.present.return_value = False
                self.rail.present.side_effect = failure if isinstance(failure, Exception) else None
                self.app.sync_risk_ui()
                self.assertEqual(self.app.risk_score(), 3)
                self.assertIn("CRIMSON (risk 3)", self._banner_text())
                self.rail.present.assert_not_called()

    def test_actual_current_dac_apply_still_refuses_before_capture_or_write(self):
        self.rail.present.return_value = False
        self.app.guard = Mock(return_value=True)

        self.app.apply_current_dac(1, 10)

        self.rail.present.assert_called_once_with()
        self.rail.capture_control.assert_not_called()
        self.rail.set_current_ua.assert_not_called()
        self.app.autosave_before.assert_not_called()
        self.assertIn("no longer answering", self.app.log.call_args.args[0])

    def test_i2c_only_is_red_and_unchecking_reverts_to_stock(self):
        self.values["vlim_mode"] = False
        self.app.sync_risk_ui()
        self.assertEqual(self.app.risk_features(), {"i2c"})
        self.assertEqual(self.app.risk_score(), 2)
        self.assertIn("RED (risk 2)", self._banner_text())

        self.values["i2c_mode"] = False
        self.app.sync_risk_ui()
        self.assertEqual(self.app.risk_features(), set())
        self.ui.configure_item.assert_any_call("risk_banner", show=False)
        self.ui.bind_item_theme.assert_any_call("tab_control", 0)

    def test_read_only_or_unselected_rail_has_no_i2c_risk(self):
        self.values["vlim_mode"] = False
        self.rail.p.read_only = True
        self.assertEqual(self.app.risk_features(), set())
        self.rail.present.assert_not_called()

        self.rail.p.read_only = False
        self.app.rail = None
        self.assertEqual(self.app.risk_features(), set())

    def test_async_publication_refreshes_risk_without_a_presence_probe(self):
        self.app._lock = threading.Lock()
        self.app._i2c_scan_token = 9
        self.app._i2c_scan_result = (9, 1, self.app.gpu, False, [self.rail],
                                     None, "scan", [])
        self.app._i2c_scan_busy = True
        self.app._i2c_scan_thread = Mock()
        self.app._i2c_scan_started = 0.0
        self.app._i2c_scan_scope = None
        self.app._closing = False
        self.app._rail_candidates = []
        self.app.remember_i2c_route = Mock()
        self.app.sync_lock_ui = Mock()
        self.app.build_i2c_candidates = Mock()
        self.app.relayout = Mock()
        self.values["i2c_candidates"] = True

        self.app.poll_i2c_discovery()

        self.assertIs(self.app.rail, self.rail)
        self.assertEqual(self.app.risk_score(), 3)
        self.assertIn("CRIMSON (risk 3)", self._banner_text())
        self.rail.present.assert_not_called()


if __name__ == "__main__":
    unittest.main()
