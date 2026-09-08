# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Profile regression tests using fake GPUs and an in-memory I2C transport."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import profiles
from tests import test_mp2888_discovery as mp_discovery
from tests import test_tune_profiles as tune_profiles


class ProfilePreflightTests(unittest.TestCase):
    def setUp(self):
        self.gpu, self.rail, self.calls = tune_profiles.hardware()
        self.state = profiles.capture(self.gpu, self.rail)

    def assert_rejected_without_writes(self, state, *, rail=None):
        rail = self.rail if rail is None else rail
        error = profiles.preflight(self.gpu, state, rail)
        self.assertIsInstance(error, str)
        result = profiles.restore(self.gpu, state, rail=rail, i2c_verified=True)
        self.assertEqual(len(result), 1, result)
        self.assertFalse(result[0][0], result)
        self.assertEqual(self.calls, [])
        return error

    def test_fractional_memory_round_trip_preserves_driver_units(self):
        for scale, units in ((8, 100), (8, -101), (4, 101), (1, -101)):
            with self.subTest(scale=scale, units=units):
                self.gpu.mem_offset_scale.return_value = scale, "true MHz"
                self.gpu.read.return_value["mem_off"] = units
                state = json.loads(json.dumps(profiles.capture(self.gpu)))
                results = profiles.restore(self.gpu, state)
                self.assertTrue(all(ok for ok, _ in results), results)
                self.gpu.set_clock_offset.assert_any_call(2, units / scale)
                self.assertIn(f"mem {units / scale:+g} MHz", profiles.summarize(state))
                self.gpu.set_clock_offset.reset_mock()

    def test_nonrepresentable_memory_is_rejected_before_voltage_writes(self):
        for value in (0.1, 1 << 31, float("inf"), float("nan"), True, "12.5"):
            with self.subTest(value=value):
                state = copy.deepcopy(self.state)
                state["mem_off_true_mhz"] = value
                self.assert_rejected_without_writes(state)

    def test_invalid_memory_scale_cannot_silently_change_replay_units(self):
        for scale in (0, -1, True, None, float("inf"), "8"):
            with self.subTest(scale=scale):
                self.gpu.mem_offset_scale.return_value = scale, "true MHz"
                self.assert_rejected_without_writes(self.state)

    def test_malformed_vf_payloads_are_rejected_before_any_write(self):
        for deltas in ([30000], "0", {"not-an-index": 30000}, {"-1": 30000},
                       {"0.5": 30000}, {"00": 30000}, {True: 30000},
                       {"0": 30000, 0: 30000}, {"0": True}, {"0": "30000"},
                       {"0": 30000.5}, {"0": float("nan")}, {"0": float("inf")},
                       {"0": 1_000_001}, {"0": -1_000_001}):
            with self.subTest(deltas=deltas):
                state = copy.deepcopy(self.state)
                state["vf_deltas"] = deltas
                self.assertIn("V/F", self.assert_rejected_without_writes(state))

    def test_vf_indices_use_gpu_rows_from_current_layout(self):
        self.gpu.vfp_layout.return_value = SimpleNamespace(gpu_idx=(0, 2))
        # A real table can contain non-GPU rows below its total entry count.
        self.state["vf_deltas"] = {"1": 30000}
        self.assertIn("not GPU points", self.assert_rejected_without_writes(self.state))
        self.state["vf_deltas"] = {"0": 30000, "2": -15000}
        self.assertIsNone(profiles.preflight(self.gpu, self.state, self.rail))

    def test_unreadable_vf_layout_stops_before_other_settings(self):
        self.gpu.vfp_layout.return_value = None
        self.assertIn("layout", self.assert_rejected_without_writes(self.state))

    def test_explicit_curve_skip_does_not_require_live_curve_layout(self):
        self.gpu.vfp_layout.return_value = None
        result = profiles.restore(self.gpu, self.state, apply_curve=False,
                                  rail=self.rail, i2c_verified=True)
        self.assertTrue(all(ok for ok, _ in result), result)
        self.gpu.vfp_layout.assert_not_called()
        self.gpu.apply_vf_deltas.assert_not_called()

    def test_schema_one_subset_and_integer_delta_values_remain_supported(self):
        state = {"schema": 1, "core_off_mhz": 90, "vf_deltas": {"0": 30000.0}}
        self.assertIsNone(profiles.preflight(self.gpu, state))
        result = profiles.restore(self.gpu, state)
        self.assertTrue(all(ok for ok, _ in result), result)
        self.gpu.apply_vf_deltas.assert_called_once_with({0: 30000})
        self.gpu.set_volt_rail_limits.assert_not_called()
        self.rail.set_offset_mv.assert_not_called()

    def test_deterministic_invalid_fields_block_the_entire_profile(self):
        mutations = (
            ("core_off_mhz", "90"), ("core_off_mhz", float("nan")),
            ("core_off_mhz", 1 << 31),
            ("volt_boost_pct", 101), ("power_limit_mw", -1),
            ("fan_pct", 101), ("fan_manual", "false"), ("xoc", 1),
            ("device", []), ("rail_limits_mv", []),
            ("rail_limits_mv", {"0": [1200]}),
            ("clock_domain_offsets_mhz", {"7": float("inf")}),
            ("clock_domain_offsets_mhz", {"7": 2147483.648}),
            ("clock_domain_offsets_mhz", {"7": 2147483.6}),
            ("clock_domain_offsets_mhz", {"oops": 100}),
            ("i2c", []), ("fan_control_state", []),
            ("fan_control_state", {"source": "nvml", "fans": [
                {"manual": True, "level": "75"}]}),
            ("fan_control_state", {"source": "nvml", "fans": [
                {"manual": True, "level": 75.5}]}),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                state = copy.deepcopy(self.state)
                state[key] = value
                self.assert_rejected_without_writes(state)

    def test_mp_offset_checks_actual_recipe_before_bus_access_or_other_writes(self):
        discovery = mp_discovery.DiscoveryTests()
        discovery.setUp()
        discovery.bus.add()
        [rail] = discovery.scan()
        state = profiles.capture(self.gpu, rail)
        state["xoc"] = False
        # 800 mV would wrap the signed offset field; 150 exceeds normal mode.
        for value in (9000, 800, 150, -700, float("inf"), True, "18.75", None):
            with self.subTest(value=value):
                state["i2c"]["offset_mv"] = value
                discovery.bus.reads.clear()
                self.assert_rejected_without_writes(state, rail=rail)
                self.assertEqual(discovery.bus.reads, [])
                self.assertEqual(discovery.bus.writes, [])

    def test_mp_saved_xoc_envelope_is_used_before_live_mode_is_enabled(self):
        self.state["i2c"]["offset_mv"] = 150
        self.state["xoc"] = True
        self.rail.xoc = False
        self.assertIsNone(profiles.preflight(self.gpu, self.state, self.rail))
        self.state["xoc"] = False
        self.assertIn("envelope", self.assert_rejected_without_writes(self.state))

    def test_wrong_offset_recipe_or_controller_type_is_rejected_before_presence_read(self):
        for mutation in (lambda i: i.update(sha256="other"),
                         lambda i: i.update(control={"enabled": False})):
            state = copy.deepcopy(self.state)
            mutation(state["i2c"])
            self.assert_rejected_without_writes(state)
            self.rail.present.assert_not_called()

    def test_invalid_profile_is_rejected_before_automatic_i2c_verification(self):
        flow = tune_profiles.ProfileFlow()
        flow.setUp()
        app = flow.app
        app.verify_i2c_rail = Mock()
        for mutation in (lambda s: s["i2c"].update(offset_mv=9000),
                         lambda s: s.update(vf_deltas={"not-an-index": 30000})):
            with self.subTest(mutation=mutation):
                state = copy.deepcopy(flow.state)
                mutation(state)
                app.begin_profile_load("invalid", state, automatic=True)
                app.verify_i2c_rail.assert_not_called()
                app.autosave_before.assert_not_called()
                app.gpu.set_volt_rail_limits.assert_not_called()
                app.rail.set_offset_mv.assert_not_called()

    def test_live_i2c_dry_run_still_immediately_precedes_i2c_write(self):
        result = profiles.restore(self.gpu, self.state, rail=self.rail, i2c_verified=True)
        self.assertTrue(all(ok for ok, _ in result), result)
        labels = [label for label, _, _ in self.calls]
        index = labels.index("I2C offset")
        self.assertEqual(labels[index - 1], "I2C plan")

    def test_dynamic_i2c_refusal_stops_clocks_and_keeps_prior_voltage_results(self):
        self.rail.plan.side_effect = None
        self.rail.plan.return_value = False, "live predicted voltage exceeds ceiling"
        result = profiles.restore(self.gpu, self.state, rail=self.rail, i2c_verified=True)
        self.assertEqual(result[:3], [(True, "set_volt_rail_limits"),
                                     (True, "set_volt_rail_limits"),
                                     (True, "set_rail_offset_mv")])
        self.assertEqual(result[-1], (False, "live predicted voltage exceeds ceiling"))
        self.rail.set_offset_mv.assert_not_called()
        self.gpu.set_clock_offset.assert_not_called()

    def test_unexpected_late_exception_preserves_completed_step_results(self):
        self.gpu.vf_curve_applicable = Mock(side_effect=[True, RuntimeError("layout vanished")])
        result = profiles.restore(self.gpu, self.state, rail=self.rail, i2c_verified=True)
        self.assertEqual(len(result), len(self.calls) + 1, result)
        self.assertTrue(all(ok for ok, _ in result[:-1]), result)
        self.assertFalse(result[-1][0])
        self.assertIn("layout vanished", result[-1][1])
        self.gpu.apply_vf_deltas.assert_not_called()

    def test_setter_exception_is_reported_without_erasing_other_results(self):
        self.gpu.set_power_limit_mw.side_effect = RuntimeError("power failure")
        result = profiles.restore(self.gpu, self.state, rail=self.rail, i2c_verified=True)
        self.assertIn((False, "power limit: power failure"), result)
        self.assertEqual(result[0], (True, "set_volt_rail_limits"))
        self.assertEqual(result[-1], (True, "apply_vf_deltas"))


if __name__ == "__main__":
    unittest.main()
