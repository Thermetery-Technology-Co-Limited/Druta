# Druta - hardware-free tests for Windows NVML driver discovery.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""No driver DLLs are loaded here; all native calls and file checks are fake."""

import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import druta.nvbackend as nvbackend


SYSTEM_DLL = r"D:\Windows\System32\nvml.dll"
LEGACY_DLL = r"E:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"


class NvmlLoaderTests(unittest.TestCase):
    def test_nondefault_windows_and_program_files_drives(self):
        with patch.object(nvbackend, "_windows_system_directory",
                          return_value=r"D:\Windows\System32"), \
             patch.object(nvbackend, "_windows_program_files_directory",
                          return_value=r"E:\Program Files"):
            paths, errors = nvbackend._nvml_driver_paths()
        self.assertEqual(paths, [SYSTEM_DLL, LEGACY_DLL])
        self.assertEqual(errors, [])

    def test_folder_resolution_failure_preserves_other_candidate(self):
        with patch.object(nvbackend, "_windows_system_directory",
                          side_effect=OSError("system lookup failed")), \
             patch.object(nvbackend, "_windows_program_files_directory",
                          return_value=r"E:\Program Files"):
            paths, errors = nvbackend._nvml_driver_paths()
        self.assertEqual(paths, [LEGACY_DLL])
        self.assertIn("system lookup failed", errors[0])

    def test_relative_folder_never_becomes_a_dll_candidate(self):
        for folder in ("", "Program Files", r"D:Program Files", r"\Program Files"):
            with self.subTest(folder=folder), \
                 patch.object(nvbackend, "_windows_system_directory",
                              return_value=folder), \
                 patch.object(nvbackend, "_windows_program_files_directory",
                              return_value=folder):
                paths, errors = nvbackend._nvml_driver_paths()
            self.assertEqual(paths, [])
            self.assertEqual(len(errors), 2)

    def loader_patches(self, exists=True):
        self.paths = self.enterContext(patch.object(
            nvbackend, "_nvml_driver_paths",
            return_value=([SYSTEM_DLL, LEGACY_DLL], [])))
        self.exists = self.enterContext(patch.object(
            nvbackend.os.path, "isfile", return_value=exists))
        self.cdll = self.enterContext(patch.object(nvbackend.ctypes, "CDLL"))

    def test_dch_load_does_not_fall_through_to_legacy(self):
        self.loader_patches()
        result = nvbackend._load_nvml_library()
        self.assertEqual(result, (self.cdll.return_value, SYSTEM_DLL))
        self.cdll.assert_called_once_with(SYSTEM_DLL, winmode=0x900)

    def test_missing_system_dll_loads_standard_driver(self):
        self.loader_patches()
        self.exists.side_effect = [False, True]
        result = nvbackend._load_nvml_library()
        self.assertEqual(result, (self.cdll.return_value, LEGACY_DLL))
        self.cdll.assert_called_once_with(LEGACY_DLL, winmode=0x900)

    def test_failed_system_dll_load_falls_back_to_standard(self):
        self.loader_patches()
        legacy = object()
        self.cdll.side_effect = [OSError("missing dependency"), legacy]
        self.assertEqual(nvbackend._load_nvml_library(), (legacy, LEGACY_DLL))
        self.assertEqual(self.cdll.call_args_list, [
            call(SYSTEM_DLL, winmode=0x900), call(LEGACY_DLL, winmode=0x900)])

    def test_no_files_never_invokes_pyinstaller_basename_fallback(self):
        self.loader_patches(exists=False)
        with self.assertRaises(OSError) as caught:
            nvbackend._load_nvml_library()
        self.cdll.assert_not_called()
        detail = str(caught.exception)
        self.assertIn(SYSTEM_DLL + ": file not found", detail)
        self.assertIn(LEGACY_DLL + ": file not found", detail)
        self.assertIn(f"{ctypes.sizeof(ctypes.c_void_p) * 8}-bit process", detail)

    def test_frozen_load_failure_reports_original_windows_error(self):
        self.loader_patches()
        frozen_error = OSError("Failed to load dynlib/dll")
        frozen_error.__cause__ = OSError("[WinError 193] wrong architecture")
        self.cdll.side_effect = [frozen_error, OSError("access denied")]
        with self.assertRaises(OSError) as caught:
            nvbackend._load_nvml_library()
        detail = str(caught.exception)
        self.assertIn(SYSTEM_DLL, detail)
        self.assertIn("WinError 193", detail)
        self.assertIn(LEGACY_DLL + ": access denied", detail)

    def test_missing_nvml_is_nonfatal_and_reports_candidates(self):
        self.loader_patches(exists=False)
        nvml = nvbackend.Nvml()
        self.assertFalse(nvml.ok)
        self.assertEqual(nvml.gpus, [])
        self.assertEqual(nvml.dll_path, "")
        self.assertIn("nvml.dll not loadable", nvml.err_detail)
        self.assertIn(LEGACY_DLL, nvml.err_detail)

    def test_legacy_library_reaches_existing_nvml_initialization(self):
        self.loader_patches()
        self.exists.side_effect = [False, True]
        dll = SimpleNamespace(nvmlErrorString=Mock(),
                              nvmlInit_v2=Mock(return_value=9))
        self.cdll.return_value = dll
        nvml = nvbackend.Nvml()
        dll.nvmlInit_v2.assert_called_once_with()
        self.assertFalse(nvml.ok)
        self.assertEqual(nvml.dll_path, LEGACY_DLL)
        self.assertEqual(nvml.err_detail, "nvmlInit_v2 status 9")

    @staticmethod
    def fake_dll(*, modern=True, pci_version=3):
        def count(pointer):
            ctypes.cast(pointer, ctypes.POINTER(nvbackend.u32))[0] = 1
            return 0

        def handle(index, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 123
            return 0

        def pci(device, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(nvbackend._NvmlPciInfo)).contents
            value.domain, value.bus, value.device = 0, 5, 0
            value.pciDeviceId, value.pciSubSystemId = 0x123410DE, 0x56789ABC
            return 0

        suffix = "_v2" if modern else ""
        exports = {"nvmlInit" + suffix: Mock(return_value=0),
                   "nvmlDeviceGetCount" + suffix: Mock(side_effect=count),
                   "nvmlDeviceGetHandleByIndex" + suffix: Mock(side_effect=handle)}
        if pci_version:
            exports[f"nvmlDeviceGetPciInfo_v{pci_version}"] = Mock(side_effect=pci)
        return SimpleNamespace(**exports)

    def test_old_exports_pair_devices_using_v2_pci(self):
        dll = self.fake_dll(modern=False, pci_version=2)
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, LEGACY_DLL)):
            nvml = nvbackend.Nvml("0000:05:00.0")
        self.assertTrue(nvml.ok)
        self.assertEqual((nvml.selected["devid"], nvml.selected["subsys"]), (0x1234, 0x56789ABC))
        self.assertEqual(nvml.selected["name"], "")
        dll.nvmlDeviceGetPciInfo_v2.assert_called_once()

    def test_modern_pair_is_preferred_and_no_mixed_indices(self):
        dll = self.fake_dll()
        dll.nvmlDeviceGetCount = Mock(return_value=0)
        dll.nvmlDeviceGetHandleByIndex = Mock(return_value=0)
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, SYSTEM_DLL)):
            self.assertTrue(nvbackend.Nvml("0000:05:00.0").ok)
        dll.nvmlDeviceGetCount_v2.assert_called_once()
        dll.nvmlDeviceGetHandleByIndex_v2.assert_called_once()
        dll.nvmlDeviceGetCount.assert_not_called()
        dll.nvmlDeviceGetHandleByIndex.assert_not_called()

        del dll.nvmlDeviceGetHandleByIndex_v2
        # A complete older pair is selected over an incomplete newer pair.
        dll.nvmlDeviceGetCount = self.fake_dll(modern=False).nvmlDeviceGetCount
        dll.nvmlDeviceGetHandleByIndex = self.fake_dll(modern=False).nvmlDeviceGetHandleByIndex
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, SYSTEM_DLL)):
            self.assertTrue(nvbackend.Nvml("0000:05:00.0").ok)
        dll.nvmlDeviceGetCount.assert_called_once()
        dll.nvmlDeviceGetHandleByIndex.assert_called_once()

        del dll.nvmlDeviceGetCount
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, SYSTEM_DLL)):
            nvml = nvbackend.Nvml()
        self.assertFalse(nvml.ok)
        self.assertIn("matching device-count/index", nvml.err_detail)

    def test_real_init_count_and_pci_errors_do_not_retry_older_exports(self):
        dll = self.fake_dll()
        dll.nvmlInit = Mock(return_value=0)
        dll.nvmlInit_v2.return_value = 9
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, SYSTEM_DLL)):
            self.assertFalse(nvbackend.Nvml().ok)
        dll.nvmlInit.assert_not_called()

        dll.nvmlInit_v2.return_value = 0
        dll.nvmlDeviceGetCount_v2 = Mock(return_value=9)
        dll.nvmlDeviceGetCount = Mock(return_value=0)
        dll.nvmlDeviceGetHandleByIndex = Mock(return_value=0)
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, SYSTEM_DLL)):
            self.assertFalse(nvbackend.Nvml().ok)
        dll.nvmlDeviceGetCount.assert_not_called()

        dll = self.fake_dll()
        dll.nvmlDeviceGetPciInfo_v3 = Mock(return_value=9)
        dll.nvmlDeviceGetPciInfo_v2 = Mock(return_value=0)
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, SYSTEM_DLL)):
            nvml = nvbackend.Nvml("0000:05:00.0")
        self.assertFalse(nvml.ok)
        dll.nvmlDeviceGetPciInfo_v2.assert_not_called()

    def test_unversioned_pci_is_not_used_for_pairing(self):
        dll = self.fake_dll(modern=False, pci_version=0)
        dll.nvmlDeviceGetPciInfo = Mock(return_value=0)
        with patch.object(nvbackend, "_load_nvml_library", return_value=(dll, LEGACY_DLL)):
            nvml = nvbackend.Nvml("0000:05:00.0")
        self.assertFalse(nvml.ok)
        self.assertEqual(nvml.gpus[0]["slot"], "")
        dll.nvmlDeviceGetPciInfo.assert_not_called()

    def test_missing_constraints_does_not_hide_independent_power_default(self):
        def default(device, pointer):
            ctypes.cast(pointer, ctypes.POINTER(nvbackend.u32))[0] = 175000
            return 0

        gpu = nvbackend.GPU.__new__(nvbackend.GPU)
        gpu.nvml = SimpleNamespace(ok=True, dev=123, selected={},
                                   dll=SimpleNamespace(nvmlDeviceGetPowerManagementDefaultLimit=default),
                                   has=lambda name: False)
        gpu.nvapi = SimpleNamespace(ok=False)
        gpu.slot = lambda: "0000:05:00.0"
        gpu._supported_nvml_clocks = lambda *args: None
        gpu._offset_range = lambda *args: None
        gpu._native_fan_data = lambda: None
        with patch.object(nvbackend, "is_admin", return_value=False):
            result = gpu._read_static()
        self.assertEqual(result["pl_def_mw"], 175000)
        self.assertNotIn("pl_min_mw", result)


if __name__ == "__main__":
    unittest.main()
