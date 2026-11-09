# Druta - parity tests for the native pure rail-limit and hold-headroom helpers.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every helper of drutacore/rail_limits.h against its nvbackend.GPU original.

Two sources of inputs: the exact calls the existing rail tests make (recorded by spying on the
Python helpers while test_hold_headroom's planner, apply/restore and rail-availability cases,
test_volt_rails' rail-profile cases and test_rail_limit_headroom_intent run), and hand-written
edge values: missing and None fields, NaN, infinities, negative, zero and huge values, strings,
each architecture in _VOLT_RAIL_ARCHITECTURES and ones outside it. Results must be identical in
type and bits (NaN to NaN, -0.0 to -0.0); exceptions identical in type and, except for
OverflowError, in message. The documented refusals (arithmetic on ints beyond 2**53) are checked
to raise OverflowError where the Python computes."""

import copy
import math
import threading
import unittest
from contextlib import ExitStack
from fractions import Fraction
from unittest.mock import patch

from druta import native
from druta.nvbackend import GPU

if not native.available():
    raise unittest.SkipTest("native core not built")

from druta import native_rail_limits as nr  # noqa: E402

from tests import test_hold_headroom as th  # noqa: E402
from tests import test_rail_limit_headroom_intent as intent  # noqa: E402
from tests import test_volt_rails as tv  # noqa: E402

NAN, INF = float("nan"), float("inf")
BIG = 2 ** 60                 # an int the core carries but will not add
HUGE = 10 ** 400              # an int beyond the double range
BASES = (1068.75, 1093.75, 1125.0, 643.75)


def outcome(function, *args):
    try:
        return ("value", function(*args))
    except Exception as exc:                                     # noqa: BLE001
        return ("raise", type(exc), str(exc))


def bare(arch=None, active=None):
    gpu = GPU.__new__(GPU)
    gpu._lock = threading.RLock()
    gpu.arch = lambda: arch
    if active is not None:
        gpu._hold_headroom = active
    return gpu


# ----------------------------------------------------------------------- recording the suite's calls
SPIED_CLASSMETHODS = ("_valid_volt_rail_state", "volt_boost_headroom_mv", "abs_limit_mv", "rail_ceiling_mv",
                      "rail_floor_mv", "hold_headroom_ceiling_mv", "hold_headroom_targets",
                      "_headroom_enforced_mv")
SPIED_METHODS = ("hold_headroom_bound_mv", "hold_headroom_allowance_mv", "hold_headroom_architecture")


def record_calls(run):
    """Run `run()` with the Python helpers spied; returns [(name, args)] in call order, where an
    instance method's args carry the instance attribute the native function takes explicitly."""
    calls = []

    def class_spy(name, original):
        def spy(cls, *args):
            calls.append((name, copy.deepcopy(args)))
            return original(cls, *args)
        return classmethod(spy)

    def method_spy(name, original):
        def spy(self, *args):
            if name == "hold_headroom_architecture":
                calls.append((name, (self.arch(),)))
            else:
                record = args[0] if args else None
                calls.append((name, copy.deepcopy((record, self.hold_headroom_record()))))
            return original(self, *args)
        return spy

    with ExitStack() as stack:
        for name in SPIED_CLASSMETHODS:
            stack.enter_context(patch.object(GPU, name, class_spy(name, GPU.__dict__[name].__func__)))
        for name in SPIED_METHODS:
            stack.enter_context(patch.object(GPU, name, method_spy(name, GPU.__dict__[name])))
        run()
    return calls


def run_cases(*classes):
    def run():
        suite = unittest.TestSuite()
        for cls in classes:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
        result = unittest.TestResult()
        suite.run(result)
        assert result.wasSuccessful(), (result.failures, result.errors)
    return run


def run_intent():
    for raw in (False, True):
        intent.test_selected_hold_value_becomes_user_request_and_other_raise_releases(raw)
    intent.test_failed_verification_does_not_change_owned_record()
    intent.test_write_other_rail_does_not_change_owned_record()
    intent.test_selected_stock_equal_to_hold_survives_release()
    intent.test_failed_stock_verification_preserves_hold_record()


# The Python original and the native function for each recorded name.
def _python(name):
    if name == "hold_headroom_bound_mv":
        return lambda record, active: bare(active=active).hold_headroom_bound_mv(record)
    if name == "hold_headroom_allowance_mv":
        return lambda record, active: bare(active=active).hold_headroom_allowance_mv(record)
    if name == "hold_headroom_architecture":
        return lambda arch: bare(arch=arch).hold_headroom_architecture()
    return getattr(GPU, name)


def _native(name):
    return getattr(nr, name)


class ParityCase(unittest.TestCase):
    def assertSameValue(self, py, nat, where=""):
        if isinstance(py, (tuple, list)):
            self.assertIs(type(nat), type(py), where)
            self.assertEqual(len(nat), len(py), where)
            for a, b in zip(py, nat):
                self.assertSameValue(a, b, where)
        elif isinstance(py, dict):
            self.assertIs(type(nat), dict, where)
            self.assertEqual(list(nat), list(py), where)      # insertion order too
            for key in py:
                self.assertSameValue(py[key], nat[key], where)
        elif isinstance(py, float):
            self.assertIs(type(nat), float, f"{where}: {nat!r} for {py!r}")
            if math.isnan(py):
                self.assertTrue(math.isnan(nat), f"{where}: {nat!r} for NaN")
            else:
                self.assertEqual(nat, py, where)
                self.assertEqual(math.copysign(1.0, nat), math.copysign(1.0, py), f"{where}: sign of zero")
        else:
            self.assertIs(type(nat), type(py), f"{where}: {nat!r} for {py!r}")
            self.assertEqual(nat, py, where)

    def assertParity(self, name, *args):
        py = outcome(_python(name), *copy.deepcopy(args))
        nat = outcome(_native(name), *copy.deepcopy(args))
        where = f"{name}{args!r}"
        self.assertEqual(nat[0], py[0], f"{where}: python {py!r}, native {nat!r}")
        if py[0] == "raise":
            self.assertIs(nat[1], py[1], f"{where}: python {py!r}, native {nat!r}")
            if py[1] is not OverflowError:
                self.assertEqual(nat[2], py[2], where)
        else:
            self.assertSameValue(py[1], nat[1], where)

    def assertRefused(self, name, *args):
        """A documented refusal: the Python computes, the core will not add an int beyond 2**53."""
        self.assertEqual(outcome(_python(name), *copy.deepcopy(args))[0], "value")
        with self.assertRaises(OverflowError):
            _native(name)(*copy.deepcopy(args))


class RecordedSuiteParity(ParityCase):
    def replay(self, calls, expect):
        names = {name for name, _ in calls}
        self.assertTrue(set(expect) <= names, f"expected calls to {set(expect) - names}")
        for name, args in calls:
            with self.subTest(name=name, args=args):
                self.assertParity(name, *args)

    def test_hold_headroom_planner_and_apply_cases(self):
        calls = record_calls(run_cases(th.PlannerTests, th.ApplyRestoreTests))
        self.replay(calls, ("hold_headroom_targets", "hold_headroom_ceiling_mv", "abs_limit_mv",
                            "rail_ceiling_mv", "_headroom_enforced_mv", "hold_headroom_bound_mv",
                            "hold_headroom_allowance_mv", "hold_headroom_architecture"))

    def test_rail_availability_cases(self):
        self.replay(record_calls(run_cases(th.RailAvailabilityTests)), ("_valid_volt_rail_state",))

    def test_volt_rails_cases(self):
        self.replay(record_calls(run_cases(tv.RailProfileTests)),
                    ("_valid_volt_rail_state", "abs_limit_mv", "rail_ceiling_mv", "rail_floor_mv"))

    def test_rows_read_through_the_volt_rail_fixtures(self):
        # Every row the three fixture cards and an unknown generation report, as the helpers see it.
        rows = []
        for kind in ("turing", "pascal", "blackwell"):
            rows.extend(tv.fake_gpu(kind).read_volt_rail_limits().values())
        rows.extend(tv.fake_gpu(architecture=9).read_volt_rail_limits().values())
        rows.extend(tv.fake_gpu("turing", architecture=GPU.ARCH_AMPERE).read_volt_rail_limits().values())
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row=row):
                for key in GPU.VOLT_LIMIT_FIELDS:
                    self.assertParity("abs_limit_mv", row, key)
                for name in ("rail_ceiling_mv", "rail_floor_mv", "hold_headroom_ceiling_mv",
                             "_headroom_enforced_mv", "volt_boost_headroom_mv"):
                    self.assertParity(name, row)
                self.assertParity("hold_headroom_targets", row, 1093.75, 25.0, 1200.0)

    def test_headroom_intent_cases(self):
        self.replay(record_calls(run_intent), ("hold_headroom_targets", "abs_limit_mv"))


