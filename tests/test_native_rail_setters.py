# Druta - parity tests for the native verified rail setters (increment R5).
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native rail setters against nvbackend.GPU.set_volt_rail_limits, set_volt_rail_limits_raw,
reset_volt_rail_limits and _adopt_verified_hold_fields.

The native.Gpu methods of those names (druta.native_rails' set_volt_rail_limits,
set_volt_rail_limits_raw and reset_volt_rail_limits) run dc_rails_set_limits, dc_rails_set_limits_raw
and dc_rails_reset_limits. The Python originals run unmodified on the identity twin of the R4 write
rig (tests/test_native_rail_write.WriteRig): one fake driver serves both rail getters, the boost and
the rail GET/SET escapes to both sides, and a SET is stored so the read-back sees it.

Every operation records its result, the ordered driver log (getter calls, escapes with their packet
bytes, boost reads), the four getter caches, the architecture reads, the profile (the reference and
the first-read record) afterwards, the writer's three attributes (seeded stale so "untouched" is
visible), the fake's rail state and the hold record (Python's _hold_headroom projected onto the
"rail", "written_uv" and "prior_uv" keys the setters read; natively druta.native_rails.hold_record).
All of it must be equal on the two sides. The session flags (volt_limits_write_enabled,
voltage_xoc_enabled) are set on each side's GPU object and read at the call. Fixture values are inputs
to the logic, not claims about any board; the first-read record is this session's first read, never
a factory default.

Recorded divergences (asserted separately, each side explicitly): a rail equal to a rail but not a
plain int, a non-number limit value, and texts above 1024 bytes are refused by the binding before
any read; a one-shot `fields` iterator is seen by every per-rail reset; an int beyond the double
range (OverflowError) and a non-string field (TypeError) raise before any read (BindingContractTests);
a NUL inside an unknown or bad name shortens the native message at the NUL (CallerTextTests)."""
import copy
import ctypes
import math
import unittest

from druta import native, nvbackend as NV
from tests.test_native_rail_write import WriteNativeSide, WritePythonSide, WriteParity, WriteRig, blackwell, kinds
from tests.test_native_rail_write import pascal, sets, turing
from tests.test_native_rail_reference import BASES, after_live, config, shape

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_rails as nr  # noqa: E402

FIELDS = NV.GPU.VOLT_LIMIT_FIELDS
DISABLED = "rail limit writes are disabled: nothing bounds this value but Druta, so it is off by default"
UNSUPPORTED = "rail limit runtime getters are unavailable or structurally inconsistent"
NO_CURRENT = "cannot read the current limits"
NO_BOOST = "cannot preserve the current voltage boost"
NOT_SEEN = "the rails request was not seen - nothing was written"
NO_INITIAL = "no readable initial rail state is available for restoration"
RESET_ABSENT = "requested rail is not present on this GPU"
NO_REFERENCE = "selected rail has no readable current-card reference"
RESTORED = " restored to first-read values (not factory defaults)"
NOTE = "; Pascal vmin was verified at idle; a V/F point lock or P2 can hold the live voltage below this floor"
TAIL = " mV; absolute requests are relative to this session's quantized first read"


def project(record):
    """The keys of _hold_headroom the setters read, as druta.native_rails.hold_record returns them."""
    if not record:
        return None
    return {"rail": record["rail"], "written_uv": dict(record.get("written_uv") or {}),
            "prior_uv": dict(record.get("prior_uv") or {})}


class SetterSide:
    """The setter operations, the session flags and the hold record on each side."""

    def _record(self, operation, call):
        result = super()._record(operation, call)
        self.trace[-1] = self.trace[-1] + (self._hold(),)
        return result

    def seed(self, *, write=True, xoc=False, hold=None):
        super().seed()
        self.gpu.volt_limits_write_enabled = write
        self.gpu.voltage_xoc_enabled = xoc
        self._set_hold(copy.deepcopy(hold))

    def flags(self, *, write, xoc):
        self.gpu.volt_limits_write_enabled = write
        self.gpu.voltage_xoc_enabled = xoc

    def hold(self, record):
        self._set_hold(copy.deepcopy(record))

    def _op(self, operation, call):
        self.fake.rig = self.rig
        return self._record(operation, lambda: self._armed(call))

    def set(self, rail, **limits):
        return self._op(("set", rail, repr(limits)), lambda: self.gpu.set_volt_rail_limits(rail, **limits))

    def raw(self, rail, **limits_uv):
        return self._op(("raw", rail, repr(limits_uv)), lambda: self.gpu.set_volt_rail_limits_raw(rail, **limits_uv))

    def reset(self, rail=None, fields=None):
        return self._op(("reset", rail, repr(fields)), lambda: self.gpu.reset_volt_rail_limits(rail, fields))


class SetterPythonSide(SetterSide, WritePythonSide):
    def _hold(self):
        return copy.deepcopy(project(getattr(self.gpu, "_hold_headroom", None)))

    def _set_hold(self, record):
        self.gpu._hold_headroom = record


class SetterNativeSide(SetterSide, WriteNativeSide):
    def _hold(self):
        return nr.hold_record(self.gpu)

    def _set_hold(self, record):
        nr.set_hold_record(self.gpu, record)


class SetterRig(WriteRig):
    python_side = SetterPythonSide
    native_side = SetterNativeSide


class SetterParity(WriteParity):
    def run_both(self, scenario, **options):
        """parity() on the setter rig; every step's trace (the hold record is its last entry)."""
        rig = SetterRig(**options)
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertEqual(len(python), len(core))
        for step, (p, c) in enumerate(zip(python, core)):
            with self.subTest(step=step, operation=p[0]):
                self.assertEqual(p[0], c[0])
                self.assertEqual(shape(p[1]), shape(c[1]), "result")
                self.assertEqual(p[2], c[2], "driver calls")
                self.assertEqual(p[3], c[3], "caches")
                self.assertEqual(p[4], c[4], "architecture reads")
                self.assertEqual(shape(p[5]), shape(c[5]), "profile afterwards")
                self.assertEqual(shape(p[6]), shape(c[6]), "attributes")
                self.assertEqual(p[7:], c[7:], "rail state and hold record")
        return python

    def results(self, scenario, **options):
        return [step[1] for step in self.run_both(scenario, **options)]


