# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise actual EX packet transactions with an independent register device."""
import ctypes
import math
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from druta import railctl
from druta.controllers import mp2888
from tests.controllers.test_mp2888_discovery import Bus


class PMBus(Bus):
    def __init__(self):
        super().__init__()
        self.selected = deepcopy(self.selected)
        self.regs = self.add()
        self.regs.update({0x00: 0, 0x01: 0x80, 0x1F: 48, 0x21: 126,
                          0x2A: 0x2220, 0x2D: 0, 0x45: 0x4100, 0x78: 0, 0xE1: 0x8203})
        self.regs[0x8B] = self.voltage
        self.fail_reads = {}
        self.after_write = None
        self.before_read = None
        self.ignore_command = False
        self.write_available = True

    def voltage(self):
        if not self.regs[0xE1] & 0x10:
            return 676
        if self.ignore_command:
            return 1000
        # The report deliberately differs from the nominal VID. Acceptance must
        # establish response/reversal without calibrating away this difference.
        return math.floor(self.regs[0x21] * 6.25
                          + railctl._signed(self.regs[0x23], 8) * 6.25 - 31.5)

    def _i(self, fn_id, *signature):
        if fn_id == railctl.I2C_READ_EX:
            reader = super()._i(fn_id, *signature)

            def read(gpu, ptr, extra):
                packet = ctypes.cast(ptr, ctypes.POINTER(railctl._V3)).contents
                reg = packet.pbI2cRegAddress[0]
                if self.before_read:
                    self.before_read(reg, packet)
                if self.fail_reads.get(reg, 0):
                    self.fail_reads[reg] -= 1
                    return -1
                return reader(gpu, ptr, extra)
            return read
        if fn_id != railctl.I2C_WRITE_EX:
            raise AssertionError(fn_id)
        if not self.write_available:
            return None

        def write(gpu, ptr, extra):
            packet = ctypes.cast(ptr, ctypes.POINTER(railctl._V3)).contents
            assert gpu == self.gpu
            assert packet.version == railctl.VER3
            assert packet.portId == 1 and packet.i2cDevAddress == 0x40
            assert packet.regAddrSize == 1 and packet.cbSize == 2
            assert packet.i2cSpeed == 0xFFFF and packet.i2cSpeedKhz == 0
            assert packet.bIsPortIdSet == 1 and packet.displayMask == 0
            assert packet.bIsDDCPort == 0
            assert list(ctypes.cast(extra, ctypes.POINTER(ctypes.c_uint32 * 2)).contents) == [0, 0]
            reg = packet.pbI2cRegAddress[0]
            value = int.from_bytes(bytes(packet.pbData[:packet.cbSize]), 'little')
            self.writes.append((reg, value, packet.cbSize))
            self.regs[reg] = value
            if self.after_write:
                return self.after_write(reg, value)
            return 0
        return write


