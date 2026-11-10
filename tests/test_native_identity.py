# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native core's NVML/NVAPI identity logic, with fake drivers served
through the resolver seam (what tests/test_nvml_loader.py fakes with
patch.object on nvbackend). No driver DLL is loaded."""
import ctypes
import unittest

from druta import native, nvbackend

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE


from tests.fake_driver import TITAN, XP, FakeNvapi, FakeNvml, install, write_text, write_u32  # noqa: E402, F401


class NativeIdentityTests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def test_no_driver_reports_both_halves_unavailable(self):
        install(None, None)
        gpu = native.Gpu("0000:01:00.0")
        self.assertFalse(gpu.available())
        self.assertIn("nvml.dll not loadable", gpu.nvml.err_detail)
        self.assertIn("not loadable", gpu.nvapi.err_detail)
        self.assertEqual(gpu.slot(), "0000:01:00.0")  # the requested slot, nothing else
        self.assertTrue(gpu.status_line().startswith("NVAPI: FAIL ("))
        self.assertIn("   |   slot 0000:01:00.0", gpu.status_line())
        self.assertEqual(native.enumerate_gpus(), [])
        gpu.close()

    def test_enumeration_pairs_by_slot_not_index_and_sorts_by_slot(self):
        # NVAPI and NVML hand the two cards back in opposite orders.
        install(FakeNvml([XP, TITAN]), FakeNvapi([TITAN, XP]))
        gpus = native.enumerate_gpus()
        self.assertEqual([g["slot"] for g in gpus], ["0000:01:00.0", "0000:02:00.0"])
        self.assertEqual(gpus[0], {"slot": "0000:01:00.0", "name": "TITAN RTX", "uuid": "GPU-aaaa",
                                   "devid": 0x1E02, "subsys": 0x12A010DE, "nvml_index": 1, "has_nvapi": True})

    def test_enumeration_keeps_nvapi_only_cards_and_nvml_names_win(self):
        only_nvapi = dict(XP, name="NVAPI name")
        install(FakeNvml([TITAN]), FakeNvapi([dict(TITAN, name="other name"), only_nvapi]))
        gpus = native.enumerate_gpus()
        self.assertEqual([(g["slot"], g["name"], g["nvml_index"], g["has_nvapi"]) for g in gpus],
                         [("0000:01:00.0", "TITAN RTX", 0, True), ("0000:02:00.0", "NVAPI name", None, True)])
        self.assertEqual(gpus[1]["uuid"], "")

    def test_default_selection_is_the_lowest_slot_and_named_selection_is_exact(self):
        install(FakeNvml([XP, TITAN]), FakeNvapi([XP, TITAN]))
        with native.Gpu() as gpu:
            self.assertEqual(gpu.slot(), "0000:01:00.0")
            self.assertEqual(gpu.static["name"], "TITAN RTX")
            self.assertEqual(gpu.nvml.selected["nvml_index"], 1)
        with native.Gpu("00000000:02:00.0") as gpu:  # NVML's spelling selects by bus/device
            self.assertEqual(gpu.slot(), "0000:02:00.0")
            self.assertEqual(gpu.static["uuid"], "GPU-bbbb")
            self.assertEqual(gpu.pairing_error, "")
        with native.Gpu("0000:03:00.0") as gpu:
            self.assertFalse(gpu.nvml.ok)
            self.assertEqual(gpu.nvml.err_detail,
                             "no NVML device at slot 0000:03:00.0 (NVML sees: 0000:02:00.0, 0000:01:00.0)")
            self.assertEqual(gpu.nvapi.err_detail,
                             "no NVAPI GPU at slot 0000:03:00.0 (NVAPI sees: 0000:02:00.0, 0000:01:00.0)")

    def test_static_identity_prefers_nvml_and_falls_back_to_nvapi(self):
        install(FakeNvml([TITAN]), FakeNvapi([TITAN]))
        with native.Gpu() as gpu:
            self.assertEqual((gpu.static["name"], gpu.static["driver"], gpu.static["vbios"]),
                             ("TITAN RTX", "581.57", "90.02.0B.00.01"))
        install(FakeNvml([dict(TITAN, name=None, vbios=None)], missing=["nvmlSystemGetDriverVersion"]),
                FakeNvapi([dict(TITAN, name="NVAPI TITAN")]))
        with native.Gpu() as gpu:
            self.assertEqual((gpu.static["name"], gpu.static["driver"], gpu.static["vbios"]),
                             ("NVAPI TITAN", "581.57", "?"))
            self.assertEqual(gpu.status_line(), "NVAPI: ok   |   NVML: ok   |   slot 0000:01:00.0")

    def test_pairing_mismatch_disables_nvapi_with_the_python_message(self):
        install(FakeNvml([TITAN]), FakeNvapi([dict(TITAN, devid=0x1B02, subsys=0x119A10DE)]))
        with native.Gpu("0000:01:00.0") as gpu:
            self.assertEqual(gpu.pairing_error,
                             "NVAPI and NVML disagree about the card at 0000:01:00.0: NVAPI says devid 1B02/subsys "
                             "119A10DE, NVML says devid 1E02/subsys 12A010DE. NVAPI disabled rather than risk "
                             "writing the wrong card.")
            self.assertFalse(gpu.nvapi.ok)
            self.assertTrue(gpu.nvml.ok)
            self.assertEqual(gpu.nvapi.err_detail, gpu.pairing_error)
            self.assertIsNotNone(gpu.nvapi.selected)  # not cleared, as in nvbackend
            self.assertNotEqual(gpu.nvapi.selected["handle"], 0)  # the record keeps its handle
            self.assertIsNone(gpu.nvapi.gpu)  # NvAPI.disable clears the live handle
            self.assertTrue(gpu.status_line().startswith("NVAPI: FAIL (NVAPI and NVML disagree"))

    def test_pairing_is_accepted_unverified_without_nvml_pci_identity(self):
        install(FakeNvml([dict(TITAN, pci_status=3)]), FakeNvapi([dict(TITAN, devid=0x1B02)]))
        with native.Gpu() as gpu:
            self.assertEqual(gpu.pairing_error, "")
            self.assertTrue(gpu.nvapi.ok)
            self.assertEqual(gpu.nvml.selected["slot"], "")
            self.assertIsNone(gpu.nvml.selected["devid"])
            self.assertEqual(gpu.slot(), "0000:01:00.0")  # NVAPI's slot when NVML has none

    # ---- the test_nvml_loader scenarios ----------------------------------------------------------

    def test_legacy_v1_exports_are_used_when_v2_are_absent(self):
        install(FakeNvml([TITAN], modern=False, pci_version=2), None)
        with native.Gpu() as gpu:
            self.assertTrue(gpu.nvml.ok)
            self.assertEqual(gpu.nvml.selected["slot"], "0000:01:00.0")

    def test_mixed_count_index_versions_are_never_paired(self):
        v2 = FakeNvml([TITAN])
        v2.exports.pop("nvmlDeviceGetHandleByIndex_v2")  # a v2 count with only a v1 index lookup
        install(v2, None)
        with native.Gpu() as gpu:
            self.assertFalse(gpu.nvml.ok)
            self.assertEqual(gpu.nvml.err_detail, "NVML has no matching device-count/index exports")
        complete_v1 = FakeNvml([TITAN], modern=False)
        partial_v2 = FakeNvml([TITAN])
        partial_v2.exports.pop("nvmlDeviceGetCount_v2")
        complete_v1.exports.update({k: v for k, v in partial_v2.exports.items() if k.endswith("_v2")})
        install(complete_v1, None)
        with native.Gpu() as gpu:
            self.assertTrue(gpu.nvml.ok)  # the complete older pair beats the incomplete newer one

    def test_init_and_count_failures_are_not_retried_on_older_exports(self):
        install(FakeNvml([TITAN], init_status=9), None)
        with native.Gpu() as gpu:
            self.assertEqual(gpu.nvml.err_detail, "nvmlInit_v2 status 9")
        install(FakeNvml([TITAN], count_status=7), None)
        with native.Gpu() as gpu:
            self.assertEqual(gpu.nvml.err_detail, "nvmlDeviceGetCount_v2 status 7")
        install(FakeNvml([TITAN], missing=["nvmlInit_v2"]) if False else FakeNvml([TITAN], modern=False,
                                                                                  missing=["nvmlInit"]), None)
        with native.Gpu() as gpu:
            self.assertEqual(gpu.nvml.err_detail, "NVML has no initialization export")

    def test_unversioned_pci_is_never_used_for_pairing(self):
        install(FakeNvml([TITAN], pci_version=0), None)
        with native.Gpu() as gpu:
            self.assertTrue(gpu.nvml.ok)
            self.assertEqual(gpu.nvml.selected["slot"], "")
            self.assertIsNone(gpu.nvml.selected["devid"])
            self.assertEqual(gpu.static["uuid"], "GPU-aaaa")

    def test_python_and_native_enumeration_agree_on_the_same_fakes(self):
        """The Python backend over the same fake tables must produce the same list."""
        nvml, nvapi = FakeNvml([XP, TITAN]), FakeNvapi([TITAN, XP])
        install(nvml, nvapi)
        from unittest.mock import patch

        class PyDll:
            def __getattr__(self, name):
                try:
                    return nvml.exports[name]
                except KeyError:
                    raise AttributeError(name)

        class PyNvapiDll:
            nvapi_QueryInterface = nvapi.query

        with patch.object(nvbackend, "_load_nvml_library", return_value=(PyDll(), "fake")), \
                patch.object(nvbackend.ctypes, "WinDLL", return_value=PyNvapiDll(), create=True):
            expected = nvbackend.enumerate_gpus()
        self.assertEqual(native.enumerate_gpus(), expected)


if __name__ == "__main__":
    unittest.main()
