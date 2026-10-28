"""The existing power knob exposes live extensions only through XOC."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta.druta import DIM, WARN, Druta, KnobRange


class LivePowerUiTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(
            static={"pl_min_mw": 180000, "pl_max_mw": 420000, "pl_def_mw": 300000},
            read_power_limit_mw=Mock(return_value=300000),
            power_limit_bounds_mw=Mock(return_value=(1, 1999000)),
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
        self.assertEqual(self.app._slider_ranges["pl"], KnobRange("Power", 180, 420, 0.001, 1999))
        self.app.sync_slider_ranges(False)
        self.assertEqual(self.config["sl_pl"], {"min_value": 180, "max_value": 420})
        self.app.sync_slider_ranges(True)
        self.assertEqual(self.config["sl_pl"], {"min_value": 0.001, "max_value": 1999})

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

    def test_four_digit_typed_power_applies_exact_milliwatts_and_clamps_to_xoc_ceiling(self):
        self.app.note_gated_knob = Mock()
        self.app.refresh_power_request_range()
        self.app.sync_slider_ranges(True)
        self.values["in_pl"] = 1999
        self.app.knob_typed("pl")
        self.assertEqual(self.values, {"sl_pl": 1999, "in_pl": 1999})
        self.app.gpu.read_power_limit_mw.return_value = 1999000
        self.app.apply_pl(self.values["sl_pl"])
        self.app.gpu.set_power_limit_mw.assert_called_once_with(1999000)
        self.values["in_pl"] = 2000
        self.app.knob_typed("pl")
        self.assertEqual(self.values, {"sl_pl": 1999, "in_pl": 1999})

    def test_xoc_off_carryover_tracks_the_observed_endpoint_both_directions(self):
        for value, bounds in (
            (600000, (180, 600)),
            (1999000, (180, 1999)),
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
                reported = self.app.report.call_args.args[0]
                self.assertEqual(reported[0], result[0])
                self.assertIn(result[1], reported[1])
                self.assertEqual(self.values["in_pl"], readback / 1000)
                self.app._registry_panel.refresh_arbitration.assert_called()
                self.app._power_slider_style.refresh.assert_called()

    def test_rejected_request_returns_both_widgets_to_actual_configured_limit(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.return_value = 350000
        self.app.gpu.set_power_limit_mw.return_value = (False, "driver rejected 400 W")
        self.app.apply_pl(400)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (350, 350))
        self.assertFalse(self.app.report.call_args.args[0][0])
        self.assertIn("Requested 400 W did not match configured readback 350 W", self.values["power_request_status"])

    def test_success_without_matching_configured_readback_is_reported_as_failure(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.return_value = 350000
        self.app.apply_pl(400)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (350, 350))
        ok, message = self.app.report.call_args.args[0]
        self.assertFalse(ok)
        self.assertIn("slider returned to readback", message)

    def test_accepted_configured_request_keeps_slider_and_discloses_lower_nvml_limit(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.side_effect = [350000, 400000]
        self.app.gpu._read_power = Mock(side_effect=lambda state: state.update(pl_now_mw=350000))
        self.app.apply_pl(400)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (400, 400))
        ok, message = self.app.report.call_args.args[0]
        self.assertTrue(ok)
        self.assertIn("configured request 400 W, NVML-reported enforced limit 350 W", message)
        self.assertIn("Configured request: 400 W", self.values["power_request_status"])
        self.assertIn("Below configured request; downstream limits may apply", self.values["power_request_status"])

    def test_native_limit_warns_even_when_nvml_echoes_the_accepted_request(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.side_effect = [350000, 400000]
        self.app.gpu._read_power = Mock(side_effect=lambda state: state.update(pl_now_mw=400000))
        self.app.gpu.read_native_power_limit_mw = Mock(return_value=(350000, None))
        self.app.apply_pl(400)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (400, 400))
        self.assertTrue(self.app.report.call_args.args[0][0])
        self.assertIn("NVML-reported enforced limit: 400 W", self.values["power_request_status"])
        self.assertIn("Native effective limit: 350 W", self.values["power_request_status"])
        self.assertIn("Native limit is below configured request", self.values["power_request_status"])
        self.assertIn("native effective limit 350 W is below configured request 400 W",
                      self.app.report.call_args.args[0][1])
        self.assertEqual(self.config["power_request_status"]["color"], WARN)
        self.app.gpu.read_native_power_limit_mw.assert_called_once_with()

    def test_fresh_control_polls_resample_native_limit_and_clear_resolved_discrepancy(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_native_power_limit_mw = Mock(side_effect=[(360938, None), (400000, None)])
        self.app.gpu.clkdom_is_blackwell = lambda: False
        self.app.gpu.clkdom_pairing = lambda rows: {}
        self.app.mem_fmt = Mock(return_value=("--", "MHz"))
        self.app.refresh_rail_live = Mock()
        self.app.i2c_rail_text = Mock(return_value=None)
        self.app.sync_knob_boxes = Mock()
        sample = {"pl_requested_mw": 400000, "pl_now_mw": 400000}
        self.app.refresh_control(sample)
        self.assertIn("Native effective limit: 360.938 W", self.values["power_request_status"])
        self.assertEqual(self.config["power_request_status"]["color"], WARN)
        self.app.refresh_control(sample)
        self.assertIn("Native effective limit: 400 W", self.values["power_request_status"])
        self.assertNotIn("below configured request", self.values["power_request_status"])
        self.assertEqual(self.config["power_request_status"]["color"], DIM)
        self.assertEqual(self.app.gpu.read_native_power_limit_mw.call_count, 2)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (400, 400))

    def test_native_unavailable_or_malformed_never_turns_nvml_echo_into_native_proof(self):
        self.values["power_request_status"] = ""
        self.app.gpu.read_power_limit_mw.return_value = 400000
        self.app.gpu._read_power = Mock(side_effect=lambda state: state.update(pl_now_mw=400000))
        for result in ((None, "not available"), (True, None), ("350000", None),
                       (-1, None), (0xFFFFFFFF, None), OSError("native getter failed")):
            with self.subTest(result=result):
                self.app.gpu.read_native_power_limit_mw = (
                    Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result))
                self.app.apply_pl(400)
                self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (400, 400))
                self.assertTrue(self.app.report.call_args.args[0][0])
                self.assertIn("Native effective limit unavailable", self.values["power_request_status"])
                self.assertIn("NVML readback does not verify it", self.values["power_request_status"])

    def test_unavailable_postwrite_readback_restores_last_known_and_stays_unverified_until_fresh_poll(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.side_effect = [350000, None]
        self.app.apply_pl(400)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (350, 350))
        self.assertFalse(self.app.report.call_args.args[0][0])
        self.assertIn("last verified 350 W", self.values["power_request_status"])
        self.assertIn("not a confirmed current setting", self.values["power_request_status"])
        self.app.refresh_power_request_status(None, 350000)
        self.assertIn("unverified", self.values["power_request_status"])
        # A later accepted write may become readable. Only then claim 400 W.
        self.app.refresh_power_request_status(400000, 350000)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (400, 400))
        self.assertNotIn("unverified", self.values["power_request_status"])
        self.assertIn("Below configured request", self.values["power_request_status"])

    def test_unknown_state_never_borrows_last_verified_value_from_another_gpu(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app._verified_power_request = (object(), 350000)
        self.app.gpu.read_power_limit_mw.return_value = None
        self.app.apply_pl(400)
        self.assertIn("Slider value is an unverified proposal", self.values["power_request_status"])
        self.assertNotIn("350 W", self.values["power_request_status"])
        self.assertFalse(self.app.report.call_args.args[0][0])

    def test_setter_exception_still_reconciles_widgets_and_refreshes_peers(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.return_value = 350000
        self.app.gpu.set_power_limit_mw.side_effect = OSError("driver unavailable")
        self.app.apply_pl(400)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (350, 350))
        self.assertIn("driver unavailable", self.app.report.call_args.args[0][1])
        self.app.refresh_current_limits.assert_called_once_with(sync=True)
        self.app.refresh_power_policies.assert_called_once_with(force=True)

    def test_failed_peer_reapply_still_shows_configured_request_that_took_effect(self):
        self.values.update(sl_pl=400, in_pl=400, power_request_status="")
        self.app.gpu.read_power_limit_mw.side_effect = [350000, 400000]
        self.app.gpu.set_power_limit_mw.return_value = (False, "peer reapply failed")
        self.app.apply_pl(400)
        self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (400, 400))
        self.assertFalse(self.app.report.call_args.args[0][0])
        self.assertIn("peer reapply failed", self.app.report.call_args.args[0][1])
        self.assertIn("Configured request: 400 W", self.values["power_request_status"])

    def test_noninteger_or_failed_readback_never_confirms_a_numeric_setting(self):
        self.values["power_request_status"] = ""
        self.app.sync_power_slider_from_card()
        for value in (None, 0, -1, True, "350000", 350000.5, 10**1000, OSError("unreadable")):
            with self.subTest(value=value):
                self.app.gpu.read_power_limit_mw.side_effect = (
                    value if isinstance(value, Exception) else None)
                self.app.gpu.read_power_limit_mw.return_value = value
                self.values.update(sl_pl=400, in_pl=400)
                self.app.apply_pl(400)
                self.assertEqual((self.values["sl_pl"], self.values["in_pl"]), (300, 300))
                self.assertFalse(self.app.report.call_args.args[0][0])
                self.assertIn("unverified", self.values["power_request_status"])

    def test_locked_apply_never_dispatches_or_refreshes(self):
        self.app.guard.return_value = False
        self.app.apply_pl(999)
        self.app.gpu.set_power_limit_mw.assert_not_called()
        self.app._registry_panel.refresh_arbitration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
