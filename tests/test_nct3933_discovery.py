# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find the NCT3933U by both IDs, on documented straps and any GPU bus port."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import railctl
from druta.controllers import nct3933


class NCT3933DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.nvapi = SimpleNamespace(ok=True, gpu=None,
                                    selected={"devid": 0x1234,
                                              "subsys": 0xDEADBEEF})
        self.routes = {(6, 0x13): {0x5D: 0x39, 0x5E: 0x33,
                                   0x01: 0x80, 0x02: 0x02,
                                   0x03: 0x03, 0x05: 0x00}}
        self.reads = []

        def read(device, reg, width):
            self.reads.append((device.p.port, device.addr7, reg, width))
            return self.routes.get((device.p.port, device.addr7), {}).get(reg)

        for target, kwargs in (
                ("druta.railctl.load_profiles", {"return_value": []}),
                ("druta.controllers.ncp4206.DISCOVERY_PORTS", {"new": ()}),
                ("druta.controllers.nct3933.NCT3933U.read",
                 {"autospec": True, "side_effect": read}),
                ("druta.controllers.nct3933.NCT3933U._raw_write",
                 {"autospec": True})):
            patcher = patch(target, **kwargs)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if target.endswith("._raw_write"):
                self.write = mocked

    def tearDown(self):
        self.write.assert_not_called()

    def test_alt_port_and_strap_identify_without_architecture_or_board_gate(self):
        for architecture in (None, 2, 6):
            with self.subTest(architecture=architecture):
                hits = railctl.discover(self.nvapi, dev_id=0, subsys=0,
                                        architecture=architecture)
                self.assertEqual(len(hits), 1)
                device = hits[0]
                self.assertIsInstance(device, nct3933.NCT3933U)
                self.assertEqual((device.p.port, device.addr7), (6, 0x13))
                self.assertEqual(device.discovery_telemetry["outputs"],
                                 [0x80, 0x02, 0x03])
        self.assertEqual({address for _, address, _, _ in self.reads},
                         set(range(0x10, 0x16)))
        self.assertEqual({port for port, _, _, _ in self.reads}, set(range(8)))
        self.assertNotIn(0x04, {reg for _, _, reg, _ in self.reads})

    def test_both_vendor_bytes_are_required_and_unlisted_address_is_not_scanned(self):
        self.routes = {
            (6, 0x13): {0x5D: 0x39, 0x5E: 0xFF},
            (1, 0x16): {0x5D: 0x39, 0x5E: 0x33, 0x01: 0,
                         0x02: 0, 0x03: 0, 0x05: 0},
        }
        self.assertEqual(railctl.discover(self.nvapi, architecture=None), [])
        self.assertNotIn(0x16, {address for _, address, _, _ in self.reads})
        self.assertIn((6, 0x13, 0x5E, 1), self.reads)

    def test_progress_totals_and_cooperative_cancel(self):
        events = []
        self.routes = {}
        hits = railctl.discover(self.nvapi,
                                progress=lambda done, total, label:
                                events.append((done, total, label)),
                                cancelled=lambda: len(events) >= 5)
        self.assertEqual(hits, [])
        self.assertEqual(events[0][:2], (0, 48))
        self.assertEqual(events[-1][:2], (4, 48))
        self.assertEqual([event[0] for event in events], list(range(5)))
        self.assertTrue(all("NCT3933U" in event[2] for event in events[1:]))
        self.assertEqual(len(self.reads), 4)

    def test_unavailable_backend_does_not_probe_any_route(self):
        self.nvapi.ok = False
        progress = Mock()
        self.assertEqual(railctl.discover(self.nvapi, progress=progress), [])
        self.assertEqual(self.reads, [])
        progress.assert_called_once_with(0, 0, "Preparing controller probes")


if __name__ == "__main__":
    unittest.main()
