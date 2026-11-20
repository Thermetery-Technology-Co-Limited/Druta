# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native V/F planners must behave exactly like nvbackend.GPU's.

Every case runs the Python static method and the native binding on the same
inputs and compares the outcomes exactly: values and their types (int versus
float, the sign of a zero), dict key order, and for a raise the exception type
and message. The curves are built here; they are shaped like a Turing and a
Pascal table (rising voltage and frequency with flats near the top) and are not
measurements of any particular card.
"""
import itertools
import math
import random
import unittest

import pytest

from druta import native_vfplan
from druta.nvbackend import GPU

if not native_vfplan.available():
    pytest.skip("native core with the V/F planners not built (native/build.sh linux)", allow_module_level=True)

NAN = float("nan")
INF = float("inf")


# ---- curves ------------------------------------------------------------------------------------

def curve(volts, freqs, deltas=None, idxs=None):
    return [dict(idx=idxs[i] if idxs else i, volt_mv=volts[i], freq_mhz=freqs[i],
                 delta_khz=deltas[i] if deltas else 0) for i in range(len(volts))]


def turing_curve():
    """128 points on a 15 MHz grid: an idle floor, a rise with a flat every fourth point, a
    stepped top."""
    volts = [450.0 + 6.25 * i for i in range(128)]
    freqs = []
    for i in range(128):
        if i < 24:
            f = 435.0
        elif i < 112:
            f = min(750.0 + 15 * ((i - 24) * 3 // 4), 1965.0)
        else:
            f = 1980.0 if i < 118 else 1995.0 if i < 122 else 2010.0
        freqs.append(f)
    return curve(volts, freqs)


def pascal_curve():
    """80 points on a 12.657 MHz grid whose readings are rounded to 0.5 MHz, a 1911 plateau on
    top, and non-zero deltas."""
    volts = [700.0 + 6.25 * i for i in range(80)]
    freqs = []
    for i in range(80):
        k = min(i - i // 5, 61)
        freqs.append(min(round((1139.0 + 12.657 * k) * 2) / 2, 1911.0))
    deltas = [25314 + (i % 3) * 12657 for i in range(80)]
    return curve(volts, freqs, deltas)


def shuffled(points, seed=7):
    out = [dict(p) for p in points]
    random.Random(seed).shuffle(out)
    return out


def with_freq(points, position, value):
    out = [dict(p) for p in points]
    out[position]["freq_mhz"] = value
    return out


def with_volt(points, position, value):
    out = [dict(p) for p in points]
    out[position]["volt_mv"] = value
    return out


TURING = turing_curve()
PASCAL = pascal_curve()
CURVES = {
    "turing": TURING,
    "pascal": PASCAL,
    "empty": [],
    "single": curve([900.0], [1800.0], [15000]),
    "flat": curve([800.0 + 12.5 * i for i in range(10)], [1500.0] * 10),
    "ties": curve([800.0, 800.0, 812.5, 812.5, 825.0, 825.0], [1500.0, 1515.0, 1515.0, 1515.0, 1530.0, 1530.0],
                  [0, -15000, 30000, 0, 0, 7]),
    "shuffled_turing": shuffled(TURING),
    "negative_deltas": curve([800.0 + 12.5 * i for i in range(12)], [1500.0 + 15 * (i // 2) for i in range(12)],
                             [-90000 + 15000 * i for i in range(12)]),
    "ints": curve([800 + 12 * i for i in range(10)], [1500 + 15 * (i // 3) for i in range(10)],
                  [0] * 10, [40 + i for i in range(10)]),
    # the staging test's at-hardware-max curve: de-flatten has to lower the shadow instead
    "hard_max": curve([800 + 100 * i for i in range(4)], [1850, 1911, 1911, 1911],
                      [23000 + i * 21000 for i in range(4)]),
    "falling": curve([800.0 + 12.5 * i for i in range(8)], [1800.0 - 45 * i for i in range(8)]),
    "steep": curve([800.0 + 12.5 * i for i in range(8)], [1200.0, 1210.0, 1400.0, 1405.0, 1405.0, 1700.0,
                                                          1700.0, 1700.0]),
    # freq * 1000 lands exactly on .5: round() goes to the even neighbour
    "round_ties": curve([800.0, 812.5, 825.0, 837.5], [1234.5625, 1234.6875, 1234.6875, 1300.0]),
    "nan_freq_late": with_freq(TURING, 60, NAN),
    "nan_freq_first": with_freq(TURING, 0, NAN),
    "inf_freq": with_freq(TURING, 30, INF),
    "neg_inf_freq": with_freq(TURING, 31, -INF),
    "inf_volt": with_volt(PASCAL, 79, INF),
    "neg_zero": curve([0.0, -0.0, 12.5], [-0.0, 0.0, 15.0]),
}
# curves holding a NaN voltage: only the planners that never sort voltages take them in parity
NAN_VOLT = with_volt(TURING, 50, NAN)


# ---- exact comparison --------------------------------------------------------------------------

def canon(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return (type(value).__name__, value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", repr(value))  # nan == nan, -0.0 != 0.0
    if isinstance(value, dict):
        return ("dict", [(canon(k), canon(v)) for k, v in value.items()])
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [canon(v) for v in value])
    raise AssertionError(f"unexpected {type(value)!r}")


def outcome(function, *args, **kwargs):
    try:
        return ("returned", canon(function(*args, **kwargs)))
    except Exception as exc:  # noqa: BLE001 - the exception IS the result being compared
        return ("raised", type(exc).__name__, str(exc))


class Parity(unittest.TestCase):
    def same(self, name, *args, **kwargs):
        expected = outcome(getattr(GPU, name), *args, **kwargs)
        actual = outcome(getattr(native_vfplan, name), *args, **kwargs)
        self.assertEqual(actual, expected)
        return expected


CAPS = [None, 100.0, 2000.0, 1093.75, 900.0, 893.75, 1050, 1062.5, NAN, INF]


class PeakAndResolve(Parity):
    def test_peak_info(self):
        for (name, points), cap in itertools.product(CURVES.items(), CAPS):
            with self.subTest(curve=name, cap=cap):
                self.same("peak_info", points, cap)
        with self.subTest("default cap"):
            self.same("peak_info", TURING)

    def test_peak_info_nan_first_raises_like_python(self):
        result = self.same("peak_info", CURVES["nan_freq_first"])
        self.assertEqual(result[:2], ("raised", "IndexError"))

    def test_peak_info_nan_voltage_outside_the_peak_run(self):
        # the peak run is what gets sorted, so a NaN elsewhere is ordinary data
        self.same("peak_info", NAN_VOLT, 1093.75)

    def test_resolve_vf_point(self):
        volts = [100.0, 2000.0, 900.0, 893.75, 900, 1093.75, 1243.75, 450.0, 449.99, 450.0 - 0.01, NAN, INF, None]
        for (name, points), volt in itertools.product(list(CURVES.items()) + [("nan_volt", NAN_VOLT)], volts):
            with self.subTest(curve=name, volt=volt):
                self.same("resolve_vf_point", points, volt)

    def test_resolve_returns_the_callers_own_point(self):
        native = native_vfplan.resolve_vf_point(TURING, 900.0)
        self.assertIs(native, GPU.resolve_vf_point(TURING, 900.0))

    def test_resolve_nan_frequency_raises_like_python(self):
        points = with_freq(TURING, 72, NAN)
        result = self.same("resolve_vf_point", points, points[72]["volt_mv"])
        self.assertEqual(result[:2], ("raised", "ValueError"))


class Deflatten(Parity):
    def test_compute_deflatten(self):
        caps = [1093.75, 100.0, 2000.0, 1050.0, None, NAN]
        ceilings = [None, 1911000, 1911000.0, 2010000, 1980000.5, 1500000, -INF, INF, NAN]
        extras = [None, 0, 1, 2, -1, 200, True]
        steps = [None, 15000, 12657, 0, 0.5, -15000]
        for (name, points), cap, ceiling, extra, step in itertools.product(
                CURVES.items(), caps, ceilings, extras, steps):
            with self.subTest(curve=name, cap=cap, max_khz=ceiling, extra=extra, step=step):
                self.same("compute_deflatten", points, cap, ceiling, extra, step)

    def test_float_extra_points(self):
        for name, extra in itertools.product(["turing", "pascal", "single", "ties"],
                                             [0.5, 2.0, 1e-9, -0.5, 0.0, 1000.0, INF, NAN]):
            with self.subTest(curve=name, extra=extra):
                self.same("compute_deflatten", CURVES[name], 1093.75, None, extra)

    def test_bad_steps_raise_like_python(self):
        for step in [NAN, INF, -INF]:
            with self.subTest(step=step):
                self.assertEqual(self.same("compute_deflatten", TURING, 1093.75, None, None, step)[0], "raised")

    def test_nan_voltage_is_data_here(self):
        for cap in [1093.75, 700.0]:
            with self.subTest(cap=cap):
                self.same("compute_deflatten", NAN_VOLT, cap)

    def test_a_float_ceiling_makes_float_deltas(self):
        changes, _, after, meta = native_vfplan.compute_deflatten(TURING, 2000.0, max_khz=1980000.5)
        self.assertTrue(meta["clamped"])
        self.assertTrue(changes and all(type(c[4]) is float for c in changes))
        self.assertEqual(after, 1980.0005)
        self.same("compute_deflatten", TURING, 2000.0, 1980000.5)

    def test_lowering_branch_at_the_hardware_max(self):
        result = self.same("compute_deflatten", CURVES["hard_max"], 1000, 1911000, None, 12657)
        meta = result[1][1][3]
        self.assertIn((("str", "lowered"), ("int", 1)), meta[1])


class CurveLaw(Parity):
    def test_evaluate_curve_law(self):
        lists = [[], [1500000], [1500000, 1400000, 1600000], [1000000, 1200000, 1210000],
                 [1500000.9, 1500000.1, -0.5], list(range(1800000, 1000000, -45000)), [5, True, False],
                 [0, 10**6, 0, 10**6], [NAN], [1, INF], [None], [1, -INF]]
        rises = [None, 0, 45000, 37971, 2.5, -15000, 0.0, NAN, INF, 10**15]
        for values, rise in itertools.product(lists, rises):
            with self.subTest(values=values, rise=rise):
                self.same("evaluate_curve_law", values, rise)

    def test_predict_curve(self):
        for name in ["turing", "pascal", "shuffled_turing", "ties", "single", "empty", "falling", "steep",
                     "inf_volt"]:
            points = CURVES[name]
            khz = [int(round(p["freq_mhz"] * 1000)) for p in points]
            plans = [{}, {0: 2500000}, {len(points) // 2: 1.5e6, len(points) - 1: 900000.7},
                     {999: 1, -1: 2}, {i: khz[i] + 150000 for i in range(0, len(points), 3)}]
            for new, rise in itertools.product(plans, [None, 37971, 0]):
                with self.subTest(curve=name, new=sorted(new.items()), rise=rise):
                    result = self.same("predict_curve", points, khz, new, rise)
                    if result[0] == "returned":
                        # pos is keyed in voltage order, like the Python's enumerate(order)
                        self.assertEqual(result[1][1][1][0], "dict")

    def test_predict_curve_errors(self):
        khz = [int(round(p["freq_mhz"] * 1000)) for p in TURING]
        for khz_values, new in [(khz[:10], {}), (khz, {3: NAN}), (khz, {3: None}), (khz, {3: INF})]:
            with self.subTest(len=len(khz_values), new=new):
                self.assertEqual(self.same("predict_curve", TURING, khz_values, new)[0], "raised")

    def test_cascade_meta(self):
        for name in ["turing", "pascal", "shuffled_turing", "steep", "falling", "ties", "neg_zero"]:
            points = CURVES[name]
            khz = [int(round(p["freq_mhz"] * 1000)) for p in points]
            for new in [{}, {len(points) - 1: khz[-1] + 600000}, {i: 0 for i in range(len(points))}]:
                real, pos = GPU.predict_curve(points, khz, new)
                for idxs in [[], list(range(len(points))), list(range(len(points)))[::-2], [0, 0, 0]]:
                    with self.subTest(curve=name, new=sorted(new.items()), idxs=idxs):
                        self.same("cascade_meta", points, khz, real, pos, idxs)

    def test_cascade_meta_mixed_numbers_and_lookups(self):
        points = curve([800.0, 790.0, 810.0, NAN], [1.0, 1.0, 1.0, 1.0])
        real = [5, 7.5, 7, 6]
        pos = {0: 0, 1: 1, 2: 2, 3: 3}
        for khz, idxs in [([5, 5, 5.0, 5], [0, 1, 2, 3]), ([5, 7.5, 7.0, 6], [0, 1, 2, 3]),
                          ([NAN, 5, 7, 6], [0, 3, 1]), ([5, 5, 5, 5], [3, 2])]:
            with self.subTest(khz=khz, idxs=idxs):
                self.same("cascade_meta", points, khz, real, pos, idxs)
        # a missing voltage raises only when the row is lifted, exactly as Python's min() reaches it
        holey = points[:1] + [{"idx": 1}] + points[2:]
        for khz in ([5, 7.5, 7, 6], [5, 0, 7, 6]):
            with self.subTest(holey=khz):
                self.same("cascade_meta", holey, khz, real, pos, [0, 1, 2])
        self.same("cascade_meta", points, [5, 7.5, 7, 6], real, pos, [9])  # pos lookup KeyError


class HardDeflatten(Parity):
    def test_compute_hard_deflatten(self):
        floors = [800.0, 100.0, 2000.0, 1093.75, 800, NAN, None]
        targets = [2010000, 1965000, 2017500, 2017500.9, 0, -5000, 10**9, 1e9, NAN, INF, None]
        steps = [None, 15000, 12657, 0, 0.5, -15000, 7500.9]
        for (name, points), floor, target, step in itertools.product(CURVES.items(), floors, targets, steps):
            with self.subTest(curve=name, floor=floor, target=target, step=step):
                self.same("compute_hard_deflatten", points, floor, target, step)

    def test_cascade_is_reported(self):
        result = native_vfplan.compute_hard_deflatten(TURING, 800.0, 2010000)
        self.assertGreater(result[3]["lifted_below"], 0)
        self.same("compute_hard_deflatten", TURING, 800.0, 2010000)


class Ramp(Parity):
    def test_compute_ramp(self):
        bounds = [(700.0, 1093.75), (100.0, 2000.0), (2000.0, 2100.0), (1000.0, 1062.5), (975, 1094),
                  (1093.75, 1093.75), (1100.0, 900.0), (None, 1000.0), (800.0, None), (NAN, 1000.0), (800.0, NAN)]
        ceilings = [None, 1911000, 1911000.0, 1911000.7, 1500000, -INF, INF, NAN, 2 * 10**9]
        steps = [None, 15000, 12657, 0, 0.5, -15000, 7500.5]
        for (name, points), (lo, cap), ceiling, step in itertools.product(CURVES.items(), bounds, ceilings, steps):
            with self.subTest(curve=name, lo=lo, cap=cap, max_khz=ceiling, step=step):
                self.same("compute_ramp", points, lo, cap, ceiling, step)

    def test_staging_scenarios(self):
        # the shapes tests/test_vf_staging.py drives the editor with
        cases = [
            (curve([1000 + i * 12.5 for i in range(6)], [1860, 1873, 1885.5, 1898, 1911, 1911]),
             1000, 1094, 1911000, 12657),
            (curve([975 + i * 12.5 for i in range(8)], [1847.5, 1860, 1860, 1873, 1885.5, 1898, 1911, 1911]),
             975, 1094, 1911000, 12657),
            (curve([900 + i * 12.5 for i in range(3)], [2000] * 3), 900, 1000, 1911000, 12657),
            (curve([987.5 + i * 12.5 for i in range(8)], [1911] * 8), 1000, 1062.5, None, 15000),
        ]
        for points, lo, cap, ceiling, step in cases:
            with self.subTest(points=[p["freq_mhz"] for p in points], step=step):
                self.same("compute_ramp", points, lo, cap, ceiling, step)


class Rephase(Parity):
    def test_compute_rephase(self):
        deltas = [{}, {0: 0, 1: 15000, 2: 30000}, {0: 0, 1: 22500, 2: 30000, 3: 7}, {5: 7, 6: 0, 7: 7, 8: 0},
                  {i: -12657 * i + (i % 4) * 1000 for i in range(20)}, {"a": 25314.9, "b": -0.5, "c": 12657},
                  {i: 3 * i for i in range(128)}, {1: True, 2: False}]
        steps = [15000, 25314, 12657, -15000, 12657.9, 1]
        for values, step in itertools.product(deltas, steps):
            with self.subTest(deltas=values, step=step):
                self.same("compute_rephase", values, step)

    def test_rephase_errors(self):
        for values, step in [({0: 1}, 0), ({0: 1}, 0.5), ({0: 1}, None), ({0: 1}, NAN), ({0: 1}, INF),
                             ({0: NAN}, 15000), ({0: 1, 1: None}, 15000), ({0: INF}, 0), ({}, None)]:
            with self.subTest(deltas=values, step=step):
                self.same("compute_rephase", values, step)


class NativeOnly(unittest.TestCase):
    """What the core refuses instead of approximating, and its ABI guards."""

    def test_more_than_128_points_is_refused(self):
        points = curve([400.0 + i for i in range(129)], [1000.0] * 129)
        with self.assertRaises(ValueError):
            native_vfplan.peak_info(points)
        with self.assertRaises(ValueError):
            native_vfplan.compute_rephase({i: 0 for i in range(129)}, 15000)

    def test_nan_voltage_that_python_would_sort_is_refused(self):
        with self.assertRaisesRegex(ValueError, "NaN voltage"):
            native_vfplan.compute_ramp(NAN_VOLT, 700.0, 1093.75)
        with self.assertRaisesRegex(ValueError, "NaN voltage"):
            native_vfplan.predict_curve(NAN_VOLT, [0] * len(NAN_VOLT), {})

    def test_unbounded_integers_are_refused(self):
        with self.assertRaises(OverflowError):
            native_vfplan.compute_ramp(TURING, 700.0, 1093.75, max_khz=2 ** 60)
        with self.assertRaises(OverflowError):
            native_vfplan.compute_ramp(TURING, 700.0, 1093.75, max_khz=-1e30)  # int(-1e30) is a big int
        self.assertEqual(native_vfplan.compute_ramp(TURING, 700.0, 1093.75, max_khz=1e30),
                         GPU.compute_ramp(TURING, 700.0, 1093.75, max_khz=1e30))  # never reached: no refusal

    def test_status_text_and_size_guard(self):
        library = native_vfplan.load()
        self.assertEqual(library.dc_vfp_status_text(native_vfplan.DC_E_VFP_UNORDERED),
                         b"a NaN voltage cannot be ordered")
        self.assertEqual(library.dc_vfp_status_text(-1), b"invalid argument")
        out = native_vfplan._Peak(size=1)
        array = (native_vfplan._VfPoint * 1)()
        self.assertEqual(library.dc_vfp_peak_info(array, 0, None, out), -1)
        self.assertEqual(library.dc_vfp_peak_info(array, 129, None, native_vfplan._Peak(
            size=native_vfplan.ctypes.sizeof(native_vfplan._Peak))), -1)


def test_every_planner_is_bound():
    for name in ["peak_info", "resolve_vf_point", "compute_deflatten", "evaluate_curve_law", "predict_curve",
                 "cascade_meta", "compute_hard_deflatten", "compute_ramp", "compute_rephase"]:
        assert callable(getattr(native_vfplan, name))
        assert callable(getattr(GPU, name))
    assert not math.isnan(native_vfplan.compute_ramp(TURING, 700.0, 1093.75)[2])
