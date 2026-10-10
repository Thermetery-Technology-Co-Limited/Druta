# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Byte-transport, compound rollback and measured-response contracts."""
import ctypes
import unittest
from unittest.mock import Mock, patch

from druta import railctl
from druta.controllers import up9512r as u


class Bus:
    def __init__(self):
        self.ok = True
        self.gpu = ctypes.c_void_p(1)
        self.selected = {'slot': '0000:01:00.0', 'devid': 0x2702, 'subsys': 1}
        self.regs = {0x27: 0, 0x28: 0x2B, 0x0A: 0, 0x0B: 0, 0x0C: 0x0B,
                     0x2A: 0x2C, 0x39: 0x94, 0x2C: 11, 0x2D: 90}
        self.reads, self.writes = [], []
        self.read_hook = self.write_hook = None
        self.response = False

    def _i(self, api, *args):
        return self.read if api == railctl.I2C_READ_EX else self.write

    def packet(self, pointer):
        p = ctypes.cast(pointer, ctypes.POINTER(railctl._V3)).contents
        assert p.i2cDevAddress == 0x4A and p.portId == 2 and p.cbSize == 1
        assert p.regAddrSize == 1 and p.bIsPortIdSet == 1
        return p, int(p.pbI2cRegAddress[0])

    def read(self, gpu, pointer, extra):
        p, reg = self.packet(pointer)
        self.reads.append((reg, int(p.pbData[0])))
        if self.read_hook is not None:
            result = self.read_hook(reg, p)
            if result is not None:
                return result
        value = self.regs[reg]
        if reg == 0x2D and self.response:
            value += (self.regs[0x0A] >> 4) if self.regs[0x2A] & 0x40 else 0
        p.pbData[0] = value
        return 0

    def write(self, gpu, pointer, extra):
        p, reg = self.packet(pointer)
        value = int(p.pbData[0])
        self.writes.append((reg, value))
        if self.write_hook is not None:
            result = self.write_hook(reg, value, p)
            if result is not None:
                return result
        self.regs[reg] = value
        return 0