# ----------------------------------------------------------------------- hand-written edge values
def edge_rows():
    """Rows covering each way a dict value can be missing, None, odd or extreme."""
    stock = th.row(1068.75, 1093.75, 1125.0, boost=25.0)
    rows = [stock, th.row(1068.75, 1093.75, 1125.0), th.row(1175.0, 1175.0, 1175.0),
            th.row(1068.75, 1093.75, 1125.0, effective=1087.5), {}]
    for card in th.CARDS:
        rows.append(th.stock_row(card))
    for key in (*GPU.VOLT_LIMIT_FIELDS, "_base_mv", "_boost_mv", "_effective_mv"):
        r = copy.deepcopy(stock)
        del r[key]
        rows.append(r)
    for value in (None, NAN, INF, -INF, -50.0, 0.0, -0.0, 0, 7, True, 1e308, -1e308, "12"):
        for key in (*GPU.VOLT_LIMIT_FIELDS, "_boost_mv", "_effective_mv"):
            r = copy.deepcopy(stock)
            r[key] = value
            rows.append(r)
        for key in GPU.VOLT_LIMIT_FIELDS:
            r = copy.deepcopy(stock)
            r["_base_mv"][key] = value
            rows.append(r)
    for bases in (None, {}, [], {"reliability": 1000.0}):
        r = copy.deepcopy(stock)
        r["_base_mv"] = bases
        rows.append(r)
    # NaN in each ceiling term: min() keeps a leading NaN and skips a later one
    for key in ("reliability", "alt_reliability", "overvoltage"):
        r = copy.deepcopy(stock)
        r["_base_mv"][key] = NAN
        rows.append(r)
    # ints all the way: int + int stays an int, and int ties keep the first
    rows.append({"reliability": 10, "alt_reliability": 20, "overvoltage": 10, "vmin": 1,
                 "_base_mv": {"reliability": 1000, "alt_reliability": 990, "overvoltage": 1000, "vmin": 600},
                 "_boost_mv": 0})
    rows.append({"reliability": 1, "alt_reliability": 1.0, "overvoltage": True, "vmin": 0,
                 "_base_mv": {"reliability": 1000, "alt_reliability": 1000, "overvoltage": 1000, "vmin": 0}})
    # the card's own effective limit in every shape
    for effective in (None, NAN, INF, 0.0, -5.0, 1087.5, 1100, 1200.0, "1087.5", "x", True, HUGE, BIG):
        rows.append(th.row(1068.75, 1093.75, 1125.0, boost=25.0, effective=effective))
    # the boost contribution's shapes, for the planner's `or 0.0`
    for boost in (None, 0, 0.0, -0.0, "", "6.25", "x", NAN, INF, HUGE, 6, BIG, [], [1]):
        r = copy.deepcopy(stock)
        r["_boost_mv"] = boost
        rows.append(r)
    # an int row with a big int beside it (_headroom_mv, untouched by the arithmetic)
    rows.append({"reliability": 10, "alt_reliability": 20, "overvoltage": 10, "vmin": 1, "_headroom_mv": BIG,
                 "_base_mv": {"reliability": 1000, "alt_reliability": 990, "overvoltage": 1000, "vmin": 600}})
    # headroom
    for headroom in (None, 25, 25.5, "12.5", NAN, True, BIG):
        r = copy.deepcopy(stock)
        r["_headroom_mv"] = headroom
        rows.append(r)
    return rows


