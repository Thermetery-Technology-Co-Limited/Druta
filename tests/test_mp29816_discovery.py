# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controller discovery, PAGE binding and profile compatibility without a GPU."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from druta import mp29816, profiles, railctl
from tests.test_mp2888_discovery import Bus
from tests.test_mp29816_profile import profile, RESPONSES


class MP29816DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.profile = profile()

    def add(self, port=6, addr=0x45, page=0, selector=1):
        values = {reg: value for (reg, _), value in RESPONSES.items()}
        values[0], values[0x29] = page, (selector << 10) | 0x20
        self.bus.devices[port, addr] = values
        return values

    def scan(self):
        hits = mp29816.discover(self.bus, self.profile)
        self.assertEqual(self.bus.writes, [])
        return hits

    def test_unsampled_gpu_route_oem_strings_and_revision_do_not_gate(self):
        values = self.add()
        values.update({0x99: None, 0x9A: 0, 0x9B: 0xFE0002})
        original = deepcopy(self.profile.src)
        [rail] = self.scan()
        self.assertEqual((rail.p.port, rail.addr7), (6, 0x45))
        self.assertTrue(rail.p.candidate_for(0x1234, 0x5678))
        self.assertEqual(rail.p.rail, 'PAGE 0 output')
        self.assertIn('physical rail unassigned', rail.p.name)
        self.assertEqual(rail.discovery_diagnostics['revision_block'], 0xFE0002)
        self.assertEqual(rail.discovery_telemetry['vout_mv'], 1150)
        self.assertFalse(rail.p.read_only)
        self.assertEqual(self.profile.src, original)

    def test_wrong_or_shifted_model_id_never_identifies_even_with_oem_strings(self):
        values = self.add()
        for value in (None, 0, 0x0002A816, 0x0002A81603, 0x0002A81704):
            with self.subTest(value=value):
                values[0xAD] = value
                self.assertEqual(self.scan(), [])

    def test_unknown_page_and_unavailable_scale_are_reported_without_write(self):
        values = self.add()
        for key, value in ((0, 2), (0, None), (0x29, None)):
            original = dict(values)
            values[key] = value
            log = Mock()
            self.assertEqual(mp29816.discover(self.bus, self.profile, log), [])
            self.assertTrue(any('PAGE/scaling unavailable' in call.args[0]
                                for call in log.call_args_list))
            self.assertEqual(self.bus.writes, [])
            values.update(original)

    def test_all_documented_scales_on_both_pages_decode_from_runtime(self):
        for page in (0, 1):
            for selector, lsb in enumerate(mp29816.VOUT_SCALES_MV):
                with self.subTest(page=page, selector=selector):
                    self.bus.devices.clear()
                    values = self.add(page=page, selector=selector)
                    values[0x8B] = 0xF123
                    [rail] = self.scan()
                    self.assertEqual(rail.telemetry()['vout_mv'], 0x123 * lsb)
                    self.assertEqual(rail.p.read_only, selector != 1)
                    self.assertEqual(rail.p.writable, {0x22} if selector == 1 else set())
                    if selector != 1:
                        self.assertIsNone(rail.telemetry()['offset_mv'])
                        self.assertFalse(rail.set_offset_mv(5, acknowledged=True)[0])
        self.assertEqual(self.bus.writes, [])

    def test_selected_page_change_invalidates_binding_and_next_scan_finds_new_page(self):
        values = self.add()
        [rail] = self.scan()
        values[0] = 1
        self.assertFalse(rail.present())
        self.assertEqual(rail.telemetry(), {})
        self.assertFalse(rail.set_offset_mv(5, acknowledged=True)[0])
        [new] = self.scan()
        self.assertEqual(new.page, 1)
        self.assertNotEqual(profiles.rail_identity(new), profiles.rail_identity(rail))

    def test_scale_change_around_telemetry_discards_sample(self):
        values = self.add()
        [rail] = self.scan()
        def voltage():
            values[0x29] = 0x820
            return 230
        values[0x8B] = voltage
        self.assertEqual(rail.telemetry(), {})
        self.assertFalse(rail.set_offset_mv(5, acknowledged=True)[0])
        self.assertEqual(self.bus.writes, [])

    def test_both_pages_write_the_understood_signed_low_byte_preserving_upper_byte(self):
        for page in (0, 1):
            self.bus.devices.clear()
            values = self.add(page=page)
            [rail] = self.scan()
            values[0x22] = 0xA500
            writes = []
            def write(reg, value, width):
                writes.append((reg, value, width))
                values[reg] = value
                return True
            rail._raw_write = write
            self.assertTrue(rail.set_offset_mv(-5, acknowledged=True)[0])
            self.assertEqual(writes, [(0x22, 0xA5FF, 2)])

    def test_original_profile_hash_survives_runtime_discovery(self):
        self.add(port=2, addr=0x30)
        [rail] = self.scan()
        legacy = railctl.Rail(self.profile, self.bus)
        self.assertEqual(rail.p.src, self.profile.src)
        self.assertEqual(profiles.rail_identity(rail), profiles.rail_identity(legacy))
        self.assertEqual(rail.p.rail, 'PAGE 0 output')

    def test_relocated_profile_hash_binds_page_scale_and_route(self):
        values = self.add()
        [rail] = self.scan()
        [again] = self.scan()
        self.assertEqual(profiles.rail_identity(rail), profiles.rail_identity(again))
        values[0x29] = 0x820
        [different] = self.scan()
        self.assertNotEqual(profiles.rail_identity(rail), profiles.rail_identity(different))

    def test_multiple_locations_are_returned_without_arbitrary_selection(self):
        self.add()
        self.add(port=3, addr=0x20, page=1)
        self.assertEqual(len(self.scan()), 2)

    def test_empty_bus_cost_is_one_model_read_per_unicast_route(self):
        self.assertEqual(self.scan(), [])
        self.assertEqual(len(self.bus.reads), 8 * 112)
        self.assertEqual({(reg, width) for _, _, reg, width in self.bus.reads}, {(0xAD, 5)})

    def test_route_exception_does_not_hide_other_routes(self):
        self.add()
        # Exercise exceptions at the public read boundary, including the first route.
        original_read = railctl.Rail.read
        def read(rail, reg, width):
            if rail.p.port == 0:
                raise RuntimeError('temporary route error')
            return original_read(rail, reg, width)
        with patch.object(railctl.Rail, 'read', read):
            self.assertEqual(len(self.scan()), 1)


if __name__ == '__main__':
    unittest.main()
