"""Live power requests participate in profile validation and replay."""

import unittest
from unittest.mock import Mock

from druta import profiles
from tests.test_current_limit_profiles import fixture


class LivePowerProfileTests(unittest.TestCase):
    def setUp(self):
        self.gpu = fixture()
        self.gpu.static.update(pl_min_mw=180000, pl_max_mw=420000)
        self.gpu.voltage_xoc_enabled = False
        self.current = 300000
        self.gpu.read_power_limit_mw = Mock(side_effect=lambda: self.current)
        self.gpu.power_limit_bounds_mw = Mock(side_effect=self.bounds)
        self.gpu.set_power_limit_mw = Mock(return_value=(True, "accepted request"))

    def bounds(self, *, xoc=None):
        if xoc:
            return 1, 1999000
        return min(180000, self.current), max(420000, self.current)

    def state(self, mw, xoc=False):
        return {"schema": 2, "xoc": xoc, "power_limit_mw": mw}

    def test_capture_marks_both_extended_directions_for_replay_with_xoc(self):
        for value, required in ((179999, True), (180000, False), (420000, False), (420001, True), (1999000, True)):
            with self.subTest(value=value):
                self.current = value
                state = profiles.capture(self.gpu)
                self.assertEqual(state["power_limit_mw"], value)
                self.assertEqual(state["xoc"], required)
                self.assertFalse(self.gpu.voltage_xoc_enabled)

    def test_extended_profile_replays_exact_request_through_common_setter(self):
        state = self.state(1999000, True)
        result = profiles.restore(self.gpu, state, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in result), result)
        self.gpu.power_limit_bounds_mw.assert_called_with(xoc=True)
        self.gpu.set_power_limit_mw.assert_called_once_with(1999000)

    def test_non_xoc_profile_cannot_introduce_an_extended_request(self):
        result = profiles.restore(self.gpu, self.state(420001), apply_curve=False)
        self.assertFalse(result[0][0])
        self.gpu.power_limit_bounds_mw.assert_called_with(xoc=False)
        self.gpu.set_power_limit_mw.assert_not_called()

    def test_xoc_profile_above_1999_w_is_rejected_before_writing(self):
        result = profiles.restore(self.gpu, self.state(1999001, True), apply_curve=False)
        self.assertFalse(result[0][0])
        self.gpu.set_power_limit_mw.assert_not_called()

    def test_existing_carryover_can_stay_or_move_toward_normal_without_xoc(self):
        self.current = 600000
        for value in (600000, 599999, 420000, 180000):
            self.assertIsNone(profiles.preflight(self.gpu, self.state(value), apply_curve=False))
        self.assertIsNotNone(profiles.preflight(self.gpu, self.state(600001), apply_curve=False))

    def test_unavailable_live_capability_keeps_the_ordinary_profile_range(self):
        self.gpu.power_limit_bounds_mw.side_effect = None
        self.gpu.power_limit_bounds_mw.return_value = (180000, 420000)
        self.assertIsNotNone(profiles.preflight(self.gpu, self.state(420001, True), apply_curve=False))
        self.assertIsNone(profiles.preflight(self.gpu, self.state(420000), apply_curve=False))

    def test_bounds_read_failure_stops_before_any_setting_is_written(self):
        self.gpu.power_limit_bounds_mw.side_effect = OSError("temporary read failure")
        result = profiles.restore(self.gpu, {**self.state(500000, True), "volt_boost_pct": 5}, apply_curve=False)
        self.assertFalse(result[0][0])
        self.gpu.set_power_limit_mw.assert_not_called()
        self.gpu.set_voltage_boost.assert_not_called()

    def test_malformed_milliwatts_cannot_be_hidden_by_xoc_bounds(self):
        for value in (True, 420000.25, "420001", float("nan"), float("inf")):
            with self.subTest(value=value):
                result = profiles.restore(self.gpu, self.state(value, True), apply_curve=False)
                self.assertFalse(result[0][0])
        self.gpu.set_power_limit_mw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
