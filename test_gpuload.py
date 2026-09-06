# Druta - selected-card CUDA workload regression tests.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gpuload


class GpuLoadTests(unittest.TestCase):
    def test_pci_selection_ignores_a_different_cuda_ordinal_order(self):
        def lookup(ptr, slot):
            self.assertEqual(slot, b"0000:02:00.0")
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_int))[0] = 7
            return 0

        def identity(buf, size, dev):
            self.assertEqual(dev.value, 7)
            buf.value = b"0000:02:00.0"
            return 0

        cu = SimpleNamespace(cuDeviceGetByPCIBusId=Mock(side_effect=lookup),
                             cuDeviceGetPCIBusId=Mock(side_effect=identity),
                             cuDeviceGet=Mock())
        load = gpuload.BandwidthLoad(slot="0000:02:00.0")
        self.assertEqual(load._select_device(cu, lambda rc, _: self.assertEqual(rc, 0)).value, 7)
        self.assertEqual(load.device_slot, "0000:02:00.0")
        cu.cuDeviceGet.assert_not_called()

    def test_wrong_card_readback_is_refused_before_context_creation(self):
        def identity(buf, size, dev):
            buf.value = b"0000:01:00.0"
            return 0

        cu = SimpleNamespace(cuDeviceGetByPCIBusId=Mock(return_value=0),
                             cuDeviceGetPCIBusId=Mock(side_effect=identity))
        load = gpuload.BandwidthLoad(slot="0000:02:00.0")
        with self.assertRaisesRegex(gpuload.LoadError, "CUDA selected"):
            load._select_device(cu, lambda *_: None)

    def test_induce_without_card_identity_never_starts_an_unrelated_load(self):
        for slot in (None, "", "invalid"):
            with self.subTest(slot=slot), patch("gpuload.BandwidthLoad") as load:
                result = gpuload.induce(SimpleNamespace(slot=lambda: slot))
                self.assertIn("cannot target", result["error"])
                load.assert_not_called()

    def test_induce_passes_the_selected_slot_and_joins_its_load(self):
        gpu = SimpleNamespace(slot=lambda: "0000:02:00.0",
                              read=lambda: {"mem": 5705, "pstate": 2})
        with patch("gpuload.BandwidthLoad") as make, patch("gpuload.time.sleep"):
            load = make.return_value
            load.error = ""
            load.done.is_set.return_value = False
            load.wait_started.return_value = True
            load.stats = {"slot": "0000:02:00.0"}
            result = gpuload.induce(gpu, max_seconds=5, on_settled=lambda: "captured")
            make.assert_called_once_with(max_seconds=5, slot="0000:02:00.0")
            load.stop.assert_called_once()
            load.join.assert_called_once()
        self.assertTrue(result["settled"])
        self.assertEqual(result["result"], "captured")
        self.assertEqual(result["stats"]["slot"], gpu.slot())


if __name__ == "__main__":
    unittest.main()
