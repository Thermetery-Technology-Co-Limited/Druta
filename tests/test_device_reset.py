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

    def test_pnputil_results_name_their_method(self):
        result, _ = self.restart(subprocess.CompletedProcess([], 0))
        self.assertEqual(result.method, devicereset.RESTART_PNPUTIL)


def _windows(version):
    return (patch.object(devicereset.platform, "system", return_value="Windows"),
            patch.object(devicereset.platform, "version", return_value=version))


class RestartMethodSelectionTests(unittest.TestCase):
    def method(self, version, *, setupapi=True, system="Windows"):
        with patch.object(devicereset.platform, "system", return_value=system), \
                patch.object(devicereset.platform, "version", return_value=version), \
                patch.object(devicereset, "setupapi_restart_supported", return_value=setupapi):
            return devicereset.restart_method()

    def test_windows_10_2004_and_later_keep_pnputil(self):
        for version in ("10.0.19041", "10.0.22631", "10.0.26100"):
            self.assertEqual(self.method(version), devicereset.RESTART_PNPUTIL, version)
            self.assertEqual(self.method(version, setupapi=False), devicereset.RESTART_PNPUTIL, version)

    def test_older_windows_uses_setupapi(self):
        # Windows 10 1909, 8.1, 7 SP1, Vista SP2 and XP SP3.
        for version in ("10.0.18363", "6.3.9600", "6.1.7601", "6.0.6002", "5.1.2600"):
            self.assertEqual(self.method(version), devicereset.RESTART_SETUPAPI, version)

    def test_no_method_without_setupapi_or_off_windows(self):
        self.assertIsNone(self.method("6.1.7601", setupapi=False))
        with patch.object(devicereset.platform, "system", return_value="Linux"):
            self.assertIsNone(devicereset.restart_method())  # the real capability check
            self.assertFalse(devicereset.device_restart_supported())


class SetupApiCapabilityTests(unittest.TestCase):
    EXPORTS = dict.fromkeys(devicereset._SETUPAPI_RESTART_EXPORTS, object())

    def supported(self, exports, *, wow64=False):
        from types import SimpleNamespace
        system, _ = _windows("6.1.7601")
        with system, patch.object(devicereset, "_is_wow64", return_value=wow64):
            return devicereset.setupapi_restart_supported(lambda: SimpleNamespace(**exports))

    def test_every_export_and_a_native_process_are_required(self):
        self.assertTrue(self.supported(self.EXPORTS))
        self.assertFalse(self.supported(self.EXPORTS, wow64=True))
        for missing in devicereset._SETUPAPI_RESTART_EXPORTS:
            exports = {k: v for k, v in self.EXPORTS.items() if k != missing}
            self.assertFalse(self.supported(exports), missing)

    def test_unloadable_setupapi_is_unsupported(self):
        def fail():
            raise OSError("setupapi.dll not found")

        system, _ = _windows("6.1.7601")
        with system:
            self.assertFalse(devicereset.setupapi_restart_supported(fail))

    def test_wow64_detection(self):
        from types import SimpleNamespace

        def kernel(result, flag):
            def query(_process, pointer):
                pointer._obj.value = flag
                return result
            return lambda *a, **k: SimpleNamespace(IsWow64Process=query, GetCurrentProcess=lambda: 1)

        self.assertFalse(devicereset._is_wow64(lambda *a, **k: SimpleNamespace()))  # no export: no WOW64
        self.assertFalse(devicereset._is_wow64(kernel(1, 0)))
        self.assertTrue(devicereset._is_wow64(kernel(1, 1)))
        self.assertTrue(devicereset._is_wow64(kernel(0, 0)))  # unknown: do not attempt


class SetupApiLayoutTests(unittest.TestCase):
    def test_structure_sizes_match_setupapi_h_on_x86_and_x64(self):
        for pointer_size, install_size in ((8, 584), (4, 556)):
            header, propchange, install = devicereset._setupapi_structs(pointer_size)
            self.assertEqual(ctypes_sizeof(header), 8)
            self.assertEqual(ctypes_sizeof(propchange), 20)
            self.assertEqual(ctypes_sizeof(install), install_size)


def ctypes_sizeof(structure):
    import ctypes
    return ctypes.sizeof(structure)


class FakeSetupApi:
    """Records the SetupAPI sequence; each step can be made to fail."""

    def __init__(self, *, fail=None, flags=0):
        self.fail, self.flags, self.calls = fail, flags, []
        # Plain functions, like ctypes function pointers, accept argtypes/restype.
        for name in devicereset._SETUPAPI_RESTART_EXPORTS:
            bound = getattr(self, name)
            setattr(self, name, lambda *args, _bound=bound: _bound(*args))

    def _step(self, name):
        self.calls.append(name)
        return 0 if self.fail == name else 1

    def SetupDiCreateDeviceInfoList(self, guid, hwnd):
        self.calls.append("create")
        return 0x1234

    def SetupDiOpenDeviceInfoW(self, handle, instance_id, hwnd, flags, info):
        self.opened = (handle, instance_id, flags, info._obj.cbSize)
        return self._step("open")

    def SetupDiSetClassInstallParamsW(self, handle, info, params, size):
        p = params._obj
        self.params = (p.ClassInstallHeader.cbSize, p.ClassInstallHeader.InstallFunction,
                       p.StateChange, p.Scope, p.HwProfile, size)
        return self._step("set")

    def SetupDiCallClassInstaller(self, function, handle, info):
        self.function = function
        return self._step("call")

    def SetupDiGetDeviceInstallParamsW(self, handle, info, params):
        self.install_size = params._obj.cbSize
        params._obj.Flags = self.flags
        return self._step("get")

    def SetupDiDestroyDeviceInfoList(self, handle):
        self.calls.append(("destroy", handle))
        return 1


