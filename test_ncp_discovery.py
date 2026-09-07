# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded Kepler NCP4206 discovery reads identities without hardware writes."""

import unittest
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ncp4206
import profiles
import railctl


class NcpDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.nvapi = SimpleNamespace(ok=True, selected={"devid": 0x1188,
                                                       "subsys": 0x84061043})
        self.ports = {2}
        self.reads = []
        self.values = {0x99: 65, 0x9A: 12952, 0x9B: 1, 0x20: 32}

        def read(rail, reg, width):
            self.reads.append((rail.p.port, rail.addr7, reg, width))
            return self.values.get(reg) if rail.p.port in self.ports else None

        for target, kwargs in (("railctl.Rail.read", {"autospec": True, "side_effect": read}),
                               ("railctl.Rail._raw_write", {}),
                               ("railctl.load_profiles", {"return_value": []})):
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

    def test_non_kepler_unknown_or_disabled_api_do_not_probe(self):
        for architecture in (None, 1, 3, 4, 6):
            self.assertIsNone(railctl.find(self.nvapi, architecture=architecture))
        self.nvapi.ok = False
        self.assertIsNone(railctl.find(self.nvapi, architecture=2))
        self.assertEqual(self.reads, [])

    def test_discovered_port_is_bound_into_transport_name_and_profile_identity(self):
        self.ports = {6}
        rail = railctl.find(self.nvapi, architecture=2)
        self.assertEqual(rail.p.port, 6)
        self.assertEqual(rail.p.src["port"], 6)
        self.assertIn("port 6", rail.p.name)
        self.assertEqual([p for p, _, reg, _ in self.reads if reg == 0x99],
                         list(ncp4206.DISCOVERY_PORTS))
        self.assertEqual({addr for _, addr, _, _ in self.reads}, {0x20})
        at_two = ncp4206.NCP4206(self.nvapi, architecture=2, port=2)
        self.assertNotEqual(profiles.rail_identity(rail), profiles.rail_identity(at_two))

    def test_every_identity_register_is_required(self):
        for reg in list(self.values):
            with self.subTest(reg=reg):
                previous = self.values[reg]
                self.values[reg] = 0
                self.assertIsNone(railctl.find(self.nvapi, architecture=2))
                self.values[reg] = previous

    def test_multiple_matching_ports_are_ambiguous_even_if_reads_are_identical(self):
        self.ports = {2, 6}
        log = Mock()
        self.assertIsNone(railctl.find(self.nvapi, architecture=2, log=log))
        self.assertIn("ambiguous", log.call_args.args[0])
        self.assertFalse(log.call_args.args[1])

    def test_present_rejects_non_kepler_before_transport(self):
        rail = ncp4206.NCP4206(self.nvapi, architecture=3)
        self.assertFalse(rail.present())
        self.assertEqual(self.reads, [])

    def test_datasheet_default_identity_is_detected_and_pinned(self):
        self.values.update({0x9A: 0x0208, 0x9B: 3})
        rail = railctl.find(self.nvapi, architecture=2)
        self.assertEqual(rail.p.src["identity"], [0x41, 0x0208, 3])
        self.assertTrue(rail.present())
        self.values.update({0x9A: 0x3298, 0x9B: 1})
        self.assertFalse(rail.present())
        self.assertEqual(rail.p.src["identity"], [0x41, 0x0208, 3])

    def test_other_onsemi_models_and_revision_combinations_are_rejected(self):
        for model, revision in ((0x0209, 3), (0x0208, 1), (0x3298, 3), (0x3298, 2)):
            with self.subTest(model=model, revision=revision):
                self.values.update({0x9A: model, 0x9B: revision})
                self.assertIsNone(railctl.find(self.nvapi, architecture=2))

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
        with patch("railctl.load_profiles") as generic:
            self.assertIsNone(railctl.find(self.nvapi, subsys=0, architecture=2))
        generic.assert_called_once()  # discover MP recipes independently of PCI IDs


if __name__ == "__main__":
    unittest.main()
