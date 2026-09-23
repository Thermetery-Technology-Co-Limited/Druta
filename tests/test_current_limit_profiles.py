"""Current-limit profile round trips without a driver or GUI."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import profiles
from druta.nvbackend import GPU


def fixture():
    return SimpleNamespace(
        static={"name": "fixture"},
        read=Mock(return_value={}),
        mem_offset_scale=Mock(return_value=(1, "")),
        read_fan_control_state=Mock(return_value=None),
        read_fan_manual=Mock(return_value=None),
        read_voltage_boost=Mock(return_value=0),
        read_vf_curve=Mock(return_value=([{"idx": 0, "delta_khz": 0}], None)),
        get_current_limits=Mock(return_value=[
            {"policy": 13, "limit_ma": 300125},
            {"policy": 14, "limit_ma": 120000}]),
        set_current_limit_ma=Mock(return_value=(True, "applied")),
        set_voltage_boost=Mock(return_value=(True, "applied")))


class CurrentLimitProfileTests(unittest.TestCase):
    def test_capture_replays_requested_current_when_effective_limit_differs(self):
        gpu = fixture()
        gpu.get_current_limits.return_value = [
            {"policy": 13, "requested_ma": 300125, "limit_ma": 300000}]
        state = profiles.capture(gpu)
        self.assertEqual(state["current_limits_ma"], {"13": 300125})
        result = profiles.restore(gpu, state, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in result))
        gpu.set_current_limit_ma.assert_called_once_with(13, 300125)

    def test_round_trip_preserves_milliamps(self):
        gpu = fixture()
        state = profiles.capture(gpu)
        self.assertEqual(state["current_limits_ma"], {"13": 300125, "14": 120000})
        self.assertFalse(profiles.incomplete(state))
        result = profiles.restore(gpu, state, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in result))
        self.assertEqual([call.args for call in gpu.set_current_limit_ma.call_args_list],
                         [(13, 300125), (14, 120000)])
        self.assertIn("core limit 300.125 A", profiles.summarize(state))

    def test_old_or_unsupported_profiles_do_not_write_currents(self):
        gpu = fixture()
        profiles.restore(gpu, {}, apply_curve=False)
        gpu.set_current_limit_ma.assert_not_called()
        gpu.get_current_limits.return_value = []
        self.assertEqual(profiles.capture(gpu)["current_limits_ma"], {})
        del gpu.get_current_limits
        self.assertEqual(profiles.capture(gpu)["current_limits_ma"], {})

    def test_capture_failure_is_visible(self):
        gpu = fixture()
        gpu.get_current_limits.side_effect = RuntimeError("read failed")
        state = profiles.capture(gpu)
        self.assertEqual(state["current_limits_ma"], {})
        self.assertTrue(any("Current limits NOT captured" in s for s in profiles.incomplete(state)))

    def test_supported_backend_read_failure_is_not_treated_as_unsupported(self):
        gpu = fixture()
        gpu.get_current_limits.return_value = []
        gpu._current_limit_error = "policy read failed"
        state = profiles.capture(gpu)
        self.assertTrue(any("policy read failed" in s for s in profiles.incomplete(state)))

    def test_partial_current_capture_keeps_valid_row_and_reports_failed_present_row(self):
        gpu = fixture()
        gpu.get_current_limits.return_value = [{"policy": 13, "limit_ma": 300125}]
        gpu._current_limit_error = "policy 14: invalid current record"
        state = profiles.capture(gpu)
        self.assertEqual(state["current_limits_ma"], {"13": 300125})
        self.assertTrue(any("policy 14" in s for s in profiles.incomplete(state)))

    def test_absent_second_current_does_not_make_capture_incomplete(self):
        gpu = fixture()
        gpu.get_current_limits.return_value = [{"policy": 13, "limit_ma": 300125}]
        gpu._current_limit_error = ""
        state = profiles.capture(gpu)
        self.assertEqual(state["current_limits_ma"], {"13": 300125})
        self.assertFalse(profiles.incomplete(state))

    def test_unsupported_generations_omit_currents_without_making_snapshot_incomplete(self):
        for generation in (GPU.ARCH_KEPLER, GPU.ARCH_MAXWELL):
            with self.subTest(generation=generation):
                gpu = fixture()
                gpu._current_limit_generation_policies = Mock(
                    return_value=GPU.CURRENT_LIMIT_GENERATION_POLICIES.get(generation, {}))
                gpu.get_current_limits.side_effect = AssertionError("unsupported reader called")
                gpu._current_limit_error = "NVAPI unavailable or GPU generation unsupported"
                state = profiles.capture(gpu)
                self.assertEqual(state["current_limits_ma"], {})
                self.assertFalse(profiles.incomplete(state))
                gpu.get_current_limits.assert_not_called()

    def test_applicable_generation_with_broken_api_still_marks_snapshot_incomplete(self):
        gpu = fixture()
        gpu._current_limit_generation_policies = Mock(
            return_value=GPU.CURRENT_LIMIT_GENERATION_POLICIES[GPU.ARCH_TURING])
        gpu.get_current_limits.return_value = []
        gpu._current_limit_error = "NVAPI unavailable or GPU generation unsupported"
        state = profiles.capture(gpu)
        gpu.get_current_limits.assert_called_once_with()
        self.assertTrue(any("Current limits NOT captured" in s
                            for s in profiles.incomplete(state)))

    def test_applicability_read_exception_does_not_silently_omit_currents(self):
        gpu = fixture()
        gpu._current_limit_generation_policies = Mock(side_effect=RuntimeError("architecture failed"))
        state = profiles.capture(gpu)
        self.assertTrue(any("architecture failed" in s for s in profiles.incomplete(state)))
        gpu.get_current_limits.assert_not_called()

    def test_profile_cannot_enable_xoc_or_bypass_setter_rejection(self):
        gpu = fixture()
        gpu.voltage_xoc_enabled = False
        gpu.set_current_limit_ma.return_value = (False, "XOC required")
        result = profiles.restore(gpu, {"current_limits_ma": {"13": 600000},
                                      "voltage_xoc_enabled": True}, apply_curve=False)
        self.assertEqual(result, [(False, "XOC required")])
        self.assertFalse(gpu.voltage_xoc_enabled)
        gpu.set_current_limit_ma.assert_called_once_with(13, 600000)

    def test_one_failed_limit_does_not_hide_other_result(self):
        gpu = fixture()
        gpu.set_current_limit_ma.side_effect = [RuntimeError("rejected"), (True, "restored")]
        results = profiles.restore(gpu, {"current_limits_ma": {"13": 300000, "14": 120000}},
                                   apply_curve=False)
        self.assertFalse(results[0][0])
        self.assertEqual(results[1], (True, "restored"))

    def test_invalid_mapping_reports_failure(self):
        gpu = fixture()
        self.assertEqual(profiles.restore(gpu, {"current_limits_ma": [300000]}, apply_curve=False),
                         [(False, "current limits: invalid profile data")])
        gpu.set_current_limit_ma.assert_not_called()

    def test_summary_keeps_milliamps_near_api_maximum(self):
        state = {"current_limits_ma": {"13": 5000999}}
        self.assertIn("core limit 5000.999 A", profiles.summarize(state))


if __name__ == "__main__":
    unittest.main()
