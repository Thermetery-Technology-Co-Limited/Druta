"""The existing power knob exposes live extensions only through XOC."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta.druta import Druta, KnobRange


class LivePowerUiTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(
            static={"pl_min_mw": 180000, "pl_max_mw": 420000, "pl_def_mw": 300000},
            read_power_limit_mw=Mock(return_value=300000),
            power_limit_bounds_mw=Mock(return_value=(1, 999000)),
            set_power_limit_mw=Mock(return_value=(True, "accepted request")),
        )
        self.app._slider_ranges = {"pl": KnobRange("Power", 180, 420, None, None)}
        self.app._carryover_hi, self.app._carryover_lo = {}, {}
        self.app._xoc_bounds, self.app._knob_sync = False, False
        self.app._power_slider_style = Mock()
        self.app.log, self.app.report, self.app.guard = Mock(), Mock(), Mock(return_value=True)
        self.app.refresh_current_limits, self.app.refresh_power_policies = Mock(), Mock()
        self.app._registry_panel = Mock()
        self.values = {"sl_pl": 300.0, "in_pl": 300.0}
        self.config = {}
        self.ui = MagicMock()
        self.ui.does_item_exist.side_effect = self.values.__contains__
        self.ui.get_value.side_effect = self.values.get
        self.ui.set_value.side_effect = self.values.__setitem__
        self.ui.configure_item.side_effect = lambda tag, **kw: self.config.update({tag: kw})
        self.ui.is_item_focused.return_value = self.ui.is_item_active.return_value = False
        patcher = patch("druta.druta.dpg", self.ui)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_native_metadata_can_supply_missing_nvml_slider_fields(self):
        self.app.gpu.static.clear()

        def supply():
            self.app.gpu.static.update(pl_min_mw=190000, pl_def_mw=310000, pl_max_mw=470000)
            return {"requested_mw": 310000}, None

        self.app.gpu.read_live_power_request = Mock(side_effect=supply)
        self.app.slider_row = Mock()
        self.app.refresh_power_request_range = Mock()
        self.app.sync_power_slider_from_card = Mock()
        self.app.setup_power_slider_style = Mock()
        self.assertTrue(self.app.build_power_limit_row(self.app.gpu.static))
        self.assertEqual(self.app.slider_row.call_args.args[:5], ("pl", "Power limit (W)", 190, 470, 310))

    def test_missing_all_power_metadata_creates_no_invented_slider(self):
        self.app.gpu.static.clear()
        self.app.gpu.read_live_power_request = Mock(return_value=(None, "unsupported"))
        self.app.slider_row = Mock()
        self.assertFalse(self.app.build_power_limit_row(self.app.gpu.static))
        self.app.slider_row.assert_not_called()

    def test_probe_preserves_normal_range_and_supplies_xoc_wire_bounds(self):
        self.app.refresh_power_request_range()
        self.assertEqual(self.app._slider_ranges["pl"], KnobRange("Power", 180, 420, 0.001, 999))
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.config["sl_pl"], {"min_value": 180, "max_value": 420})
        self.app.sync_slider_ranges(True)
        self.assertEqual(self.config["sl_pl"], {"min_value": 0.001, "max_value": 999})

    def test_missing_or_failed_probe_preserves_existing_control(self):
        normal = self.app._slider_ranges["pl"]
        self.app.gpu.power_limit_bounds_mw.side_effect = OSError("probe failed")
        self.app.refresh_power_request_range()
        self.assertEqual(self.app._slider_ranges["pl"], normal)
        self.ui.delete_item.assert_not_called()
        self.app.log.assert_called_once()
        del self.app.gpu.power_limit_bounds_mw
        self.app.refresh_power_request_range()
        self.assertEqual(self.app._slider_ranges["pl"], normal)

    def test_xoc_off_carryover_tracks_the_observed_endpoint_both_directions(self):
        for value, bounds in (
            (600000, (180, 600)),
            (599999, (180, 599.999)),
            (150000, (150, 420)),
            (300000, (180, 420)),
        ):
            with self.subTest(value=value):
                self.app.gpu.read_power_limit_mw.return_value = value
                self.app.sync_power_slider_from_card()
                self.assertEqual(self.values, {"sl_pl": value / 1000, "in_pl": value / 1000})
                self.assertEqual((self.config["sl_pl"]["min_value"], self.config["sl_pl"]["max_value"]), bounds)

    def test_unavailable_observation_does_not_erase_established_carryover(self):
        self.app.power_request_carryover(600000)
        for value in (None, 0, True, "300000"):
            self.assertFalse(self.app.power_request_carryover(value))
            self.assertEqual(self.app._carryover_hi["pl"], 600)

    def test_apply_syncs_accepted_request_and_refreshes_arbitration_even_on_failure(self):
        for result, readback in (((True, "accepted request"), 500001), ((False, "failed; original restored"), 300000)):
            with self.subTest(result=result):
                self.app.gpu.set_power_limit_mw.return_value = result
                self.app.gpu.read_power_limit_mw.return_value = readback
                self.app.apply_pl(500.001)
                self.app.gpu.set_power_limit_mw.assert_called_with(500001)
                self.app.report.assert_called_with(result)
                self.assertEqual(self.values["in_pl"], readback / 1000)
                self.app._registry_panel.refresh_arbitration.assert_called()
                self.app._power_slider_style.refresh.assert_called()

    def test_locked_apply_never_dispatches_or_refreshes(self):
        self.app.guard.return_value = False
        self.app.apply_pl(999)
        self.app.gpu.set_power_limit_mw.assert_not_called()
        self.app._registry_panel.refresh_arbitration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
