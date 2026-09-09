# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fan-only undo must not change tuning modes or require a V/F refresh."""
import unittest
from unittest.mock import Mock, patch
from druta.druta import Druta


class FanProfileUiTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = Mock()
        self.app.rail = None
        self.app._i2c_busy = False
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        self.app.guard = Mock(return_value=True)
        self.app.rail_for_profile = Mock(return_value=None)
        self.app.autosave_before = Mock(return_value=True)
        self.app.log = Mock()
        self.app.profile_failure = Mock()
        self.app.sync_risk_ui = Mock()
        self.state = {"schema": 2, "scope": "fan"}

    def test_loading_fan_undo_captures_only_fan_and_preserves_risk_modes(self):
        self.app.finish_profile_load = Mock()
        with patch("druta.druta.profiles.preflight", return_value=None), \
                patch("druta.druta.dpg.set_value") as set_value:
            self.app.begin_profile_load("fan-undo", self.state)
        self.app.autosave_before.assert_called_once_with("load-fan-undo", scope="fan")
        self.app.finish_profile_load.assert_called_once_with("fan-undo", self.state, False)
        self.app.sync_risk_ui.assert_not_called()
        set_value.assert_not_called()
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": True})

    def test_unreadable_fan_undo_blocks_even_manual_restore(self):
        self.app.autosave_before.return_value = False
        self.app.finish_profile_load = Mock()
        with patch("druta.druta.profiles.preflight", return_value=None):
            self.app.begin_profile_load("fan-undo", self.state)
        self.app.finish_profile_load.assert_not_called()
        self.app.profile_failure.assert_called_once()

    def test_finishing_fan_restore_does_not_require_curve_or_rail_reads(self):
        self.app.i2c_verified = Mock(return_value=False)
        self.app.sync_sliders_from_gpu = Mock()
        self.app.sync_profile_rail_sliders = Mock()
        self.app.refresh_volt_limits = Mock()
        self.app.vf_read = Mock()
        with patch("druta.druta.profiles.restore", return_value=[(True, "fan restored")]):
            self.app.finish_profile_load("fan-undo", self.state)
        self.app.sync_sliders_from_gpu.assert_called_once_with(self.state)
        self.app.sync_profile_rail_sliders.assert_not_called()
        self.app.refresh_volt_limits.assert_not_called()
        self.app.vf_read.assert_not_called()
        self.assertFalse(self.app._profile_applying)
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": True})
