# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""MP offset verification cannot pass without confirmed restoration."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from druta import railctl


class MPVerifyRestoreTests(unittest.TestCase):
    def setUp(self):
        self.rail = railctl.Rail.__new__(railctl.Rail)
        self.rail.p = SimpleNamespace(read_only=False, wreg=0x23, wbytes=2, wbits=(7, 0),
                                     lsb_mv=6.25, min_loaded_mv=800,
                                     rungs=[6.25, 12.5, 25.0])
        self.rail.addr7 = 0x20
        self.rail.present = Mock(return_value=True)
        # A negative starting offset exercises two's-complement restoration;
        # the captured upper transaction bits must also be restored.
        self.rail.read = Mock(side_effect=[0xABFE, 0xABFE])
        self.rail.read_vout = Mock(return_value=737.5)
        self.rail._sample = Mock(side_effect=[(0.0, 1.0), (6.25, 1.0)])
        self.rail.set_offset_mv = Mock(return_value=(True, "written"))
        self.rail._restore_word = Mock(return_value=(True, "restored"))
        self.log = Mock()
        self.sleep = patch("druta.railctl.time.sleep").start()
        self.addCleanup(patch.stopall)

    def verify(self):
        return self.rail.verify(acknowledged=True, log=self.log,
                                ref=lambda: 1050.0)

    def assert_restoration_attempted(self):
        self.assertEqual(self.rail._restore_word.call_args,
                         call(0xABFE))
        self.assertEqual(self.rail.read.call_args, call(0x23, 2))

    def assert_restore_failed(self):
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED", message)
        self.assertNotIn("Restored to", message)
        self.assertFalse(any(c.args[0].startswith("restored to")
                             for c in self.log.call_args_list))
        self.assert_restoration_attempted()
        return message, ladder

    def test_response_passes_only_after_exact_original_word_restored(self):
        ok, message, ladder = self.verify()
        self.assertTrue(ok)
        self.assertIn("Restored to -12.50 mV", message)
        self.assertTrue(ladder[0]["moved"])
        self.assertEqual(self.rail.set_offset_mv.call_args_list,
                         [call(-6.25, acknowledged=True)])
        self.assert_restoration_attempted()

    def test_sub_800mv_reference_does_not_block_response_verification(self):
        ok, message, ladder = self.rail.verify(acknowledged=True, ref=lambda: 737.5)
        self.assertTrue(ok, message)
        self.assertTrue(ladder[0]['moved'])
        self.assertIn('tested operating point', message)
        self.assertNotIn('CONFIRMED under load', message)
        self.rail.set_offset_mv.assert_called_once()
        self.assert_restoration_attempted()

    def test_missing_reference_uses_measured_rail_without_idle_override(self):
        self.rail._sample.side_effect = [(737.5, 1.0), (743.75, 1.0)]
        ok, message, ladder = self.rail.verify(acknowledged=True, ref=lambda: None)
        self.assertTrue(ok, message)
        self.assertTrue(ladder[0]['moved'])
        self.assertEqual(self.rail._sample.call_args_list[-1], call(ref=None))
        self.assert_restoration_attempted()

    def test_unreadable_rail_still_refuses_without_any_write(self):
        self.rail._sample.side_effect = [(None, None), (None, None)]
        ok, message, ladder = self.rail.verify(acknowledged=True, ref=lambda: None)
        self.assertFalse(ok)
        self.assertIn('could not read the rail', message)
        self.rail.set_offset_mv.assert_not_called()
        self.rail._restore_word.assert_not_called()

    def test_unstable_raw_rail_refuses_even_with_constant_reference_difference(self):
        self.rail.read_vout.side_effect = [737.5, 750.0] * 5
        reference = Mock(side_effect=[737.5, 750.0] * 5)
        ok, message, _ = self.rail.verify(acknowledged=True, ref=reference)
        self.assertFalse(ok)
        self.assertIn('selected rail changed', message)
        self.rail.set_offset_mv.assert_not_called()

    def test_clock_transition_refuses_before_writing(self):
        point = Mock(side_effect=[(8, 300, 405)] * 4 + [(0, 1800, 850)] * 5)
        ok, message, _ = self.rail.verify(acknowledged=True, ref=lambda: 737.5,
                                          operating_point=point)
        self.assertFalse(ok)
        self.assertIn('clocks changed', message)
        self.rail.set_offset_mv.assert_not_called()

    def test_stable_low_voltage_and_clocks_pass_without_a_voltage_floor(self):
        ok, message, _ = self.rail.verify(acknowledged=True, ref=lambda: 737.5,
                                          operating_point=lambda: (0, 1800, 850))
        self.assertTrue(ok, message)
        self.assert_restoration_attempted()

    def test_new_stable_operating_point_after_write_is_inconclusive_and_restored(self):
        point = Mock(side_effect=[(0, 1800, 850)] * 10 + [(0, 1785, 850)] * 9)
        ok, message, _ = self.rail.verify(acknowledged=True, ref=lambda: 737.5,
                                          operating_point=point)
        self.assertFalse(ok)
        self.assertIn('operating point changed', message)
        self.assert_restoration_attempted()

    def test_intermittent_reference_refuses_before_writing(self):
        reference = Mock(side_effect=[737.5] * 8 + [None])
        ok, message, _ = self.rail.verify(acknowledged=True, ref=reference)
        self.assertFalse(ok)
        self.assertIn('intermittent', message)
        self.rail.set_offset_mv.assert_not_called()

    def test_refused_restore_overrides_success_even_if_readback_matches(self):
        self.rail._restore_word.return_value = (False, "identity changed")
        message, _ = self.assert_restore_failed()
        self.assertIn("identity changed", message)
        self.assertEqual(self.rail.read.call_count, 2)

    def test_response_noise_cannot_be_reported_as_movement(self):
        self.rail.p.rungs = [6.25, 12.5]
        self.rail._sample.side_effect = [(0, 1), (6.25, 15), (12.5, 15)]
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertEqual(len(ladder), 2)
        self.assertTrue(all(row["threshold_mv"] == 15 for row in ladder))
        self.assertTrue(all(not row["moved"] for row in ladder))
        self.assert_restoration_attempted()

    def test_wrong_restore_field_overrides_success(self):
        self.rail.read.side_effect = [0xABFE, 0x00FF]
        message, _ = self.assert_restore_failed()
        self.assertIn("expected word 0xABFE", message)

    def test_unreadable_restore_field_overrides_success(self):
        self.rail.read.side_effect = [0xABFE, None]
        message, _ = self.assert_restore_failed()
        self.assertIn("unreadable", message)

    def test_restore_exception_still_attempts_readback_and_fails(self):
        self.rail._restore_word.side_effect = RuntimeError("bus gone")
        message, _ = self.assert_restore_failed()
        self.assertIn("bus gone", message)
        self.assertEqual(self.rail.read.call_count, 2)

    def test_restore_readback_exception_overrides_success(self):
        self.rail.read.side_effect = [0xABFE, RuntimeError("read exploded")]
        message, _ = self.assert_restore_failed()
        self.assertIn("read exploded", message)

    def test_measurement_read_failure_stops_ladder_and_restores(self):
        self.rail._sample.side_effect = [(0.0, 1.0), (None, None)]
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn("INCONCLUSIVE", message)
        self.assertIn("Restored to", message)
        self.assertTrue(ladder[0]["read_failed"])
        self.assertEqual(self.rail.set_offset_mv.call_count, 1)
        self.assert_restoration_attempted()

    def test_measurement_exception_restores_and_returns_failure(self):
        self.rail._sample.side_effect = [(0.0, 1.0), RuntimeError("read lost")]
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("read lost", message)
        self.assertIn("Restored to", message)
        self.assert_restoration_attempted()

    def test_early_rung_refusal_does_not_bypass_failed_restoration(self):
        self.rail.set_offset_mv.return_value = (False, "no headroom")
        self.rail._restore_word.return_value = (False, "restore failed")
        message, ladder = self.assert_restore_failed()
        self.assertIn("no headroom", message)
        self.assertEqual(ladder, [{"rung_mv": 6.25,
                                   "refused": "no headroom"}])

    def test_early_rung_refusal_restores_and_preserves_inconclusive_verdict(self):
        self.rail.set_offset_mv.return_value = (False, "no headroom")
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("INCONCLUSIVE", message)
        self.assertIn("Restored to", message)
        self.assert_restoration_attempted()

    def test_rung_write_exception_also_attempts_restoration(self):
        self.rail.set_offset_mv.side_effect = RuntimeError("write lost")
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("write lost", message)
        self.assert_restoration_attempted()


if __name__ == "__main__":
    unittest.main()
