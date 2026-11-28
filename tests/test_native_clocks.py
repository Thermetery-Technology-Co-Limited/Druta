# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clock offsets, NVML frequency locks and the clock grid on a fake driver.

Shared behavior is checked against Python. Native core writes also verify
NVML readback before shifting the V/F deltas.
"""
import ctypes
import unittest

from druta import native, nvbackend
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend
from tests.fake_driver import PSTATES20_GET, PSTATES20_SET, ClockRig, gp102_clocks  # noqa: E402, F401


class ClockGridTests(ParityCase):
    def test_scale_and_grid(self):
        rig = ClockRig()
        self.assertEqual(self.both(rig, lambda g: g.mem_offset_scale())[0], (8, "MHz true"))
        self.assertEqual(self.both(ClockRig(mem_div=None), lambda g: g.mem_offset_scale())[0], (2, "MHz eff"))
        self.assertEqual(self.both(rig, lambda g: g.memory_offset_step_units())[0], 1)
        self.assertEqual(self.both(rig, lambda g: (g.clock_step_khz(), g.step_is_measured(), g.max_rise_khz()))[0],
                         (15000, True, 45000))
        step, measured, _ = self.both(ClockRig(grid=gp102_clocks()), lambda g: (g.clock_step_khz(), g.step_is_measured(), 0))[0]
        self.assertTrue(measured)
        self.assertTrue(12600 <= step <= 12700)
        rows, *_ = self.both(rig, lambda g: g.lockable_clocks_by_mem())
        self.assertEqual([m for m, _ in rows], [405, 810, 5001, 7001])
        self.assertEqual(len(rows[3][1]), 100)

    def test_grid_falls_back_without_a_table(self):
        rig = ClockRig(with_curve=False)
        rig.nvml.exports.pop("nvmlDeviceGetSupportedGraphicsClocks")
        self.assertEqual(self.both(rig, lambda g: (g.clock_step_khz(), g.step_is_measured()))[0], (15000, False))
        short = ClockRig(grid=[2100, 2085, 2070], with_curve=False)
        self.assertEqual(self.both(short, lambda g: (g.clock_step_khz(), g.step_is_measured()))[0], (15000, False))

    def test_grid_from_sparse_curve_and_quantized_adjacent_bins(self):
        # Driver rounds to whole MHz in the wire domain. Neither the frequency
        # base nor the skipped V/F rows need match the hardware used to qualify it.
        bins = [0, 0, 3, 8, 16, 19, 22, 24, 31, 32, 35, *range(36, 65), 64]
        for arch, divisor, step in ((4, 2, 12656.25), (6, 1, 18500)):
            rows = [(500000 + i * 6250, 1000 * round((412500 + b * step) * divisor / 1000))
                    for i, b in enumerate(bins)]
            rig = ClockRig(arch=arch, grid=[], rows=rows)
            with self.subTest(arch=arch), rig.native_gpu() as gpu:
                self.assertAlmostEqual(gpu.clock_step_khz(), step, delta=10)
                self.assertFalse(gpu.step_is_measured())
                self.assertEqual(gpu.max_rise_khz(), 3 * gpu.clock_step_khz())
                self.assertFalse(any(c[0] in ("BoostTableSet", "Pstates20Set", "SetClockOffsets") for c in rig.calls))

    def test_irregular_shaped_flat_or_uniformly_sampled_curves_do_not_qualify_a_grid(self):
        bins = [0, 3, 8, 15, *range(16, 34)]
        base = [(500000 + i * 6250, 300000 + 12500 * b) for i, b in enumerate(bins)]
        for kind in ("irregular", "shaped", "flat", "uniform"):
            rows = list(base)
            if kind == "irregular":
                rows[1] = (rows[1][0], rows[1][1] + 5000)
            elif kind == "flat":
                rows = [(v, 900000) for v, f in rows]
            elif kind == "uniform":
                rows = [(v, 300000 + i * 30000) for i, (v, f) in enumerate(rows)]
            rig = ClockRig(arch=6, grid=[], rows=rows)
            if kind == "shaped":
                rig.deltas[3] = 30000
            with self.subTest(kind=kind), rig.native_gpu() as gpu:
                self.assertEqual(gpu.clock_step_khz(), 15000)
                self.assertFalse(gpu.step_is_measured())
                if kind == "shaped":
                    rig.deltas[3] = 0
                    gpu.refresh_capabilities()
                    self.assertEqual(gpu.clock_step_khz(), 12500)
                    self.assertFalse(gpu.step_is_measured())


class ClockOffsetTests(ParityCase):
    def both(self, rig, action):
        # The native writer corroborates the request with both getters. Compare
        # the result, final state, setter packets and all other driver calls.
        return super().both(rig, action, extra_reads=("GetClockOffsets", "Pstates20Get"))

    def native_offset(self, rig, value):
        with rig.native_gpu() as gpu:
            gpu.capabilities()
            rig.calls.clear()
            return gpu.set_clock_offset(0, value)

    def test_core_offset_through_nvml_keeps_the_curve_shape(self):
        rig = ClockRig()
        result = self.native_offset(rig, 60)
        self.assertEqual(result, (True, "core offset set to +60 MHz; V/F shape kept"))
        set_call = ("SetClockOffsets", NV.Nvml.ver(NV._ClockOffset, 1), 0, 0, 60)
        write = rig.calls.index(set_call)
        self.assertEqual(rig.calls[write + 1], ("GetClockOffsets", 0, 0))
        self.assertEqual(rig.offsets[0]["off"], 60)
        self.assertEqual(set(rig.deltas), {60000})  # every point shifted by the verified core change

        result, calls, *_ = self.both(rig, lambda g: g.set_clock_offset(0, 60, preserve_curve=False))
        self.assertEqual(result, (True, "core offset set to +60 MHz"))
        self.assertEqual([c[0] for c in calls if c[0].startswith("Boost") or c[0] == "VfpCurve"], [])

    def test_core_offset_is_snapped_to_the_grid(self):
        result, *_ = self.both(ClockRig(), lambda g: g.set_clock_offset(0, 52, preserve_curve=False))
        self.assertEqual(result, (True, "core offset set to +45 MHz"))
        result, calls, _ = self.both(ClockRig(grid=gp102_clocks()), lambda g: g.set_clock_offset(0, 60, preserve_curve=False))
        self.assertEqual(result, (True, "core offset set to +64 MHz"))  # 5 bins of 12.657, rounded up
        self.assertEqual([c for c in calls if c[0] == "SetClockOffsets"][0][4], 64)
        result, *_ = self.both(ClockRig(), lambda g: g.set_clock_offset(0, -52.9, preserve_curve=False))
        self.assertEqual(result, (True, "core offset set to -45 MHz"))

    def test_unverified_nvml_core_write_restores_the_curve_without_a_shift(self):
        for missing in (False, True):
            with self.subTest(missing_readback=missing):
                rig = ClockRig(nvml_apply=False, pstates=not missing)
                rig.deltas[100] = 45000
                baseline = list(rig.deltas)
                dispatch = rig.nvml.exports["nvmlDeviceSetClockOffsets"]

                @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
                def flatten_on_set(handle, request):
                    status = dispatch(handle, request)
                    rig.deltas[100] = 0
                    if missing:
                        del rig.offsets[0]
                    return status

                rig.nvml.exports["nvmlDeviceSetClockOffsets"] = flatten_on_set
                ok, message = self.native_offset(rig, 15)
                self.assertFalse(ok)
                self.assertIn("readback failed" if missing else "the driver reported +0 MHz", message)
                self.assertEqual(rig.deltas, baseline)
                writes = [c for c in rig.calls if c[0] in ("SetClockOffsets", "Pstates20Set")]
                self.assertEqual([c[0] for c in writes], ["SetClockOffsets"])

    def test_memory_offset_scale_readback_and_representability(self):
        rig = ClockRig()
        result, calls, (_, _, offsets, _) = self.both(rig, lambda g: g.set_clock_offset(2, 100))
        self.assertEqual(result, (True, "mem offset set to +100 MHz true"))
        self.assertEqual([c for c in calls if c[0] == "SetClockOffsets"][0][2:], (2, 0, 800))
        self.assertEqual(offsets[2]["off"], 800)
        result, *_ = self.both(rig, lambda g: g.set_clock_offset(2, 100.0625))
        self.assertEqual(result, (False, "mem offset +100.062 MHz true is not representable; use multiples of 0.125 MHz true"))
        result, calls, _ = self.both(ClockRig(nvml_apply=False), lambda g: g.set_clock_offset(2, 100))
        self.assertEqual(result, (False, "mem offset requested +100 MHz true, but the driver reported +0 MHz true"))
        self.assertEqual([c[0] for c in calls if c[0] in ("SetClockOffsets", "Pstates20Set")], ["SetClockOffsets"])
        result, *_ = self.both(ClockRig(mem_div=None), lambda g: g.set_clock_offset(2, 100))
        self.assertEqual(result, (True, "mem offset set to +100 MHz eff"))

    def test_range_refusals(self):
        rig = ClockRig()
        self.assertEqual(self.both(rig, lambda g: g.set_clock_offset(0, 2000))[0],
                         (False, "core offset +1995 MHz out of range [-1000..1000]"))  # snapped first
        self.assertEqual(self.both(rig, lambda g: g.set_clock_offset(2, -300))[0],
                         (False, "mem offset -300 MHz true out of range [-250..750]"))
        missing = ClockRig(pstates=False)
        original = missing.reset

        def no_mem_range():
            original()
            del missing.offsets[2]
        missing.reset = no_mem_range
        self.assertEqual(self.both(missing, lambda g: g.set_clock_offset(2, 100))[0],
                         (False, "memory offset range/readback is unavailable; no write issued"))

    def test_nvml_stub_falls_back_to_the_p_state_setter(self):
        rig = ClockRig(nvml_set_status=3)
        result, calls, (_, _, _, p0) = self.both(rig, lambda g: g.set_clock_offset(0, 60, preserve_curve=False))
        self.assertEqual(result, (True, "core offset set to +60 MHz (NVAPI Pstates20)"))
        self.assertEqual([c for c in calls if c[0] == "Pstates20Set"],
                         [("Pstates20Set", NV.NvAPI.ver(NV._Pstates20V2, 3), 1, 1, 0, 0, 60000)])
        self.assertEqual(p0[0]["value"], 60000)
        result, *_ = self.both(rig, lambda g: g.set_clock_offset(2, 100, preserve_curve=False))
        self.assertEqual(result, (True, "mem offset set to +100 MHz true (NVAPI Pstates20)"))
        self.assertEqual(self.both(ClockRig(nvml_set_status=4), lambda g: g.set_clock_offset(0, 60, preserve_curve=False))[0],
                         (False, "core offset failed: Insufficient Permissions"))

    def test_p_state_path_without_the_nvml_setter(self):
        rig = ClockRig(nvml_set=False, nvml_offsets=False)
        result, *_ = self.both(rig, lambda g: g.set_clock_offset(0, 60, preserve_curve=False))
        self.assertEqual(result, (True, "core offset set to +60 MHz (NVAPI Pstates20)"))
        self.assertEqual(self.both(ClockRig(nvml_set=False, nvml_offsets=False, pstates_set_status=-6),
                                   lambda g: g.set_clock_offset(0, 60, preserve_curve=False))[0],
                         (False, "core offset failed: NVAPI status -6"))
        locked = ClockRig(nvml_set=False, nvml_offsets=False)
        original = locked.reset

        def not_editable():
            original()
            locked.p0[0]["flags"] = 0
        locked.reset = not_editable
        self.assertEqual(self.both(locked, lambda g: g.set_clock_offset(0, 60, preserve_curve=False))[0],
                         (False, "P0 core clock offset is not editable"))
        self.assertEqual(self.both(ClockRig(nvml_set=False, pstates=False), lambda g: g.set_clock_offset(0, 60))[0],
                         (False, "clock offset setter is unavailable"))

    def test_resending_the_stored_offset_writes_nothing(self):
        rig = ClockRig()
        original = rig.reset

        def stored():
            original()
            rig.offsets[0]["off"] = 60
            rig.p0[0]["value"] = 60000
        rig.reset = stored
        result, calls, _ = self.both(rig, lambda g: g.set_clock_offset(0, 60))
        self.assertEqual(result, (True, "core offset set to +60 MHz"))
        self.assertEqual([c for c in calls if c[0] == "SetClockOffsets"], [])

    def test_a_refused_write_still_restores_the_shape_unshifted(self):
        rig = ClockRig(nvml_set_status=4)
        original = rig.reset

        def shaped():
            original()
            rig.deltas[100] = 45000
        rig.reset = shaped

        def scenario(g):
            result = g.set_clock_offset(0, 60)
            rig.deltas[100] = 0  # the driver flattened the table despite refusing
            return result
        # Both sides read the shape before the write; the fake's flatten after the write is restored unshifted.
        result, calls, (_, deltas, _, _) = self.both(rig, scenario)
        self.assertEqual(result, (False, "core offset failed: Insufficient Permissions"))

    def test_python_argument_checks(self):
        rig = ClockRig()
        for args, message in (((0, 60, 1), "preserve_curve must be a bool"),
                              ((1, 60), "clock offset type must be 0 (core) or 2 (memory)"),
                              ((0, "60"), "clock offset must be a finite number, not a bool or string"),
                              ((0, float("inf")), "clock offset must be a finite number"),
                              ((0, 1 << 31), "clock offset exceeds the driver's signed 32-bit range")):
            with self.subTest(message=message):
                result, calls, _ = self.both(rig, lambda g: g.set_clock_offset(*args))
                self.assertEqual(result, (False, message))
                self.assertEqual(calls, [])


class PairedOffsetTests(ParityCase):
    @staticmethod
    def stale_rig(*, error=None):
        rig = ClockRig(nvml_apply=False)
        rig.p0[4].update(min=-1000000, max=3000000)
        dispatch = rig.nvml.exports["nvmlDeviceSetClockOffsets"]

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def apply_to_pstates(handle, request):
            status = dispatch(handle, request)
            co = NV._ClockOffset.from_address(request)
            domain, khz_per_unit = (0, 1000) if co.type == 0 else (4, 500)
            rig.p0[domain]["value"] = co.off * khz_per_unit
            if error:
                error(rig, co, domain)
            return status

        rig.nvml.exports["nvmlDeviceSetClockOffsets"] = apply_to_pstates
        return rig

    def assert_range(self, gpu, ctype, current):
        get = native.load().dc_gpu_clock_offset_range_get
        get.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(native._OffsetRange))
        result = native._OffsetRange()
        status = get(gpu._handle, ctype, ctypes.byref(result))
        if current is None:
            self.assertEqual(status, native.DC_E_UNAVAILABLE)
        else:
            self.assertEqual((status, result.present, result.current), (native.DC_OK, 1, current))

    def test_one_nvml_set_per_apply_and_undo_with_independent_readbacks(self):
        rig = self.stale_rig()
        rig.deltas[100] = 45000
        with rig.native_gpu() as gpu:
            baseline = gpu.vf_delta_snapshot()
            self.assert_range(gpu, 0, 0)
            self.assert_range(gpu, 2, 0)
            for ctype, mhz, units in ((0, 15, 15), (2, 1, 8)):
                ok, message = gpu.set_clock_offset(ctype, mhz)
                self.assertTrue(ok, message)
                self.assertIn("verified by NVAPI Pstates20", message)
                self.assert_range(gpu, ctype, units)
                self.assertEqual(rig.offsets[ctype]["off"], 0)
            self.assertEqual(gpu.vf_delta_snapshot()["deltas"],
                             {idx: delta + 15000 for idx, delta in baseline["deltas"].items()})
            self.assertEqual((gpu.read()["core_off"], gpu.read()["mem_off"]), (15, 8))
            for ctype, peer, peer_units in ((0, 2, 8), (2, 0, 0)):
                ok, message = gpu.set_clock_offset(ctype, 0)
                self.assertTrue(ok, message)
                self.assert_range(gpu, ctype, 0)
                self.assert_range(gpu, peer, peer_units)
            self.assertEqual(gpu.vf_delta_snapshot(), baseline)
            self.assertEqual([c[2:] for c in rig.calls if c[0] == "SetClockOffsets"],
                             [(0, 0, 15), (2, 0, 8), (0, 0, 0), (2, 0, 0)])
            self.assertFalse(any(c[0] == "Pstates20Set" for c in rig.calls))

    def test_conflicting_baseline_and_external_change_are_not_adopted(self):
        rig = self.stale_rig()
        with rig.native_gpu() as gpu:
            rig.p0[0]["value"] = 15000
            self.assert_range(gpu, 0, None)
            self.assertFalse(gpu.set_clock_offset(0, 30)[0])
            self.assertFalse(any(c[0] == "SetClockOffsets" for c in rig.calls))

            rig.p0[0]["value"] = 0
            self.assertTrue(gpu.set_clock_offset(0, 15)[0])
            with native.Gpu() as fresh:
                self.assert_range(fresh, 0, None)
            self.assert_range(gpu, 0, 15)
            rig.p0[0]["flags"] = 0
            self.assert_range(gpu, 0, None)
            self.assertFalse(gpu.set_clock_offset(0, 0)[0])
            rig.p0[0]["flags"] = 1
            self.assert_range(gpu, 0, 15)
            rig.p0[0]["value"] = 30000  # one-sided external change
            self.assert_range(gpu, 0, None)
            self.assertFalse(gpu.set_clock_offset(0, 0)[0])
            self.assert_range(gpu, 2, 0)
            self.assertEqual([c[0] for c in rig.calls if c[0] in ("SetClockOffsets", "Pstates20Set")],
                             ["SetClockOffsets"])

    def test_partial_conflicting_or_inexact_post_write_is_not_verified(self):
        variants = (
            lambda r, co, d: r.p0[d].update(value=r.p0[d]["value"] + 1),
            lambda r, co, d: r.p0[d].update(flags=0),
            lambda r, co, d: r.p0[d].update(max=r.p0[d]["max"] + 1),
            lambda r, co, d: r.offsets[co.type].update(off=co.off + 1),
        )
        for index, error in enumerate(variants):
            rig = self.stale_rig(error=error)
            with self.subTest(case=index), rig.native_gpu() as gpu:
                before = gpu.vf_delta_snapshot()
                self.assertFalse(gpu.set_clock_offset(0, 15)[0])
                self.assertEqual(gpu.vf_delta_snapshot(), before)
                self.assert_range(gpu, 0, None)
                self.assertFalse(gpu.set_clock_offset(0, 0)[0])
                self.assertEqual([c[0] for c in rig.calls if c[0] in ("SetClockOffsets", "Pstates20Set")],
                                 ["SetClockOffsets"])


class ClockLockTests(ParityCase):
    def test_lock_snaps_down_and_remembers_the_request(self):
        rig = ClockRig()
        result, calls, _ = self.both(rig, lambda g: (g.lock_gpu_clocks(1000, 1500), g.last_clock_lock_request()))
        self.assertEqual(result, ((True, "GPU clock locked to [990..1500] MHz (snapped down from [1000..1500]: the driver "
                                         "would have rounded UP to a clock you did not ask for)"), (990, 1500)))
        self.assertEqual([c for c in calls if c[0] == "SetGpuLockedClocks"], [("SetGpuLockedClocks", 990, 1500)])
        self.assertEqual(self.both(rig, lambda g: g.lock_gpu_clocks(990, 1500))[0], (True, "GPU clock locked to [990..1500] MHz"))
        self.assertEqual(self.both(rig, lambda g: g.lock_gpu_clocks(1500, 1000))[0], (False, "lock: min 1500 > max 1000 MHz"))
        self.assertEqual(self.both(rig, lambda g: g.lock_gpu_clocks(500, 1000))[0],
                         (False, "lock: values must be within [615..2100] MHz"))
        self.assertEqual(self.both(ClockRig(lock_status=4), lambda g: (g.lock_gpu_clocks(990, 1500), g.last_clock_lock_request()))[0],
                         ((False, "lock failed: Insufficient Permissions (needs admin)"), None))
        self.assertEqual(self.both(ClockRig(with_lock=False), lambda g: g.lock_gpu_clocks(990, 1500))[0],
                         (False, "SetGpuLockedClocks not available"))

    def test_reset(self):
        rig = ClockRig()
        self.assertEqual(self.both(rig, lambda g: (g.lock_gpu_clocks(990, 1500), g.reset_gpu_clocks(), g.last_clock_lock_request()))[0],
                         ((True, "GPU clock locked to [990..1500] MHz"), (True, "GPU clock lock released"), None))
        self.assertEqual(self.both(ClockRig(reset_status=3), lambda g: g.reset_gpu_clocks())[0],
                         (False, "reset failed: Not Supported"))
        self.assertEqual(self.both(ClockRig(reset_status=3, arch=4), lambda g: g._reset_gpu_clocks(allow_pascal_noop=True))[0],
                         (True, "NVML frequency lock is not applicable to Pascal; nothing to reset"))
        self.assertEqual(self.both(ClockRig(reset_status=3, arch=4), lambda g: g.reset_gpu_clocks())[0],
                         (False, "reset failed: Not Supported"))
        self.assertEqual(self.both(ClockRig(with_lock=False), lambda g: g.reset_gpu_clocks())[0],
                         (False, "ResetGpuLockedClocks not available"))


if __name__ == "__main__":
    unittest.main()