class SetupApiRestartTests(unittest.TestCase):
    def run_fake(self, **kwargs):
        fake = FakeSetupApi(**kwargs)
        with patch.object(devicereset, "_last_error", return_value=0xE000020B):
            try:
                return fake, devicereset._setupapi_property_change(GPU.instance_id, lambda: fake)
            except devicereset.DeviceResetError as error:
                return fake, error

    def test_exact_device_receives_one_documented_property_change(self):
        import ctypes
        fake, restarted = self.run_fake()
        self.assertIs(restarted, True)
        self.assertEqual(fake.opened[1], GPU.instance_id)
        self.assertEqual(fake.opened[2], 0)
        self.assertEqual(fake.params, (8, 0x12, 3, 2, 0, 20))  # DIF_PROPERTYCHANGE, DICS_PROPCHANGE, CONFIGSPECIFIC
        self.assertEqual(fake.function, 0x12)
        native = devicereset._setupapi_structs(ctypes.sizeof(ctypes.c_void_p))[2]
        self.assertEqual(fake.install_size, ctypes.sizeof(native))
        self.assertEqual(fake.calls, ["create", "open", "set", "call", "get", ("destroy", 0x1234)])

    def test_reboot_flags_mean_not_restarted(self):
        for flag in (0x100, 0x80):
            _, restarted = self.run_fake(flags=flag)
            self.assertIs(restarted, False)

    def test_each_refusal_raises_with_the_win32_error_and_frees_the_list(self):
        for step in ("open", "set", "call", "get"):
            fake, error = self.run_fake(fail=step)
            self.assertIsInstance(error, devicereset.DeviceResetError, step)
            self.assertIn("0xe000020b", str(error), step)
            self.assertEqual(fake.calls[-1], ("destroy", 0x1234), step)
            order = ["open", "set", "call", "get"]
            self.assertNotIn(order[order.index(step) + 1] if step != "get" else None, fake.calls)


class SetupApiRouteTests(unittest.TestCase):
    def restart(self, change=None, *, current=GPU, status=(True, "started"), timeout=7):
        change = change or Mock(return_value=True)
        with patch.object(devicereset, "restart_method", return_value=devicereset.RESTART_SETUPAPI), \
                patch.object(devicereset, "is_admin", return_value=True), \
                patch.object(devicereset, "resolve_target", return_value=current), \
                patch.object(devicereset, "_post_restart_status", return_value=status) as post, \
                patch.object(devicereset, "_setupapi_property_change", change), \
                patch.object(devicereset.subprocess, "run") as runner:
            result = devicereset.restart_target(GPU, timeout=timeout)
        runner.assert_not_called()
        return result, change, post

    def test_success_needs_the_same_started_device_afterwards(self):
        result, change, post = self.restart()
        change.assert_called_once_with(GPU.instance_id)
        post.assert_called_once_with(GPU)
        self.assertTrue(result.ok)
        self.assertEqual((result.method, result.returncode), (devicereset.RESTART_SETUPAPI, 0))
        failed, _, _ = self.restart(status=(False, "not started"))
        self.assertFalse(failed.ok)
        self.assertEqual(failed.message, "not started")

    def test_reboot_required_and_refusal_are_not_success(self):
        reboot, _, post = self.restart(Mock(return_value=False))
        self.assertFalse(reboot.ok)
        self.assertTrue(reboot.reboot_required)
        self.assertEqual(reboot.returncode, 3010)
        post.assert_not_called()
        refused, _, _ = self.restart(Mock(side_effect=devicereset.DeviceResetError("error 0x5")))
        self.assertFalse(refused.ok)
        self.assertFalse(refused.reboot_required)
        self.assertIn("0x5", refused.message)

    def test_stale_target_is_never_restarted(self):
        changed = devicereset.DeviceTarget(GPU.slot, GPU.instance_id + "-changed", GPU.name)
        result, change, _ = self.restart(current=changed)
        self.assertFalse(result.ok)
        change.assert_not_called()

    def test_a_hung_class_installer_is_bounded(self):
        import threading
        release = threading.Event()
        try:
            result, _, post = self.restart(Mock(side_effect=lambda _id: release.wait(5)), timeout=0.05)
        finally:
            release.set()
        self.assertFalse(result.ok)
        self.assertIn("timeout", result.message)
        post.assert_not_called()

    def test_missing_admin_is_refused_before_any_restart(self):
        with patch.object(devicereset, "restart_method", return_value=devicereset.RESTART_SETUPAPI), \
                patch.object(devicereset, "is_admin", return_value=False), \
                patch.object(devicereset, "_setupapi_property_change") as change:
            result = devicereset.restart_target(GPU)
        self.assertFalse(result.ok)
        self.assertIn("Administrator", result.message)
        change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