class EdgeParity(ParityCase):
    def each_row(self, check):
        for row in edge_rows():
            with self.subTest(row=row):
                check(row)

    def test_abs_limit_and_floor(self):
        def check(row):
            for key in GPU.VOLT_LIMIT_FIELDS:
                self.assertParity("abs_limit_mv", row, key)
            self.assertParity("rail_floor_mv", row)
        self.each_row(check)

    def test_ceiling_estimate_and_the_cards_own(self):
        def check(row):
            self.assertParity("rail_ceiling_mv", row)
            self.assertParity("hold_headroom_ceiling_mv", row)
        self.each_row(check)

    def test_enforced_ceiling_with_each_grain(self):
        def check(row):
            for grain in (None, 0, 0.0, 2.5, 5, 6.25, -1.0, NAN, INF, True, "", "x"):
                self.assertParity("_headroom_enforced_mv", row, grain)
        self.each_row(check)

    def test_boost_headroom(self):
        self.assertParity("volt_boost_headroom_mv")
        self.assertParity("volt_boost_headroom_mv", None)
        self.assertParity("volt_boost_headroom_mv", {})
        for value in (None, 0, 25, 25.5, -0.0, "12.5", "x", NAN, INF, True, BIG, HUGE):
            with self.subTest(value=value):
                self.assertParity("volt_boost_headroom_mv", {"_headroom_mv": value})
        self.each_row(lambda row: self.assertParity("volt_boost_headroom_mv", row))

    def test_planner_on_every_row(self):
        def check(row):
            for hold, margin, maximum in ((1093.75, 25.0, 1200.0), (1150.0, 25.0, 1200.0),
                                          (1200.0, 25.0, 1200.0), (1500.0, 100.0, 1500.0)):
                self.assertParity("hold_headroom_targets", row, hold, margin, maximum)
        self.each_row(check)

    def test_planner_arguments(self):
        row = th.row(1068.75, 1093.75, 1125.0, boost=25.0)
        values = (1100.0, 1100, 0.0, -0.0, 25.0, 25, 100.0, 100.00000001, 100, 101, -25.0, 1200.0000005,
                  1200.000002, 1e308, -1e308, NAN, INF, -INF, None, "1100", "x", True, False, BIG, HUGE,
                  Fraction(1, 2))
        for hold in values:
            for margin in (25.0, 100.0, 100.01, 0, -1.0, NAN, "5", None, True, HUGE):
                for maximum in (1200.0, 1500, 0.0, -5.0, NAN, "1200", None, 1e308, BIG):
                    with self.subTest(hold=hold, margin=margin, maximum=maximum):
                        self.assertParity("hold_headroom_targets", row, hold, margin, maximum)

    def test_planner_messages_for_extreme_values(self):
        row = th.row(1068.75, 1093.75, 1125.0)
        for hold, maximum in ((1.7976931348623157e308, -1.7976931348623157e308), (1206.25, 1200.0),
                              (0.005, -0.001), (1206.125, 1200.5), (2.675, 1.5), (-0.001, -5.0)):
            with self.subTest(hold=hold, maximum=maximum):
                self.assertParity("hold_headroom_targets", row, hold, 25.0, maximum)

    def test_valid_volt_rail_state(self):
        good = dict(zip(GPU.VOLT_LIMIT_FIELDS, (1068.75, 1093.75, 1125.0, 643.75)))
        states = [{}]
        for rail_type in (1, 3, 1.0, True, None, "1", NAN, 2, BIG):
            states.append(dict(good, type=rail_type))
        states.append(dict(good))
        for value in (None, 0, 0.0, -1.0, NAN, INF, 4294967.295, 4294967.296, 4294967, 4294968, True, False,
                      "1", 1, 1e-300, BIG, -BIG, HUGE, -HUGE):
            for key in GPU.VOLT_LIMIT_FIELDS:
                state = dict(good, type=1)
                state[key] = value
                states.append(state)
                partial = dict(good, type=1)
                del partial[key]
                states.append(partial)
        for rail in (0, 1, 2, -1, 0.0, -0.0, 1.0, True, False, None, "0", NAN, BIG, HUGE):
            for state in states:
                with self.subTest(rail=rail, state=state):
                    self.assertParity("_valid_volt_rail_state", rail, state)

    def test_architecture_membership(self):
        for arch in (*GPU._VOLT_RAIL_ARCHITECTURES, None, 0, 1, 2, 3, 5, 9, 11, 99, -1, 4.0, 2 ** 70):
            with self.subTest(arch=arch):
                self.assertParity("hold_headroom_architecture", arch)

    def test_bound_and_allowance(self):
        base = {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0, "raised_ceiling_mv": 1118.75,
                "grain_mv": 5.0, "live_offset_mv": 1.25}
        records = [None, {}, base]
        for key in ("raised_ceiling_mv", "hold_mv", "margin_mv", "grain_mv", "live_offset_mv"):
            r = dict(base)
            del r[key]
            records.append(r)
            for value in (None, 0, 0.0, -0.0, -3.0, 2.5, 7, "4", "x", "", NAN, INF, -INF, True, BIG, HUGE,
                          Fraction(1, 3)):
                r = dict(base)
                r[key] = value
                records.append(r)
                # with no ceiling read back, the hold + margin fallback is the one in play
                r = dict(base, raised_ceiling_mv=NAN)
                r[key] = value
                records.append(r)
        records.append({"rail": 0, "hold_mv": 1e308, "margin_mv": 1e308, "raised_ceiling_mv": None})
        actives = (None, {}, dict(base, raised_ceiling_mv=1200.0, grain_mv=2.0))
        for record in records:
            for active in actives:
                with self.subTest(record=record, active=active):
                    self.assertParity("hold_headroom_bound_mv", record, active)
                    self.assertParity("hold_headroom_allowance_mv", record, active)


