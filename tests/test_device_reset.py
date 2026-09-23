# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware-free tests for the deliberately narrow PnP device restart path."""
import subprocess
import unittest
from unittest.mock import Mock, patch

from druta import devicereset


GPU = devicereset.DeviceTarget(
    "0000:01:00.0", "PCI\\VEN_10DE&DEV_2B85&SUBSYS_00000000&REV_A1\\4&123", "NVIDIA Example"
)


class TargetBindingTests(unittest.TestCase):
    def test_only_exact_nvidia_pci_bus_device_function_becomes_a_target(self):
        target = devicereset._target_from_properties(
            instance_id=GPU.instance_id,
            name=GPU.name,
            enumerator="PCI",
            hardware_ids=["PCI\\VEN_10DE&DEV_2B85&SUBSYS_00000000&REV_A1"],
            bus=1,
            address=(0 << 16) | 0,
        )
        self.assertEqual(target, GPU)
        self.assertIsNone(devicereset._target_from_properties(
            instance_id=GPU.instance_id, name=GPU.name, enumerator="PCI",
            hardware_ids=["PCI\\VEN_1002&DEV_744C"], bus=1, address=0,
        ))
        self.assertIsNone(devicereset._target_from_properties(
            instance_id=GPU.instance_id, name=GPU.name, enumerator="USB",
            hardware_ids=["PCI\\VEN_10DE&DEV_2B85"], bus=1, address=0,
        ))

    def test_resolve_requires_one_exact_slot_and_refuses_unknown_segment(self):
        with patch.object(devicereset, "_enumerate_display_pci_devices", return_value=[GPU]):
            self.assertEqual(devicereset.resolve_target("0000:01:00.0"), GPU)
            with self.assertRaisesRegex(devicereset.DeviceResetError, "no present"):
                devicereset.resolve_target("0000:02:00.0")
            with self.assertRaisesRegex(devicereset.DeviceResetError, "non-zero PCI segment"):
                devicereset.resolve_target("0001:01:00.0")

    def test_resolve_rejects_duplicate_bdf_claims(self):
        duplicate = devicereset.DeviceTarget(GPU.slot, GPU.instance_id + "-other", GPU.name)
        with patch.object(devicereset, "_enumerate_display_pci_devices", return_value=[GPU, duplicate]):
            with self.assertRaisesRegex(devicereset.DeviceResetError, "multiple"):
                devicereset.resolve_target(GPU.slot)

    def test_unrelated_adapter_without_pci_address_does_not_block_nvidia_binding(self):
        fake_adapter = object()
        with patch.object(devicereset, "_property_string", return_value="ROOT\\RDPDR") as string, \
                patch.object(devicereset, "_property_multisz", side_effect=devicereset.DeviceResetError("no hardware IDs")) as hardware_ids, \
                patch.object(devicereset, "_property_dword") as dword, \
                patch.object(devicereset, "_instance_id") as instance_id:
            self.assertIsNone(devicereset._target_from_display_info(object(), object(), fake_adapter))
        string.assert_called_once_with(unittest.mock.ANY, unittest.mock.ANY, fake_adapter,
                                       devicereset._SPDRP_ENUMERATOR_NAME)
        hardware_ids.assert_not_called()
        dword.assert_not_called()
        instance_id.assert_not_called()

    def test_matching_nvidia_adapter_with_missing_bdf_property_fails_closed(self):
        fake_adapter = object()

        def strings(_api, _handle, _info, property_id):
            return {devicereset._SPDRP_ENUMERATOR_NAME: "PCI", devicereset._SPDRP_FRIENDLYNAME: GPU.name}[property_id]

        with patch.object(devicereset, "_property_string", side_effect=strings), \
                patch.object(devicereset, "_property_multisz", return_value=["PCI\\VEN_10DE&DEV_2B85"]), \
                patch.object(devicereset, "_instance_id", return_value=GPU.instance_id), \
                patch.object(devicereset, "_property_dword", side_effect=devicereset.DeviceResetError("address unavailable")):
            with self.assertRaisesRegex(devicereset.DeviceResetError, "address unavailable"):
                devicereset._target_from_display_info(object(), object(), fake_adapter)