def turing_with(control):
    """turing() whose first read already holds these control deltas (the live record follows them)."""
    return dict(config("turing", control=control), bases=BASES["turing"])


CONTROL = 7  # the fake's control records in a step's trace


def stored(rail_name, wrote, floor, cap, note=""):
    return f"{rail_name}: {wrote} mV stored; floor {floor}, estimated cap at current boost {cap}{TAIL}{note}"


class RefusalOrderTests(SetterParity):
    def test_writes_disabled_comes_first_and_reads_nothing(self):
        def scenario(side, fake, clock):
            side.seed(write=False)
            side.set(0, bogus=1, vmin=5000)
            side.set(7, vmin=700)

        trace = self.run_both(scenario, **turing())
        for step in trace:
            self.assertEqual(step[1], (False, DISABLED))
            self.assertEqual(step[2], [])

    def test_unsupported_rail_or_architecture(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(1, vmin=700)      # turing has no rail 1
            side.set(5, bogus=1)       # a key naming no rail, before the unknown-keyword check
        self.assertEqual(self.results(scenario, **turing()), [(False, UNSUPPORTED)] * 2)
        trace = self.run_both(lambda side, fake, clock: (side.seed(), side.set(0, vmin=700)),
                              **turing(card={"arch": 99}))
        self.assertEqual(trace[0][1], (False, UNSUPPORTED))

    def test_unknown_keywords_are_sorted_and_joined_after_the_support_check(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, zeta=1, vmin="not converted", alpha=None)

        trace = self.run_both(scenario, **turing())
        self.assertEqual(trace[0][1], (False, "not a rail limit: alpha, zeta"))
        self.assertIn("control", kinds(trace[0]))

    def test_current_limits_unreadable(self):
        def scenario(side, fake, clock):
            side.seed()
            side.limits()
            fake.script["control"] = [None, -1, -1]  # the support check reads; the setter's own read fails
            side.set(0, vmin=700)

        self.assertEqual(self.results(scenario, **turing())[1], (False, NO_CURRENT))

    def test_rail_missing_from_the_current_read(self):
        def scenario(side, fake, clock):
            side.seed()
            side.limits()
            fake.script["control"] = [None, None, None, -1, -1]  # rail 1 drops out of the setter's read
            side.set(1, vmin=850)

        self.assertEqual(self.results(scenario, **blackwell())[1], (False, "rail 1 is not present on this GPU"))

    def test_no_boost(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=700)

        trace = self.run_both(scenario, vboost_status=-1, **turing())
        self.assertEqual(trace[0][1], (False, NO_BOOST))
        self.assertEqual(sets(trace[0]), [])


class BoundTests(SetterParity):
    def test_normal_and_xoc_maxima(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, reliability=1250)
            side.set(0, reliability=1200.5)  # :.0f rounds half to even: 1200
            side.set(0, reliability=649.5)   # 650 below, printed 650 (half to even)
            side.flags(write=True, xoc=True)
            side.set(0, reliability=1501.5)
            side.set(0, overvoltage=1450)

        results = self.results(scenario, **turing())
        self.assertEqual(results[0], (False, "reliability 1250 mV is outside Druta's 650-1200 mV bound"))
        self.assertEqual(results[1], (False, "reliability 1200 mV is outside Druta's 650-1200 mV bound"))
        self.assertEqual(results[2], (False, "reliability 650 mV is outside Druta's 650-1200 mV bound"))
        self.assertEqual(results[3], (False, "reliability 1502 mV is outside Druta's 650-1500 mV bound"))
        self.assertTrue(results[4][0])

    def test_first_failing_field_in_the_callers_order(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=600, reliability=1300)
            side.set(0, reliability=1300, vmin=600)
            side.set(0, vmin=None, reliability=1300)

        results = self.results(scenario, **turing())
        self.assertEqual(results[0], (False, "vmin 600 mV is outside Druta's 650-1200 mV bound"))
        self.assertEqual(results[1], (False, "reliability 1300 mV is outside Druta's 650-1200 mV bound"))
        self.assertEqual(results[2], (False, "reliability 1300 mV is outside Druta's 650-1200 mV bound"))

    def test_a_first_read_below_the_minimum_lowers_it(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=590)
            side.set(0, vmin=610)
            side.set(0, vmin=595)  # still bounded by the first read (600), not the current 610

        results = self.results(scenario, **turing_with({0: [0, 0, 0, -50000]}))
        self.assertEqual(results[0], (False, "vmin 590 mV is outside Druta's 600-1200 mV bound"))
        self.assertTrue(results[1][0])
        self.assertEqual(results[2], (False, "vmin 595 mV is outside Druta's 600-1200 mV bound"))

    def test_a_current_value_below_the_first_read_lowers_the_minimum(self):
        def scenario(side, fake, clock):
            side.seed()
            side.limits()
            fake.control[0][3] = -60000  # another application lowered vmin after the first read
            side.set(0, vmin=585)
            side.set(0, vmin=595)

        results = self.results(scenario, **turing())
        self.assertEqual(results[1], (False, "vmin 585 mV is outside Druta's 590-1200 mV bound"))
        self.assertTrue(results[2][0])

    def test_leaving_xoc_keeps_an_above_normal_value_but_cannot_raise_it(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, reliability=1300)
            side.set(0, reliability=1268.75)  # the current value itself
            side.set(0, reliability=1250)     # lowering is allowed

        results = self.results(scenario, **turing_with({0: [200000, 0, 0, 0]}))
        self.assertEqual(results[0], (False, "reliability 1300 mV is outside Druta's 650-1269 mV bound"))
        self.assertTrue(results[1][0])
        self.assertTrue(results[2][0])

    def test_the_delta_rounds_half_to_even(self):
        mv = 650.0625  # (mv - 650.0) * 1000 is exactly 62.5
        self.assertEqual((mv - 650.0) * 1000, 62.5)

        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=mv)
            side.set(0, vmin=mv + 0.001)  # 63.49999999997635, not exactly 63.5: 63

        trace = self.run_both(scenario, **turing())
        self.assertEqual(trace[0][CONTROL][0], [0, 0, 0, 62])
        self.assertEqual(trace[1][CONTROL][0][3], round((mv + 0.001 - 650.0) * 1000))

    def test_exact_halves_separate_half_to_even_from_floor_truncation_and_half_away(self):
        # (field, request mV, the turing base mV, half to even): each (mv - base) * 1000 is an exact
        # binary half. 187.5 -> 188 (floor and truncation 187); -62.5 -> -62 (floor and half away
        # -63); 62.5 -> 62 (half away 63); -187.5 -> -188 (truncation -187).
        cases = (("vmin", 650.1875, 650.0, 188), ("reliability", 1068.6875, 1068.75, -62),
                 ("overvoltage", 1125.0625, 1125.0, 62), ("alt_reliability", 1093.5625, 1093.75, -188))
        for key, mv, base, expected in cases:
            delta = (mv - base) * 1000
            self.assertEqual(delta % 1, 0.5, key)  # an exact half, not a near one
            self.assertEqual(round(delta), expected, key)
        index = {key: FIELDS.index(key) for key, *_ in cases}

        def scenario(side, fake, clock):
            side.seed()
            for key, mv, _, _ in cases:
                side.set(0, **{key: mv})

        trace = self.run_both(scenario, **turing())
        for step, (key, _, _, expected) in zip(trace, cases, strict=True):
            with self.subTest(key=key):
                self.assertTrue(step[1][0])
                self.assertEqual(step[CONTROL][0][index[key]], expected)

    def test_non_finite_requests_are_refused_with_pythons_spelling(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=math.nan)
            side.set(0, vmin=-math.nan)
            side.set(0, vmin=math.inf)
            side.set(0, vmin=-math.inf)

        results = self.results(scenario, **turing())
        self.assertEqual([r[1].split(" mV")[0] for r in results], ["vmin nan", "vmin nan", "vmin inf", "vmin -inf"])


