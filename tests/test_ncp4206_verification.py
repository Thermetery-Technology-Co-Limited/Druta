# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded NCP4206 verification using fake controller state and physical VMON."""
import unittest
from unittest.mock import Mock, patch

from druta import ncp4206 as n
from tests import test_ncp4206


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.rail = test_ncp4206.NCPTests().rail()
        self.original = dict(self.rail.regs)
        self.base = 737.5
        self.quantum = 1000 / 512
        self.rise = 7.8125  # Detectable response below half of a 25 mV command.
        self.cancelled = False
        self.point = (0, 1000, 3000)
        self.voltage_reads = 0
        self.rail._verification_vmon = self.vmon
        self.sleep = patch('druta.ncp4206.time.sleep').start()
        self.addCleanup(patch.stopall)

    def vmon(self):
        self.voltage_reads += 1
        active = bool(self.rail.regs[0xd3] & 8)
        return self.base + (self.rise if active else 0), self.quantum

    def verify(self, **kwargs):
        return self.rail.verify(acknowledged=True,
                                operating_point=lambda: self.point,
                                cancelled=lambda: self.cancelled, **kwargs)

    def assert_restored(self):
        self.assertEqual(self.rail.regs, self.original)
        self.assertTrue(self.rail._verification_restore_ok)

    def test_sub_800_vmon_uses_direct_i2c_and_requires_full_aba_windows(self):
        ref = Mock(side_effect=AssertionError('NVAPI must not be sampled'))
        ok, message, ladder = self.verify(ref=ref)
        self.assertTrue(ok, message)
        ref.assert_not_called()
        self.assertEqual(self.voltage_reads, 75)
        self.assertEqual(len(ladder[0]['samples_mv']), 25)
        self.assertEqual(len(ladder[0]['restored_samples_mv']), 25)
        self.assertEqual(ladder[0]['delta_mv'], self.rise)
        self.assertEqual(ladder[0]['reversal_mv'], self.rise)
        self.assertEqual(ladder[0]['target_mv'], 762.5)
        self.assert_restored()

    def test_flat_rail_is_inconclusive_and_all_three_trials_are_bounded(self):
        self.rise = 0
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn('INCONCLUSIVE', message)
        self.assertEqual([r['target_mv'] for r in ladder], [762.5, 775, 787.5])
        self.assert_restored()

    def test_noisy_response_cannot_pass_even_with_high_median(self):
        def noisy():
            self.voltage_reads += 1
            active = bool(self.rail.regs[0xd3] & 8)
            return self.base + (20 if active and self.voltage_reads % 2 else 0), self.quantum
        self.rail._verification_vmon = noisy
        ok, _, _ = self.verify()
        self.assertFalse(ok)
        self.assert_restored()

    def test_baseline_noise_larger_than_command_step_can_pass_above_noise(self):
        def noisy():
            self.voltage_reads += 1
            active = bool(self.rail.regs[0xd3] & 8)
            # 10 mV of measured ripple is larger than the 6.25 mV command
            # resolution. A reversible 20 mV physical response still clears it.
            position = (self.voltage_reads - 1) % 25
            ripple = 0 if position == 0 else (5 if position % 2 else -5)
            return self.base + (20 if active else 0) + ripple, self.quantum
        self.rail._verification_vmon = noisy
        ok, message, ladder = self.verify()
        self.assertTrue(ok, message)
        self.assertEqual(ladder[0]['response_noise_mv'], 10)
        self.assertGreater(ladder[0]['delta_mv'], 10)
        self.assertGreater(ladder[0]['reversal_mv'], 10)
        self.assert_restored()

    def test_baseline_noise_larger_than_response_remains_inconclusive(self):
        def noisy():
            self.voltage_reads += 1
            active = bool(self.rail.regs[0xd3] & 8)
            position = (self.voltage_reads - 1) % 25
            ripple = 0 if position == 0 else (5 if position % 2 else -5)
            return self.base + (5 if active else 0) + ripple, self.quantum
        self.rail._verification_vmon = noisy
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn('INCONCLUSIVE', message)
        self.assertEqual(len(ladder), 3)
        self.assertTrue(all(not rung['moved'] for rung in ladder))
        self.assert_restored()

    def test_baseline_point_transition_refuses_without_writes(self):
        def voltage():
            self.point = (0, 1013, 3000)
            return self.base, self.quantum
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('clocks changed', message)
        self.assertEqual(self.rail.calls, [])

    def test_missing_operating_point_refuses_before_writes(self):
        ok, message, _ = self.rail.verify(acknowledged=True)
        self.assertFalse(ok)
        self.assertIn('held P0', message)
        self.assertEqual(self.rail.calls, [])

    def test_control_change_during_final_sample_is_a_restore_failure(self):
        def voltage():
            result = self.vmon()
            if self.voltage_reads == 75:
                self.rail.regs[0x21] = n.encode_vid(750)
            return result
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('restoration failed', message)
        self.assertFalse(self.rail._verification_restore_ok)

    def test_zero_or_negative_core_or_memory_refuses_before_writes(self):
        for point in ((0, 0, 3000), (0, -1, 3000), (0, 1000, 0), (0, 1000, -1)):
            with self.subTest(point=point):
                self.point = point
                ok, message, _ = self.verify()
                self.assertFalse(ok)
                self.assertIn('nonpositive', message)
                self.assertEqual(self.rail.calls, [])
                self.assertFalse(self.rail._verification_write_attempted)

    def test_p2_is_refused_before_writes(self):
        self.point = (2, 1000, 3000)
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('not held in P0', message)
        self.assertEqual(self.rail.calls, [])

    def test_cancel_in_last_trial_sample_restores_and_cannot_pass(self):
        def voltage():
            result = self.vmon()
            if self.voltage_reads == 50:
                self.cancelled = True
            return result
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('cancelled', message)
        self.assertEqual(self.voltage_reads, 50)
        self.assert_restored()

    def test_cancel_in_baseline_never_writes(self):
        self.sleep.side_effect = lambda _: setattr(self, 'cancelled', True)
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('nothing written', message)
        self.assertEqual(self.rail.calls, [])

    def test_restore_that_reports_success_but_does_not_restore_fails_readback(self):
        restore = self.rail.restore_control
        self.rail.restore_control = lambda state, **kw: ((True, 'fake')
            if kw.get('recovery') else restore(state, **kw))
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('independent readback mismatch', message)
        self.assertFalse(self.rail._verification_restore_ok)

    def test_restore_failure_wins_over_detected_voltage_response(self):
        restore = self.rail.restore_control
        self.rail.restore_control = lambda state, **kw: ((False, 'identity disappeared')
            if kw.get('recovery') else restore(state, **kw))
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('restoration failed: identity disappeared', message)
        self.assertFalse(self.rail._verification_restore_ok)

    def test_persistent_voltage_rise_after_register_restoration_cannot_pass(self):
        self.rise = 15.625
        self.rail._verification_vmon = lambda: (
            self.base + (self.rise if self.rail.calls else 0), self.quantum)
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('did not return', message)
        self.assert_restored()

    def test_return_in_band_still_requires_downward_response_above_resolution(self):
        def voltage():
            # 6.25 mV is within the return band, but a 1.5625 mV reversal
            # cannot demonstrate that the provisional command caused the rise.
            value = self.base
            if self.rail.calls:
                value += self.rise if self.rail.regs[0xd3] & 8 else 6.25
            return value, self.quantum
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('downward response', message)
        self.assert_restored()

    def test_actual_vmon_quantum_sets_overshoot_allowance(self):
        self.rise = 27
        self.quantum = 1000 / 256
        ok, message, ladder = self.verify()
        self.assertTrue(ok, message)
        self.assertEqual(ladder[0]['overshoot_allowance_mv'], self.quantum)
        self.assert_restored()
        self.rail.calls.clear()
        self.rise = 30
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn('exceeded the command', message)
        self.assertEqual(len(ladder), 1)
        self.assert_restored()

    def test_baseline_near_ceiling_never_increases_normal_verification_bounds(self):
        self.base = 1275
        self.rail.xoc = True
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('lacks headroom', message)
        self.assertEqual(self.rail.calls, [])

    def test_read_error_after_write_restores_exact_original_control(self):
        self.rail._verification_vmon = lambda: ((self.base, self.quantum)
            if not self.rail.calls else (None, self.quantum))
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('VMON read failed', message)
        self.assert_restored()

    def test_original_active_command_is_restored_exactly(self):
        self.rail.regs[0x21] = n.encode_vid(750)
        self.rail.regs[0xd2] |= 8
        self.rail.regs[0xd3] |= 8
        self.original = dict(self.rail.regs)
        self.rail._verification_vmon = lambda: (
            self.base + (self.rise if self.rail.regs[0x21] != self.original[0x21] else 0),
            self.quantum)
        ok, message, _ = self.verify()
        self.assertTrue(ok, message)
        self.assert_restored()

    def test_point_transition_during_response_restores_and_refuses(self):
        def voltage():
            result = self.vmon()
            if self.rail.calls:
                self.point = (0, 1013, 3000)
            return result
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('clocks changed', message)
        self.assert_restored()

    def test_point_transition_after_restoration_cannot_pass(self):
        def voltage():
            result = self.vmon()
            if self.rail.calls and not self.rail.regs[0xd3] & 8:
                self.point = (0, 1013, 3000)
            return result
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('clocks changed', message)
        self.assert_restored()

    def test_cancel_during_restored_window_cannot_pass(self):
        def voltage():
            result = self.vmon()
            if self.voltage_reads == 75:
                self.cancelled = True
            return result
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('cancelled', message)
        self.assert_restored()

    def test_mode_change_after_write_restores_original_control(self):
        def voltage():
            result = self.vmon()
            if self.rail.calls:
                self.rail.xoc = True
            return result
        self.rail._verification_vmon = voltage
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('XOC mode changed', message)
        self.assert_restored()

    def test_linear11_resolution_is_derived_from_each_raw_exponent(self):
        self.rail._verification_vmon = n.NCP4206._verification_vmon.__get__(self.rail)
        for exponent, mantissa in ((-9, 384), (-10, 768), (-8, 192)):
            with self.subTest(exponent=exponent):
                self.rail.regs[0xd7] = ((exponent & 31) << 11) | mantissa
                value, quantum = self.rail._verification_vmon()
                self.assertEqual(value, 750)
                self.assertEqual(quantum, 2 ** exponent * 1000)
        self.rail.regs[0xd7] = None
        with self.assertRaisesRegex(ValueError, 'read failed'):
            self.rail._verification_vmon()


if __name__ == '__main__':
    unittest.main()