class UP9512RTests(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.rail = u.UP9512R(self.bus)

    def controls(self):
        return tuple(self.bus.regs[r] for r in u.CONTROL_REGISTERS)

    def state(self, offsets=None, enabled=True):
        return {'kind': u.KIND, 'offsets_mv': offsets or [10] * 5, 'enabled': enabled}

    def test_documented_byte_ids_and_adc_units(self):
        self.assertTrue(self.rail.present())
        self.assertEqual(self.rail.telemetry()['vout_mv'], 900)
        self.assertEqual(self.rail.telemetry()['imon_mv'], 110)
        self.assertNotIn('iout_a', self.rail.telemetry())
        self.bus.regs[0x28] = 0x2C
        self.assertFalse(self.rail.present())
        self.assertFalse(self.rail.set_offset_mv(10, acknowledged=True)[0])
        self.assertFalse(self.bus.writes)

    def test_transport_rejects_untouched_buffer_short_read_and_error(self):
        for hook in (lambda reg, p: 0,
                     lambda reg, p: (setattr(p, 'cbSize', 0) or 0),
                     lambda reg, p: -1):
            with self.subTest(hook=hook):
                self.bus.read_hook = hook
                self.assertIsNone(self.rail.read(0x27))
                self.assertFalse(self.rail.present())
        self.bus.read_hook = None
        self.bus.regs[0x2C] = 0xA5
        self.assertEqual(self.rail.read(0x2C), 0xA5)
        self.assertEqual(self.bus.reads[-2:], [(0x2C, 0xA5), (0x2C, 0x5A)])

    def test_target_binding_stops_all_bus_access_after_gpu_or_route_change(self):
        for mutate in (lambda: setattr(self.bus, 'gpu', ctypes.c_void_p(2)),
                       lambda: self.bus.selected.update(subsys=2),
                       lambda: setattr(self.rail.p, 'port', 3)):
            self.setUp()
            mutate()
            self.assertFalse(self.rail.present())
            self.assertFalse(self.rail.set_offset_mv(10, acknowledged=True)[0])
            self.assertFalse(self.bus.reads)
            self.assertFalse(self.bus.writes)

    def test_only_understood_bytes_are_accessible_and_lock_never_writable(self):
        for reg in (0x39, 0x27, 0x2D, 0x2E, 0x3C):
            with self.assertRaises(ValueError):
                self.rail._raw_write(reg, 0)
        for reg, width in ((0x3C, 1), (0x27, 2), (0x27, True)):
            with self.assertRaises(ValueError):
                self.rail.read(reg, width)
        self.assertFalse(self.bus.writes)

    def test_acknowledgment_grid_hardware_and_mode_guards(self):
        self.assertFalse(self.rail.set_offset_mv(10)[0])
        for value in (-10, 1, 15, 60, 150, 160, True, '10', float('nan'), float('inf')):
            self.assertFalse(self.rail.set_offset_mv(value, acknowledged=True)[0], value)
        self.assertFalse(self.bus.writes)
        self.assertTrue(self.rail.plan(50)[0])
        self.assertFalse(self.bus.writes)
        self.rail.xoc = True
        self.assertTrue(self.rail.set_offset_mv(150, acknowledged=True)[0])
        self.assertEqual(self.controls(), (0xFF, 0xFF, 0xFB, 0x6C))
        self.assertEqual(self.rail.telemetry()['offsets_mv'], [150] * 5)
        self.assertFalse(self.rail.plan(160)[0])

    def test_locked_unreadable_or_test_mode_discoverable_but_cannot_apply(self):
        for lock, misc in ((0x87, 0x2C), (0, 0x2C), (0x94, 0xAC), (0x94, 0x2D)):
            self.setUp()
            self.bus.regs.update({0x39: lock, 0x2A: misc})
            self.assertTrue(self.rail.present())
            self.assertTrue(self.rail.telemetry())
            self.assertFalse(self.rail.plan(10)[0])
            self.assertFalse(self.rail.reset()[0])
            self.assertFalse(self.bus.writes)
        self.setUp()
        self.bus.read_hook = lambda reg, p: -1 if reg == 0x39 else None
        self.assertIsNone(self.rail.telemetry()['write_locked'])
        self.assertFalse(self.rail.plan(10)[0])

    def test_enable_last_disable_first_and_preserve_other_bits(self):
        before = self.controls()
        self.assertTrue(self.rail.set_offset_mv(20, acknowledged=True)[0])
        self.assertEqual(self.bus.writes, [(0x0A, 0x22), (0x0B, 0x22), (0x0C, 0x2B), (0x2A, 0x6C)])
        self.bus.writes.clear()
        self.assertTrue(self.rail.set_offset_mv(30, acknowledged=True)[0])
        self.assertNotIn(0x2A, [reg for reg, value in self.bus.writes])
        self.bus.writes.clear()
        self.assertTrue(self.rail.reset()[0])
        self.assertEqual(self.bus.writes[0], (0x2A, 0x2C))
        self.assertEqual(self.controls(), before)

    def test_nonuniform_disabled_state_round_trip_without_replaying_unknown_bits(self):
        self.bus.regs.update({0x0A: 0xF8, 0x0B: 0x90, 0x0C: 0x1B})
        state = self.rail.capture_control()
        self.assertEqual(state, self.state([150, 80, 90, 0, 10], False))
        self.assertIsNone(self.rail.telemetry()['offset_mv'])
        self.assertTrue(self.rail.validate_control(state, xoc=False))
        self.assertTrue(self.rail.reset()[0])
        self.bus.regs[0x0C] = 0x03
        self.assertTrue(self.rail.restore_control(state)[0])
        self.assertEqual(self.rail.capture_control(), state)
        self.assertEqual(self.bus.regs[0x0C], 0x13)

    def test_static_validation_and_live_profile_preflight_are_separate(self):
        state = self.state([100] * 5)
        with self.assertRaises(ValueError):
            self.rail.validate_control(state, xoc=False)
        self.assertTrue(self.rail.validate_control(state, xoc=True))
        self.assertFalse(self.bus.reads)
        self.bus.regs[0x39] = 0x87
        self.assertFalse(self.rail.plan_control(state, xoc=True)[0])
        self.assertFalse(self.bus.writes)
        for bad in (dict(state, unknown=1), self.state([10, 20]),
                    self.state([True] * 5), dict(state, enabled=1), dict(state, kind='other')):
            with self.assertRaises(ValueError):
                u.parse_control(bad)

    def test_ceiling_guard_uses_largest_state_increase_without_assuming_lcs0(self):
        self.bus.regs.update({0x0A: 0x50, 0x0B: 0x55, 0x0C: 0x5B, 0x2A: 0x6C, 0x2D: 118})
        self.assertFalse(self.rail.plan(50)[0])  # LCS1 could rise 50 mV, not zero.
        self.assertTrue(self.rail.plan(10)[0])
        self.assertFalse(self.bus.writes)

    def test_expected_state_conflict_does_not_overwrite_another_client(self):
        expected = self.rail.capture_control()
        self.bus.regs[0x0A] = 0x11
        self.assertFalse(self.rail.set_offset_mv(20, acknowledged=True, expected=expected)[0])
        self.assertFalse(self.rail.reset(expected=expected)[0])
        self.assertFalse(self.bus.writes)
        self.assertEqual(self.bus.regs[0x0A], 0x11)

    def test_each_partial_failed_or_raised_write_restores_exact_entry_bytes(self):
        for failing_reg in u.CONTROL_REGISTERS:
            for raises in (False, True):
                with self.subTest(reg=failing_reg, raises=raises):
                    self.setUp()
                    self.bus.regs.update({0x0A: 0x12, 0x0B: 0x30, 0x0C: 0x4B})
                    before, fired = self.controls(), [False]
                    def fault(reg, value, packet):
                        if reg == failing_reg and not fired[0]:
                            fired[0] = True
                            self.bus.regs[reg] = value
                            if raises:
                                raise RuntimeError('injected exception after hardware write')
                            return -1
                    self.bus.write_hook = fault
                    ok, message = self.rail.set_offset_mv(50, acknowledged=True)
                    self.assertFalse(ok)
                    self.assertIn('exact entry control restored', message)
                    self.assertEqual(self.controls(), before)
                    self.assertTrue(self.rail._verification_restore_ok)
                    self.assertNotIn(0x39, [r for r, v in self.bus.writes])

    def test_success_status_short_write_and_ignored_write_do_not_succeed(self):
        for short in (False, True):
            self.setUp()
            before, fired = self.controls(), [False]
            def fault(reg, value, p):
                if not fired[0]:
                    fired[0] = True
                    if short:
                        self.bus.regs[reg] = value
                        p.cbSize = 0
                    return 0
            self.bus.write_hook = fault
            self.assertFalse(self.rail.set_offset_mv(20, acknowledged=True)[0])
            self.assertEqual(self.controls(), before)

    def test_lock_or_identity_change_after_dispatch_reports_failed_restoration(self):
        for change in ({0x39: 0x87}, {0x28: 0}):
            self.setUp()
            def fault(reg, value, p):
                self.bus.regs[reg] = value
                self.bus.regs.update(change)
                return -1
            self.bus.write_hook = fault
            ok, message = self.rail.set_offset_mv(20, acknowledged=True)
            self.assertFalse(ok)
            self.assertIn('RESTORE FAILED', message)
            self.assertFalse(self.rail._verification_restore_ok)
            self.assertEqual(len(self.bus.writes), 1)

    def test_preserved_bit_drift_is_not_sanitized_or_replayed_by_rollback(self):
        def fault(reg, value, p):
            self.bus.regs[reg] = value
            self.bus.regs[0x0C] ^= 1
            return 0
        self.bus.write_hook = fault
        ok, message = self.rail.set_offset_mv(20, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn('preserved bits changed', message)
        self.assertEqual(self.bus.regs[0x0C], 0x0A)
        self.assertEqual(len(self.bus.writes), 1)

    def test_reset_and_recovery_restore_bypass_normal_envelope_without_unlocking(self):
        self.bus.regs.update({0x0A: 0xFF, 0x0B: 0xF8, 0x0C: 0xAB, 0x2A: 0x6C, 0x2D: 130})
        state, raw = self.rail.capture_control(), self.controls()
        self.assertTrue(self.rail.reset()[0])
        self.assertFalse(self.rail.restore_control(state)[0])
        self.assertTrue(self.rail.restore_control(state, recovery=True)[0])
        self.assertEqual(self.controls(), raw)

    def test_post_write_missing_or_excessive_feedback_rolls_back_without_requiring_adc(self):
        for unavailable in (False, True):
            self.setUp()
            before = self.controls()
            def feedback(reg, packet):
                if reg == 0x2D and self.bus.writes:
                    if unavailable:
                        return -1
                    packet.pbData[0] = 130
                    return 0
            self.bus.read_hook = feedback
            ok, message = self.rail.set_offset_mv(10, acknowledged=True)
            self.assertFalse(ok)
            self.assertIn('post-write FB ADC', message)
            self.assertEqual(self.controls(), before)
            self.assertTrue(self.rail._verification_restore_ok)


class UP9512RVerificationTests(unittest.TestCase):
    controls = UP9512RTests.controls

    def setUp(self):
        UP9512RTests.setUp(self)
        self.bus.response = True
        self.sleep = patch('druta.controllers.up9512r.time.sleep').start()
        self.addCleanup(patch.stopall)

    def verify(self, **kwargs):
        kwargs.setdefault('operating_point', lambda: (0, 3060, 1438))
        return self.rail.verify(acknowledged=True, **kwargs)

    def test_response_verifies_then_restores_enabled_nonuniform_entry(self):
        self.bus.regs.update({0x0A: 0x01, 0x0B: 0x23, 0x0C: 0x4B, 0x2A: 0x6C})
        original = self.controls()
        ok, message, ladder = self.verify(ref=Mock(side_effect=AssertionError('VID must not be sampled')))
        self.assertTrue(ok, message)
        self.assertEqual(self.controls(), original)
        self.assertEqual(ladder[0]['delta_mv'], 10)
        self.assertEqual(ladder[0]['reversal_mv'], 10)
        self.assertNotIn(0x2A, [reg for reg, value in self.bus.writes])

    def test_disabled_nonuniform_stored_offsets_are_restored_not_zeroed(self):
        self.bus.regs.update({0x0A: 0xF8, 0x0B: 0x90, 0x0C: 0xAB})
        original = self.controls()
        ok, message, _ = self.verify()
        self.assertTrue(ok, message)
        self.assertEqual(self.controls(), original)
        self.assertFalse(self.rail.capture_control()['enabled'])

    def test_no_response_never_confirms_and_restores_full_state(self):
        self.bus.response = False
        original = self.controls()
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn('INCONCLUSIVE', message)
        self.assertEqual(len(ladder), 5)
        self.assertEqual(self.controls(), original)
        self.assertTrue(self.rail._verification_restore_ok)

    def test_cancel_and_xoc_change_after_write_restore(self):
        for reason in ('cancel', 'xoc'):
            self.setUp()
            original = self.controls()
            def point():
                if self.bus.writes and reason == 'xoc':
                    self.rail.xoc = True
                return (0, 3060, 1438)
            ok, message, _ = self.verify(operating_point=point,
                                        cancelled=lambda: bool(self.bus.writes) and reason == 'cancel')
            self.assertFalse(ok)
            self.assertEqual(self.controls(), original)
            self.assertTrue(self.rail._verification_restore_ok)

    def test_no_headroom_missing_hold_or_precancel_never_writes(self):
        self.bus.regs.update({0x0A: 0x55, 0x0B: 0x55, 0x0C: 0x5B, 0x2A: 0x6C})
        self.assertFalse(self.verify()[0])
        self.assertFalse(self.rail.verify(acknowledged=True)[0])
        self.assertFalse(self.verify(cancelled=lambda: True)[0])
        self.assertFalse(self.bus.writes)
        self.assertFalse(self.rail._verification_write_attempted)

    def test_failed_restoration_overrides_positive_response(self):
        def fail_restore(reg, value, p):
            if reg == 0x2A and value == 0x2C:
                return -1
        self.bus.write_hook = fail_restore
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertTrue(ladder[0]['moved'])
        self.assertIn('RESTORE FAILED', message)
        self.assertFalse(self.rail._verification_restore_ok)

    def test_no_dispatch_conflict_is_not_overwritten_by_verification_finally(self):
        real = self.rail._transaction
        def conflict(state, **kwargs):
            self.bus.regs[0x0A] = 0x22
            return real(state, **kwargs)
        self.rail._transaction = conflict
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertFalse(self.bus.writes)
        self.assertFalse(self.rail._verification_write_attempted)
        self.assertEqual(self.bus.regs[0x0A], 0x22)

    def test_response_without_measured_reversal_cannot_verify(self):
        def stale_feedback(reg, packet):
            if reg == 0x2D and self.bus.writes:
                packet.pbData[0] = 94
                return 0
        original = self.controls()
        self.bus.read_hook = stale_feedback
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertTrue(ladder[0]['moved'])
        self.assertIn('baseline', message)
        self.assertEqual(self.controls(), original)
        self.assertTrue(self.rail._verification_restore_ok)


if __name__ == '__main__':
    unittest.main()
