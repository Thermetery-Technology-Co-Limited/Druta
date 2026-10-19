# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the tag-1.8.0 MP2888 offset contract independent of manual PMBus.

The +50 mV response fixture follows the two recorded TITAN worker trials in
experiments/i2c-direct-vout-worker-titan-47212-20260909.json. It is a regression
fixture, not a response curve or hardware identity gate for other controllers.
"""
import ctypes
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from druta import profiles, railctl
from druta.controllers import mp2888
from tests.controllers.test_mp2888_discovery import Bus
from tests.controllers.test_mp2888_pmbus import PMBus
from tests.test_tune_profiles import hardware

TAG_180_RECIPE_SHA256 = '4a3979d6500eca53c3f37e7ccf7f0a1ad2ea71ee819a8e26c11a535c912470f7'


class OffsetBus(Bus):
    def __init__(self, *, port=1, addr=0x20):
        super().__init__()
        self.selected = {'devid': 0xDEAD, 'subsys': 0x12345678, 'driver': 'a different release'}
        self.route = port, addr
        self.regs = self.add(port, addr)
        self.regs[0x8B] = self.voltage
        self.response_at_50_mv = 1088
        self.reject_next_write = False
        self.fail_reads = {}

    def voltage(self):
        raw = railctl._signed(self.regs[0x23], 8)
        responses = {0: 1063, 1: 1063, 2: 1063, 4: 1063,
                     8: self.response_at_50_mv, 12: 1113}
        return responses.get(raw, round(1063 + raw * 6.25))

    def _i(self, fn_id, *signature):
        if fn_id == railctl.I2C_READ_EX:
            reader = super()._i(fn_id, *signature)

            def read(gpu, ptr, extra):
                packet = ctypes.cast(ptr, ctypes.POINTER(railctl._V3)).contents
                reg = packet.pbI2cRegAddress[0]
                if self.fail_reads.get(reg, 0):
                    self.fail_reads[reg] -= 1
                    return -1
                return reader(gpu, ptr, extra)
            return read
        if fn_id != railctl.I2C_WRITE_EX:
            raise AssertionError(fn_id)

        def write(gpu, ptr, extra):
            packet = ctypes.cast(ptr, ctypes.POINTER(railctl._V3)).contents
            reg = packet.pbI2cRegAddress[0]
            assert reg == 0x23, 'legacy offset must never write command, mode, or calibration'
            assert (packet.portId, packet.i2cDevAddress >> 1) == self.route
            assert packet.regAddrSize == 1 and packet.cbSize == 2
            assert packet.version == railctl.VER3 and packet.i2cSpeed == 0xFFFF
            assert list(ctypes.cast(extra, ctypes.POINTER(ctypes.c_uint32 * 2)).contents) == [0, 0]
            value = int.from_bytes(bytes(packet.pbData[:2]), 'little')
            self.writes.append((reg, value, 2))
            self.regs[reg] = value
            if self.reject_next_write:
                self.reject_next_write = False
                return -1
            return 0
        return write


class TestOffsetCompatibility(unittest.TestCase):
    def setUp(self):
        with (Path(__file__).resolve().parents[2] / 'i2c/rtx2080ti-mp2888a.toml').open('rb') as source:
            self.profile = railctl.Profile(tomllib.load(source), 'original')
        self.bus = OffsetBus()
        self.rail = mp2888.MP2888Candidate(self.profile, self.bus, 1, 0x20)
        sleeper = patch.object(railctl.time, 'sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def apply(self, value=50, **kwargs):
        return self.rail.set_offset_mv(value, acknowledged=True, **kwargs)

    def test_default_discovery_and_original_profile_identity_are_preserved(self):
        [found] = mp2888.discover(self.bus, self.profile, routes={(1, 0x20)})
        self.assertIsInstance(found, mp2888.MP2888Candidate)
        self.assertEqual(found.control_method, 'offset')
        self.assertIs(found.discovery_candidate, found)
        self.assertFalse(found.absolute_voltage)
        self.assertFalse(found.requires_voltage_clamp)
        self.assertFalse(found.session_only_voltage)
        self.assertEqual(found.p.src, self.profile.src)
        identity = profiles.rail_identity(found)
        self.assertEqual(identity, profiles.rail_identity(railctl.Rail(self.profile, self.bus)))
        self.assertEqual(identity['sha256'], TAG_180_RECIPE_SHA256)
        self.assertEqual((found.p.env_min, found.p.env_max), (-200, 100))
        self.assertEqual((found.p.hw_min_mv, found.p.hw_max_mv), (-693.75, 700))
        self.assertEqual(self.bus.writes, [])

    def test_recorded_plus_50_request_retains_23_word_and_24_to_25_mv_response(self):
        for reported in (1088, 1087):
            with self.subTest(reported=reported):
                self.bus.regs[0x23] = 0
                self.bus.writes.clear()
                self.bus.response_at_50_mv = reported
                before = self.rail.read_vout()
                ok, message = self.apply()
                self.assertTrue(ok, message)
                self.assertEqual(self.bus.writes, [(0x23, 8, 2)])
                self.assertEqual(self.rail.read_vout() - before, reported - 1063)
                self.assertEqual(self.rail.telemetry()['offset_mv'], 50)

    def test_verifier_still_detects_recorded_deadband_and_restores_exact_word(self):
        ok, message, ladder = self.rail.verify(
            acknowledged=True, operating_point=lambda: (0, 1965, 7000))
        self.assertTrue(ok, message)
        self.assertEqual([row['rung_mv'] for row in ladder], [6.25, 12.5, 25, 50])
        self.assertEqual([row['delta_mv'] for row in ladder], [0, 0, 0, 25])
        self.assertEqual(self.bus.regs[0x23], 0)
        self.assertTrue(self.rail._verification_restore_ok)
        self.assertEqual(self.bus.writes, [(0x23, code, 2) for code in (1, 2, 4, 8, 0)])

    def test_reset_zeros_only_23_and_restore_keeps_nonzero_entry(self):
        self.bus.regs.update({0x23: 7, 0x21: 126, 0xE1: 0x8313})
        untouched = {reg: value for reg, value in self.bus.regs.items() if reg != 0x23}
        self.assertTrue(self.apply()[0])
        self.assertTrue(self.rail._restore_word(7)[0])
        self.assertEqual(self.bus.regs[0x23], 7)
        self.assertTrue(self.rail.reset()[0])
        self.assertEqual(self.bus.regs[0x23], 0)
        self.assertEqual({reg: value for reg, value in self.bus.regs.items() if reg != 0x23}, untouched)
        self.assertEqual(self.bus.writes, [(0x23, 8, 2), (0x23, 7, 2), (0x23, 0, 2)])

    def test_negative_encoding_and_dry_run_api_remain_compatible(self):
        self.assertTrue(self.apply(-6.25, dry_run=True)[0])
        self.assertEqual(self.bus.writes, [])
        self.assertTrue(self.apply(-6.25)[0])
        self.assertEqual(self.bus.writes, [(0x23, 0xFF, 2)])
        self.assertEqual(self.rail.telemetry()['offset_mv'], -6.25)
        self.assertTrue(self.rail._restore_word(0)[0])

    def test_original_offset_envelope_and_xoc_bounds_remain(self):
        self.assertTrue(self.rail.plan(100)[0])
        self.assertFalse(self.rail.plan(106.25)[0])
        self.assertFalse(self.apply(-206.25)[0])
        self.rail.xoc = True
        self.assertTrue(self.rail.plan(150)[0])
        self.assertFalse(self.rail.plan(706.25)[0])
        self.assertFalse(self.rail.plan(-700)[0])
        self.assertEqual(self.bus.writes, [])

    def test_failed_write_restores_original_offset_and_never_touches_mode(self):
        self.bus.regs[0x23] = 3
        self.bus.reject_next_write = True
        ok, message = self.apply()
        self.assertFalse(ok)
        self.assertIn('restored original word', message)
        self.assertEqual(self.bus.regs[0x23], 3)
        self.assertEqual(self.bus.writes, [(0x23, 8, 2), (0x23, 3, 2)])

    def test_legacy_is_independent_of_all_manual_pmbus_capabilities(self):
        for values in ({}, {0x21: 0xFFFF, 0xE1: None, 0x1F: None, 0x2A: 0xFFFF,
                           0x2D: 0xFF, 0x45: 0xFFFF, 0x78: None, 0x00: None}):
            with self.subTest(values=values):
                self.bus.regs.update(values)
                self.assertTrue(self.rail.present())
                self.assertTrue(self.apply()[0])
                manual = self.rail.for_control_method('pmbus')
                self.assertFalse(manual.plan(1000)[0])
                self.assertFalse(manual.set_voltage_mv(1000, acknowledged=True)[0])
                self.assertEqual(self.rail.telemetry()['offset_mv'], 50)
                self.assertTrue(self.rail.reset()[0])
        self.assertTrue(all(reg == 0x23 for reg, _, _ in self.bus.writes))

    def test_cached_method_factory_has_no_hardware_side_effects_and_shares_binding(self):
        before = dict(self.bus.regs)
        manual = self.rail.for_control_method('pmbus')
        self.assertIsInstance(manual, mp2888.MP2888PMBus)
        self.assertIs(manual.discovery_candidate, self.rail)
        self.assertIs(manual._mutex, self.rail._mutex)
        self.assertEqual(manual._bound_route, self.rail._bound_route)
        self.assertIs(self.rail.for_control_method('pmbus'), manual)
        self.assertIs(manual.for_control_method('offset'), self.rail)
        self.assertIs(manual.for_control_method('pmbus'), manual)
        self.assertEqual(manual.control_method, 'pmbus')
        self.assertNotEqual(profiles.rail_identity(manual), profiles.rail_identity(self.rail))
        direct = mp2888.MP2888PMBus(self.profile, self.bus, 1, 0x20)
        self.assertEqual(profiles.rail_identity(manual), profiles.rail_identity(direct))
        self.assertEqual(self.rail.p.src, self.profile.src)
        self.assertEqual(self.bus.regs, before)
        self.assertEqual(self.bus.reads, [])
        self.assertEqual(self.bus.writes, [])
        with self.assertRaises(ValueError):
            self.rail.for_control_method('automatic hardware guess')

    def test_offset_and_manual_write_permissions_cannot_cross(self):
        manual = self.rail.for_control_method('pmbus')
        self.assertFalse(manual.set_offset_mv(50, acknowledged=True)[0])
        self.assertFalse(self.rail.set_voltage_mv(1000, acknowledged=True)[0])
        for rail, command in ((manual, 0x23), (self.rail, 0x21), (self.rail, 0xE1)):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    rail._raw_write(command, 0, 2)
        self.assertEqual(self.bus.writes, [])

    def test_changed_route_or_gpu_binding_refuses_both_adapters(self):
        manual = self.rail.for_control_method('pmbus')
        self.bus.gpu = 999
        self.assertFalse(self.apply()[0])
        self.assertFalse(manual.plan(1000)[0])
        self.bus.gpu = 123
        self.rail.p.port = 2
        self.assertFalse(self.apply()[0])
        self.assertEqual(self.bus.writes, [])

    def test_alternate_route_and_metadata_do_not_disable_legacy_control(self):
        bus = OffsetBus(port=6, addr=0x33)
        rail = mp2888.MP2888Candidate(self.profile, bus, 6, 0x33)
        self.assertTrue(rail.set_offset_mv(50, acknowledged=True)[0])
        self.assertEqual(bus.writes, [(0x23, 8, 2)])
        self.assertEqual((rail.p.port, rail.addr7), (6, 0x33))

    def test_transient_runtime_read_recovers_without_duplicate_offset_write(self):
        self.assertTrue(self.rail.present())
        self.bus.fail_reads[0x23] = 2
        self.assertTrue(self.apply()[0])
        self.assertEqual(self.bus.writes, [(0x23, 8, 2)])

    def test_tag_180_profile_capture_and_replay_preserve_offset_format(self):
        gpu, _, _ = hardware()
        self.assertTrue(self.apply()[0])
        state = profiles.capture(gpu, self.rail)
        self.assertEqual(state['i2c']['sha256'], TAG_180_RECIPE_SHA256)
        self.assertEqual(state['i2c']['offset_mv'], 50)
        self.assertNotIn('control', state['i2c'])
        self.assertTrue(self.rail.reset()[0])
        self.bus.writes.clear()
        result = profiles.restore(gpu, state, rail=self.rail, i2c_verified=True, apply_curve=False)
        self.assertTrue(all(ok for ok, _ in result), result)
        self.assertEqual(self.bus.regs[0x23], 8)
        self.assertEqual(self.bus.writes, [(0x23, 8, 2)])

    def test_manual_ownership_predicate_never_probes_the_controller(self):
        self.assertFalse(self.rail.has_owned_manual_control())
        manual = self.rail.for_control_method('pmbus')
        self.assertFalse(manual.has_owned_manual_control())
        self.assertFalse(self.rail.has_owned_manual_control())
        self.assertEqual(self.bus.reads, [])
        self.assertEqual(self.bus.writes, [])

    def test_owned_manual_voltage_blocks_all_legacy_mutations_until_auto(self):
        bus = PMBus()
        offset = mp2888.MP2888Candidate(self.profile, bus, 1, 0x20)
        manual = offset.for_control_method('pmbus')
        self.assertTrue(manual.set_voltage_mv(1000, acknowledged=True)[0])
        self.assertTrue(offset.has_owned_manual_control())
        self.assertTrue(manual.has_owned_manual_control())
        reads, writes = list(bus.reads), list(bus.writes)
        self.assertFalse(offset.set_offset_mv(50, acknowledged=True)[0])
        self.assertFalse(offset.plan(50)[0])
        self.assertFalse(offset.reset()[0])
        self.assertFalse(offset.verify(acknowledged=True)[0])
        self.assertFalse(offset._restore_word(0)[0])
        self.assertEqual(bus.reads, reads)
        self.assertEqual(bus.writes, writes)
        self.assertTrue(manual.reset(expected=manual.last_written_control)[0])
        self.assertFalse(offset.has_owned_manual_control())
        self.assertTrue(offset.set_offset_mv(50, acknowledged=True)[0])
        self.assertEqual(bus.regs[0x23], 8)

    def test_pending_manual_write_blocks_legacy_until_guarded_recovery(self):
        bus = PMBus()
        offset = mp2888.MP2888Candidate(self.profile, bus, 1, 0x20)
        manual = offset.for_control_method('pmbus')
        entry = manual.capture_control()
        bus.after_write = lambda reg, value: bus.fail_reads.update({0x21: 99}) or 0
        self.assertFalse(manual.set_voltage_mv(1000, acknowledged=True)[0])
        self.assertIsNone(manual.last_written_control)
        self.assertTrue(offset.has_owned_manual_control())
        bus.fail_reads.clear()
        bus.after_write = None
        self.assertFalse(offset.set_offset_mv(50, acknowledged=True)[0])
        self.assertTrue(manual.recover_control(entry)[0])
        self.assertFalse(offset.has_owned_manual_control())

    def test_inflight_manual_write_cannot_reenter_legacy_offset(self):
        bus = PMBus()
        offset = mp2888.MP2888Candidate(self.profile, bus, 1, 0x20)
        manual = offset.for_control_method('pmbus')
        refused = []

        def reenter(reg, value):
            refused.append(not offset.set_offset_mv(50, acknowledged=True)[0])
            return 0

        bus.after_write = reenter
        self.assertTrue(manual.set_voltage_mv(1000, acknowledged=True)[0])
        self.assertEqual(refused, [True, True])
        self.assertTrue(all(reg in (0x21, 0xE1) for reg, _, _ in bus.writes))
        self.assertFalse(manual._control_inflight)

    def test_canonical_profile_capture_and_replay_cannot_bypass_owned_manual_voltage(self):
        gpu, _, gpu_writes = hardware()
        bus = PMBus()
        offset = mp2888.MP2888Candidate(self.profile, bus, 1, 0x20)
        state = profiles.capture(gpu, offset)
        manual = offset.for_control_method('pmbus')
        self.assertTrue(manual.set_voltage_mv(1000, acknowledged=True)[0])
        writes = list(bus.writes)
        result = profiles.restore(gpu, state, rail=offset, i2c_verified=True, apply_curve=False)
        self.assertFalse(result[0][0])
        self.assertIn('manual', result[0][1].lower())
        self.assertEqual(bus.writes, writes)
        self.assertEqual(gpu_writes, [])
        captured = profiles.capture(gpu, offset)
        self.assertIsNone(captured['i2c'])
        self.assertTrue(any('manual' in reason.lower() for reason in profiles.incomplete(captured)))


if __name__ == '__main__':
    unittest.main()
