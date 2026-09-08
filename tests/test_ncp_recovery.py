# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover partial NCP overrides and revoke stale UI verification without hardware."""

import unittest
from unittest.mock import Mock, patch

from druta.druta import Druta
from tests import test_ncp4206


class NcpAutoRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.rail = test_ncp4206.NCPTests().rail()

    def mixed_modes(self):
        self.rail.regs.update({0x21: 58, 0xd2: 0xda, 0xd3: 0x63})

    def test_auto_recovers_after_transient_write_and_rollback_failures(self):
        rail = self.rail
        write = rail._raw_write
        failed = [False, False]

        def transient_failure(reg, value, width):
            if reg == 0xd3 and value & 8 and not failed[0]:
                failed[0] = True
                return False
            if reg == 0xd2 and not value & 8 and failed[0] and not failed[1]:
                failed[1] = True
                return False
            return write(reg, value, width)

        rail._raw_write = transient_failure
        ok, message = rail.set_voltage_mv(1250, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn('restoration also failed', message)
        self.assertEqual((rail.regs[0xd2], rail.regs[0xd3]), (0x7a, 0x72))

        rail._raw_write = write
        rail.calls.clear()
        self.assertFalse(rail.set_voltage_mv(1200, acknowledged=True)[0])
        self.assertEqual(rail.calls, [])  # Normal Apply cannot bypass the mismatch.
        command = rail.regs[0x21]
        self.assertTrue(rail.reset()[0])
        self.assertEqual(rail.calls, [(0xd2, 0x72, 1), (0xd3, 0x72, 1)])
        self.assertEqual(rail.regs[0x21], command)
        self.assertFalse(rail.capture_control()['enabled'])

    def test_recovery_preserves_each_registers_other_bits_and_command(self):
        self.mixed_modes()
        before = dict(self.rail.regs)
        self.assertTrue(self.rail.reset()[0])
        expected = dict(before)
        expected[0xd2] &= ~8
        expected[0xd3] &= ~8
        self.assertEqual(self.rail.regs, expected)
        self.assertEqual(self.rail.calls, [(0xd2, 0xd2, 1), (0xd3, 0x63, 1)])

    def test_unidentified_controller_is_never_written(self):
        self.mixed_modes()
        self.rail.regs[0x9a] = 0
        ok, message = self.rail.reset()
        self.assertFalse(ok)
        self.assertIn('identity', message)
        self.assertEqual(self.rail.calls, [])

    def test_missing_configuration_refuses_before_either_write(self):
        self.mixed_modes()
        self.rail.regs[0xd3] = None
        self.assertFalse(self.rail.reset()[0])
        self.assertEqual(self.rail.calls, [])

    def test_recovery_attempts_both_halves_and_reports_failed_write(self):
        self.mixed_modes()
        write = self.rail._raw_write
        calls = []

        def fail_first(reg, value, width):
            calls.append((reg, value, width))
            return False if reg == 0xd2 else write(reg, value, width)

        self.rail._raw_write = fail_first
        ok, message = self.rail.reset()
        self.assertFalse(ok)
        self.assertIn('Auto recovery failed', message)
        self.assertEqual(calls, [(0xd2, 0xd2, 1), (0xd3, 0x63, 1)])
        self.rail._raw_write = write
        self.assertTrue(self.rail.reset()[0])

    def test_acknowledged_write_with_wrong_readback_is_not_success(self):
        self.mixed_modes()
        self.rail._raw_write = Mock(return_value=True)  # Device ignores the writes.
        ok, message = self.rail.reset()
        self.assertFalse(ok)
        self.assertIn('readback', message)
        self.assertEqual(self.rail._raw_write.call_count, 2)
        self.assertEqual(self.rail.regs[0xd2], 0xda)

    def test_final_readback_checks_both_modes_after_the_second_write(self):
        self.mixed_modes()
        write = self.rail._raw_write

        def mode_changes_again(reg, value, width):
            result = write(reg, value, width)
            if reg == 0xd3:
                self.rail.regs[0xd2] |= 8
            return result

        self.rail._raw_write = mode_changes_again
        ok, message = self.rail.reset()
        self.assertFalse(ok)
        self.assertIn('0xD2 Auto readback mismatch', message)


class NcpVerificationLossTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = object()
        self.app.rail = test_ncp4206.NCPTests().rail()
        self.assertTrue(self.app.rail.present())
        self.app._gpu_gen = 1
        self.app._i2c_busy = False
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.app.log = Mock()
        self.app.guard = Mock(return_value=True)
        self.app.i2c_gate = Mock(return_value=(True, ''))
        self.app.rail.set_voltage_mv = Mock()
        self.app.autosave_before = Mock()

    def assert_revoked_after(self, telemetry):
        self.app.rail.telemetry = telemetry
        with patch('druta.druta.dpg.does_item_exist', return_value=True):
            self.assertIsNone(self.app.i2c_rail_text(1000))
            self.assertFalse(self.app.i2c_verified())
            self.assertIsNone(self.app._i2c_verified_for)
            # A successful read after reconnection cannot revive the old pass.
            self.app.rail.telemetry = Mock(return_value={'vout_mv': 1000, 'target_mv': 1100})
            self.assertIsNotNone(self.app.i2c_rail_text(1000))
            self.assertFalse(self.app.i2c_verified())
        self.app.apply_i2c_rail(1100)
        self.app.rail.set_voltage_mv.assert_not_called()
        self.app.autosave_before.assert_not_called()

    def test_missing_absolute_vout_requires_fresh_verification(self):
        self.assert_revoked_after(Mock(return_value={}))

    def test_telemetry_exception_requires_fresh_verification(self):
        self.assert_revoked_after(Mock(side_effect=OSError('controller disconnected')))


if __name__ == '__main__':
    unittest.main()
