# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""NCP4206 discovery uses controller identity, independent of GPU and route."""

import unittest
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import ncp4206
from druta import profiles
from druta import railctl


class NcpDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.nvapi = SimpleNamespace(ok=True, selected={"devid": 0x1188,
                                                       "subsys": 0x84061043})
        self.ports = {2}
        self.addresses = {0x20}
        self.reads = []
        self.values = {0x99: 65, 0x9A: 12952, 0x9B: 1, 0x20: 32}

        def read(rail, reg, width):
            self.reads.append((rail.p.port, rail.addr7, reg, width))
            return self.values.get(reg) if (rail.p.port in self.ports
                                            and rail.addr7 in self.addresses) else None

        for target, kwargs in (("druta.railctl.Rail.read", {"autospec": True, "side_effect": read}),
                               ("druta.railctl.Rail._raw_write", {}),
                               ("druta.railctl.load_profiles", {"return_value": []})):
            p = patch(target, **kwargs)
            mocked = p.start()
            self.addCleanup(p.stop)
            if target.endswith("_raw_write"):
                self.write = mocked

    def tearDown(self):
        self.write.assert_not_called()

    def test_gk104_and_gk110_are_discovered_without_board_id_whitelist(self):
        for devid in (0x1184, 0x1188, 0x1004, 0x100A, 0x1005, 0x100C):
            with self.subTest(devid=devid):
                self.nvapi.selected = {"devid": devid, "subsys": 0xDEADBEEF}
                rail = railctl.find(self.nvapi, architecture=2)
                self.assertIsInstance(rail, ncp4206.NCP4206)
                self.assertEqual(rail.p.port, 2)

    def test_non_kepler_and_unknown_architecture_still_identify_controller(self):
        for architecture in (None, 1, 3, 4, 6):
            self.assertIsInstance(railctl.find(self.nvapi, architecture=architecture), ncp4206.NCP4206)
        self.reads.clear()
        self.nvapi.ok = False
        self.assertIsNone(railctl.find(self.nvapi, architecture=2))
        self.assertEqual(self.reads, [])

    def test_discovered_port_is_bound_into_transport_name_and_profile_identity(self):
        self.ports = {6}
        rail = railctl.find(self.nvapi, architecture=2)
        self.assertEqual(rail.p.port, 6)
        self.assertEqual(rail.p.src["port"], 6)
        self.assertIn("port 6", rail.p.name)
        self.assertEqual([p for p, a, reg, _ in self.reads if reg == 0x99 and a == 0x20],
                         list(ncp4206.DISCOVERY_PORTS))
        self.assertEqual({addr for _, addr, _, _ in self.reads}, set(ncp4206.DISCOVERY_ADDRESSES))
        at_two = ncp4206.NCP4206(self.nvapi, architecture=2, port=2)
        self.assertNotEqual(profiles.rail_identity(rail), profiles.rail_identity(at_two))

    def test_every_identity_register_is_required(self):
        for reg in list(self.values):
            with self.subTest(reg=reg):
                previous = self.values[reg]
                self.values[reg] = None
                self.assertIsNone(railctl.find(self.nvapi, architecture=2))
                self.values[reg] = previous

    def test_multiple_matching_ports_are_ambiguous_even_if_reads_are_identical(self):
        self.ports = {2, 6}
        log = Mock()
        self.assertIsNone(railctl.find(self.nvapi, architecture=2, log=log))
        self.assertIn("ambiguous", log.call_args.args[0])
        self.assertFalse(log.call_args.args[1])

    def test_present_on_maxwell_reads_controller_identity(self):
        rail = ncp4206.NCP4206(self.nvapi, architecture=3)
        self.assertTrue(rail.present())
        self.assertTrue(self.reads)

    def test_datasheet_default_identity_is_detected_and_pinned(self):
        self.values.update({0x9A: 0x0208, 0x9B: 3})
        rail = railctl.find(self.nvapi, architecture=2)
        self.assertEqual(rail.p.src["identity"], [0x41, 0x0208, 3])
        self.assertTrue(rail.present())
        self.values.update({0x9A: 0x3298, 0x9B: 1})
        self.assertFalse(rail.present())
        self.assertEqual(rail.p.src["identity"], [0x41, 0x0208, 3])

    def test_known_models_accept_unsampled_revisions_but_pin_them(self):
        for model, revision in ((0x0208, 1), (0x3298, 3), (0x3298, 2), (0x0208, 0)):
            with self.subTest(model=model, revision=revision):
                self.values.update({0x9A: model, 0x9B: revision})
                rail = railctl.find(self.nvapi, architecture=2)
                self.assertIsNotNone(rail)
                self.values[0x9B] += 1
                self.assertFalse(rail.present())

    def test_other_onsemi_model_remains_unknown_and_is_reported(self):
        self.values[0x9A] = 0x0209
        log = Mock()
        self.assertEqual(railctl.discover(self.nvapi, log=log), [])
        self.assertTrue(any('no understood NCP4206 layout' in call.args[0]
                            for call in log.call_args_list))

    def test_relocated_controller_is_bound_to_actual_address(self):
        self.addresses = {0x37}
        rail = railctl.find(self.nvapi, architecture=6)
        self.assertEqual((rail.addr7, rail.p.src['addr7']), (0x37, 0x37))
        self.assertIn('0x37', rail.p.name)

    def test_all_empty_routes_have_one_identity_read_each(self):
        self.ports = set()
        self.assertEqual(railctl.discover(self.nvapi), [])
        self.assertEqual(len(self.reads), 8 * 112)
        self.assertEqual({(reg, width) for _, _, reg, width in self.reads}, {(0x99, 1)})

    def test_oem_port2_saved_recipe_stays_compatible_without_misnaming_ui(self):
        rail = railctl.find(self.nvapi, architecture=2)
        old_recipe = {"kind": "ncp4206-absolute-v1", "port": 2, "addr7": 32,
                      "identity": [65, 12952, 1], "normal_max": 1281,
                      "xoc_max": 2000, "vid_max": 1600, "min_mv": 600}
        old_hash = hashlib.sha256(json.dumps(old_recipe, sort_keys=True).encode()).hexdigest()
        self.assertEqual(profiles.rail_identity(rail), {
            "profile": "GTX 770 - NVVDD (NCP4206)", "sha256": old_hash,
            "port": 2, "addr7": 32, "rail": "NVVDD"})
        self.assertNotIn("GTX 770", rail.p.name)

    def test_explicit_mismatched_pci_ids_do_not_suppress_kepler_controller_scan(self):
        rail = railctl.find(self.nvapi, dev_id=0, subsys=0, architecture=2)
        self.assertIsInstance(rail, ncp4206.NCP4206)

    def test_generic_profiles_still_reject_explicit_identity_mismatch(self):
        self.ports = set()
        with patch("druta.railctl.load_profiles") as generic:
            self.assertIsNone(railctl.find(self.nvapi, subsys=0, architecture=2))
        generic.assert_called_once()  # discover MP recipes independently of PCI IDs


if __name__ == "__main__":
    unittest.main()