class SuccessTests(SetterParity):
    def test_message_lists_every_named_key_sorted_with_floor_and_cap(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=700, overvoltage=None, reliability=1100, alt_reliability=1150)

        trace = self.run_both(scenario, **turing())
        self.assertEqual(trace[0][1], (True, stored("core rail (NVVDD)",
                                                    "alt_reliability 1150, overvoltage 1125, reliability 1100, vmin 700",
                                                    700, 1100, "")))
        self.assertEqual(trace[0][CONTROL][0], [31250, 56250, 0, 50000])

    def test_msvdd_and_the_boost_in_the_cap(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(1, vmin=850)
            side.set(1)  # no field: an identity write of the current record

        results = self.results(scenario, boost=25000, **blackwell())
        self.assertTrue(results[0][1].startswith("MSVDD: vmin 850 mV stored; floor 850, estimated cap"))
        self.assertTrue(results[1][1].startswith("MSVDD:  mV stored; floor"))

    def test_pascal_vmin_note_reads_the_architecture_only_when_vmin_is_named(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, reliability=1100)
            side.set(0, vmin=None)
            side.set(0, vmin=700)

        trace = self.run_both(scenario, scripts=[["rail"]] * 8, **pascal())
        self.assertFalse(trace[0][1][1].endswith(NOTE))
        self.assertTrue(trace[1][1][1].endswith(NOTE))
        self.assertTrue(trace[2][1][1].endswith(NOTE))
        self.assertNotIn("Pascal", self.results(lambda side, fake, clock: (side.seed(), side.set(0, vmin=700)),
                                                **turing())[0][1])


class WriteFailureTests(SetterParity):
    def test_the_writers_error_text_or_the_fallback(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=700)

        trace = self.run_both(scenario, scripts=[["unrelated"]], **turing())
        self.assertTrue(trace[0][1][1].startswith("The driver did not emit a recognized rail-control packet."))
        # the locked write's own boost read fails: no attribute set, the stale error was cleared
        trace = self.run_both(scenario, boost_fail_at=(2,), **turing())
        self.assertEqual(trace[0][1], (False, NOT_SEEN))
        self.assertIsNone(trace[0][6][0])
        self.assertEqual(trace[0][6][1:], ({"stale": True}, ["stale packet"]))

    def test_a_refused_status_in_upper_case_hex(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=700)
            side.reset()

        results = self.results(scenario, set_rm=0xABC, **turing())
        self.assertEqual(results, [(False, "driver refused the write (NV_STATUS 0xABC)"),
                                   (False, "reset refused (NV_STATUS 0xABC)")])

    def test_verification_errors(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=700)

        trace = self.run_both(scenario, store=lambda rail, values: [v + 1000 for v in values], **turing())
        self.assertEqual(trace[0][1], (False, "rail 0 read-back disagrees: wanted [0, 0, 0, 50000], "
                                              "got [1000, 1000, 1000, 51000]"))
        trace = self.run_both(scenario, set_boost=40, **turing())
        self.assertEqual(trace[0][1], (False, "rail write changed the voltage boost setting"))


HOLD = {"rail": 0, "written_uv": {"vmin": 50000, "reliability": 12500, "overvoltage": 0},
        "prior_uv": {"vmin": 0, "reliability": 0}}


class HoldAdoptionTests(SetterParity):
    def test_prior_follows_a_verified_field_equal_to_the_raise(self):
        def scenario(side, fake, clock):
            side.seed(hold=HOLD)
            side.set(0, vmin=700, reliability=1068.75, overvoltage=1125)  # vmin == written; others not

        trace = self.run_both(scenario, **turing())
        self.assertTrue(trace[0][1][0])
        self.assertEqual(trace[0][-1]["prior_uv"], {"vmin": 50000, "reliability": 0})

    def test_other_rail_none_values_and_failed_writes_adopt_nothing(self):
        def scenario(side, fake, clock):
            side.seed(hold=dict(HOLD, rail=1))
            side.set(0, vmin=700)
            side.hold(HOLD)
            side.set(0, vmin=None)
            side.set(0, vmin=700, reliability=1300)

        trace = self.run_both(scenario, **turing())
        for step in trace:
            self.assertEqual(step[-1]["prior_uv"], {"vmin": 0, "reliability": 0})


class RawTests(SetterParity):
    def test_type_refusal_reference_refusal_and_arithmetic(self):
        def scenario(side, fake, clock):
            side.seed()
            side.raw(0, vmin=1.0)
            side.raw(0, vmin=1 << 31)
            side.raw(0, bogus=1)
            side.raw(1, vmin=1000)          # turing has no rail 1: no reference
            side.raw(0, vmin=62, reliability=-1)
            side.raw(0, vmin=-60000)        # 590 mV: below the bound

        trace = self.run_both(scenario, **turing())
        for step in trace[:3]:
            self.assertEqual(step[1], (False, nr.RAW_REFUSAL))
            self.assertEqual(step[2], [])
        self.assertEqual(trace[3][1], (False, NO_REFERENCE))
        self.assertTrue(trace[4][1][0])
        self.assertEqual(trace[4][CONTROL][0], [-1, 0, 0, 62])
        self.assertEqual(trace[5][1], (False, "vmin 590 mV is outside Druta's 650-1200 mV bound"))

    def test_the_request_is_base_plus_microvolts_over_1000(self):
        # A reliability base of 1024003 uV is not exact in binary: 1024.003 + -374003 / 1000.0 is
        # exactly 650.0 (inside the bound), where (1024003 + -374003) / 1000.0 computed from the
        # base in mV, (1024.003 * 1000 + -374003) / 1000.0, is not.
        bases = (1024003, 1093750, 1125000, 650000)
        self.assertEqual(1024.003 + -374003 / 1000.0, 650.0)
        self.assertNotEqual((1024.003 * 1000 + -374003) / 1000.0, 650.0)

        def scenario(side, fake, clock):
            side.seed()
            side.raw(0, reliability=-374003)

        trace = self.run_both(scenario, **dict(config("turing", bases=bases), bases=bases))
        self.assertTrue(trace[0][1][0], trace[0][1])
        self.assertIn(": reliability 650 mV stored;", trace[0][1][1])
        self.assertEqual(trace[0][CONTROL][0], [-374003, 0, 0, 0])

    def test_a_present_rail_without_a_reference_is_refused(self):
        def scenario(side, fake, clock):
            side.seed()
            # the control changes between the two samples: rail 0 keeps its row, '_base_mv' stays {}
            fake.hook = after_live(lambda fake: fake.control[0].__setitem__(0, 1))
            side.raw(0, vmin=1)
            fake.hook = None

        trace = self.run_both(scenario, **turing())
        self.assertEqual(trace[0][1], (False, NO_REFERENCE))
        self.assertIn("control", kinds(trace[0]))
        self.assertEqual(sets(trace[0]), [])

    def test_the_type_gate_at_its_edges(self):
        def scenario(side, fake, clock):
            side.seed()
            side.raw(0, vmin=True)
            side.raw(0, vmin=-(1 << 31) - 1)
            side.raw(0, vmin=-(1 << 31))
            side.raw(0, vmin=(1 << 31) - 1)

        trace = self.run_both(scenario, **turing())
        for step in trace[:2]:
            self.assertEqual(step[1], (False, nr.RAW_REFUSAL))
            self.assertEqual(step[2], [])
        # the int32 edges pass the gate and reach the bound refusal (650 mV base + the microvolts)
        self.assertEqual(trace[2][1], (False, "vmin -2146834 mV is outside Druta's 650-1200 mV bound"))
        self.assertEqual(trace[3][1], (False, "vmin 2148134 mV is outside Druta's 650-1200 mV bound"))
        for step in trace[2:]:
            self.assertIn("control", kinds(step))
            self.assertEqual(sets(step), [])

    def test_raw_reads_before_the_write_flag(self):
        def scenario(side, fake, clock):
            side.seed(write=False)
            side.raw(0, vmin=1)

        trace = self.run_both(scenario, **turing())
        self.assertEqual(trace[0][1], (False, DISABLED))
        self.assertIn("control", kinds(trace[0]))


class ResetTests(SetterParity):
    def test_multi_rail_reset_in_first_read_order_with_a_joined_message(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=700)
            side.set(1, vmin=850)
            side.reset()
            side.set(1, vmin=850)
            side.reset(fields=["vmin"])

        trace = self.run_both(scenario, **blackwell())
        self.assertEqual(trace[2][1], (True, f"core rail (NVVDD) limits{RESTORED}; MSVDD limits{RESTORED}"))
        self.assertEqual(trace[2][CONTROL], {0: [0, 0, 0, 0], 1: [-50000, 0, 0, 0]})
        self.assertEqual(trace[4][1], (True, f"core rail (NVVDD) vmin{RESTORED}; MSVDD vmin{RESTORED}"))
        self.assertEqual(len(sets(trace[2])), 2)

    def test_a_failed_per_rail_reset_fails_the_whole_reset(self):
        # Blackwell: each per-rail reset reads the boost itself (VoltCtrlGet 1 / 4), in the locked
        # write (2 / 5) and in the verification (3 / 6). The hold record is on rail 1, so rail 0's
        # reset runs first and leaves it; rail 1's reset clears it (every field reset) or not.
        hold = {"rail": 1, "written_uv": {"vmin": 50000}, "prior_uv": {"vmin": 0}}

        def scenario(side, fake, clock):
            side.seed(hold=hold)
            side.reset()

        n0, n1 = "core rail (NVVDD) limits" + RESTORED, "MSVDD limits" + RESTORED
        cases = (
            ("rail 1's own boost read", {"boost_fail_at": (4,)}, f"{n0}; {NO_BOOST}", hold, "rail 0"),
            ("rail 0's own boost read", {"boost_fail_at": (1,)}, f"{NO_BOOST}; {n1}", None, "rail 1"),
            ("rail 1's locked write", {"boost_fail_at": (5,)}, f"{n0}; reset refused (NV_STATUS 0x0)", hold,
             "cleared"),
        )
        for name, options, message, record, attributes in cases:
            with self.subTest(name):
                trace = self.run_both(scenario, **blackwell(**options))
                self.assertEqual(trace[0][1], (False, message))
                self.assertEqual(trace[0][-1], record)
                error, protocol, packets = trace[0][6]
                if attributes == "cleared":
                    # rail 0's write set all three; rail 1's write call cleared the error only
                    self.assertIsNone(error)
                    self.assertNotEqual(protocol, {"stale": True})
                    self.assertNotEqual(packets, ["stale packet"])
                else:
                    self.assertEqual(len(sets(trace[0])), 1)
                    self.assertNotEqual(protocol, {"stale": True})

        # a driver that keeps rail 1's records off by 1000 uV: rail 1's reset fails its verification
        trace = self.run_both(scenario, **blackwell(store=lambda rail, values: (
            values if rail == 0 else [v + 1000 for v in values])))
        self.assertFalse(trace[0][1][0])
        self.assertTrue(trace[0][1][1].startswith(n0 + "; rail 1 read-back disagrees"), trace[0][1])
        self.assertEqual(trace[0][-1], hold)

    def test_a_hold_record_on_the_later_rail_is_adopted_by_its_own_reset(self):
        record = {"rail": 1, "written_uv": {"vmin": 0, "reliability": 5}, "prior_uv": {"vmin": 70, "reliability": 9}}

        def scenario(side, fake, clock):
            side.seed(hold=record)
            side.reset(None, ["vmin"])

        trace = self.run_both(scenario, **blackwell())
        self.assertEqual(trace[0][1], (True, f"core rail (NVVDD) vmin{RESTORED}; MSVDD vmin{RESTORED}"))
        self.assertEqual(trace[0][-1], {"rail": 1, "written_uv": {"vmin": 0, "reliability": 5},
                                        "prior_uv": {"vmin": 0, "reliability": 9}})
        # rail 1's reset fails at its own boost read: nothing adopted
        trace = self.run_both(scenario, **blackwell(boost_fail_at=(4,)))
        self.assertEqual(trace[0][1], (False, f"core rail (NVVDD) vmin{RESTORED}; {NO_BOOST}"))
        self.assertEqual(trace[0][-1], record)

    def test_reset_follows_the_first_read_order_not_the_rail_number(self):
        def scenario(side, fake, clock):
            side.seed()
            fake.live[0][0] = 3  # rail 0's state is invalid: only rail 1 is referenced
            side.limits()
            fake.live[0][0] = 1
            side.limits()        # rail 0 follows: the first-read order is [1, 0]
            side.reset()

        trace = self.run_both(scenario, **blackwell())
        self.assertEqual(list(trace[1][5]["poweron"]), [1, 0])
        self.assertEqual(trace[2][1], (True, f"MSVDD limits{RESTORED}; core rail (NVVDD) limits{RESTORED}"))
        packets = sets(trace[2])
        self.assertEqual(len(packets), 2)
        self.assertNotEqual(packets[0], packets[1])

    def test_single_rail_and_field_subset_texts(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, vmin=700, reliability=1100)
            side.reset(0, ["vmin", "vmin"])
            side.reset(None, ("reliability",))
            side.reset(0)
            side.reset()
            side.reset(0, [])

        results = self.results(scenario, **turing())
        self.assertEqual(results[1:], [(True, f"core rail (NVVDD) vmin, vmin{RESTORED}"),
                                       (True, f"core rail (NVVDD) reliability{RESTORED}"),
                                       (True, f"core rail (NVVDD) limits{RESTORED}"),
                                       (True, f"rail limits{RESTORED}"),
                                       (True, f"core rail (NVVDD) limits{RESTORED}")])

    def test_reset_refusals(self):
        def scenario(side, fake, clock):
            side.seed()
            side.reset(1)
            side.reset(9)
            side.reset(0, ["vmin", "zeta", "alpha"])

        self.assertEqual(self.results(scenario, **turing()),
                         [(False, RESET_ABSENT), (False, RESET_ABSENT), (False, "not a rail limit: alpha, zeta")])
        self.assertEqual(self.results(lambda side, fake, clock: (side.seed(), side.reset()),
                                      **turing(card={"arch": 99})), [(False, NO_INITIAL)])
        self.assertEqual(self.results(lambda side, fake, clock: (side.seed(), side.reset(0)),
                                      vboost_status=-1, **turing()), [(False, NO_BOOST)])

    def test_reset_refusal_order(self):
        def scenario(side, fake, clock):
            side.seed()
            side.reset(1, ["zeta"])  # the absent rail before the bad key
            side.reset(9, ["zeta"])

        self.assertEqual(self.results(scenario, **turing()), [(False, RESET_ABSENT), (False, RESET_ABSENT)])

        def unreadable(side, fake, clock):
            side.seed()
            side.limits()
            fake.script["control"] = [None, -1, -1]  # the support check reads; the reset's own read fails
            side.reset(0)

        trace = self.run_both(unreadable, **turing())
        self.assertEqual(trace[1][1], (False, NO_CURRENT))
        self.assertEqual(sets(trace[1]), [])
        # the overall support check comes first, with a rail given too
        for rail in (0, 1):
            with self.subTest(rail=rail):
                self.assertEqual(self.results(lambda side, fake, clock, rail=rail: (side.seed(), side.reset(rail, ["zeta"])),
                                              **turing(card={"arch": 99})), [(False, NO_INITIAL)])

    def test_reset_refused_without_a_status_prints_zero(self):
        def scenario(side, fake, clock):
            side.seed()
            side.reset(0)

        trace = self.run_both(scenario, scripts=[["unrelated"]], **turing())
        self.assertEqual(trace[0][1], (False, "reset refused (NV_STATUS 0x0)"))
        self.assertTrue(trace[0][6][0].startswith("The driver did not emit"))

    def test_reset_does_not_need_the_write_flag(self):
        def scenario(side, fake, clock):
            side.seed(write=False)
            side.reset()

        self.assertEqual(self.results(scenario, **turing()), [(True, f"rail limits{RESTORED}")])

    def test_hold_record_cleared_only_when_its_written_fields_were_all_reset(self):
        written = {"rail": 0, "written_uv": {"vmin": 50000}, "prior_uv": {"vmin": 0}}
        both = {"rail": 0, "written_uv": {"vmin": 50000, "reliability": 1}, "prior_uv": {"vmin": 0, "reliability": 0}}

        def scenario(side, fake, clock):
            side.seed(hold=written)
            side.reset(0, ["vmin", "reliability"])  # cleared
            side.hold(both)
            side.reset(0, ["vmin"])                 # kept: reliability was not reset
            side.reset(0)                           # cleared: every field
            side.hold(dict(written, rail=1))
            side.reset(0)                           # kept: another rail
            side.hold({"rail": 0, "written_uv": {}, "prior_uv": {}})
            side.reset(0, ["overvoltage"])          # cleared: the empty set is a subset

        trace = self.run_both(scenario, **turing())
        self.assertEqual([step[-1] for step in trace],
                         [None, both, None, dict(written, rail=1), None])

    def test_reset_adopts_a_first_read_value_equal_to_the_raise(self):
        record = {"rail": 0, "written_uv": {"vmin": 0, "reliability": 5}, "prior_uv": {"vmin": 70, "reliability": 9}}

        def scenario(side, fake, clock):
            side.seed(hold=record)
            side.set(0, vmin=700)
            side.reset(0, ["vmin"])  # vmin back to 0 == written: prior follows; reliability kept

        trace = self.run_both(scenario, **turing())
        self.assertEqual(trace[1][-1], {"rail": 0, "written_uv": {"vmin": 0, "reliability": 5},
                                        "prior_uv": {"vmin": 0, "reliability": 9}})


class CallerTextTests(SetterParity):
    """The unknown / bad texts are refused for their presence, not their emptiness, carry NUL bytes
    and lone surrogates, and decode back (dc_rails_limit_request / dc_rails_reset_request carry each
    as presence + pointer + byte length)."""

    def test_an_empty_keyword_is_refused_and_writes_nothing(self):
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, **{"": 1})
            side.set(0, vmin=700, **{"": 1})

        trace = self.run_both(scenario, **turing())
        for step in trace:
            self.assertEqual(step[1], (False, "not a rail limit: "))
            self.assertEqual(sets(step), [])
            self.assertNotIn("escape", kinds(step))

    def test_an_empty_field_name_is_refused_and_writes_nothing(self):
        def scenario(side, fake, clock):
            side.seed(hold=HOLD)
            side.reset(0, fields=("vmin", ""))

        trace = self.run_both(scenario, **turing())
        self.assertEqual(trace[0][1], (False, "not a rail limit: "))
        self.assertEqual(sets(trace[0]), [])
        self.assertNotIn("escape", kinds(trace[0]))
        self.assertEqual(trace[0][-1], project(HOLD))

    def test_lone_surrogates_are_carried_and_decoded(self):
        def disabled(side, fake, clock):
            side.seed(write=False)
            side.set(0, **{"\ud800": 1})

        self.assertEqual(self.results(disabled, **turing()), [(False, DISABLED)])

        def enabled(side, fake, clock):
            side.seed()
            side.set(0, **{"\ud800": 1})
            side.reset(0, fields=["\ud800"])

        self.assertEqual(self.results(enabled, **turing()), [(False, "not a rail limit: \ud800")] * 2)

    def test_a_nul_inside_a_name_refuses_and_shortens_only_the_native_message(self):
        """Recorded divergence: dc_outcome.message is NUL-terminated, so the native message stops at
        the NUL; the refusal itself and everything else the step records are the same."""
        def scenario(side, fake, clock):
            side.seed()
            side.set(0, **{"x\x00y": 1})
            side.reset(0, fields=["x\x00y"])
            side.set(0, **{"\x00x": 1})  # a name starting with NUL: still refused

        rig = SetterRig(**turing())
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertEqual(len(python), len(core))
        for p, c in zip(python, core, strict=True):
            with self.subTest(operation=p[0]):
                self.assertEqual(p[0], c[0])
                self.assertEqual(p[2:], c[2:])
                self.assertEqual(sets(p), [])
        self.assertEqual([p[1] for p in python], [(False, "not a rail limit: x\x00y")] * 2
                         + [(False, "not a rail limit: \x00x")])
        self.assertEqual([c[1] for c in core], [(False, "not a rail limit: x")] * 2
                         + [(False, "not a rail limit: ")])


