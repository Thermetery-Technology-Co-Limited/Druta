# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live VF policies use the selected adapter and retain a practical undo."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta.registry_panel import RegistryPanel

MONO, SMOOTH = "clock_vf.monotonicity", "clock_vf.smoothening"


def observation(*, slot="0000:03:00.0", monotonicity=True, smoothing=True):
    return {
        "identity": {"slot": slot, "architecture": 9, "device_id": 0x1234, "subsystem_id": 0x56789ABC},
        "vf_monotonicity_enforced": monotonicity,
        "vf_smoothening_enforced": smoothing,
    }


class LiveClockVfPanelTests(unittest.TestCase):
    def setUp(self):
        self.gpu = SimpleNamespace(slot=lambda: "0000:03:00.0")
        self.app = SimpleNamespace(gpu=self.gpu, log=Mock(), guard=Mock(return_value=True))
        self.panel = RegistryPanel(self.app)
        self.panel.invalidate = Mock()
        self.values = {}
        self.enabled = {}
        self.ui = MagicMock()
        self.ui.set_value.side_effect = self.values.__setitem__
        self.ui.get_value.side_effect = self.values.get
        self.ui.configure_item.side_effect = lambda tag, **kw: self.enabled.update({tag: kw.get("enabled")})
        self.patch(patch("druta.registry_panel.dpg", self.ui))
        self.factory = self.patch(patch("druta.clock_vf_runtime.ClockVfController"))
        self.controller = self.factory.return_value
        self.controller.gpu = self.gpu
        self.controller.read.return_value = observation()

    def patch(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def read(self):
        self.panel.refresh_clock_vf_live()

    def test_read_is_independent_of_registry_field_or_voltage_gate_reads(self):
        self.read()
        self.factory.assert_called_once_with(self.gpu)
        self.assertTrue(self.values["reg_vf_live_monotonicity"])
        self.assertTrue(self.values["reg_vf_live_smoothening"])
        self.assertTrue(self.enabled["reg_vf_live_apply"])
        self.assertFalse(self.enabled["reg_vf_live_restore"])
        self.assertEqual(self.panel.fields, {})

    def test_apply_both_named_values_and_enable_restore(self):
        self.read()
        self.values.update(reg_vf_live_monotonicity=False, reg_vf_live_smoothening=True)
        self.controller.set.return_value = observation(monotonicity=False)
        self.panel.apply_clock_vf_live()
        self.controller.set.assert_called_once_with({MONO: False, SMOOTH: True})
        self.assertTrue(self.enabled["reg_vf_live_restore"])
        self.panel.invalidate.assert_called_once_with()
        self.assertIn("Applied live", self.values["reg_vf_live_status"])

    def test_refresh_retains_initial_values_and_restore_targets_them(self):
        self.read()
        self.controller.read.return_value = observation(monotonicity=False)
        self.read()
        self.assertEqual(self.panel.clock_vf_initial, {MONO: True, SMOOTH: True})
        self.controller.set.return_value = observation()
        self.panel.restore_clock_vf_live()
        self.controller.set.assert_called_once_with({MONO: True, SMOOTH: True})
        self.assertFalse(self.enabled["reg_vf_live_restore"])

    def test_switch_adapter_requires_new_read_and_has_its_own_initial_values(self):
        self.read()
        other = SimpleNamespace(slot=lambda: "0000:05:00.0")
        self.app.gpu = other
        self.panel.apply_clock_vf_live()
        self.controller.set.assert_not_called()
        self.assertFalse(self.enabled["reg_vf_live_apply"])
        other_controller = Mock(gpu=other)
        other_controller.read.return_value = observation(slot="0000:05:00.0", smoothing=False)
        self.factory.return_value = other_controller
        self.read()
        self.assertEqual(self.panel.clock_vf_initial, {MONO: True, SMOOTH: False})
        self.assertEqual(self.factory.call_count, 2)

    def test_read_failure_disables_live_apply_then_fresh_read_recovers(self):
        self.read()
        self.controller.read.side_effect = RuntimeError("temporary read failure")
        self.read()
        self.assertFalse(self.enabled["reg_vf_live_apply"])
        self.assertIsNone(self.panel.clock_vf_snapshot)
        self.controller.read.side_effect = None
        self.read()
        self.assertTrue(self.enabled["reg_vf_live_apply"])
        self.assertEqual(self.panel.clock_vf_initial, {MONO: True, SMOOTH: True})

    def test_verified_rollback_displays_actual_restored_state(self):
        self.read()
        error = RuntimeError("Startup mode rejects this request")
        error.rollback = {"verified": True, "readback": observation()}
        self.controller.set.side_effect = error
        self.values.update(reg_vf_live_monotonicity=False)
        self.panel.apply_clock_vf_live()
        self.assertTrue(self.values["reg_vf_live_monotonicity"])
        self.assertTrue(self.enabled["reg_vf_live_apply"])
        self.assertIn("Original policies restored", self.values["reg_vf_live_status"])
        self.panel.invalidate.assert_not_called()

    def test_unverified_failure_requires_read_and_never_claims_restore(self):
        self.read()
        error = RuntimeError("ambiguous SET")
        error.rollback = {"verified": False, "readback": None}
        self.controller.set.side_effect = error
        self.panel.apply_clock_vf_live()
        self.assertFalse(self.enabled["reg_vf_live_apply"])
        self.assertNotIn("Original policies restored", self.values["reg_vf_live_status"])
        self.panel.invalidate.assert_called_once_with()

    def test_guard_refusal_does_not_write_or_change_ui(self):
        self.read()
        before = copy.deepcopy(self.values)
        self.app.guard.return_value = False
        self.panel.apply_clock_vf_live()
        self.controller.set.assert_not_called()
        self.assertEqual(self.values, before)

    def test_restore_without_initial_read_does_nothing(self):
        self.panel.restore_clock_vf_live()
        self.controller.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
