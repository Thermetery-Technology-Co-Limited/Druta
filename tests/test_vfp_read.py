# Druta - hardware-free regression tests for incomplete V/F responses.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Model the successful-but-empty GP102 response captured on driver 580.97.

These tests instantiate no driver or GUI. The fake API honors request masks,
including Pascal's rejection above 84 entries, so the real layout probe runs.
"""

import ctypes
import threading
import unittest
from unittest.mock import Mock

from druta.nvbackend import GPU, NvAPI, _BoostTable, _VfpCurve


class CurveAPI:
    def __init__(self, kind):
        self.ok = True
        self.gpu = object()
        self.kind = kind
        self.incomplete = False
        self.core_cap_khz = None
        self.core_offset_khz = 0
        self.boost_reads = 0
        self.capacity = 84 if kind == "pascal" else 128
        self.gpu_count = 80 if kind == "pascal" else 128
        self.ver = NvAPI.ver

    def requested(self, mask):
        indices = [i for i in range(128)
                   if int(mask[i // 32]) & (1 << (i % 32))]
        return indices if all(i < self.capacity for i in indices) else None

    def VfpCurve(self, handle, ptr):
        assert handle is self.gpu
        cv = ctypes.cast(ptr, ctypes.POINTER(_VfpCurve)).contents
        indices = self.requested(cv.masks)
        if indices is None:
            return -1
        for index in indices:
            entry = cv.entries[index]
            if self.incomplete:
                # Exact failure signature: a MHz-sized number in the kHz
                # field, then memory-type records without any voltage/clock.
                if index == 0:
                    entry.freq_kHz, entry.volt_uV = 278, 450000
                else:
                    entry.u0 = 1
            elif self.kind == "pascal":
                if index < 80:
                    entry.volt_uV = 450000 + index * 10000
                    entry.freq_kHz = 2 * round(
                        139000 + (1911000 - 139000) * index / 79)
                    if self.core_cap_khz is not None:
                        entry.freq_kHz = min(entry.freq_kHz, self.core_cap_khz)
                else:
                    entry.u0 = 1
                    entry.volt_uV = 550000 + (index - 80) * 62500
                    entry.freq_kHz = (405000, 810000, 5505000,
                                      5705000)[index - 80]
            else:
                entry.volt_uV = 450000 + index * 6250
                entry.freq_kHz = round(
                    360000 + (2160000 - 360000) * index / 127) + self.core_offset_khz
        return 0

    def BoostTableGet(self, handle, ptr):
        assert handle is self.gpu
        table = ctypes.cast(ptr, ctypes.POINTER(_BoostTable)).contents
        indices = self.requested(table.masks)
        if indices is None:
            return -1
        self.boost_reads += 1
        for index in indices:
            table.rows[index].w[5] = index * 7500
        return 0


def fake_gpu(kind="pascal"):
    gpu = GPU.__new__(GPU)
    gpu._lock = threading.RLock()
    gpu.nvapi = CurveAPI(kind)
    gpu.static = {"gfx_max": 1911 if kind == "pascal" else 2160,
                  "mem_clocks": [405, 810, 5505, 5705]}
    return gpu


class VfpReadTests(unittest.TestCase):
    def test_known_turing_keeps_direct_units_above_the_old_scale_threshold(self):
        for maximum in (2160, None):
            with self.subTest(gfx_max=maximum):
                gpu = fake_gpu("turing")
                gpu.arch = Mock(return_value=GPU.ARCH_TURING)
                gpu.static["gfx_max"] = maximum
                gpu.nvapi.core_offset_khz = 1_200_000
                points, error = gpu.read_vf_curve()
                self.assertIsNone(error)
                self.assertEqual(gpu.vfp_layout().freq_div, 1)
                self.assertEqual(len(points), 128)
                self.assertEqual(max(p["freq_mhz"] for p in points), 3360)
                self.assertEqual(points[-1]["delta_khz"], 127 * 7500)

    def test_unknown_architecture_retains_original_scale_inference(self):
        gpu = fake_gpu("turing")
        gpu.nvapi.core_offset_khz = 1_200_000
        self.assertIsNone(gpu.arch())
        self.assertEqual(gpu.vfp_layout().freq_div, 2)

    def test_fresh_pascal_layout_keeps_doubled_units_for_a_lowered_curve(self):
        for maximum in (1911, None):
            with self.subTest(gfx_max=maximum):
                gpu = fake_gpu()
                gpu.arch = Mock(return_value=GPU.ARCH_PASCAL)
                gpu.static["gfx_max"] = maximum
                # A persisted 1000 MHz cap is below the former scale heuristic.
                gpu.nvapi.core_cap_khz = 2_000_000
                points, error = gpu.read_vf_curve()
                self.assertIsNone(error)
                layout = gpu.vfp_layout()
                self.assertEqual((layout.n_entries, layout.n_gpu, layout.freq_div),
                                 (84, 80, 2))
                self.assertEqual(layout.other_idx, list(range(80, 84)))
                self.assertEqual(max(p["freq_mhz"] for p in points), 1000)
                self.assertEqual(points[0]["freq_mhz"], 139)
                self.assertEqual(points[-1]["delta_khz"], 79 * 7500)

    def test_pascal_scale_without_max_clock_does_not_require_a_lowered_curve(self):
        gpu = fake_gpu()
        gpu.arch = Mock(return_value=GPU.ARCH_PASCAL)
        gpu.static.pop("gfx_max")
        points, error = gpu.read_vf_curve()
        self.assertIsNone(error)
        self.assertEqual(gpu.vfp_layout().freq_div, 2)
        self.assertEqual(max(p["freq_mhz"] for p in points), 1911)

    def test_backend_rephase_uses_pascal_raw_delta_units(self):
        gpu = fake_gpu()
        self.assertIsNotNone(gpu.vfp_layout())
        gpu.clock_step_khz = Mock(return_value=12657)
        gpu.read_vf_curve = Mock(return_value=([
            {"idx": 0, "delta_khz": 0}, {"idx": 1, "delta_khz": 12657},
            {"idx": 2, "delta_khz": 25314}, {"idx": 3, "delta_khz": 0}], None))
        gpu.apply_vf_deltas = Mock(return_value=(True, "applied"))
        ok, message = gpu.rephase_deltas()
        self.assertTrue(ok)
        gpu.apply_vf_deltas.assert_called_once_with({1: 0})
        self.assertIn("12.66 MHz", message)

    def test_incomplete_first_response_never_becomes_a_cached_layout(self):
        gpu = fake_gpu()
        gpu.nvapi.incomplete = True
        for _ in range(2):
            points, error = gpu.read_vf_curve()
            self.assertIsNone(points)
            self.assertIn("incomplete", error)
            self.assertIsNone(gpu._vfp_layout_cache)
        self.assertEqual(gpu.nvapi.boost_reads, 0)

    def test_full_pascal_response_recovers_with_gpu_scale_and_memory_exclusion(self):
        gpu = fake_gpu()
        gpu.nvapi.incomplete = True
        self.assertIsNone(gpu.vfp_layout())
        gpu.nvapi.incomplete = False
        points, error = gpu.read_vf_curve()
        self.assertIsNone(error)
        layout = gpu.vfp_layout()
        self.assertEqual((layout.n_entries, layout.n_gpu, layout.freq_div),
                         (84, 80, 2))
        self.assertEqual(layout.other_idx, list(range(80, 84)))
        self.assertEqual([p["idx"] for p in points], list(range(80)))
        self.assertEqual(points[0]["freq_mhz"], 139)
        self.assertEqual(points[-1]["freq_mhz"], 1911)
        self.assertEqual(points[-1]["delta_khz"], 79 * 7500)
        self.assertEqual(gpu._vfp_layout_error, "")

    def test_partial_read_invalidates_previously_valid_layout_and_recovers(self):
        gpu = fake_gpu()
        points, error = gpu.read_vf_curve()
        self.assertIsNone(error)
        self.assertEqual(len(points), 80)
        old_layout = gpu.vfp_layout()
        old_boost_reads = gpu.nvapi.boost_reads
        gpu.nvapi.incomplete = True
        points, error = gpu.read_vf_curve()
        self.assertIsNone(points)
        self.assertIn("incomplete", error)
        self.assertIsNone(gpu._vfp_layout_cache)
        self.assertEqual(gpu.nvapi.boost_reads, old_boost_reads)
        gpu.nvapi.incomplete = False
        points, error = gpu.read_vf_curve()
        self.assertIsNone(error)
        self.assertEqual(len(points), 80)
        self.assertIsNot(gpu.vfp_layout(), old_layout)

    def test_turing_full_curve_remains_direct_and_complete(self):
        gpu = fake_gpu("turing")
        points, error = gpu.read_vf_curve()
        self.assertIsNone(error)
        layout = gpu.vfp_layout()
        self.assertEqual((layout.n_entries, layout.n_gpu, layout.freq_div),
                         (128, 128, 1))
        self.assertEqual(layout.other_idx, [])
        self.assertEqual([p["idx"] for p in points], list(range(128)))
        self.assertEqual((points[0]["volt_mv"], points[-1]["volt_mv"]),
                         (450, 1243.75))
        self.assertEqual((points[0]["freq_mhz"], points[-1]["freq_mhz"]),
                         (360, 2160))
        self.assertEqual(points[-1]["delta_khz"], 127 * 7500)

    def test_requested_row_with_missing_voltage_is_not_silently_dropped(self):
        curve = _VfpCurve()
        for index in range(2):
            curve.entries[index].freq_kHz = 1800000
            curve.entries[index].volt_uV = 900000 + index * 6250
        self.assertIsNone(GPU._vfp_curve_error(curve, 2))
        curve.entries[1].volt_uV = 0
        self.assertIn("incomplete", GPU._vfp_curve_error(curve, 2))


if __name__ == "__main__":
    unittest.main()
