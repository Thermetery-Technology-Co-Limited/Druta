# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep board-specific I2C profiles on the selected GPU before bus probing."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import railctl


class RailIdentityTests(unittest.TestCase):
    def setUp(self):
        self.profile = railctl.Profile.__new__(railctl.Profile)
        self.profile.pci_device = {"0x1e02"}
        self.profile.pci_subsys = {"0x12a310de"}
        self.profile.addrs = [0x20]
        self.profile.name = "measured board"
        self.nvapi = SimpleNamespace(selected={
            "devid": 0x1E02, "subsys": 0x12A310DE})
        for target, name in (("druta.railctl.load_profiles", "profiles"),
                             ("druta.railctl.Rail", "rail"),
                             ("druta.ncp4206.NCP4206", "ncp")):
            p = patch(target)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        self.profiles.return_value = [self.profile]
        self.ncp.return_value.present.return_value = False
        self.rail.return_value.present.return_value = True

    def test_find_derives_both_ids_from_selected_gpu(self):
        result = railctl.find(self.nvapi)
        self.assertIs(result, self.rail.return_value)
        self.rail.assert_called_once_with(self.profile, self.nvapi, addr7=0x20)

    def test_other_board_is_not_probed_even_if_bus_identity_would_match(self):
        for identity in ({"devid": 0x1188, "subsys": 0x12A310DE},
                         {"devid": 0x1E02, "subsys": 0xFFFFFFFF}):
            with self.subTest(identity=identity):
                self.nvapi.selected = identity
                self.assertIsNone(railctl.find(self.nvapi))
                self.rail.assert_not_called()

    def test_constrained_profile_requires_each_identity_field(self):
        for identity in (None, {}, {"devid": 0x1E02},
                         {"subsys": 0x12A310DE}):
            with self.subTest(identity=identity):
                self.nvapi.selected = identity
                self.assertIsNone(railctl.find(self.nvapi))
                self.rail.assert_not_called()

    def test_matching_explicit_ids_and_selected_defaults_are_preserved(self):
        for kwargs in ({"dev_id": 0x1E02}, {"subsys": 0x12A310DE},
                       {"dev_id": 0x1E02, "subsys": 0x12A310DE}):
            with self.subTest(kwargs=kwargs):
                self.assertIs(railctl.find(self.nvapi, **kwargs),
                              self.rail.return_value)

    def test_conflicting_explicit_ids_refuse_before_any_bus_probe(self):
        for kwargs in ({"dev_id": 0x1188}, {"subsys": 0xFFFFFFFF}):
            with self.subTest(kwargs=kwargs):
                log = Mock()
                self.assertIsNone(railctl.find(self.nvapi, log=log, **kwargs))
                self.ncp.assert_not_called()
                self.rail.assert_not_called()
                self.profiles.assert_called()  # MP discovery bypasses board IDs
                self.assertFalse(log.call_args.args[1])

    def test_explicit_identity_can_fill_missing_selected_fields(self):
        self.nvapi.selected = {"devid": 0x1E02}
        self.assertIs(railctl.find(self.nvapi, subsys=0x12A310DE),
                      self.rail.return_value)

    def test_unconstrained_profile_still_requires_live_bus_identity(self):
        self.nvapi.selected = None
        self.profile.pci_device = self.profile.pci_subsys = set()
        self.rail.return_value.present.return_value = False
        self.assertIsNone(railctl.find(self.nvapi))
        self.rail.return_value.present.assert_called_once()


if __name__ == "__main__":
    unittest.main()
