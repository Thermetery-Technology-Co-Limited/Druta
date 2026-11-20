# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native V/F curve (layout probe, readers, delta writer) against its
Python twin, from one fake V/F table whose width, rows and deltas the test
chooses. The probing call sequence, every layout field including the notes
text, the points, the written rows and every refusal must agree."""
import ctypes
import unittest

from druta import native, nvbackend
from tests.test_native_vflock import ParityCase

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

CDECL = ctypes.CFUNCTYPE
NV = nvbackend
from tests.fake_driver import (  # noqa: E402, F401
    BOOST_GET,
    BOOST_SET,
    VFP_CURVE,
    CurveRig,
    mask_bits,
    pascal_rows,
    turing_rows,
)


def delta_snapshot(g):
    return (g._vf_delta_snapshot if hasattr(g, "_vf_delta_snapshot") else g.vf_delta_snapshot)()


def layout_tuple(lay):
    return None if lay is None else (lay.n_entries, list(lay.gpu_idx), list(lay.other_idx), lay.freq_div, lay.notes)


def layout_and_error(g, **kw):
    lay = g.vfp_layout(**kw)
    return layout_tuple(lay), getattr(g, "_vfp_layout_error", "")


class VfLayoutTests(ParityCase):
    def test_turing_table_all_gpu_points(self):
        rig = CurveRig()
        (layout, error), calls, _ = self.both(rig, layout_and_error)
        self.assertEqual(layout, (128, list(range(128)), [], 1,
                                  "128 entries; 128 GPU points; GPU frequency is direct; top raw 2205 vs gfx_max 2100"))
        self.assertEqual(error, "")
        self.assertEqual(calls, [("VfpCurve", 128), ("VfpCurve", 128)])  # 128 accepted outright, then the read

    def test_pascal_table_probes_its_width_and_splits_the_memory_rows(self):
        rig = CurveRig(arch=4, rows=pascal_rows())
        (layout, _), calls, _ = self.both(rig, layout_and_error)
        self.assertEqual(layout[:4], (84, list(range(80)), [80, 81, 82, 83], 2))
        self.assertEqual(layout[4], "84 entries; 80 GPU points; 4 trailing rows (4 match mem_clocks); "
                                    "GPU frequency is GPC2CLK (halved); top raw 2278 vs gfx_max 2100")
        widths = [c[1] for c in calls if c[0] == "VfpCurve"]
        self.assertEqual(widths[0], 128)
        self.assertIn(84, widths)
        self.assertEqual(widths[-1], 84)  # the read at the accepted width follows the search
        self.assertTrue(all(w <= 128 for w in widths))
        self.assertLessEqual(len(widths), 10)

    def test_unmapped_architecture_infers_the_scale_from_gfx_max(self):
        doubled = [(v, 2 * f) for v, f in turing_rows()]
        (layout, _), *_ = self.both(CurveRig(arch=7, rows=doubled), layout_and_error)
        self.assertEqual(layout[3], 2)
        (layout, _), *_ = self.both(CurveRig(arch=7), layout_and_error)
        self.assertEqual(layout[3], 1)

    def test_layout_refusals(self):
        placeholder = turing_rows()
        placeholder[40] = (450000, 278)
        (layout, error), *_ = self.both(CurveRig(rows=placeholder), layout_and_error)
        self.assertIsNone(layout)
        self.assertEqual(error, "driver returned an incomplete V/F curve (127 valid rows of 128); press Read curve to retry")
        (layout, error), *_ = self.both(CurveRig(rows=[(500000, 1000000), (400000, 2000000)]), layout_and_error)
        self.assertEqual((layout, error), (None, "driver returned fewer than two GPU V/F points; press Read curve to retry"))
        self.assertEqual(self.both(CurveRig(rows=[]), layout_and_error)[0], (None, ""))
        self.assertEqual(self.both(CurveRig(with_curve=False), layout_and_error)[0], (None, ""))
        self.assertEqual(self.both(CurveRig(arch=2), layout_and_error)[0], (None, ""))

    def test_layout_is_cached_until_forced(self):
        rig = CurveRig()

        def twice(g):
            first = layout_tuple(g.vfp_layout())
            second = layout_tuple(g.vfp_layout())
            forced = layout_tuple(g.vfp_layout(force=True))
            return first == second == forced
        same, calls, _ = self.both(rig, twice)
        self.assertTrue(same)
        self.assertEqual(len([c for c in calls if c[0] == "VfpCurve"]), 4)


class VfCurveReadTests(ParityCase):
    def test_points_and_other_rows(self):
        rig = CurveRig(arch=4, rows=pascal_rows())
        original = rig.reset

        def with_deltas():
            original()
            rig.deltas[10], rig.deltas[79] = 45000, -15000
        rig.reset = with_deltas
        (points, error), *_ = self.both(rig, lambda g: g.read_vf_curve())
        self.assertIsNone(error)
        self.assertEqual(len(points), 80)
        self.assertEqual(points[10], {"idx": 10, "volt_mv": 512.5, "freq_mhz": 265.57, "delta_khz": 45000})
        self.assertEqual(points[79]["delta_khz"], -15000)
        rows, *_ = self.both(rig, lambda g: g.read_vfp_other_rows())
        self.assertEqual(rows[0], {"idx": 80, "volt_mv": 550.0, "value": 405.0, "kind": "memory"})
        self.assertEqual([r["kind"] for r in rows], ["memory"] * 4)
        self.assertEqual(self.both(CurveRig(), lambda g: g.read_vfp_other_rows())[0], [])

    def test_read_errors(self):
        self.assertEqual(self.both(CurveRig(arch=2), lambda g: g.read_vf_curve())[0],
                         (None, "V/F curves are not applicable on this GPU"))
        self.assertEqual(self.both(CurveRig(with_get=False), lambda g: g.read_vf_curve())[0],
                         (None, "VF curve APIs unavailable"))
        self.assertEqual(self.both(CurveRig(with_curve=False), lambda g: g.read_vf_curve())[0],
                         (None, "VF curve APIs unavailable"))
        self.assertEqual(self.both(CurveRig(rows=[]), lambda g: g.read_vf_curve())[0],
                         (None, "could not determine this card's VF table layout"))
        self.assertEqual(self.both(CurveRig(get_status=-104), lambda g: g.read_vf_curve())[0],
                         (None, "delta-table read failed (status -104)"))
        placeholder = turing_rows()
        placeholder[3] = (450000, 278)
        self.assertEqual(self.both(CurveRig(rows=placeholder), lambda g: g.read_vf_curve())[0],
                         (None, "driver returned an incomplete V/F curve (127 valid rows of 128); press Read curve to retry"))

    def test_a_failed_read_drops_the_cached_layout(self):
        rig = CurveRig()

        def scenario(g):
            layout_tuple(g.vfp_layout())
            rig.curve_status = -3
            failed = g.read_vf_curve()
            rig.curve_status = 0
            layout_tuple(g.vfp_layout())  # re-probed, because the cache was cleared
            return failed
        failed, calls, _ = self.both(rig, scenario)
        self.assertEqual(failed, (None, "curve read failed (status -3)"))
        self.assertEqual([c[1] for c in calls if c[0] == "VfpCurve"], [128, 128, 128, 128, 128])

    def test_delta_snapshot(self):
        rig = CurveRig()
        original = rig.reset

        def with_deltas():
            original()
            rig.deltas[5] = 30000
        rig.reset = with_deltas
        snapshot, *_ = self.both(rig, delta_snapshot)
        self.assertEqual(snapshot["freq_div"], 1)
        self.assertEqual(snapshot["deltas"][5], 30000)
        self.assertEqual(len(snapshot["deltas"]), 128)
        self.assertIsNone(self.both(CurveRig(with_get=False), delta_snapshot)[0])
        self.assertIsNone(self.both(CurveRig(arch=2), delta_snapshot)[0])


class VfDeltaWriteTests(ParityCase):
    def test_only_changed_rows_are_written(self):
        rig = CurveRig(arch=4, rows=pascal_rows())
        original = rig.reset

        def with_deltas():
            original()
            rig.deltas[7] = 15000
        rig.reset = with_deltas
        result, calls, (_, deltas) = self.both(rig, lambda g: g.apply_vf_deltas({7: 15000, 8: 30000, 9: -15000}))
        self.assertEqual(result, (True, "VF delta table written (2 points changed)"))
        write = [c for c in calls if c[0] == "BoostTableSet"][0]
        self.assertEqual((write[1], write[2]), (NV.NvAPI.ver(NV._BoostTable, 1), 84))
        self.assertEqual(write[3][7:10], (15000, 30000, -15000))
        self.assertEqual(deltas[:10], [0] * 7 + [15000, 30000, -15000])
        result, calls, _ = self.both(rig, lambda g: g.apply_vf_deltas({7: 15000}))
        self.assertEqual(result, (True, "no delta changes to apply"))
        self.assertEqual([c for c in calls if c[0] == "BoostTableSet"], [])

    def test_refusals(self):
        rig = CurveRig(arch=4, rows=pascal_rows())
        cases = [
            ({80: 15000}, "point index 80 is not a GPU V/F point on this card (80 GPU points of 84 entries)"),
            ({5: 0, 200: 1}, "point index 200 is not a GPU V/F point on this card (80 GPU points of 84 entries)"),
            ({5: -1500500}, "refusing point 5: delta -1501 MHz More than a whole gigahertz of delta? Lol, bro thinks "
                            "he's Seby. Caught you with a mouse-slip and saved you a total driver crash this time. "
                            "Don't do it next time. :copege:"),
            ({5: 1000001}, "refusing point 5: delta 1000 MHz More than a whole gigahertz of delta? Lol, bro thinks "
                           "he's Seby. Caught you with a mouse-slip and saved you a total driver crash this time. "
                           "Don't do it next time. :copege:"),
        ]
        for deltas, message in cases:
            with self.subTest(message=message[:40]):
                result, calls, _ = self.both(rig, lambda g: g.apply_vf_deltas(deltas))
                self.assertEqual(result, (False, message))
                self.assertEqual([c for c in calls if c[0] == "BoostTableSet"], [])
        self.assertEqual(self.both(CurveRig(arch=2), lambda g: g.apply_vf_deltas({0: 1}))[0],
                         (False, "V/F curves are not applicable on this GPU"))
        self.assertEqual(self.both(CurveRig(with_set=False), lambda g: g.apply_vf_deltas({0: 1}))[0],
                         (False, "boost-table APIs unavailable"))
        self.assertEqual(self.both(CurveRig(with_curve=False), lambda g: g.apply_vf_deltas({0: 1}))[0],
                         (False, "could not determine this card's VF table layout"))
        self.assertEqual(self.both(CurveRig(get_status=-6), lambda g: g.apply_vf_deltas({0: 1}))[0],
                         (False, "pre-write table read failed (status -6)"))
        self.assertEqual(self.both(CurveRig(set_status=-104), lambda g: g.apply_vf_deltas({0: 1}))[0],
                         (False, "VF table write failed (status -104)"))

    def test_reset_zeroes_only_gpu_rows(self):
        rig = CurveRig(arch=4, rows=pascal_rows())
        original = rig.reset

        def with_deltas():
            original()
            rig.deltas[3], rig.deltas[79], rig.deltas[81] = 15000, 30000, 45000  # 81 is a memory row
        rig.reset = with_deltas
        result, calls, (_, deltas) = self.both(rig, lambda g: g.reset_vf_curve())
        self.assertEqual(result, (True, "VF delta table written (2 points changed)"))
        self.assertEqual((deltas[3], deltas[79], deltas[81]), (0, 0, 45000))
        self.assertEqual(self.both(CurveRig(with_curve=False), lambda g: g.reset_vf_curve())[0],
                         (False, "could not determine this card's VF table layout"))


if __name__ == "__main__":
    unittest.main()
