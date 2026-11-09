# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native rephase_deltas against nvbackend.GPU.rephase_deltas: one fake
V/F table, delta table and lockable clock list serve both, and the outcome
(message byte for byte), the driver call sequence, the written delta table and
the layout and clock-grid caches left behind must agree."""
import ctypes
import unittest

from druta import native, nvbackend
from tests.test_native_clocks import ClockRig, gp102_clocks
from tests.test_native_vfcurve import layout_tuple, pascal_rows
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NOT_APPLICABLE = "could not determine this card's VF delta units"
NV_BOOST_TABLE_VERSION = nvbackend.NvAPI.ver(nvbackend._BoostTable, 1)
# One clock-grid enumeration as ClockRig(log_grid=True) logs it: per memory clock of the twin's
# static mem_clocks, the count query and then the call that fills the buffer.
GRID_ENUMERATION = [("GfxClocks", mem, filled) for mem in (405, 810, 5001, 7001) for filled in (False, True)]


def with_deltas(rig, deltas):
    """Every run (Python, then native) starts from these hardware deltas."""
    original = rig.reset

    def reset():
        original()
        for idx, delta in deltas.items():
            rig.deltas[idx] = delta
    rig.reset = reset
    return rig


def rephase_and_caches(g):
    """The outcome, then the full layout (notes included) and grid the GPU object holds afterwards.

    Both later reads are answered from the caches when rephase_deltas left them
    filled, so the call log shows whether it did (a re-probe adds VfpCurve calls)."""
    return g.rephase_deltas(), layout_tuple(g.vfp_layout()), g.clock_step_khz()


def rephase_and_caches_without_notes(g):
    """rephase_and_caches with the layout notes left out, for a grid whose top clock is not 2100 MHz.

    The notes quote gfx_max, which this rig's Python twin carries as a fixed 2100 while the
    core reads it from the grid (see ClockRig.python_gpu); only such a grid needs this."""
    result, layout, step = rephase_and_caches(g)
    return result, layout[:4], step


def writes(calls):
    return [c for c in calls if c[0] == "BoostTableSet"]


def grid_calls(calls):
    return [c for c in calls if c[0] == "GfxClocks"]


def without_written_rows(calls):
    """The call log with each write reduced to (name, version, width), for an order comparison."""
    return [c[:3] if c[0] == "BoostTableSet" else c for c in calls]


class RephaseTests(ParityCase):
    def test_all_deltas_on_one_phase_writes_nothing(self):
        for deltas in ({}, {k: 5000 + 15000 * (k % 4) for k in range(128)}):
            with self.subTest(uniform=bool(deltas)):
                rig = with_deltas(ClockRig(), deltas)
                (result, layout, step), calls, (_, after, _, _) = self.both(rig, rephase_and_caches)
                self.assertEqual(result, (True, "all 128 deltas already share one 15 MHz phase"))
                self.assertEqual(writes(calls), [])
                self.assertEqual(after[:4], [deltas.get(k, 0) for k in range(4)])
                self.assertEqual(layout, (128, list(range(128)), [], 1, "128 entries; 128 GPU points; GPU frequency "
                                                                        "is direct; top raw 2205 vs gfx_max 2100"))
                self.assertEqual(step, 15000)
                # One width probe and one read for the layout, one read of the curve itself,
                # one delta-table read; the follow-up vfp_layout() came from the cache.
                self.assertEqual([c for c in calls if c[0] != "BoostTableSet"],
                                 [("VfpCurve", 128), ("VfpCurve", 128), ("VfpCurve", 128), ("BoostTableGet", 128)])

    def test_off_phase_points_are_rounded_down_to_the_common_phase(self):
        # Majority phase 0 mod 15 MHz; idx 7 sits 5 MHz above a bin, idx 3 is 1 MHz below
        # zero, idx 100 is 14.999 MHz above a whole bin: each loses less than one bin.
        rig = with_deltas(ClockRig(), {3: -1000, 7: 20000, 50: 30000, 100: 44999})
        (result, layout, step), calls, (_, after, _, _) = self.both(rig, rephase_and_caches)
        self.assertEqual(result, (True, "re-phased 3 off-phase point(s) (idx [3, 7, 100]) onto the common "
                                        "15 MHz grid"))
        self.assertEqual(layout[:4], (128, list(range(128)), [], 1))
        self.assertEqual(step, 15000)
        self.assertEqual((after[3], after[7], after[50], after[100]), (-15000, 15000, 30000, 30000))
        self.assertEqual(sum(1 for d in after if d), 4)
        (write,) = writes(calls)  # ONE write carries every correction
        self.assertEqual(write[2], 128)

    def test_a_single_off_phase_point(self):
        rig = with_deltas(ClockRig(), {9: 7500})
        result, calls, (_, after, _, _) = self.both(rig, lambda g: g.rephase_deltas())
        self.assertEqual(result, (True, "re-phased 1 off-phase point(s) (idx [9]) onto the common 15 MHz grid"))
        self.assertEqual(after[9], 0)
        self.assertEqual(len(writes(calls)), 1)

    def test_pascal_grid_is_the_physical_step_times_freq_div(self):
        rig = ClockRig(arch=4, rows=pascal_rows(), grid=gp102_clocks())
        step, *_ = self.both(rig, lambda g: g.clock_step_khz())
        self.assertEqual(step, 12657)
        # One physical bin (12.657 MHz) is half a GPC2CLK bin, so it is off the phase of the
        # whole-bin deltas and loses that half; the 81st row (a memory point) is never read.
        two_bins = 2 * 2 * step
        with_deltas(rig, {k: two_bins for k in range(0, 80, 2)} | {k: 0 for k in range(1, 80, 2)}
                    | {10: two_bins + step, 11: step, 81: step})
        # GP102's grid tops out at 1911 MHz, not the twin's fixed gfx_max of 2100: the notes differ by design.
        (result, layout, _), calls, (_, after, _, _) = self.both(rig, rephase_and_caches_without_notes)
        self.assertEqual(result, (True, "re-phased 2 off-phase point(s) (idx [10, 11]) onto the common "
                                        "12.66 MHz grid"))
        self.assertEqual(layout[3], 2)
        self.assertEqual((after[10], after[11], after[81]), (two_bins, 0, step))
        (write,) = writes(calls)
        self.assertEqual(write[2], 84)
        # Uniform physical-bin deltas share one phase of the doubled grid: nothing to do.
        with_deltas(rig, {k: 3 * 2 * step for k in range(80)})
        result, calls, _ = self.both(rig, lambda g: g.rephase_deltas())
        self.assertEqual(result, (True, "all 80 deltas already share one 12.66 MHz phase"))
        self.assertEqual(writes(calls), [])

    def test_a_curve_read_error_is_returned_and_nothing_is_written(self):
        cases = [
            (ClockRig(get_status=-104, log_grid=True), "delta-table read failed (status -104)"),
            (ClockRig(curve_status=-3, log_grid=True), "could not determine this card's VF table layout"),
            (ClockRig(arch=2, log_grid=True), "V/F curves are not applicable on this GPU"),
            (ClockRig(with_curve=False, log_grid=True), "VF curve APIs unavailable"),
        ]
        for rig, message in cases:
            with self.subTest(message=message):
                result, calls, _ = self.both(with_deltas(rig, {7: 20000}), lambda g: g.rephase_deltas())
                self.assertEqual(result, (False, message))
                self.assertEqual(writes(calls), [])
                self.assertEqual(grid_calls(calls), [])  # the grid is never enumerated for a failed read

    def test_a_layout_lost_after_the_read_refuses(self):
        # The architecture is unreadable (not cached) while the curve is read, then reads as
        # Kepler once the delta table has answered: the read succeeds, the later layout does not.
        rig = with_deltas(ClockRig(log_grid=True), {7: 20000})
        rig.kepler = False
        original_reset = rig.reset

        def reset():
            original_reset()
            rig.kepler = False
        rig.reset = reset

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def architecture(device, pointer):
            rig.calls.append(("Architecture", rig.kepler))
            if not rig.kepler:
                return 999
            ctypes.c_uint32.from_address(pointer).value = 2
            return 0
        rig.nvml.exports["nvmlDeviceGetArchitecture"] = architecture
        get = rig.names["BoostTableGet"]

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def boost_get(handle, pointer):
            status = get(handle, pointer)
            rig.kepler = True
            return status
        rig.names["BoostTableGet"] = rig.nvapi.interfaces[0x23F1B133] = boost_get
        result, calls, (_, after, _, _) = self.both(rig, lambda g: g.rephase_deltas())
        self.assertEqual(result, (False, NOT_APPLICABLE))
        self.assertEqual(writes(calls), [])
        self.assertEqual(after[7], 20000)
        # The layout is not cached here, so its read after the grid is visible (the architecture
        # query that now refuses): the grid is enumerated once, AFTER the curve and delta-table
        # read and BEFORE the layout read.
        self.assertEqual(calls, [("Architecture", False), ("Architecture", False), ("VfpCurve", 128),
                                 ("VfpCurve", 128), ("Architecture", False), ("VfpCurve", 128),
                                 ("BoostTableGet", 128)] + GRID_ENUMERATION + [("Architecture", True)])

    def test_an_apply_failure_is_passed_through(self):
        rig = with_deltas(ClockRig(set_status=-104), {7: 20000})
        result, calls, (_, after, _, _) = self.both(rig, lambda g: g.rephase_deltas())
        self.assertEqual(result, (False, "VF table write failed (status -104)"))
        self.assertEqual(len(writes(calls)), 1)
        self.assertEqual(after[7], 20000)
        # A corrected delta beyond the 1 GHz guard is refused by the writer, not by rephase.
        rig = with_deltas(ClockRig(), {5: -1000001})
        result, calls, (_, after, _, _) = self.both(rig, lambda g: g.rephase_deltas())
        self.assertEqual(result, (False, "refusing point 5: delta -1005 MHz More than a whole gigahertz of delta? "
                                         "Lol, bro thinks he's Seby. Caught you with a mouse-slip and saved you a "
                                         "total driver crash this time. Don't do it next time. :copege:"))
        self.assertEqual(writes(calls), [])
        self.assertEqual(after[5], -1000001)

    def test_the_grid_is_enumerated_once_between_the_curve_read_and_the_write_and_then_cached(self):
        rig = with_deltas(ClockRig(log_grid=True), {7: 20000})
        (first, second), calls, (_, after, _, _) = self.both(rig, lambda g: (g.rephase_deltas(), g.rephase_deltas()))
        self.assertEqual(first, (True, "re-phased 1 off-phase point(s) (idx [7]) onto the common 15 MHz grid"))
        self.assertEqual(second, (True, "all 128 deltas already share one 15 MHz phase"))
        self.assertEqual(after[7], 15000)
        # Width probe, layout read, curve read, delta read; then the grid, once; then the
        # writer's read-modify-write. The second call reads the curve again (the layout is
        # cached) but never the grid: the step is cached.
        self.assertEqual(without_written_rows(calls),
                         [("VfpCurve", 128), ("VfpCurve", 128), ("VfpCurve", 128), ("BoostTableGet", 128)]
                         + GRID_ENUMERATION
                         + [("BoostTableGet", 128), ("BoostTableSet", NV_BOOST_TABLE_VERSION, 128),
                            ("VfpCurve", 128), ("BoostTableGet", 128)])
        self.assertEqual(grid_calls(calls), GRID_ENUMERATION)

    def test_a_phase_tie_keeps_the_phase_seen_first_in_curve_order(self):
        # 64 points at 5 MHz (idx 0-63, first in curve order) and 64 at 0 (idx 64-127): the tie
        # goes to the remainder counted first, so the zero group is rounded DOWN to -10 MHz.
        deltas = {k: 5000 for k in range(64)} | {k: 0 for k in range(64, 128)}
        rig = with_deltas(ClockRig(), deltas)
        (result, layout, step), calls, (_, after, _, _) = self.both(rig, rephase_and_caches)
        self.assertEqual(result, (True, "re-phased 64 off-phase point(s) (idx [" + ", ".join(map(str, range(64, 128)))
                                        + "]) onto the common 15 MHz grid"))
        self.assertEqual((layout[0], step), (128, 15000))
        self.assertEqual(after, [5000] * 64 + [-10000] * 64)
        (write,) = writes(calls)
        self.assertEqual(write[3], tuple([5000] * 64 + [-10000] * 64))

    def test_the_grid_in_messages_is_python_4g_formatting(self):
        """The grid step in the message is '%.4g' of MHz on both sides: an exact binary tie
        (10.125) rounds half to even, a trailing zero is stripped, a whole number has no point.

        This pins glibc's snprintf against Python's formatter; the msvcrt tie behaviour on
        Windows is not exercised here and stays the README's known risk."""
        def grid(count, span_mhz):  # `count` clocks topped at 2100 MHz, evenly spread over span_mhz
            return [round(2100 - span_mhz * k / (count - 1)) for k in range(count)]
        cases = [
            (grid(9, 81), 10125, "10.12"),     # 81 MHz over 8 gaps: 10.125, a binary tie
            # 358 MHz over 29 gaps rounds to 12.345, whose nearest double is 12.34500000000000064,
            # just ABOVE the tie: "12.35", not a half-even "12.34".
            (grid(30, 358), 12345, "12.35"),
            (grid(9, 100), 12500, "12.5"),
            (grid(9, 160), 20000, "20"),
        ]
        for clocks, step_khz, text in cases:
            with self.subTest(text=text):
                rig = ClockRig(grid=clocks)
                (result, layout, step), calls, _ = self.both(rig, rephase_and_caches)
                self.assertEqual(step, step_khz)
                self.assertEqual(result, (True, f"all 128 deltas already share one {text} MHz phase"))
                self.assertEqual(writes(calls), [])
                with_deltas(rig, {9: 7})  # one point 7 kHz off phase is rounded down onto it
                result, calls, (_, after, _, _) = self.both(rig, lambda g: g.rephase_deltas())
                self.assertEqual(result, (True, f"re-phased 1 off-phase point(s) (idx [9]) onto the common "
                                                f"{text} MHz grid"))
                self.assertEqual(after[9], 0)

    def test_null_arguments_are_refused_before_any_driver_call_or_output_write(self):
        lib = native.load()
        self.assertEqual(lib.dc_gpu_rephase_deltas(None, None), native.DC_E_ARGUMENT)
        rig = with_deltas(ClockRig(log_grid=True), {7: 20000})
        rig.reset()
        gpu = rig.native_gpu()
        try:
            gpu.capabilities()
            rig.calls.clear()
            # A live handle without an outcome: refused, nothing read and nothing written.
            self.assertEqual(lib.dc_gpu_rephase_deltas(gpu._handle, None), native.DC_E_ARGUMENT)
            self.assertEqual(rig.calls, [])
            self.assertEqual(rig.deltas[7], 20000)
        finally:
            gpu.close()
        # An outcome without a handle: refused, and the pre-filled outcome is left byte for byte.
        outcome = native._Outcome()
        outcome.ok, outcome.message = 7, b"sentinel " * 400
        sentinel = bytes(outcome)
        self.assertEqual(lib.dc_gpu_rephase_deltas(None, ctypes.byref(outcome)), native.DC_E_ARGUMENT)
        self.assertEqual(bytes(outcome), sentinel)


if __name__ == "__main__":
    unittest.main()
