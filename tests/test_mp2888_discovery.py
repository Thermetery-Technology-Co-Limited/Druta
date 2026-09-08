# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Discovery uses only reads and binds candidates to their actual bus location."""
import ctypes
from copy import deepcopy
from pathlib import Path
import tomllib
import unittest

from druta import mp2888
from druta import profiles
from druta import railctl


class Bus:
    ok = True
    gpu = 123
    selected = {'devid': 0xDEAD, 'subsys': 0xCAFEBABE}

    def __init__(self):
        self.devices = {}
        self.reads = []
        self.writes = []

    def add(self, port=1, addr=0x20, **overrides):
        registers = {0xBE: 0x80 | addr, 0x23: 0, 0x24: 208, 0x8B: 682,
                     0x8C: 0xE01D, 0x8D: 285, 0x44: 3,
                     0x27: 0x25, 0x28: 0x88, 0x29: 0x11}
        registers.update({int(k, 16): v for k, v in overrides.items()})
        self.devices[port, addr] = registers
        return registers

    def _i(self, fn_id, *signature):
        if fn_id == railctl.I2C_WRITE_EX:
            def write(*args):
                self.writes.append(args)
                raise AssertionError('discovery must never write')
            return write
        if fn_id != railctl.I2C_READ_EX:
            raise AssertionError(fn_id)
        def read(gpu, ptr, _unk):
            s = ctypes.cast(ptr, ctypes.POINTER(railctl._V3)).contents
            key = (s.portId, s.i2cDevAddress >> 1)
            reg = s.pbI2cRegAddress[0]
            self.reads.append((*key, reg, s.cbSize))
            value = self.devices.get(key, {}).get(reg)
            if callable(value):
                value = value()
            if value is None:
                return -1
            for i in range(s.cbSize):
                s.pbData[i] = (value >> (8 * i)) & 0xFF
            return 0
        return read


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        with (Path(__file__).resolve().parents[1] / 'i2c/rtx2080ti-mp2888a.toml').open('rb') as f:
            self.profile = railctl.Profile(tomllib.load(f), 'original')
        self.bus = Bus()

    def scan(self):
        result = mp2888.discover(self.bus, self.profile)
        self.assertEqual(self.bus.writes, [])
        return result

    def test_no_device_ids_gate_and_no_source_mutation(self):
        original = deepcopy(self.profile.src)
        self.bus.add()
        [rail] = self.scan()
        self.assertIn('candidate', rail.p.name)
        self.assertIn('port 1, 0x20', rail.p.name)
        self.assertEqual(rail.p.pci_device, set())
        self.assertTrue(rail.p.weak_id)
        self.assertEqual(self.profile.src, original)
        self.assertEqual(rail.telemetry()['iout_a'], 7.25)
        self.assertEqual(rail.telemetry()['vrm_temp_c'], 28.5)

    def test_all_ports_addresses_scanned_and_preferred_address_first(self):
        for port, addr in ((0, 0x08), (7, 0x77), (3, 0x31)):
            self.bus.add(port, addr)
        result = self.scan()
        self.assertEqual([(r.p.port, r.addr7) for r in result],
                         [(0, 0x08), (3, 0x31), (7, 0x77)])
        reads = {(p, a) for p, a, _, _ in self.bus.reads}
        self.assertEqual(reads, {(p, a) for p in range(8) for a in range(8, 0x78)})
        first_other = next(i for i, r in enumerate(self.bus.reads) if r[1] != 0x20)
        self.assertEqual({r[0] for r in self.bus.reads[:first_other]}, set(range(8)))
        for rail in result:
            self.assertEqual(rail.p.src['bus'], {'port': rail.p.port, 'addr7': rail.addr7})

    def test_multiple_candidates_are_returned_without_selecting_one(self):
        self.bus.add(1, 0x20)
        self.bus.add(2, 0x20)
        self.bus.add(1, 0x21)
        self.assertEqual(len(self.scan()), 3)

    def test_either_address_mode_and_nondefault_user_ids(self):
        self.bus.add(6, 0x33, BE=0x33, **{'27': 0, '28': 0xFF, '29': None})
        [rail] = self.scan()
        self.assertEqual(rail.discovery_diagnostics,
                         {'vendor_id_user': 0, 'product_id_user': 255,
                          'product_rev_user': None})

    def test_missing_or_wrong_field_layout_does_not_pass(self):
        for reg, value in ((0xBE, 0xA1), (0x23, None), (0x23, 0x100),
                           (0x23, 113), (0x23, 0x90), (0x24, 0x200),
                           (0x24, 0), (0x8B, 0xF300), (0x8B, 0),
                           (0x8C, 0), (0x8C, 0xFFFF), (0x8D, 0x800),
                           (0x8D, 1501), (0x44, None)):
            with self.subTest(reg=hex(reg), value=value):
                registers = self.bus.add()
                registers[reg] = value
                self.assertEqual(self.scan(), [])

    def test_valid_nondefault_offset_limit_and_current_resolution(self):
        self.bus.add(**{'23': 0x91, '24': 256, '44': 8, '8C': 0xE100})
        [rail] = self.scan()
        self.assertEqual(rail.discovery_telemetry['offset_mv'], -693.75)
        self.assertEqual(rail.telemetry()['iout_a'], 128.0)
        self.assertEqual(rail.telemetry()['vout_max_mv'], 1600.0)

    def test_repeatability_rejects_control_and_erratic_telemetry(self):
        for reg, pair in ((0x23, [0, 1]), (0x8B, [500, 1400]),
                          (0x8D, [250, 400]), (0x44, [0, 8])):
            with self.subTest(reg=reg):
                registers = self.bus.add()
                values = iter(pair)
                registers[reg] = lambda: next(values)
                self.assertEqual(self.scan(), [])

    def test_ordinary_governor_transition_is_accepted(self):
        registers = self.bus.add()
        values = iter([682, 1050])
        registers[0x8B] = lambda: next(values)
        [rail] = self.scan()
        self.assertEqual(rail.discovery_telemetry['vout_mv'], 866)

    def test_lost_fingerprint_rechecked_and_telemetry_cleared(self):
        registers = self.bus.add()
        [rail] = self.scan()
        registers[0xBE] = 0xA1
        self.assertFalse(rail.present())
        self.assertEqual(rail.telemetry(), {})
        ok, _ = rail.set_offset_mv(25, acknowledged=True)
        self.assertFalse(ok)
        self.assertEqual(self.bus.writes, [])

    def test_legacy_profile_identity_is_preserved_at_original_location(self):
        self.bus.add()
        [rail] = self.scan()
        legacy = railctl.Rail(self.profile, self.bus)
        self.assertEqual(profiles.rail_identity(rail), profiles.rail_identity(legacy))

    def test_relocated_profile_identity_pins_location_and_recipe(self):
        self.bus.add(4, 0x24)
        [rail] = self.scan()
        saved = profiles.rail_identity(rail)
        self.assertEqual((saved['port'], saved['addr7']), (4, 0x24))
        self.assertNotEqual(saved['sha256'], profiles.rail_identity(
            railctl.Rail(self.profile, self.bus))['sha256'])
        [again] = self.scan()
        self.assertEqual(saved, profiles.rail_identity(again))

    def test_unavailable_nvapi_is_not_probed(self):
        self.bus.ok = False
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.bus.reads, [])

    def test_reserved_addresses_and_ports_rejected(self):
        for port, addr in ((-1, 0x20), (8, 0x20), (1, 0), (1, 0x78)):
            with self.assertRaises(ValueError):
                mp2888.MP2888Candidate(self.profile, self.bus, port, addr)


if __name__ == '__main__':
    unittest.main()