class BindingContractTests(unittest.TestCase):
    def setUp(self):
        self.rig = SetterRig(**turing())
        self.addCleanup(self.rig.close)
        self.gpu = self.rig.native
        self.gpu.volt_limits_write_enabled = True

    def test_constants_match_the_python_backend(self):
        self.assertEqual(nr.VOLT_LIMIT_MIN_MV, NV.GPU.VOLT_LIMIT_MIN_MV)
        self.assertEqual(nr.VOLT_LIMIT_MAX_MV, NV.GPU.VOLT_LIMIT_MAX_MV)
        self.assertEqual(nr.VOLT_LIMIT_XOC_MAX_MV, NV.GPU.VOLT_LIMIT_XOC_MAX_MV)
        self.assertEqual(nr.RAIL_NAME, NV._RAIL_NAME)
        self.assertEqual(nr.VOLT_LIMIT_FIELDS, FIELDS)

    def test_divergences_are_refused_before_any_read(self):
        fake = self.rig.fake
        for call in (lambda: self.gpu.set_volt_rail_limits(True, vmin=700),
                     lambda: self.gpu.set_volt_rail_limits(1.0, vmin=700),
                     lambda: self.gpu.reset_volt_rail_limits(True),
                     lambda: self.gpu.set_volt_rail_limits(0, vmin="700")):
            fake.calls.clear()
            with self.assertRaises(TypeError):
                call()
            self.assertEqual(fake.calls, [])
        with self.assertRaises(ValueError):
            self.gpu.set_volt_rail_limits(0, **{"x" * 2000: 1})
        with self.assertRaises(ValueError):
            self.gpu.reset_volt_rail_limits(0, ["vmin"] * 400)

    def test_a_failure_after_a_write_keeps_the_writers_attributes(self):
        """dc_rails_set_limits / _raw / _reset_limits fill `writes` even when they then return
        DC_E_FAILED: only an allocation failure after the SET reaches that (the rig cannot make one),
        so the binding's order is driven with a stubbed (status, outcome, writes) triple: the three
        attributes are applied, then the status raises."""
        for name, value in zip(("_volt_rail_write_error", "_volt_rail_write_protocol",
                                "_volt_rail_observed_packets"), ("stale error", {"stale": True}, ["stale packet"]), strict=True):
            setattr(self.gpu, name, value)
        writes = nr._new(nr._SetterWrites)
        writes.calls, writes.attributes, writes.last_attributes = 1, 1, 1
        result = writes.result
        result.size = ctypes.sizeof(result)
        result.done, result.error = 1, b"the write that ran"
        result.has_protocol, result.get_command, result.set_command, result.packet_size = 1, 0x2080B213, 0x2080F214, 1104
        result.observed_count, result.observed[0].value = 1, b"packet"
        with self.assertRaises(ValueError):
            nr._finish(self.gpu, native.DC_E_FAILED, native._Outcome(), writes)
        self.assertEqual(self.gpu._volt_rail_write_error, "the write that ran")
        self.assertEqual(self.gpu._volt_rail_write_protocol["set_command"], "0x2080F214")
        self.assertEqual(self.gpu._volt_rail_observed_packets, ["packet"])
        # a later call that cleared the error (its own write did not run) still clears it
        writes.last_attributes = 0
        with self.assertRaises(ValueError):
            nr._finish(self.gpu, native.DC_E_FAILED, native._Outcome(), writes)
        self.assertIsNone(self.gpu._volt_rail_write_error)

    def test_recorded_divergences(self):
        """The binding's recorded divergences (druta.native_rails' module notes), each side asserted."""
        def scenario(side, fake, clock):
            side.seed()
            side.reset(None, iter(["vmin"]))  # natively both rails; Python's first per-rail call consumes it

        rig = SetterRig(**blackwell())
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertEqual(python[0][1], (True, f"core rail (NVVDD) vmin{RESTORED}; MSVDD {RESTORED}"))
        self.assertEqual(core[0][1], (True, f"core rail (NVVDD) vmin{RESTORED}; MSVDD vmin{RESTORED}"))

        def raises(side, fake, clock):
            side.seed()
            side.set(0, vmin=10 ** 400)
            side.reset(0, fields=[1])

        rig = SetterRig(**turing())
        self.addCleanup(rig.close)
        python, core = rig.run(raises)
        for p, c, kind in zip(python, core, ("OverflowError", "TypeError"), strict=True):
            with self.subTest(kind=kind):
                self.assertEqual((p[1][:2], c[1][:2]), (("raises", kind), ("raises", kind)))
                self.assertIn("control", kinds(p))  # Python raises after its reads
                self.assertEqual(c[2], [])           # natively before any read

    def test_hold_record_round_trip_and_validation(self):
        self.assertIsNone(nr.hold_record(self.gpu))
        nr.set_hold_record(self.gpu, HOLD)
        self.assertEqual(nr.hold_record(self.gpu), project(HOLD))
        nr.set_hold_record(self.gpu, {})
        self.assertIsNone(nr.hold_record(self.gpu))
        for bad in ({"rail": True}, {"rail": 0, "written_uv": {"bogus": 1}}, {"rail": 0, "prior_uv": {"vmin": 1 << 40}}):
            with self.assertRaises(TypeError):
                nr.set_hold_record(self.gpu, bad)


if __name__ == "__main__":
    unittest.main()