class RestartTests(unittest.TestCase):
    def restart(self, completed=None, *, current=GPU, status=(True, "started"), side_effect=None):
        runner = Mock(return_value=completed)
        if side_effect is not None:
            runner.side_effect = side_effect
        with patch.object(devicereset, "pnputil_restart_supported", return_value=True), \
                patch.object(devicereset, "is_admin", return_value=True), \
                patch.object(devicereset, "resolve_target", return_value=current), \
                patch.object(devicereset, "_post_restart_status", return_value=status), \
                patch.object(devicereset.subprocess, "run", runner):
            result = devicereset.restart_target(GPU, timeout=7)
        return result, runner

    def test_restart_uses_exact_instance_and_never_adds_reboot_or_force(self):
        result, runner = self.restart(subprocess.CompletedProcess([], 0, "localized output", ""))
        self.assertTrue(result.ok)
        command = runner.call_args.args[0]
        self.assertEqual(command[1:], ["/restart-device", GPU.instance_id])
        self.assertNotIn("/reboot", [part.casefold() for part in command])
        self.assertNotIn("/force", [part.casefold() for part in command])
        self.assertEqual(runner.call_args.kwargs["timeout"], 7)

    def test_stale_target_never_runs_pnputil(self):
        changed = devicereset.DeviceTarget(GPU.slot, GPU.instance_id + "-changed", GPU.name)
        result, runner = self.restart(subprocess.CompletedProcess([], 0), current=changed)
        self.assertFalse(result.ok)
        self.assertIn("changed", result.message)
        runner.assert_not_called()

    def test_unsupported_windows_and_missing_admin_do_not_restart(self):
        with patch.object(devicereset, "pnputil_restart_supported", return_value=False), \
                patch.object(devicereset.subprocess, "run") as runner:
            unsupported = devicereset.restart_target(GPU)
        self.assertFalse(unsupported.ok)
        self.assertIn("2004", unsupported.message)
        runner.assert_not_called()
        with patch.object(devicereset, "pnputil_restart_supported", return_value=True), \
                patch.object(devicereset, "is_admin", return_value=False), \
                patch.object(devicereset.subprocess, "run") as runner:
            denied = devicereset.restart_target(GPU)
        self.assertFalse(denied.ok)
        self.assertIn("Administrator", denied.message)
        runner.assert_not_called()

    def test_windows_build_gate_starts_at_2004(self):
        with patch.object(devicereset.platform, "system", return_value="Windows"), \
                patch.object(devicereset.platform, "version", return_value="10.0.18363"):
            self.assertFalse(devicereset.pnputil_restart_supported())
        with patch.object(devicereset.platform, "system", return_value="Windows"), \
                patch.object(devicereset.platform, "version", return_value="10.0.19041"):
            self.assertTrue(devicereset.pnputil_restart_supported())
        with patch.object(devicereset.platform, "system", return_value="Linux"):
            self.assertFalse(devicereset.pnputil_restart_supported())

    def test_timeout_failure_and_reboot_required_are_not_success(self):
        timeout, _ = self.restart(
            side_effect=subprocess.TimeoutExpired(["pnputil"], 7)
        )
        self.assertFalse(timeout.ok)
        self.assertIsNone(timeout.returncode)
        failed, _ = self.restart(subprocess.CompletedProcess([], 5))
        self.assertFalse(failed.ok)
        self.assertEqual(failed.returncode, 5)
        self.assertIn("exit code 5", failed.message)
        reboot, _ = self.restart(subprocess.CompletedProcess([], 3010))
        self.assertFalse(reboot.ok)
        self.assertTrue(reboot.reboot_required)
        self.assertEqual(reboot.returncode, 3010)

    def test_zero_exit_needs_configmgr_started_confirmation(self):
        result, _ = self.restart(subprocess.CompletedProcess([], 0), status=(False, "not started"))
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.message, "not started")

    def test_nonzero_exit_reports_bounded_diagnostics_without_parsing_them(self):
        completed = subprocess.CompletedProcess([], 7, "x" * 1000, "localized failure")
        result, _ = self.restart(completed)
        self.assertFalse(result.ok)
        self.assertIn("exit code 7", result.message)
        self.assertIn("stderr='localized failure'", result.message)
        self.assertLess(len(result.message), 700)


if __name__ == "__main__":
    unittest.main()
