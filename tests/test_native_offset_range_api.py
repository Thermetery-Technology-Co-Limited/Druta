# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fresh offset range C ABI, exercised through mock driver exports."""
import ctypes
import unittest
from unittest.mock import patch

from druta import native
from tests.fake_driver import ClockRig, XP

if not native.available():
    raise unittest.SkipTest("native core not built")


class Range(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("present", "low", "high", "current")]


class OffsetRangeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.get = native.load().dc_gpu_clock_offset_range_get
        cls.get.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Range)]
        cls.get.restype = ctypes.c_int

    def tearDown(self):
        native.set_export_resolver(None)

    def read(self, gpu, ctype):
        result = Range(7, 8, 9, 10)
        status = self.get(gpu._handle, ctype, ctypes.byref(result))
        return status, (result.present, result.low, result.high, result.current)

    def test_nvml_is_fresh_and_does_not_refresh_cached_capabilities(self):
        rig = ClockRig(pstates=False)
        with rig.native_gpu() as gpu:
            cached = gpu.capabilities()["core_off_range"]
            rig.offsets[0].update(mn=-321, mx=765, off=42)
            rig.offsets[2].update(mn=-800, mx=2400, off=900)
            rig.calls.clear()
            self.assertEqual(self.read(gpu, 0), (0, (1, -321, 765, 42)))
            self.assertEqual(self.read(gpu, 2), (0, (1, -800, 2400, 900)))
            self.assertEqual(gpu.capabilities()["core_off_range"], cached)
            self.assertEqual(rig.calls, [("GetClockOffsets", 0, 0), ("GetClockOffsets", 2, 0)])

    def test_pstates_units_partial_capability_and_other_identity(self):
        with patch("tests.fake_driver.TITAN", XP):
            rig = ClockRig(nvml_offsets=False)
        rig.p0[0].update(min=-1501, max=2501, value=-1999)
        rig.p0[4].update(min=-1501, max=2501, value=-1999)
        with rig.native_gpu() as gpu:
            self.assertEqual(self.read(gpu, 0), (0, (1, -1, 2, -1)))
            self.assertEqual(self.read(gpu, 2), (0, (1, -3, 5, -3)))
            rig.p0[4]["flags"] = 0
            self.assertEqual(self.read(gpu, 2), (-8, (7, 8, 9, 10)))
            self.assertEqual(self.read(gpu, 0), (0, (1, -1, 2, -1)))

    def test_transient_unavailability_can_recover_and_arguments_leave_output(self):
        rig = ClockRig(pstates=False)
        with rig.native_gpu() as gpu:
            saved = rig.offsets.pop(0)
            self.assertEqual(self.read(gpu, 0), (-8, (7, 8, 9, 10)))
            rig.offsets[0] = saved
            self.assertEqual(self.read(gpu, 0), (0, (1, -1000, 1000, 0)))
            rig.calls.clear()
            self.assertEqual(self.read(gpu, 1), (-1, (7, 8, 9, 10)))
            result = Range(7, 8, 9, 10)
            self.assertEqual(self.get(None, 0, ctypes.byref(result)), -1)
            self.assertEqual(self.get(gpu._handle, 0, None), -1)
            self.assertEqual((result.present, result.low, result.high, result.current), (7, 8, 9, 10))
            self.assertEqual(rig.calls, [])