class Refusals(ParityCase):
    """Where the core refuses rather than add an int it cannot hold exactly."""

    def test_int_plus_big_int_is_refused(self):
        ints = {"reliability": 10, "alt_reliability": 20, "overvoltage": 30, "vmin": 1,
                "_base_mv": {"reliability": 1000, "alt_reliability": 1000, "overvoltage": 1000, "vmin": 600},
                "_boost_mv": 0}
        r = copy.deepcopy(ints)
        r["reliability"] = BIG
        self.assertRefused("abs_limit_mv", r, "reliability")
        self.assertRefused("rail_ceiling_mv", r)
        r = copy.deepcopy(ints)
        r["_boost_mv"] = BIG
        self.assertRefused("rail_ceiling_mv", r)
        r = copy.deepcopy(ints)
        r["reliability"] = r["_base_mv"]["reliability"] = 2 ** 52
        self.assertRefused("abs_limit_mv", r, "reliability")         # the sum reaches 2**53
        self.assertRefused("_headroom_enforced_mv", ints, BIG)       # int estimate + int grain

    def test_float_plus_big_int_is_pythons_conversion(self):
        stock = th.row(1068.75, 1093.75, 1125.0, boost=25.0)
        for value in (BIG, -BIG, 2 ** 53 + 1, HUGE):
            r = copy.deepcopy(stock)
            r["reliability"] = value
            self.assertParity("abs_limit_mv", r, "reliability")
            self.assertParity("rail_ceiling_mv", r)
            r = copy.deepcopy(stock)
            r["_boost_mv"] = value
            self.assertParity("rail_ceiling_mv", r)
            self.assertParity("_headroom_enforced_mv", stock, value)

    def test_an_unrelated_big_int_does_not_refuse(self):
        stock = th.row(1068.75, 1093.75, 1125.0, boost=25.0)
        stock["_headroom_mv"] = BIG
        self.assertParity("rail_ceiling_mv", stock)
        self.assertParity("hold_headroom_targets", stock, 1093.75, 25.0, 1200.0)

    def test_a_non_number_the_python_can_add_is_refused(self):
        r = th.row(1068.75, 1093.75, 1125.0)
        r["reliability"] = Fraction(1, 2)                            # Fraction + float is a float
        self.assertEqual(outcome(GPU.abs_limit_mv, r, "reliability")[0], "value")
        with self.assertRaises(TypeError):
            nr.abs_limit_mv(r, "reliability")


class Constants(unittest.TestCase):
    def test_constants_match_the_python(self):
        self.assertEqual(nr.volt_limit_fields(), GPU.VOLT_LIMIT_FIELDS)
        self.assertEqual(nr.hold_headroom_fields(), GPU.HOLD_HEADROOM_FIELDS)
        c = nr.constants()
        self.assertEqual(c["volt_rail_architectures"], GPU._VOLT_RAIL_ARCHITECTURES)
        self.assertEqual(c["max_margin_mv"], GPU.HOLD_HEADROOM_MAX_MARGIN_MV)
        self.assertEqual(c["fallback_grain_mv"], GPU.HOLD_HEADROOM_FALLBACK_GRAIN_MV)
        # hold_headroom_grain_mv's sanity bounds: [half, twice] the fallback estimate
        self.assertEqual(c["grain_min_mv"], GPU.HOLD_HEADROOM_FALLBACK_GRAIN_MV / 2)
        self.assertEqual(c["grain_max_mv"], 2 * GPU.HOLD_HEADROOM_FALLBACK_GRAIN_MV)


if __name__ == "__main__":
    unittest.main()
