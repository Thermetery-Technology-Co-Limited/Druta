# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controller-based discovery ignores board gates only for supported scanners."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from druta import railctl


class DiscoveryRouting(unittest.TestCase):
    def setUp(self):
        self.nvapi = SimpleNamespace(ok=True, selected={"devid": 0x9999, "subsys": 0x12345678})
        self.mp = SimpleNamespace(regulator="MPS MP2888A", candidate_for=Mock(return_value=False))
        self.generic = SimpleNamespace(regulator="Other", candidate_for=Mock(return_value=False))
        self.hit = SimpleNamespace(p=SimpleNamespace(name="MP candidate", port=4), addr7=0x24)
        self.scanner_patch = patch("druta.mp2888.discover", return_value=[self.hit])
        self.scan = self.scanner_patch.start()
        self.addCleanup(self.scanner_patch.stop)
        self.profiles_patch = patch("druta.railctl.load_profiles", return_value=[self.mp, self.generic])
        self.profiles_patch.start()
        self.addCleanup(self.profiles_patch.stop)

    def test_mp_uses_live_scan_even_when_selected_pci_is_not_in_recipe(self):
        self.assertEqual(railctl.discover(self.nvapi, architecture=4), [self.hit])
        self.scan.assert_called_once_with(self.nvapi, self.mp, log=None)
        self.mp.candidate_for.assert_not_called()
        self.generic.candidate_for.assert_called_once_with(0x9999, 0x12345678)

    def test_mp_scans_selected_handle_even_with_conflicting_supplied_ids(self):
        self.assertEqual(railctl.discover(self.nvapi, dev_id=1, subsys=2, architecture=4), [self.hit])
        self.mp.candidate_for.assert_not_called()
        self.generic.candidate_for.assert_not_called()
        self.scan.assert_called_once_with(self.nvapi, self.mp, log=None)

    def test_find_requires_exactly_one_candidate(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                self.scan.return_value = [self.hit] * count
                self.assertIs(railctl.find(self.nvapi, architecture=4), self.hit if count == 1 else None)

    def test_kepler_ncp_and_mp_candidates_are_both_returned(self):
        ncp = SimpleNamespace(present=lambda: True)
        with patch("druta.ncp4206.NCP4206", return_value=ncp), \
                patch("druta.ncp4206.DISCOVERY_PORTS", (2,)):
            self.assertEqual(railctl.discover(self.nvapi, architecture=2), [ncp, self.hit])


if __name__ == "__main__":
    unittest.main()
