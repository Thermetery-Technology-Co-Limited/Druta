# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""A fan undo point never needs or writes an unrelated GPU tuning control."""
import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from druta import profiles
from tests.test_tune_profiles import hardware


class FanProfileScopeTests(unittest.TestCase):
    def setUp(self):
        self.gpu, self.rail, self.calls = hardware()
        self.gpu.read_vf_curve.side_effect = RuntimeError("curve unavailable")
        self.gpu.vf_curve_applicable = Mock(side_effect=AssertionError("unrelated V/F query"))
        self.gpu._legacy_p0_owned = True
        self.gpu.release_legacy_p0 = Mock(side_effect=AssertionError("do not release P0"))

    def test_capture_reads_only_fan_policy_and_metadata(self):
        state = profiles.capture_fan(self.gpu)
        self.assertFalse(profiles.incomplete(state))
        self.assertEqual(state["scope"], "fan")
        self.assertEqual(state["fan_control_state"], self.gpu.read_fan_control_state.return_value)
        self.assertEqual(state["device"]["uuid"], "GPU-A")
        self.assertNotIn("vf_applicable", state)
        self.assertNotIn("vf_deltas", state)
        for name in ("read", "read_vf_curve", "vfp_layout", "mem_offset_scale",
                     "read_voltage_boost", "read_volt_rail_limits", "read_rail_offset_mv",
                     "read_clk_domain_offsets", "vf_curve_applicable"):
            getattr(self.gpu, name).assert_not_called()

    def test_json_round_trip_restores_fans_only_and_preserves_owned_p0(self):
        state = json.loads(json.dumps(profiles.capture_fan(self.gpu)))
        self.assertIsNone(profiles.preflight(self.gpu, state, self.rail))
        result = profiles.restore(self.gpu, state, rail=self.rail)
        self.assertTrue(all(ok for ok, _ in result), result)
        self.assertEqual(self.calls, [("restore_fan_control_state", (state["fan_control_state"],), {})])
        self.gpu.vf_curve_applicable.assert_not_called()
        self.gpu.release_legacy_p0.assert_not_called()
        self.assertTrue(self.gpu._legacy_p0_owned)
        self.assertIn("fan policy only", profiles.summarize(state))

    def test_unreadable_fan_policy_blocks_restore(self):
        for failure in (None, RuntimeError("fan read failed")):
            with self.subTest(failure=failure):
                self.gpu.read_fan_control_state.side_effect = failure
                self.gpu.read_fan_control_state.return_value = None
                state = profiles.capture_fan(self.gpu)
                self.assertTrue(profiles.incomplete(state))
                self.assertFalse(profiles.restore(self.gpu, state)[0][0])
        self.assertEqual(self.calls, [])

    def test_missing_reader_or_restorer_is_an_incomplete_snapshot(self):
        for name in ("read_fan_control_state", "restore_fan_control_state"):
            with self.subTest(name=name):
                self.setUp()
                setattr(self.gpu, name, None)
                state = profiles.capture_fan(self.gpu)
                self.assertTrue(profiles.incomplete(state))
                self.assertFalse(profiles.restore(self.gpu, state)[0][0])
                self.assertEqual(self.calls, [])

    def test_invalid_policy_is_rejected_before_any_writer(self):
        state = profiles.capture_fan(self.gpu)
        for mutate in (
            lambda s: s["fan_control_state"].update(source="unknown"),
            lambda s: s["fan_control_state"].update(fans=[]),
            lambda s: s["fan_control_state"]["fans"][0].update(manual="yes"),
            lambda s: s["fan_control_state"]["fans"][0].update(level=float("nan")),
            lambda s: s["fan_control_state"]["fans"][0].update(level=101),
        ):
            malformed = copy.deepcopy(state)
            mutate(malformed)
            self.assertTrue(profiles.incomplete(malformed))
            self.assertFalse(profiles.restore(self.gpu, malformed)[0][0])
        self.assertEqual(self.calls, [])

    def test_scope_cannot_hide_any_unrelated_fields_even_empty_ones(self):
        state = profiles.capture_fan(self.gpu)
        for field, value in (("vf_deltas", {}), ("vf_applicable", False),
                             ("core_off_mhz", None), ("xoc", False),
                             ("i2c", None), ("current_limits_ma", {}),
                             ("new_unrecognized_control", 10)):
            with self.subTest(field=field):
                invalid = dict(state, **{field: value})
                self.assertIn("unrelated", profiles.preflight(self.gpu, invalid))
                self.assertFalse(profiles.restore(self.gpu, invalid)[0][0])
        for scope in ("unknown", "", True):
            self.assertFalse(profiles.restore(self.gpu, dict(state, scope=scope))[0][0])
        self.assertFalse(profiles.restore(self.gpu, dict(state, schema=1))[0][0])
        self.assertEqual(self.calls, [])

    def test_wrong_card_or_missing_identity_blocks_fan_restore(self):
        state = profiles.capture_fan(self.gpu)
        for field in ("uuid", "vbios", "driver"):
            invalid = copy.deepcopy(state)
            invalid["device"][field] = "other"
            self.assertFalse(profiles.restore(self.gpu, invalid)[0][0])
        invalid = dict(state, device={})
        self.assertFalse(profiles.restore(self.gpu, invalid)[0][0])
        self.assertEqual(self.calls, [])

    def test_fan_writer_failure_does_not_continue_to_other_settings(self):
        state = profiles.capture_fan(self.gpu)
        self.gpu.restore_fan_control_state.side_effect = RuntimeError("fan write failed")
        result = profiles.restore(self.gpu, state)
        self.assertEqual(result, [(False, "fan: fan write failed")])
        self.assertEqual(self.calls, [])
        self.gpu.release_legacy_p0.assert_not_called()

    def test_autosave_scope_round_trip_and_unknown_scope_refusal(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(profiles, "DIR", directory):
            name, path, missing = profiles.autosave(self.gpu, "lock P0 and max fan", scope="fan")
            self.assertFalse(missing)
            self.assertTrue(path.endswith(name + ".json"))
            state = profiles.load(name)
            self.assertEqual(state["scope"], "fan")
            self.assertTrue(all(ok for ok, _ in profiles.restore(self.gpu, state)))
            with self.assertRaisesRegex(ValueError, "scope"):
                profiles.autosave(self.gpu, "bad", scope="unknown")

    def test_full_snapshot_still_reports_missing_curve(self):
        self.gpu.vf_curve_applicable = Mock(return_value=True)
        state = profiles.capture(self.gpu, self.rail)
        self.assertTrue(any("V/F delta table NOT captured" in item
                            for item in profiles.incomplete(state)))


if __name__ == "__main__":
    unittest.main()
