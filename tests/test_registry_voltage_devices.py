# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-device delay requests retain role labels, partial capability and undo."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta.registry_panel import RegistryPanel


def observation(*, first=95, second=180, slot="0000:09:00.0", unknown=False):
    rows = [
        {"index": 4, "type": 3, "status": "ready", "issues": [],
         "voltage_domain": 1, "operation_type": 1, "switch_delay_us": first},
        {"index": 17, "type": 3, "status": "ready", "issues": [],
         "voltage_domain": 3, "operation_type": 4, "switch_delay_us": second},
    ]
    if unknown:
        rows.append({"index": 20, "type": 91, "status": "unsupported", "issues": ["Unknown device type"]})
    return {"identity": {"slot": slot, "architecture": 8, "device_id": 0x4321, "subsystem_id": 0x76543210},
            "rows": rows}


class VoltageDevicePanelTests(unittest.TestCase):
    def setUp(self):
        self.gpu = SimpleNamespace(slot=lambda: "0000:09:00.0")
        self.app = SimpleNamespace(gpu=self.gpu, log=Mock(), guard=Mock(return_value=True))
        self.panel = RegistryPanel(self.app)
        self.panel.invalidate = Mock()
        self.values, self.enabled, self.items = {}, {}, {}
        self.ui = MagicMock()
        self.ui.set_value.side_effect = self.values.__setitem__
        self.ui.get_value.side_effect = self.values.get

        def configure(tag, **values):
            if "enabled" in values:
                self.enabled[tag] = values["enabled"]
            if "items" in values:
                self.items[tag] = values["items"]

        self.ui.configure_item.side_effect = configure
        self.patch(patch("druta.registry_panel.dpg", self.ui))
        self.factory = self.patch(patch("druta.voltage_device_runtime.VoltageDeviceController"))
        self.controller = self.factory.return_value
        self.controller.gpu = self.gpu
        self.controller.read.return_value = observation()

    def patch(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def read(self):
        self.panel.refresh_voltage_devices()

    def select_second(self):
        self.values["reg_volt_device_select"] = "17: MSVDD / IPC_VMIN"
        self.panel.select_voltage_device()

    def test_read_labels_actual_domain_and_operation_and_keeps_unknown_peer_diagnostic(self):
        self.controller.read.return_value = observation(unknown=True)
        self.read()
        self.assertEqual(self.items["reg_volt_device_select"], ["4: LOGIC / DEFAULT", "17: MSVDD / IPC_VMIN"])
        self.assertIn("Unknown device type", self.values["reg_volt_device_inventory"])
        self.assertEqual(self.values["reg_volt_device_delay"], "95")
        self.assertTrue(self.enabled["reg_volt_device_apply"])
        self.assertFalse(self.enabled["reg_volt_device_restore"])

    def test_apply_binds_index_domain_operation_and_uses_reported_readback(self):
        self.read()
        self.values["reg_volt_device_delay"] = "96"
        self.controller.set.return_value = observation(first=96)
        self.panel.apply_voltage_device_delay()
        self.controller.set.assert_called_once_with(4, 96, voltage_domain=1, operation_type=1)
        self.assertEqual(self.values["reg_volt_device_delay"], "96")
        self.assertTrue(self.enabled["reg_volt_device_restore"])
        self.assertIn("Applied live", self.values["reg_volt_device_status"])
        self.panel.invalidate.assert_not_called()

    def test_secondary_role_uses_its_own_index_value_and_initial_restore(self):
        self.read()
        self.select_second()
        self.assertEqual(self.values["reg_volt_device_delay"], "180")
        self.values["reg_volt_device_delay"] = "181"
        self.controller.set.return_value = observation(second=181)
        self.panel.apply_voltage_device_delay()
        self.controller.set.assert_called_once_with(17, 181, voltage_domain=3, operation_type=4)
        self.controller.set.reset_mock()
        self.controller.set.return_value = observation()
        self.panel.restore_voltage_device_delay()
        self.controller.set.assert_called_once_with(17, 180, voltage_domain=3, operation_type=4)
        self.assertFalse(self.enabled["reg_volt_device_restore"])

    def test_uint32_max_is_not_lost_through_signed_dpg_integer_widget(self):
        self.read()
        self.values["reg_volt_device_delay"] = "4294967295"
        self.controller.set.return_value = observation(first=0xFFFFFFFF)
        self.panel.apply_voltage_device_delay()
        self.controller.set.assert_called_once_with(4, 0xFFFFFFFF, voltage_domain=1, operation_type=1)

    def test_invalid_input_does_not_call_controller_or_discard_valid_read(self):
        self.read()
        initial = self.panel.voltage_device_snapshot
        for text in ("", "1.5", "1e3", "0", "-1", "4294967296", "delay"):
            with self.subTest(text=text):
                self.values["reg_volt_device_delay"] = text
                self.panel.apply_voltage_device_delay()
                self.assertIs(self.panel.voltage_device_snapshot, initial)
                self.assertTrue(self.enabled["reg_volt_device_apply"])
        self.controller.set.assert_not_called()

    def test_transient_read_failure_then_recovery_retains_initial_per_device_values(self):
        self.read()
        self.controller.read.side_effect = RuntimeError("temporary")
        self.read()
        self.assertFalse(self.enabled["reg_volt_device_apply"])
        self.controller.read.side_effect = None
        self.controller.read.return_value = observation(first=96)
        self.read()
        self.assertTrue(self.enabled["reg_volt_device_restore"])
        self.controller.set.return_value = observation()
        self.panel.restore_voltage_device_delay()
        self.controller.set.assert_called_once_with(4, 95, voltage_domain=1, operation_type=1)

    def test_adapter_switch_requires_new_read_and_independent_initial_capture(self):
        self.read()
        other = SimpleNamespace(slot=lambda: "0000:0A:00.0")
        self.app.gpu = other
        self.panel.apply_voltage_device_delay()
        self.controller.set.assert_not_called()
        self.assertFalse(self.enabled["reg_volt_device_apply"])
        next_controller = Mock(gpu=other)
        next_controller.read.return_value = observation(first=77, slot=other.slot())
        self.factory.return_value = next_controller
        self.read()
        self.assertEqual(self.values["reg_volt_device_delay"], "77")
        self.assertFalse(self.enabled["reg_volt_device_restore"])
        self.assertEqual(self.factory.call_count, 2)

    def test_verified_rollback_restores_visible_request_without_disabling_control(self):
        self.read()
        error = RuntimeError("PMU submission failed")
        error.rollback = {"verified": True, "readback": observation()}
        self.controller.set.side_effect = error
        self.values["reg_volt_device_delay"] = "96"
        self.panel.apply_voltage_device_delay()
        self.assertEqual(self.values["reg_volt_device_delay"], "95")
        self.assertTrue(self.enabled["reg_volt_device_apply"])
        self.assertIn("Original requests restored", self.values["reg_volt_device_status"])

    def test_unverified_failure_requires_fresh_read_without_claiming_undo(self):
        self.read()
        error = RuntimeError("Readback unavailable")
        error.rollback = {"verified": False, "readback": None}
        self.controller.set.side_effect = error
        self.panel.apply_voltage_device_delay()
        self.assertFalse(self.enabled["reg_volt_device_apply"])
        self.assertNotIn("Original requests restored", self.values["reg_volt_device_status"])
        self.panel.invalidate.assert_not_called()

    def test_empty_inventory_or_unknown_only_keeps_diagnostics_but_no_write(self):
        for rows in ([], [{"index": 9, "type": 91, "status": "unsupported", "issues": ["Unknown type"]}]):
            with self.subTest(rows=rows):
                sample = observation()
                sample["rows"] = rows
                self.controller.read.return_value = sample
                self.read()
                self.assertFalse(self.enabled["reg_volt_device_apply"])
                self.assertIsNone(self.panel.voltage_device_snapshot)
        self.controller.set.assert_not_called()

    def test_refresh_preserves_selected_operation_without_applying(self):
        self.read()
        self.select_second()
        self.read()
        self.assertEqual(self.values["reg_volt_device_select"], "17: MSVDD / IPC_VMIN")
        self.assertEqual(self.values["reg_volt_device_delay"], "180")
        self.controller.set.assert_not_called()

    def test_guard_refusal_does_not_write_or_change_values(self):
        self.read()
        old = copy.deepcopy(self.values)
        self.app.guard.return_value = False
        self.panel.apply_voltage_device_delay()
        self.controller.set.assert_not_called()
        self.assertEqual(self.values, old)


if __name__ == "__main__":
    unittest.main()
