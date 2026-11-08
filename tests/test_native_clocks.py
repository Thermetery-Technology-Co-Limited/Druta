# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native clock offsets, NVML frequency lock and clock grid against their
Python twins: the same fake NVML/NVAPI clock state serves both, and the
snapping, the P-state fallback, the V/F shape restoration, the messages and
the driver call sequences must agree."""
import copy
import ctypes
import unittest

from druta import native, nvbackend
from tests.test_native_fan import ERROR_TEXT
from tests.test_native_identity import write_u32
from tests.test_native_telemetry import struct_callback, u32_callback
from tests.test_native_vfcurve import CurveRig, turing_rows
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend
PSTATES20_GET, PSTATES20_SET = 0x6FF81213, 0x0F4DAE6B


def gp102_clocks():
    return [round(139 + 12.657 * k) for k in range(141)]


class ClockRig(CurveRig):
    """CurveRig plus NVML clock offsets, the locked-clock pair and a state-backed P-state table.

    offsets:     NVML ctype -> {off, mn, mx} (GetClockOffsets answers from it, SetClockOffsets changes off)
    p0:          NVAPI domain -> {value, min, max, flags} for the single P0 entry
    grid:        the graphics clocks enumerated at the top memory clock
    """

    def __init__(self, *, nvml_offsets=True, nvml_set=True, nvml_set_status=0, nvml_apply=True, pstates=True,
                 pstates_set=True, pstates_set_status=0, grid=None, with_lock=True, lock_status=0,
                 reset_status=0, mem_div=4, **kw):
        self.grid_config = list(grid if grid is not None else [2100 - 15 * k for k in range(100)])
        self.nvml_set_status, self.nvml_apply = nvml_set_status, nvml_apply
        self.pstates_set_status, self.lock_status, self.reset_status = pstates_set_status, lock_status, reset_status
        self.mem_div = mem_div
        super().__init__(**kw)  # calls reset()
        e, i, n = self.nvml.exports, self.nvapi.interfaces, self.names
        if mem_div is None:
            n["RamType"] = i[0x57F7CAAC] = u32_callback(99)  # an unmapped memory type: no true-MHz scale
        e["nvmlErrorString"] = CDECL(ctypes.c_char_p, ctypes.c_int)(lambda st: ctypes.addressof(ERROR_TEXT.get(st, ERROR_TEXT[-1])))

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
        def gfxclocks(device, mem, count, values):
            wanted = self.grid if mem == 7001 else [300]
            k = ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))
            if not values or k[0] < len(wanted):
                k[0] = len(wanted)
                return 7
            k[0] = len(wanted)
            ctypes.memmove(values, (ctypes.c_uint32 * len(wanted))(*wanted), 4 * len(wanted))
            return 0
        e["nvmlDeviceGetSupportedGraphicsClocks"] = gfxclocks

        if nvml_offsets:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def get_offsets(device, pointer):
                co = NV._ClockOffset.from_address(pointer)
                self.calls.append(("GetClockOffsets", co.type, co.pstate))
                state = self.offsets.get(co.type)
                if state is None:
                    return 3
                co.off, co.mn, co.mx = state["off"], state["mn"], state["mx"]
                return 0
            e["nvmlDeviceGetClockOffsets"] = get_offsets
        if nvml_set:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            def set_offsets(device, pointer):
                co = NV._ClockOffset.from_address(pointer)
                self.calls.append(("SetClockOffsets", co.version, co.type, co.pstate, co.off))
                if self.nvml_set_status:
                    return self.nvml_set_status
                if self.nvml_apply and co.type in self.offsets:
                    self.offsets[co.type]["off"] = co.off
                return 0
            e["nvmlDeviceSetClockOffsets"] = set_offsets
        if with_lock:
            @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
            def lock(device, lo, hi):
                self.calls.append(("SetGpuLockedClocks", lo, hi))
                return self.lock_status
            e["nvmlDeviceSetGpuLockedClocks"] = lock

            @CDECL(ctypes.c_int, ctypes.c_void_p)
            def unlock(device):
                self.calls.append(("ResetGpuLockedClocks",))
                return self.reset_status
            e["nvmlDeviceResetGpuLockedClocks"] = unlock

        if pstates:
            def table(info):
                if info.version != NV.NvAPI.ver(NV._Pstates20V2, 3):
                    return -9
                self.calls.append(("Pstates20Get",))
                info.num_pstates, info.num_clocks, info.num_voltages = 2, 2, 1
                p0 = info.pstates[0]
                p0.pstate = 0
                for k, (domain, kind) in enumerate(((0, 1), (4, 0))):
                    row = self.p0[domain]
                    clock = p0.clocks[k]
                    clock.domain, clock.kind, clock.flags = domain, kind, row["flags"]
                    clock.delta.value, clock.delta.minimum, clock.delta.maximum = row["value"], row["min"], row["max"]
                p0.clocks[0].data[0], p0.clocks[0].data[1] = 300000, 2100000
                p0.clocks[1].data[0] = 7000500
                info.pstates[1].pstate = 8
                return 0
            n["Pstates20Get"] = i[PSTATES20_GET] = struct_callback(NV._Pstates20V2, table)
            if pstates_set:
                @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
                def pstates_set_fn(handle, pointer):
                    req = NV._Pstates20V2.from_address(pointer)
                    clock = req.pstates[0].clocks[0]
                    self.calls.append(("Pstates20Set", req.version, req.num_pstates, req.num_clocks,
                                       req.pstates[0].pstate, clock.domain, clock.delta.value))
                    if self.pstates_set_status:
                        return self.pstates_set_status
                    self.p0[clock.domain]["value"] = clock.delta.value
                    return 0
                n["Pstates20Set"] = i[PSTATES20_SET] = pstates_set_fn
        else:
            n.pop("Pstates20Get", None)
            i.pop(PSTATES20_GET, None)

    def reset(self):
        super().reset()
        self.grid = list(self.grid_config)
        self.offsets = {0: {"off": 0, "mn": -1000, "mx": 1000}, 2: {"off": 0, "mn": -2000, "mx": 6000}}
        self.p0 = {0: {"value": 0, "min": -1000000, "max": 1000000, "flags": 1},
                   4: {"value": 0, "min": -500500, "max": 1500500, "flags": 1}}

    def state(self):
        return copy.deepcopy((self.rows, self.deltas, self.offsets, self.p0))

    def python_gpu(self):
        gpu = super().python_gpu()
        gpu.nvapi.Pstates20Set = self.names.get("Pstates20Set")
        gpu.nvapi.Pstates20Get = self.names.get("Pstates20Get")
        gpu.static.update(mem_div=self.mem_div, gfx_min=615, gfx_max=2100)  # what _read_static reads here
        return gpu


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
        rig = ClockRig()
        rig.nvml.exports.pop("nvmlDeviceGetSupportedGraphicsClocks")
        self.assertEqual(self.both(rig, lambda g: (g.clock_step_khz(), g.step_is_measured()))[0], (15000, False))
        short = ClockRig(grid=[2100, 2085, 2070])
        self.assertEqual(self.both(short, lambda g: (g.clock_step_khz(), g.step_is_measured()))[0], (15000, False))


class ClockOffsetTests(ParityCase):
    def test_core_offset_through_nvml_keeps_the_curve_shape(self):
        rig = ClockRig()
        result, calls, (_, deltas, offsets, _) = self.both(rig, lambda g: g.set_clock_offset(0, 60))
        self.assertEqual(result, (True, "core offset set to +60 MHz; V/F shape kept"))
        self.assertEqual([c for c in calls if c[0] == "SetClockOffsets"],
                         [("SetClockOffsets", NV.Nvml.ver(NV._ClockOffset, 1), 0, 0, 60)])
        self.assertEqual(offsets[0]["off"], 60)
        self.assertEqual(set(deltas), {60000})  # every point shifted by the accepted core change
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

    def test_memory_offset_scale_readback_and_representability(self):
        rig = ClockRig()
        result, calls, (_, _, offsets, _) = self.both(rig, lambda g: g.set_clock_offset(2, 100))
        self.assertEqual(result, (True, "mem offset set to +100 MHz true"))
        self.assertEqual([c for c in calls if c[0] == "SetClockOffsets"][0][2:], (2, 0, 800))
        self.assertEqual(offsets[2]["off"], 800)
        result, *_ = self.both(rig, lambda g: g.set_clock_offset(2, 100.0625))
        self.assertEqual(result, (False, "mem offset +100.062 MHz true is not representable; use multiples of 0.125 MHz true"))
        result, *_ = self.both(ClockRig(nvml_apply=False), lambda g: g.set_clock_offset(2, 100))
        self.assertEqual(result, (False, "mem offset requested +100 MHz true, but the driver reported +0 MHz true"))
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