class TestPMBus(unittest.TestCase):
    def setUp(self):
        with (Path(__file__).resolve().parents[2] / 'i2c/rtx2080ti-mp2888a.toml').open('rb') as source:
            profile = railctl.Profile(tomllib.load(source), 'original')
        self.bus = PMBus()
        self.rail = mp2888.MP2888Candidate(profile, self.bus, 1, 0x20)
        sleeper = patch.object(mp2888.time, 'sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def apply(self, target=1000, **kwargs):
        return self.rail.set_voltage_mv(target, acknowledged=True, **kwargs)

    def verify(self, **kwargs):
        return self.rail.verify(acknowledged=True, operating_point=lambda: (8, 405, 810), **kwargs)

    def test_command_precedes_enable_and_preserves_every_unrelated_register(self):
        before = dict(self.bus.regs)
        entry = self.rail.capture_control()
        ok, message = self.apply(1003, expected=entry)
        self.assertTrue(ok, message)
        self.assertEqual(self.bus.writes, [(0x21, 160, 2), (0xE1, 0x8213, 2)])
        self.assertEqual(self.bus.regs, {**before, 0x21: 160, 0xE1: 0x8213})
        state = self.rail.capture_control()
        self.assertEqual(self.rail.last_written_control, state)
        state['command'] = 0
        self.assertEqual(self.rail.last_written_control['command'], 160)
        self.assertTrue(self.rail.absolute_voltage)
        self.assertTrue(self.rail.session_only_voltage)
        self.assertTrue(self.rail.requires_voltage_clamp)

    def test_entry_restore_selects_pwm_before_restoring_inactive_command(self):
        entry = self.rail.capture_control()
        self.assertTrue(self.apply()[0])
        active = self.rail.capture_control()
        self.bus.writes.clear()
        self.assertTrue(self.rail.restore_control(entry, recovery=True, expected=active)[0])
        self.assertEqual(self.bus.writes, [(0xE1, 0x8203, 2), (0x21, 126, 2)])
        self.assertEqual(self.rail.capture_control(), entry)

    def test_auto_retains_inactive_command_and_expected_guard(self):
        self.assertTrue(self.apply()[0])
        state = self.rail.capture_control()
        self.bus.writes.clear()
        self.assertTrue(self.rail.reset(expected=state)[0])
        self.assertEqual(self.bus.writes, [(0xE1, 0x8203, 2)])
        self.assertEqual(self.bus.regs[0x21], 160)
        self.bus.writes.clear()
        self.assertFalse(self.rail.reset(expected=state)[0])
        self.assertEqual(self.bus.writes, [])

    def test_exact_nonzero_manual_entry_is_restored(self):
        self.bus.regs.update({0xE1: 0xA213, 0x21: 168, 0x23: 0xFF, 0x2D: 3})
        entry = self.rail.capture_control()
        self.assertTrue(self.apply(1100, expected=entry)[0])
        self.assertTrue(self.rail.restore_control(entry, recovery=True,
                                                 expected=self.rail.last_written_control)[0])
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertEqual({reg for reg, _, _ in self.bus.writes}, {0x21})

    def test_acknowledgment_invalid_numbers_and_legacy_offset_cannot_write(self):
        self.assertFalse(self.rail.set_voltage_mv(1000)[0])
        for value in (True, False, None, float('nan'), float('inf'), -1, 1201):
            with self.subTest(value=value):
                self.assertFalse(self.apply(value)[0])
        self.assertFalse(self.rail.set_offset_mv(25, acknowledged=True)[0])
        self.assertFalse(self.rail._restore_word(0)[0])
        self.assertEqual(self.bus.writes, [])

    def test_live_signed_coarse_and_fine_trim_reduce_nominal_headroom(self):
        self.bus.regs[0x23] = 7
        self.assertFalse(self.apply(1162.5)[0])
        self.assertTrue(self.apply(1156.25)[0])
        self.assertEqual(self.bus.regs[0x23], 7)
        self.assertEqual(self.rail.telemetry()['effective_vid_mv'], 1200)
        self.bus.regs.update({0x23: 0xFF, 0x2D: 0x7F})
        self.assertFalse(self.apply(1206.25)[0])
        self.assertTrue(self.apply(1200)[0])
        self.assertEqual(self.bus.regs[0x2D], 0x7F)
        self.assertIsNone(self.rail.telemetry()['offset_mv'])

    def test_current_min_max_limits_govern_plan_and_apply(self):
        self.bus.regs.update({0x1F: 120, 0x24: 160, 0x23: 4})
        self.assertFalse(self.rail.plan(718.75)[0])
        self.assertFalse(self.apply(981.25)[0])
        self.assertTrue(self.rail.plan(725)[0])
        self.assertTrue(self.apply(975)[0])
        self.assertEqual((self.rail.p.env_min, self.rail.p.env_max), (725, 975))

    def test_xoc_cannot_extend_live_limit_or_command_scaling(self):
        self.rail.xoc = True
        self.assertTrue(self.apply(1250)[0])
        self.assertFalse(self.apply(1306.25)[0])
        self.bus.regs[0x24] = 511
        self.assertTrue(self.apply(1600)[0])
        self.assertFalse(self.apply(1606.25)[0])

    def test_unsupported_activation_context_never_writes(self):
        for reg, value in ((0x00, 1), (0x01, 0x90), (0x01, 0), (0x01, 0xC0), (0x45, 0x4120),
                           (0x2A, 0x2221), (0x2A, 0x2A20), (0x2A, 0x2620),
                           (0x2A, 0xE220), (0x2D, 0x80), (0x2D, 0x40),
                           (0x1F, 0x200), (0x24, 0x200), (0x23, 113),
                           (0x23, 0x100), (0xBE, 0xA1)):
            with self.subTest(reg=hex(reg), value=value):
                original = self.bus.regs[reg]
                self.bus.regs[reg] = value
                self.assertFalse(self.apply()[0])
                self.assertEqual(self.bus.writes, [])
                self.bus.regs[reg] = original

    def test_documented_alternate_adc_gain_and_unknown_nonmode_bits_are_preserved(self):
        self.bus.regs.update({0x2A: 0x2420, 0xE1: 0xD143, 0x45: 0x8041})
        self.assertTrue(self.apply()[0])
        self.assertEqual(self.bus.regs[0xE1], 0xD153)
        self.assertEqual(self.bus.regs[0x2A], 0x2420)
        self.assertEqual(self.rail.read_vout(), 968)

    def test_expected_guard_rejects_another_clients_command_or_context(self):
        for reg, changed in ((0x21, 140), (0xE1, 0x8303), (0x23, 1),
                             (0x2D, 1), (0x24, 200), (0x1F, 49)):
            with self.subTest(reg=hex(reg)):
                state = self.rail.capture_control()
                previous = self.bus.regs[reg]
                self.bus.regs[reg] = changed
                self.assertFalse(self.apply(expected=state)[0])
                self.assertFalse(self.rail.restore_control(state, recovery=True, expected=state)[0])
                self.assertEqual(self.bus.writes, [])
                self.bus.regs[reg] = previous

    def test_recovery_cannot_replay_configuration_from_a_snapshot(self):
        state = self.rail.capture_control()
        state['offset_word'] = 1
        self.assertFalse(self.rail.restore_control(state, recovery=True)[0])
        self.assertEqual(self.bus.writes, [])

    def test_transient_required_reads_recover_without_retrying_writes(self):
        self.bus.fail_reads[0x2A] = 2
        self.assertTrue(self.apply()[0])
        self.assertEqual([reg for reg, _, _ in self.bus.writes], [0x21, 0xE1])

    def test_read_failures_after_accepted_command_and_mode_restore_exact_entry(self):
        for failed_reg in (0x21, 0xE1):
            with self.subTest(reg=hex(failed_reg)):
                self.setUp()
                entry = self.rail.capture_control()
                injected = False

                def fail(reg, value, target=failed_reg):
                    nonlocal injected
                    if reg == target and not injected:
                        injected = True
                        self.bus.fail_reads[target] = 3
                    return 0

                self.bus.after_write = fail
                ok, message = self.apply()
                self.assertFalse(ok)
                self.assertIn('restored exactly', message)
                self.assertEqual(self.rail.capture_control(), entry)
                self.assertTrue(self.rail.last_control_restore_ok)
                regs = [reg for reg, _, _ in self.bus.writes]
                self.assertEqual(regs, [0x21, 0x21] if failed_reg == 0x21 else [0x21, 0xE1, 0xE1, 0x21])

    def test_failed_driver_status_may_have_written_and_is_not_retried(self):
        entry = self.rail.capture_control()
        first = True

        def uncertain(reg, value):
            nonlocal first
            result, first = (-1 if first else 0), False
            return result

        self.bus.after_write = uncertain
        self.assertFalse(self.apply()[0])
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertEqual(self.bus.writes, [(0x21, 160, 2), (0x21, 126, 2)])

    def test_persistent_read_loss_is_sticky_and_does_not_guess_restoration(self):
        self.bus.after_write = lambda reg, value: self.bus.fail_reads.update({0x21: 99}) or 0
        ok, message = self.apply()
        self.assertFalse(ok)
        self.assertIn('RESTORE FAILED', message)
        self.assertFalse(self.rail.last_control_restore_ok)
        self.assertFalse(self.rail._verification_restore_ok)
        self.assertTrue(self.rail._verification_restore_error)
        self.assertEqual(self.bus.writes, [(0x21, 160, 2)])

    def test_owned_uncertain_mode_write_can_be_recovered_after_transient_loss(self):
        entry = self.rail.capture_control()

        def lose_after_enable(reg, value):
            if reg == 0xE1 and value & 0x10:
                self.bus.fail_reads[0xE1] = 99
            return 0

        self.bus.after_write = lose_after_enable
        self.assertFalse(self.apply(expected=entry)[0])
        self.assertFalse(self.rail.last_control_restore_ok)
        self.assertTrue(self.rail.pending_recovery_states)
        self.bus.fail_reads.clear()
        self.bus.after_write = None
        self.assertFalse(self.rail.restore_control(entry, recovery=True, expected=entry)[0])
        self.assertTrue(self.rail.recover_control(entry)[0])
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertEqual(self.rail.pending_recovery_states, [])
        self.assertTrue(self.rail._verification_restore_ok)
        self.assertEqual(self.rail._verification_restore_error, '')

    def test_pending_recovery_never_adopts_a_foreign_replacement(self):
        entry = self.rail.capture_control()
        self.bus.after_write = lambda reg, value: self.bus.fail_reads.update({0x21: 99}) or 0
        self.assertFalse(self.apply()[0])
        pending = self.rail.pending_recovery_states
        pending[0]['command'] = 999
        self.assertNotEqual(pending, self.rail.pending_recovery_states)
        self.bus.fail_reads.clear()
        self.bus.after_write = None
        self.bus.regs[0x21] = 161
        writes = list(self.bus.writes)
        self.assertFalse(self.rail.recover_control(entry)[0])
        self.assertEqual(self.bus.writes, writes)
        self.assertEqual(self.bus.regs[0x21], 161)
        self.assertFalse(self.rail._verification_restore_ok)

    def test_page_is_checked_before_other_registers(self):
        self.bus.regs[0x00] = 1
        self.assertFalse(self.apply()[0])
        self.assertEqual(self.bus.reads, [(1, 0x20, 0x00, 1)])

    def test_off_status_refuses_activation_but_cannot_block_restoration(self):
        entry = self.rail.capture_control()
        self.bus.regs[0x78] = 0x40
        self.assertFalse(self.apply()[0])
        self.assertEqual(self.bus.writes, [])
        self.bus.regs[0x78] = 2  # A stale unsupported-command latch is allowed.
        self.assertTrue(self.apply()[0])
        active = self.rail.capture_control()
        self.bus.regs[0x78] = None
        self.assertTrue(self.rail.restore_control(entry, recovery=True, expected=active)[0])
        self.assertEqual(self.rail.capture_control(), entry)

    def test_external_change_after_dispatch_is_not_overwritten(self):
        for reg, value in ((0x23, 3), (0x2D, 2), (0xE1, 0x8303), (0x21, 161)):
            with self.subTest(reg=hex(reg)):
                self.setUp()
                self.bus.after_write = lambda command, target, r=reg, v=value: self.bus.regs.update({r: v}) or 0
                ok, message = self.apply()
                self.assertFalse(ok)
                self.assertIn('RESTORE FAILED', message)
                self.assertFalse(self.rail.last_control_restore_ok)
                self.assertEqual(len(self.bus.writes), 1)
                self.assertEqual(self.bus.regs[reg], value)

    def test_missing_unrelated_telemetry_does_not_block_control_or_entry_restore(self):
        entry = self.rail.capture_control()
        self.assertTrue(self.apply()[0])
        self.bus.regs.update({0x8C: None, 0x8D: None})
        self.assertEqual(self.rail.telemetry()['target_mv'], 1000)
        self.assertTrue(self.rail.restore_control(entry, recovery=True,
                                                 expected=self.rail.last_written_control)[0])
        self.assertEqual(self.rail.capture_control(), entry)

    def test_telemetry_reports_missing_fresh_voltage_instead_of_discovery_cache(self):
        self.assertTrue(self.rail.present())
        self.bus.regs.update({0x8B: None, 0x8C: None, 0x8D: None})
        telemetry = self.rail.telemetry()
        self.assertIsNone(telemetry['vout_mv'])
        self.assertIsNone(telemetry['iout_a'])
        self.assertIsNone(telemetry['vrm_temp_c'])
        self.assertIn('control', telemetry)

    def test_handle_selection_and_packet_geometry_are_checked(self):
        self.bus.gpu = 124
        self.assertFalse(self.apply()[0])
        self.assertEqual(self.bus.writes, [])
        self.bus.gpu = 123
        self.bus.selected['subsys'] = 0
        self.assertFalse(self.apply()[0])
        self.bus.selected = deepcopy(self.rail._bound_selection)
        self.bus.before_read = lambda reg, packet: setattr(packet, 'cbSize', 1)
        self.assertFalse(self.apply()[0])
        self.assertEqual(self.bus.writes, [])

    def test_no_write_export_is_a_capability_refusal(self):
        self.bus.write_available = False
        entry = self.rail.capture_control()
        self.assertFalse(self.apply()[0])
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertEqual(self.bus.writes, [])

    def test_verifier_proves_two_nominal_commands_and_reversal_at_non_p0(self):
        entry = self.rail.capture_control()
        ok, message, rows = self.verify()
        self.assertTrue(ok, message)
        self.assertEqual([row['target_mv'] for row in rows], [900, 925, 900])
        self.assertEqual([row['median_mv'] for row in rows], [868, 893, 868])
        self.assertIn('Physical voltage requires an independent measurement', message)
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertTrue(self.rail._verification_write_attempted)
        self.assertTrue(self.rail._verification_restore_ok)
        self.assertEqual([reg for reg, _, _ in self.bus.writes], [0x21, 0xE1, 0x21, 0x21, 0xE1, 0x21])
        self.assertEqual({reg for reg, _, _ in self.bus.writes}, {0x21, 0xE1})

    def test_verifier_rejects_enable_only_response_when_commands_are_ignored(self):
        self.bus.ignore_command = True
        entry = self.rail.capture_control()
        ok, message, rows = self.verify()
        self.assertFalse(ok)
        self.assertEqual(len(rows), 3)
        self.assertIn('no clear positive', message)
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertTrue(self.rail._verification_restore_ok)

    def test_new_power_fault_aborts_trial_and_does_not_block_entry_restore(self):
        entry = self.rail.capture_control()
        self.bus.regs[0x78] = 2
        self.bus.after_write = lambda reg, value: self.bus.regs.update({0x78: 0x22}) or 0
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn('new output/input/current/temperature fault', message)
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertTrue(self.rail._verification_restore_ok)

    def test_verifier_can_use_consistently_missing_unrelated_point_readings(self):
        ok, message, _ = self.rail.verify(acknowledged=True, operating_point=lambda: (None, 405, None))
        self.assertTrue(ok, message)

    def test_verifier_ack_clock_and_headroom_refusals_do_not_write(self):
        self.assertFalse(self.rail.verify(operating_point=lambda: (8, 405, 810))[0])
        self.assertFalse(self.rail.verify(acknowledged=True)[0])
        self.assertFalse(self.verify(cancelled=lambda: True)[0])
        self.assertFalse(self.rail.verify(acknowledged=True, operating_point=lambda: (8, 0, 810))[0])
        self.bus.regs.update({0x1F: 190, 0x24: 192})
        self.assertFalse(self.verify()[0])
        self.assertEqual(self.bus.writes, [])

    def test_verifier_cancel_and_changed_clock_always_restore_exact_entry(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                self.setUp()
                entry = self.rail.capture_control()
                def point(cancelled=cancel):
                    return 8, 406 if self.bus.writes and not cancelled else 405, 810

                ok, message, _ = self.rail.verify(
                    acknowledged=True, operating_point=point,
                    cancelled=lambda c=cancel: bool(self.bus.writes) and c)
                self.assertFalse(ok)
                self.assertEqual(self.rail.capture_control(), entry)
                self.assertTrue(self.rail._verification_restore_ok, message)

    def test_verifier_unrelated_telemetry_loss_and_required_read_failure_restore(self):
        entry = self.rail.capture_control()
        self.bus.after_write = lambda reg, value: self.bus.regs.update({0x8C: None, 0x8D: None}) or 0
        self.assertTrue(self.verify()[0])
        self.assertEqual(self.rail.capture_control(), entry)
        self.bus.after_write = lambda reg, value: self.bus.regs.update({0x8B: None}) or 0
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertTrue(self.rail._verification_restore_ok, message)

    def test_verifier_external_change_during_final_entry_read_is_sticky_without_overwrite(self):
        entry = self.rail.capture_control()

        def point():
            if self.bus.writes and not self.bus.regs[0xE1] & 0x10:
                self.bus.regs[0x23] = 11
            return 8, 405, 810

        ok, message, _ = self.rail.verify(acknowledged=True, operating_point=point)
        self.assertFalse(ok)
        self.assertFalse(self.rail._verification_restore_ok)
        self.assertIn('RESTORE FAILED', message)
        self.assertEqual(self.bus.regs[0x23], 11)
        self.assertEqual(self.bus.regs[0x21], entry['command'])
        self.assertEqual(len(self.bus.writes), 6)

    def test_verifier_cancel_during_final_entry_read_leaves_restore_confirmed(self):
        entry = self.rail.capture_control()
        ok, message, _ = self.verify(cancelled=lambda: bool(self.bus.writes) and not self.bus.regs[0xE1] & 0x10)
        self.assertFalse(ok)
        self.assertIn('cancelled', message)
        self.assertEqual(self.rail.capture_control(), entry)
        self.assertTrue(self.rail._verification_restore_ok)


if __name__ == '__main__':
    unittest.main()
