# Druta - parity tests for the native hold-headroom runtime (increment R6).
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native hold-headroom runtime against nvbackend.GPU's hold_headroom_grain_mv,
hold_headroom_architecture, hold_headroom_rail_unavailable, hold_headroom_overwritten,
hold_headroom_record, apply_hold_headroom (with _headroom_unread), hold_headroom_live_ok,
restore_hold_headroom, user_rail_limits and adopt_foreign_hold_headroom.

The native.Gpu methods of those names (druta.native_rails) run dc_rails_hold_*. The Python originals
run unmodified on the identity twin of the R4/R5 write rig (tests/test_native_rail_write.WriteRig):
one fake driver serves both rail getters, the boost, the rail GET/SET escapes and (when a scenario
gives one) the V/F curve to both sides. A SET is stored and the live record follows it: the
reliability term carries the reported boost contribution, the card's effective limit is the lowest
term unless a scenario sets it, and the live NVVDD reading is the scenario's (0, no reading, unless
set), optionally changing once a SET has landed.

Every operation records its result, the ordered driver log (getter calls, escapes with their packet
bytes, boost reads, V/F curve reads, every before_write call with the record it was given and the
number of SETs before it), the four getter caches, the architecture reads, the profile afterwards,
the writer's three attributes (seeded stale), the fake's rail state, the hold record (Python's
_hold_headroom, natively druta.native_rails.hold_record) with its key order, and the four session
state attributes. All of it must be equal on the two sides. Fixture values are inputs to the logic,
not claims about any board; the first-read record is this session's first read, never a factory
default.

The cases follow tests/test_hold_headroom.py's ApplyRestoreTests and RailAvailabilityTests where they
reach the GPU methods (there, FakeRails stands in for the GPU's own reads and writes; here the driver
does), plus the branches the brief names. Recorded divergences are asserted separately
(BindingContractTests)."""
import copy
import ctypes
import sys
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from druta import native, nvbackend as NV, profiles
from tests.test_native_rail_reference import BASES, NO_ARCH, config, shape
from tests.test_native_rail_splice import SETS
from tests.test_native_rail_write import LAYOUTS, WriteCard, WriteFake, WriteNativeSide, WritePythonSide, WriteRig
from tests.test_native_rails import CDECL

if not native.available():
    raise unittest.SkipTest("native core not built (native/build.sh linux)")

from druta import native_rails as nr  # noqa: E402

GPU = NV.GPU
FIELDS = GPU.VOLT_LIMIT_FIELDS
VFP_CURVE, BOOST_GET = 0x21537AD4, 0x23F1B133
STATE = ("_hold_headroom_grain", "_hold_headroom_offset_mv", "_hold_headroom_unread", "_hold_headroom_live_trip")
STATE_DEFAULTS = (None, None, False, False)
# kind -> (boost contribution in uV, rails)
CARDS = {"turing": (25000, (0,)), "pascal": (6250, (0,)), "blackwell": (20000, (0, 1))}
STOCK = {"turing": (1068.75, 1093.75, 1125.0), "pascal": (1068.75, 1093.75, 1200.0),
         "blackwell": (1040.0, 1060.0, 1200.0)}
NOT_SEEN = "The driver did not emit a recognized rail-control packet."
CONTROL, LIVE, HOLD, STATE_AT = 7, 8, 10, 11  # positions in a step's trace


class Abort(BaseException):
    """A BaseException that is not an Exception: apply_hold_headroom does not catch it."""


class HoldFake(WriteFake):
    """WriteFake whose live record follows the control deltas with the reported boost in the
    reliability term, plus the live NVVDD reading, the card's effective limit, per-SET RM statuses,
    escapes that raise, and a V/F curve.

    hold_bases: the per-card bases (uV) every rail's live limits follow; boost: the boost word (uV);
    nvvdd / nvvdd_after: {rail: live uV} now and once a SET has landed; effective / effective_after:
    {rail: uV} overriding the lowest-term effective limit; set_rms: the RM status of each SET in turn (a refused one stores nothing);
    raise_before / raise_after: the (1-based) SETs whose escape raises before storing / after it;
    after_set(fake, n): runs once SET n has landed; curve: the V/F rows' voltages (uV), or None (no
    curve exports)."""

    def __init__(self, *, hold_bases, nvvdd=None, nvvdd_after=None, effective=None, effective_after=None,
                 set_rms=None, raise_before=(), raise_after=(), after_set=None, curve=None, **kw):
        super().__init__(**kw)
        self.hold_bases = hold_bases
        self.nvvdd = dict(nvvdd or {})
        self.nvvdd_after = nvvdd_after
        self.effective = dict(effective or {})
        self.effective_after = effective_after
        self.set_rms = list(set_rms or [])
        self.raise_before, self.raise_after = set(raise_before), set(raise_after)
        self.after_set = after_set
        self.curve = curve
        self.set_count = 0
        self.resync()

    def resync(self):
        for rail, deltas in self.control.items():
            if rail not in self.live:
                continue
            b = self.hold_bases
            word = self.boost.get(rail, 0) if isinstance(self.boost, dict) else self.boost
            terms = [b[0] + deltas[0] + word, b[1] + deltas[1], b[2] + deltas[2]]
            effective = self.effective.get(rail)
            self.live[rail][2:] = terms + [min(terms) if effective is None else effective, b[3] + deltas[3]]
            self.live[rail][1] = self.nvvdd.get(rail, 0)


class HoldCard(WriteCard):
    """WriteCard plus NvAPI's VfpCurve and BoostTableGet over the fake's curve."""

    def __init__(self, **options):
        super().__init__(**options)

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def vfp_curve(handle, pointer):
            fake = self.rig.fake
            cv = NV._VfpCurve.from_address(pointer)
            bits = sum(bin(m).count("1") for m in cv.masks)
            fake.calls.append(("VfpCurve", bits))
            if cv.version != NV.NvAPI.ver(NV._VfpCurve, 1):
                return -9
            rows = fake.curve or []
            if bits > len(rows):
                return -1
            for k in range(bits):
                cv.entries[k].volt_uV, cv.entries[k].freq_kHz = rows[k], 300000 + 15000 * k
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def boost_get(handle, pointer):
            fake = self.rig.fake
            bt = NV._BoostTable.from_address(pointer)
            bits = sum(bin(m).count("1") for m in bt.masks)
            fake.calls.append(("BoostTableGet", bits))
            for k in range(bits):
                bt.rows[k].w[5] = 0
            return 0
        self.vfp_curve, self.boost_get = vfp_curve, boost_get


def curve_rows(volts_mv, n=128):
    """A V/F table of n rows whose distinct voltages are volts_mv (the last one repeated)."""
    rows = [int(round(v * 1000)) for v in volts_mv]
    return rows + [rows[-1]] * (n - len(rows))


class HoldSide:
    """The hold-headroom operations, the session flags, the hold record and its state on each side."""

    def _record(self, operation, call):
        result = super()._record(operation, call)
        self.trace[-1] = self.trace[-1] + (copy.deepcopy(self._hold()), self._state())
        return result

    def seed(self, *, write=False, xoc=False, hold=None, foreign=None):
        super().seed()
        self.gpu.volt_limits_write_enabled = write
        self.gpu.voltage_xoc_enabled = xoc
        if hold is not None:
            self._set_hold(copy.deepcopy(hold))
        if foreign is not None:
            self.gpu._foreign_hold_headroom = copy.deepcopy(foreign)

    def xoc(self, value):
        self.gpu.voltage_xoc_enabled = value

    def _op(self, operation, call):
        self.fake.rig = self.rig
        return self._record(operation, lambda: self._armed(call))

    def hook(self, answer):
        """A before_write hook that logs the record it was given, then answers answer(record)."""
        fake = self.fake

        def before_write(record):
            fake.calls.append(("before_write", shape(record), fake.set_count))
            return answer(record)
        return before_write

    def apply(self, hold, margin, rail=0, answer=None, label=""):
        hook = None if answer is None else self.hook(answer)
        return self._op(("apply", repr(hold), repr(margin), repr(rail), label),
                        lambda: self.gpu.apply_hold_headroom(hold, margin, rail, before_write=hook))

    def restore(self, force=False):
        return self._op(("restore", force), lambda: self.gpu.restore_hold_headroom(force))

    def live_ok(self, record=None, live_mv=None):
        return self._op(("live_ok", repr(record), repr(live_mv)),
                        lambda: self.gpu.hold_headroom_live_ok(record, live_mv=live_mv))

    def live_ok_active(self, live_mv=None):
        """hold_headroom_live_ok(hold_headroom_record(), live_mv), as druta.py's watch calls it."""
        return self._op(("live_ok_active", repr(live_mv)),
                        lambda: self.gpu.hold_headroom_live_ok(self.gpu.hold_headroom_record(), live_mv=live_mv))

    def overwritten(self, limits=None):
        return self._op(("overwritten", repr(limits)), lambda: self.gpu.hold_headroom_overwritten(limits))

    def grain(self):
        return self._op(("grain",), self.gpu.hold_headroom_grain_mv)

    def architecture(self):
        return self._op(("architecture",), self.gpu.hold_headroom_architecture)

    def unavailable(self, rail=0):
        return self._op(("unavailable", repr(rail)), lambda: self.gpu.hold_headroom_rail_unavailable(rail))

    def record(self):
        return self._op(("record",), self.gpu.hold_headroom_record)

    def user_limits(self, cur=None):
        return self._op(("user_limits",), lambda: self.gpu.user_rail_limits(cur))

    def adopt(self, record):
        return self._op(("adopt", repr(record)), lambda: self.gpu.adopt_foreign_hold_headroom(record))

    def set(self, rail, **limits):
        return self._op(("set", rail, repr(limits)), lambda: self.gpu.set_volt_rail_limits(rail, **limits))

    def reset(self, rail=None, fields=None):
        return self._op(("reset", rail, repr(fields)), lambda: self.gpu.reset_volt_rail_limits(rail, fields))

    def capture(self):
        """profiles.capture_rails over this side's GPU, during a raise (the user's limits)."""
        def run():
            state = {profiles.INCOMPLETE_KEY: []}
            profiles.capture_rails(self._view(), state, None)
            return state
        return self._op(("capture",), run)


class HoldPythonSide(HoldSide, WritePythonSide):
    def __init__(self, gpu, rig):
        super().__init__(gpu, rig)
        for name in STATE + ("_hold_headroom", "_foreign_hold_headroom", "_vfp_layout_cache", "_vfp_layout_error"):
            if name in vars(gpu):
                delattr(gpu, name)

    def _hold(self):
        return getattr(self.gpu, "_hold_headroom", None)

    def _set_hold(self, record):
        self.gpu._hold_headroom = record

    def _state(self):
        return tuple(getattr(self.gpu, name, default) for name, default in zip(STATE, STATE_DEFAULTS))

    def _view(self):
        g = self.gpu
        return SimpleNamespace(read_volt_rail_limits=g.read_volt_rail_limits, hold_headroom_record=g.hold_headroom_record,
                               user_rail_limits=g.user_rail_limits, abs_limit_mv=GPU.abs_limit_mv,
                               volt_rail_limit_fields=g.volt_rail_limit_fields, voltage_xoc_enabled=False)


class HoldNativeSide(HoldSide, WriteNativeSide):
    def _refresh(self):
        # nr.refresh is the core's four-cache reset; _refresh_volt_rail_capabilities also clears the
        # writer's _volt_rail_write_error, which lives in the binding (see native_rails' docstring)
        super()._refresh()
        self.gpu._volt_rail_write_error = None

    def _hold(self):
        return self.gpu.hold_headroom_record()

    def _set_hold(self, record):
        self.gpu._hold_headroom = record

    def _state(self):
        return tuple(getattr(self.gpu, name) for name in STATE)

    def _view(self):
        g = self.gpu
        return SimpleNamespace(read_volt_rail_limits=lambda: nr.read_volt_rail_limits(g),
                               hold_headroom_record=g.hold_headroom_record, user_rail_limits=g.user_rail_limits,
                               abs_limit_mv=GPU.abs_limit_mv,
                               volt_rail_limit_fields=lambda r: nr.volt_rail_limit_fields(g, r),
                               voltage_xoc_enabled=False)


class HoldRig(WriteRig):
    card_class = HoldCard
    fake_class = HoldFake
    python_side = HoldPythonSide
    native_side = HoldNativeSide

    def bind(self):
        super().bind()
        interfaces = self.card.nvapi.interfaces
        for ident, callback in ((VFP_CURVE, self.card.vfp_curve), (BOOST_GET, self.card.boost_get)):
            if self.config.get("curve") is not None:
                interfaces[ident] = callback
            else:
                interfaces.pop(ident, None)
        api = self.twin.nvapi
        api.VfpCurve = api._i(VFP_CURVE, NV.PTR, NV.PTR)
        api.BoostTableGet = api._i(BOOST_GET, NV.PTR, NV.PTR)

    def escape(self, pointer):
        fake = self.fake
        e = NV.GPU._Escape.from_address(pointer)
        size = e.PrivateDriverDataSize
        words = (ctypes.c_uint32 * (size // 4)).from_address(e.pPrivateDriverData) if size >= 68 else None
        is_set = words is not None and size in LAYOUTS and words[14] in SETS
        n = 0
        if is_set:
            fake.set_count += 1
            n = fake.set_count
            if n in fake.raise_before:
                fake.calls.append(("escape raises", n))
                raise OSError("escape callback failed before the SET")
        kept = copy.deepcopy(fake.control) if is_set else None
        status = super().escape(pointer)
        if is_set:
            if fake.set_rms:
                words[16] = fake.set_rms.pop(0)
                if words[16]:
                    fake.control.clear()  # a refused SET stores nothing
                    fake.control.update(kept)
            if fake.nvvdd_after is not None:
                fake.nvvdd = dict(fake.nvvdd_after)
            if fake.effective_after is not None:
                fake.effective = dict(fake.effective_after)
            fake.resync()
            if fake.after_set is not None:
                fake.after_set(fake, n)
            if n in fake.raise_after:
                fake.calls.append(("escape raises", n))
                raise OSError("escape callback failed after the SET")
        return status


def card(kind="turing", *, control=None, boost=None, rails=None, **fake):
    """Rig options for one card of `kind` whose live record follows its control deltas (see
    HoldFake); boost is the reported contribution in uV (CARDS' by default)."""
    word, default_rails = CARDS[kind]
    options = config(kind, rails=rails or default_rails, control=control, legacy=kind == "pascal")
    options.update(hold_bases=BASES[kind], boost=word if boost is None else boost)
    options.update(fake)
    return options


def deltas(kind, rel, alt, ov, boost_mv=None):
    """Control deltas (uV) putting the ceiling terms at rel (+ the boost), alt and ov mV."""
    word = CARDS[kind][0] if boost_mv is None else int(boost_mv * 1000)
    b = BASES[kind]
    return [int(round(rel * 1000)) - b[0] - word, int(round(alt * 1000)) - b[1], int(round(ov * 1000)) - b[2], 0]


def terms(step, rail=0):
    """The fake's ceiling terms (mV) after a step: reliability with the boost, alt-reliability, overvoltage."""
    return tuple(v / 1000.0 for v in step[LIVE][rail][2:5])


def set_count(step):
    return sum(1 for call in step[2] if call[0] == "escape" and int.from_bytes(call[2][56:60], "little") in SETS)


class HoldParity(unittest.TestCase):
    def tearDown(self):
        native.hook_test_fail_protect(False)
        self.assertFalse(native.hook_test_state()["installed"])

    def run_both(self, scenario, **options):
        rig = HoldRig(**options)
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
                self.assertEqual(shape(p[6]), shape(c[6]), "writer attributes")
                self.assertEqual(p[7:10], c[7:10], "rail state")
                self.assertEqual(shape(p[HOLD]), shape(c[HOLD]), "hold record and its key order")
                self.assertEqual(shape(p[STATE_AT]), shape(c[STATE_AT]), "session state")
        return python

    def results(self, scenario, **options):
        return [step[1] for step in self.run_both(scenario, **options)]


def ceiling_of(kind):
    rel, alt, ov = STOCK[kind]
    return min(rel + CARDS[kind][0] / 1000.0, alt, ov)


# --------------------------------------------------------------------------------------------- #
class ApplyRestoreTests(HoldParity):
    def test_apply_raises_the_ceiling_and_restore_puts_it_back_on_each_generation(self):
        for kind in CARDS:
            with self.subTest(kind=kind):
                hold = ceiling_of(kind)

                def scenario(side, fake, clock, hold=hold):
                    side.seed()
                    side.apply(hold, 25.0)
                    side.record()
                    side.restore()

                trace = self.run_both(scenario, **card(kind))
                self.assertTrue(trace[0][1][0], trace[0][1])
                self.assertEqual(min(terms(trace[0])), hold + 25.0)
                self.assertEqual(trace[0][CONTROL][0][3], 0)  # vmin untouched
                if kind == "blackwell":
                    self.assertEqual(trace[0][CONTROL][1], [-50000, 0, 0, 0])  # rail 1 untouched
                self.assertEqual(list(trace[1][1]), ["rail", "hold_mv", "margin_mv", "ceiling_mv", "boost_mv",
                                                     "live_offset_mv", "grain_mv", "input_uv", "prior_uv",
                                                     "written_uv", "raised_ceiling_mv"])
                self.assertTrue(trace[2][1][0], trace[2][1])
                self.assertEqual(trace[2][CONTROL][0], [0, 0, 0, 0])
                self.assertIsNone(trace[2][HOLD])

    def test_hold_above_the_ceiling_is_withheld_on_each_generation(self):
        for kind in CARDS:
            with self.subTest(kind=kind):
                def scenario(side, fake, clock, kind=kind):
                    side.seed()
                    side.apply(ceiling_of(kind) + 6.25, 25.0)

                trace = self.run_both(scenario, **card(kind))
                self.assertIsNone(trace[0][1][0])
                self.assertIn("above the voltage ceiling", trace[0][1][1])
                self.assertEqual(set_count(trace[0]), 0)

    def test_card_effective_limit_below_the_estimate_withholds(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(effective={0: 1087500}))
        self.assertIsNone(trace[0][1][0])
        self.assertIn("(1087.50 mV)", trace[0][1][1])
        self.assertEqual(set_count(trace[0]), 0)

    def test_apply_does_not_require_the_free_form_limit_switch(self):
        def scenario(side, fake, clock):
            side.seed(write=False)
            side.apply(1093.75, 25.0)

        self.assertTrue(self.results(scenario, **card())[0][0])

    def test_already_clear_ceiling_writes_nothing_and_records_nothing(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1100.0, 25.0)

        trace = self.run_both(scenario, **card(control={0: deltas("turing", 1175.0, 1175.0, 1175.0)}))
        self.assertEqual(trace[0][1], (True, "core rail (NVVDD): the voltage ceiling (1175.00 mV) already clears "
                                             "the 1100.00 mV hold by 25 mV"))
        self.assertEqual(set_count(trace[0]), 0)
        self.assertIsNone(trace[0][HOLD])

    def test_a_second_hold_replaces_the_first_raise_instead_of_stacking(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.apply(1087.5, 25.0)
            side.restore()

        trace = self.run_both(scenario, **card())
        self.assertTrue(trace[1][1][0])
        self.assertEqual(terms(trace[1])[:2], (1087.5 + 25.0, 1112.5))
        self.assertEqual(set_count(trace[1]), 2)  # the old raise off, then the new one
        rel, alt, ov = STOCK["turing"]
        self.assertEqual(terms(trace[2]), (rel + 25.0, alt, ov))  # stock again, reliability with the 25 mV boost

    def test_above_ceiling_hold_still_restores_a_previous_raise(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.apply(1125.0, 25.0)

        trace = self.run_both(scenario, **card())
        self.assertIsNone(trace[1][1][0])
        self.assertIsNone(trace[1][HOLD])
        self.assertEqual(trace[1][CONTROL][0], [0, 0, 0, 0])

    def test_restore_first_step_is_retried_once(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.set_rms = [0x1F]  # the re-plan's first restore is refused, the retry goes through
            side.apply(1087.5, 25.0)

        trace = self.run_both(scenario, **card())
        self.assertTrue(trace[1][1][0], trace[1][1])
        self.assertEqual(set_count(trace[1]), 3)

    def test_a_restore_that_fails_twice_stops_the_re_plan(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.set_rms = [0x1F, 0x1F]
            side.apply(1087.5, 25.0)

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (False, "previous headroom could not be restored first: headroom restore "
                                              "refused (NV_STATUS 0x1F)"))
        self.assertIsNotNone(trace[1][HOLD])

    def test_record_carries_the_ceiling_and_boost_it_was_planned_against(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card())
        record = trace[0][HOLD]
        self.assertEqual((record["ceiling_mv"], record["boost_mv"]), (1093.75, 25.0))
        self.assertEqual((record["hold_mv"], record["margin_mv"], record["raised_ceiling_mv"]),
                         (1093.75, 25.0, 1118.75))

    def test_the_record_is_kept_before_the_set(self):
        """The record exists while the driver takes the SET (read from inside the escape), so a
        failure after the driver took it cannot lose the only copy of the prior limits."""
        seen = {}

        def scenario(side, fake, clock):
            side.seed()
            fake.after_set = lambda fake, n: seen.setdefault(type(side).__name__, []).append(copy.deepcopy(side._hold()))
            side.apply(1093.75, 25.0)
            fake.after_set = None

        trace = self.run_both(scenario, **card())
        self.assertEqual(sorted(seen), ["HoldNativeSide", "HoldPythonSide"])
        for name, records in seen.items():
            with self.subTest(side=name):
                self.assertEqual(len(records), 1)
                final = dict(trace[0][HOLD])
                del final["raised_ceiling_mv"]  # added once the read-back has run
                self.assertEqual(records[0], final)
        self.assertEqual(shape(seen["HoldPythonSide"][0]), shape(seen["HoldNativeSide"][0]))

    def test_write_ahead_hook_sees_the_plan_before_the_set(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0, answer=lambda record: None)

        trace = self.run_both(scenario, **card())
        hooks = [call for call in trace[0][2] if call[0] == "before_write"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0][2], 0)  # no SET before it
        planned = {k[1]: v for k, v in hooks[0][1][1]}
        self.assertEqual(planned["written_uv"], shape(trace[0][HOLD]["written_uv"]))
        self.assertNotIn("raised_ceiling_mv", planned)

    def test_live_rail_above_the_raised_ceiling_undoes_the_raise(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, nvvdd_after={0: 1125000}))
        ok, msg = trace[0][1]
        self.assertIsNone(ok)
        self.assertIn("above the 1118.75 mV ceiling the headroom raise set", msg)
        self.assertIn("raise undone", msg)
        self.assertIsNone(trace[0][HOLD])
        self.assertTrue(trace[0][STATE_AT][3])  # _hold_headroom_live_trip
        self.assertEqual(trace[0][CONTROL][0], [0, 0, 0, 0])

    def test_rail_margin_above_the_old_ceiling_is_expected_and_kept(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.live_ok_active(1112.5)

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, nvvdd_after={0: 1112500}))
        self.assertTrue(trace[0][1][0])
        self.assertEqual((trace[0][HOLD]["raised_ceiling_mv"], trace[0][HOLD]["live_offset_mv"]), (1118.75, 0.0))
        self.assertTrue(trace[1][1][0])

    def test_a_card_reading_above_its_own_ceiling_keeps_that_offset_only(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.live_ok_active(1131.25 + 6.25)

        trace = self.run_both(scenario, **card(nvvdd={0: 1106250}, nvvdd_after={0: 1131250}))
        ok, msg = trace[0][1]
        self.assertTrue(ok, msg)
        self.assertIn("12.50 mV above the ceiling before the raise", msg)
        self.assertEqual(trace[0][HOLD]["live_offset_mv"], 12.5)
        self.assertEqual(trace[0][STATE_AT][1], 12.5)  # _hold_headroom_offset_mv
        self.assertFalse(trace[1][1][0])

    def test_an_offset_past_the_margin_is_not_raised_on(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(nvvdd={0: 1125000}))
        self.assertEqual(trace[0][1], (None, "the rail read 31.25 mV above its ceiling before any raise - more than "
                                             "the 25 mV margin, so a raise could not be checked on this card. "
                                             "Headroom was NOT applied."))
        self.assertEqual(set_count(trace[0]), 0)

    def test_the_live_check_uses_this_cards_own_voltage_grain(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.live_ok_active(1118.75 + 2.5)
            side.live_ok_active(1118.75 + 3.0)

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, curve=curve_rows((800.0, 805.0, 810.0, 815.0))))
        self.assertTrue(trace[0][1][0])
        self.assertEqual(trace[0][HOLD]["grain_mv"], 5.0)
        self.assertEqual(trace[0][STATE_AT][0], 5.0)
        self.assertTrue(trace[1][1][0])
        self.assertFalse(trace[2][1][0])

    def test_without_a_curve_the_fallback_grain_is_used_and_said(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card())
        ok, msg = trace[0][1]
        self.assertTrue(ok)
        self.assertIn("uses a 6.25 mV grain", msg)
        self.assertIn("no live rail reading", msg)
        self.assertIsNone(trace[0][HOLD]["grain_mv"])

    def test_the_cards_own_enforced_limit_counts_as_holding(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 7.0)

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, nvvdd_after={0: 1106250},
                                               effective_after={0: 1106250}))
        self.assertTrue(trace[0][1][0], trace[0][1])
        self.assertEqual(trace[0][HOLD]["raised_ceiling_mv"], 1106.25)

    def test_a_zero_live_reading_is_no_reading(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.live_ok(live_mv=0.0)

        self.assertEqual(self.results(scenario, **card())[1], (True, "no live rail reading"))

    def test_a_non_finite_raised_ceiling_falls_back_to_hold_plus_margin(self):
        record = {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0, "raised_ceiling_mv": float("nan")}

        def scenario(side, fake, clock):
            side.seed()
            side.live_ok(record, 1300.0)
            side.live_ok(record, 1118.75)

        results = self.results(scenario, **card())
        self.assertEqual(results[0], (False, "the rail reads 1300.00 mV live, 181.25 mV above the 1118.75 mV ceiling "
                                             "the headroom raise set (this card's allowance is 3.12 mV)"))
        self.assertEqual(results[1], (True, "live 1118.75 mV"))

    def test_an_unread_absolute_state_is_not_planned_on(self):
        def scenario(side, fake, clock):
            side.seed()
            side.limits()  # the reference is captured
            # the support check's two live reads pass, then the plan's own state read fails
            fake.script["live"] = [None, None] + [-1] * 8
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (None, "the rail's absolute state could not be read right now; no headroom "
                                             "applied this time"))
        self.assertTrue(trace[1][STATE_AT][2])  # _hold_headroom_unread
        self.assertEqual(set_count(trace[1]), 0)

    def test_unreadable_limits_are_a_transient_not_an_unsupported_rail(self):
        def scenario(side, fake, clock):
            side.seed()
            side.limits()
            fake.script["live"] = [-1] * 8  # every state read fails from now on
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (None, "the voltage limits could not be read right now; no headroom "
                                             "applied this time"))
        self.assertTrue(trace[1][STATE_AT][2])
        self.assertEqual(set_count(trace[1]), 0)

    def test_an_unread_absolute_state_on_read_back_bounds_at_hold_plus_margin(self):
        def after_set(fake, n):
            if n == 1:
                fake.script["live"] = [-1] * 8

        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(after_set=after_set, effective_after={0: 1200000}))
        self.assertTrue(trace[0][1][0], trace[0][1])
        self.assertEqual(trace[0][HOLD]["raised_ceiling_mv"], 1118.75)

    def test_an_escape_that_raises_is_swallowed_by_ctypes_on_both_sides(self):
        """Python's except branch around _write_rail_records (the "raised" text) is not reachable
        through the real writer: an escape callback that raises is swallowed by ctypes, on both sides
        alike, and the escape's return value is then undefined (it differs run to run), so only the
        shape of the outcome is compared here, not its NTSTATUS text."""
        for when in ("raise_before", "raise_after"):
            with self.subTest(when=when):
                def scenario(side, fake, clock):
                    side.seed()
                    side.apply(1093.75, 25.0)

                rig = HoldRig(**card(**{when: (1,)}))
                self.addCleanup(rig.close)
                swallowed, hook = [], sys.unraisablehook
                sys.unraisablehook = lambda unraisable, swallowed=swallowed: swallowed.append(type(unraisable.exc_value))
                try:
                    python, core = rig.run(scenario)
                finally:
                    sys.unraisablehook = hook
                self.assertEqual(swallowed, [OSError, OSError])  # one per side, reported and dropped by ctypes
                for trace in (python, core):
                    ok, text = trace[-1][1]
                    self.assertNotIn("raised", text)
                    self.assertIn(("escape raises", 1), trace[-1][2])
                    if not ok:
                        self.assertTrue(text.endswith("limits restored"), text)

    def test_an_off_value_landing_is_forced_back(self):
        """A SET that landed alt-reliability 6.25 mV off: the rollback restores every recorded field
        (force), not only those that carry the requested value."""
        landed = []

        def store(rail, values):
            if not landed:
                landed.append(1)
                return [values[0], values[1] + 6250, values[2], values[3]]
            return values

        def scenario(side, fake, clock):
            landed.clear()
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(store=store))
        ok, msg = trace[0][1]
        self.assertFalse(ok)
        self.assertIn("headroom write not verified (rail 0 read-back disagrees", msg)
        self.assertTrue(msg.endswith("; limits restored"), msg)
        self.assertEqual(trace[0][CONTROL][0], [0, 0, 0, 0])
        self.assertIsNone(trace[0][HOLD])

    def test_a_write_the_driver_never_saw_leaves_no_record(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, scripts=[["unrelated"]], **card())
        ok, msg = trace[0][1]
        self.assertFalse(ok)
        self.assertTrue(msg.startswith(NOT_SEEN), msg)
        self.assertIsNone(trace[0][HOLD])

    def test_a_write_not_seen_without_the_writers_text(self):
        # the locked write's own boost read fails: no attribute set, the fallback text
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, boost_fail_at=(2,), **card())
        self.assertEqual(trace[0][1], (False, "the rails request was not seen - nothing was written"))
        self.assertIsNone(trace[0][HOLD])

    def test_an_untracked_raise_is_not_made(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0, answer=lambda record: False, label="false")
            side.apply(1093.75, 25.0, answer=lambda record: (_ for _ in ()).throw(OSError("disk full")),
                       label="raises")
            side.apply(1093.75, 25.0, answer=lambda record: None, label="none")

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[0][1], (None, "the crash marker could not be written, so a crash could leave the "
                                             "raise untracked; no headroom applied"))
        self.assertEqual(trace[1][1], (None, "disk full, so a crash could leave the raise untracked; no headroom "
                                             "applied"))
        for step in trace[:2]:
            self.assertEqual(set_count(step), 0)
            self.assertTrue(step[STATE_AT][2])
        self.assertTrue(trace[2][1][0])
        self.assertFalse(trace[2][STATE_AT][2])

    def test_only_false_itself_stops_the_raise(self):
        for label, answer in (("zero", lambda r: 0), ("empty", lambda r: ""), ("none", lambda r: None),
                              ("true", lambda r: True), ("record", lambda r: r)):
            with self.subTest(label=label):
                def scenario(side, fake, clock, answer=answer, label=label):
                    side.seed()
                    side.apply(1093.75, 25.0, answer=answer, label=label)

                trace = self.run_both(scenario, **card())
                self.assertTrue(trace[0][1][0], trace[0][1])
                self.assertEqual(set_count(trace[0]), 1)

    def test_a_base_exception_from_the_hook_propagates(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            try:
                side.apply(1087.5, 25.0, answer=lambda record: (_ for _ in ()).throw(Abort("stop")))
            except Abort:
                fake.calls.append(("aborted",))
            side.record()

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}))
        # the re-plan's restore ran, then the hook raised out of the call: nothing more was written
        self.assertIsNone(trace[1][HOLD])
        self.assertEqual(trace[1][CONTROL][0], [0, 0, 0, 0])

    def test_normal_voltage_holds_keep_headroom_through_1200_mv_on_each_generation(self):
        for kind in CARDS:
            for hold in (1175.0, 1200.0):
                with self.subTest(kind=kind, hold=hold):
                    boost = CARDS[kind][0] / 1000.0
                    control = {r: [0, 0, 0, 0] for r in CARDS[kind][1]}
                    control[0] = deltas(kind, hold, hold, hold)
                    if kind == "blackwell":
                        control[1][0] = -50000

                    def scenario(side, fake, clock, hold=hold):
                        side.seed()
                        side.limits()
                        side.apply(hold, 25.0)
                        side.restore()

                    trace = self.run_both(scenario, **card(kind, control=control))
                    self.assertTrue(trace[1][1][0], trace[1][1])
                    self.assertEqual(min(terms(trace[1])), hold + 25.0)
                    self.assertEqual(trace[2][CONTROL], trace[0][CONTROL])
                    self.assertEqual(trace[1][HOLD]["boost_mv"], boost)

    def test_configured_margin_is_allowed_above_the_mode_limit(self):
        for xoc, hold in ((False, 1200.0), (True, 1500.0)):
            for margin in (6.25, 25.0, 100.0):
                with self.subTest(xoc=xoc, margin=margin):
                    def scenario(side, fake, clock, hold=hold, margin=margin, xoc=xoc):
                        side.seed(xoc=xoc)
                        side.apply(hold, margin)
                        side.restore()

                    trace = self.run_both(scenario, **card(boost=0, control={0: deltas("turing", hold, hold, hold, 0)}))
                    self.assertTrue(trace[0][1][0], trace[0][1])
                    self.assertEqual(terms(trace[0]), (hold + margin,) * 3)
                    self.assertTrue(trace[1][1][0])

    def test_unbounded_margin_is_withheld_without_writing(self):
        for margin in (100.01, 1000.0, float("inf"), float("nan"), 0.0, -1.0):
            with self.subTest(margin=margin):
                def scenario(side, fake, clock, margin=margin):
                    side.seed()
                    side.apply(1093.75, margin)

                trace = self.run_both(scenario, **card())
                self.assertIsNone(trace[0][1][0], trace[0][1])
                self.assertEqual(set_count(trace[0]), 0)

    def test_a_hold_past_the_mode_bound_is_withheld_not_failed(self):
        for xoc, hold in ((False, 1206.25), (True, 1506.25)):
            with self.subTest(xoc=xoc):
                def scenario(side, fake, clock, hold=hold, xoc=xoc):
                    side.seed(xoc=xoc)
                    side.apply(hold, 25.0)

                trace = self.run_both(scenario, **card(boost=0, control={0: deltas("turing", hold, hold, hold, 0)}))
                self.assertIsNone(trace[0][1][0])
                self.assertIn("limit bound", trace[0][1][1])
                self.assertEqual(set_count(trace[0]), 0)

    def test_an_unsettled_reading_cannot_loosen_the_live_check(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)        # settled, at the ceiling: offset 0
            fake.nvvdd = {0: 1112500}         # still up from the raise
            fake.resync()
            side.apply(1093.75, 25.0)        # a re-plan: the known offset, not measured
            side.restore()
            side.apply(1093.75, 25.0)        # a new raise: measured, only lowered

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, nvvdd_after={0: 1112500}))
        for step in (1, 3):
            self.assertTrue(trace[step][1][0], trace[step][1])
            self.assertEqual(trace[step][HOLD]["live_offset_mv"], 0.0)

    def test_an_unsettled_low_reading_does_not_tighten_a_real_offset(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.nvvdd_after = None
            fake.nvvdd = {0: 1093750}
            fake.resync()
            side.apply(1093.75, 25.0)        # a re-plan keeps 12.5
            side.live_ok(live_mv=1131.25)

        trace = self.run_both(scenario, **card(nvvdd={0: 1106250}, nvvdd_after={0: 1131250}))
        self.assertEqual(trace[1][HOLD]["live_offset_mv"], 12.5)
        self.assertTrue(trace[2][1][0])

    def test_an_issued_but_unconfirmed_write_is_forced_back(self):
        """(False, status) from the writer: Pascal's settle re-send whose identity check fails. A
        control read inside the write's own pre-read changes vmin (another writer), so the settle runs;
        the first SET lands alt-reliability off, so its identity check fails."""
        state = {"armed": False, "landed": False}

        def on_read(fake):
            if state["armed"]:
                state["armed"] = False
                fake.control[0][3] = -6250
                fake.resync()

        def store(rail, values):
            if not state["landed"]:
                state["landed"] = True
                return [values[0], values[1] + 6250, values[2], values[3]]
            return values

        def scenario(side, fake, clock):
            state.update(armed=False, landed=False)
            side.seed()
            side.limits()
            fake.on_read = on_read

            def arm(record):
                state["armed"] = True
            side.apply(ceiling_of("pascal"), 25.0, answer=arm)

        trace = self.run_both(scenario, **card("pascal", store=store))
        ok, msg = trace[1][1]
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("the headroom write was issued but not confirmed; "), msg)

    def test_an_effective_limit_far_above_the_estimate_is_not_trusted(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.live_ok(live_mv=1150.0)

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, effective_after={0: 1200000}))
        self.assertTrue(trace[0][1][0])
        self.assertEqual(trace[0][HOLD]["raised_ceiling_mv"], 1118.75 + 6.25)
        self.assertFalse(trace[1][1][0])

    def test_raising_a_term_the_raise_did_not_touch_does_not_re_plan(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.control[0][2] = 25000       # overvoltage up: the plan still holds
            fake.resync()
            side.overwritten()
            fake.control[0][2] = -25000      # down: plan again
            fake.resync()
            side.overwritten()

        self.assertEqual(list(self.results(scenario, **card())[1:]), [False, True])

    def test_live_voltage_at_the_hold_is_fine_and_missing_live_is_not_a_rise(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.restore()
            fake.nvvdd = {}
            fake.resync()
            side.apply(1093.75, 25.0)

        results = self.results(scenario, **card(nvvdd={0: 1093750}))
        self.assertTrue(results[0][0] and results[2][0])

    def test_restore_leaves_a_field_the_user_changed_since(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.control[0][1] = 56250       # alt-reliability 1150
            fake.resync()
            side.restore()

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (True, "core rail (NVVDD): reliability restored after the hold"))
        self.assertEqual(trace[1][CONTROL][0], [0, 56250, 0, 0])

    def test_overwritten_detects_a_profile_or_slider_write(self):
        def scenario(side, fake, clock):
            side.seed()
            side.overwritten()
            side.apply(1093.75, 25.0)
            side.overwritten()
            fake.control[0][0] = 0
            fake.resync()
            side.overwritten()
            side.overwritten({0: dict.fromkeys(FIELDS, 0.0)})
            side.overwritten({})
            side.overwritten({0: dict(dict.fromkeys(FIELDS, 0.0), reliability=float("nan"))})

        results = self.results(scenario, **card())
        self.assertEqual([results[0], results[2], results[3], results[4], results[5], results[6]],
                         [False, False, True, True, False, False])

    def test_overwritten_by_an_infinite_row_raises_overflow_error(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.overwritten({0: dict(dict.fromkeys(FIELDS, 0.0), alt_reliability=float("inf"))})

        result = self.results(scenario, **card())[1]
        self.assertEqual(result, ("raises", "OverflowError", "cannot convert float infinity to integer"))

    def test_driver_refused_write_is_rolled_back(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(set_rms=[0x1F]))
        self.assertEqual(trace[0][1][0], False)
        self.assertTrue(trace[0][1][1].startswith("driver refused the headroom write (NV_STATUS 0x1F); limits "
                                                  "restored"), trace[0][1])
        self.assertIsNone(trace[0][HOLD])
        self.assertEqual(trace[0][CONTROL][0], [0, 0, 0, 0])

    def test_rollback_that_also_fails_keeps_the_record_for_exit(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.set_rms = []
            side.restore()

        trace = self.run_both(scenario, **card(set_rms=[0x1F, 0x1F]))
        self.assertEqual(trace[0][1], (False, "driver refused the headroom write (NV_STATUS 0x1F); RESTORE FAILED: "
                                              "headroom restore refused (NV_STATUS 0x1F)"))
        self.assertIsNotNone(trace[0][HOLD])
        self.assertTrue(trace[1][1][0])
        self.assertIsNone(trace[1][HOLD])

    def test_transient_unreadable_limits_are_worded_as_transient(self):
        def scenario(side, fake, clock):
            side.seed()
            fake.script["control"] = [-1] * 8
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[0][1], (None, "the voltage limits could not be read right now; no headroom applied "
                                             "this time"))
        self.assertTrue(trace[0][STATE_AT][2])

    def test_boost_unreadable_writes_nothing(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, vboost_status=-1, **card())
        self.assertEqual(trace[0][1], (None, "cannot preserve the current voltage boost right now; no headroom "
                                             "applied"))
        self.assertEqual(set_count(trace[0]), 0)

    def test_full_rail_reset_drops_the_record_single_field_reset_keeps_it(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.reset(0, fields=("vmin",))
            side.reset(0)

        trace = self.run_both(scenario, **card())
        self.assertIsNotNone(trace[1][HOLD])
        self.assertIsNone(trace[2][HOLD])

    def test_a_setter_equal_to_the_raise_is_adopted_into_the_fuller_record(self):
        def scenario(side, fake, clock):
            side.seed(write=True)
            side.apply(1093.75, 25.0)
            side.set(0, alt_reliability=1118.75)   # the raised value itself: the user's now
            side.restore()

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][HOLD]["prior_uv"]["alt_reliability"], trace[1][HOLD]["written_uv"]["alt_reliability"])
        self.assertEqual(list(trace[1][HOLD]), list(trace[0][HOLD]))  # the other keys kept, in order
        self.assertEqual(trace[2][CONTROL][0][1], 25000)

    def test_user_rail_limits_hides_only_our_raise(self):
        foreign = {"rail": 0, "written_uv": {"overvoltage": 0}, "prior_uv": {"overvoltage": -6250}}

        def scenario(side, fake, clock):
            side.seed()
            side.user_limits()
            side.apply(1093.75, 25.0)
            side.user_limits()
            side.gpu._foreign_hold_headroom = foreign
            side.user_limits()

        trace = self.run_both(scenario, **card())
        user = trace[2][1][0]
        self.assertEqual(tuple(GPU.abs_limit_mv(user, k) for k in FIELDS[:3]), (1068.75, 1093.75, 1125.0))
        self.assertEqual(GPU.abs_limit_mv(trace[3][1][0], "overvoltage"), 1118.75)

    def test_profile_capture_during_a_raise_records_the_users_limits(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.capture()

        state = self.results(scenario, **card())[1]
        self.assertEqual(state[profiles.INCOMPLETE_KEY], [])
        captured = state["rail_limits_mv"]["0"]
        self.assertEqual((captured["reliability"], captured["alt_reliability"], captured["overvoltage"]),
                         (1068.75, 1093.75, 1125.0))

    def test_architecture_is_three_way(self):
        # an architecture word arch() does not know reads as None (unknown), not as unsupported
        for arch, want in ((6, True), (4, True), (7, True), (8, True), (10, True), (9, False), (3, False),
                           (99, None), (None, None)):
            with self.subTest(arch=arch):
                options = card()
                options["card"] = dict(NO_ARCH) if arch is None else {"arch": arch}

                def scenario(side, fake, clock):
                    side.architecture()

                self.assertIs(self.results(scenario, **options)[0], want)


class GrainTests(HoldParity):
    def grain(self, volts):
        def scenario(side, fake, clock):
            side.seed()
            side.grain()
            side.grain()  # remembered: no second curve read
        trace = self.run_both(scenario, **card(curve=curve_rows(volts)))
        self.assertEqual([c for c in trace[1][2] if c[0] == "VfpCurve"], [])
        return trace[0][1]

    def test_the_grain_is_the_cards_usual_step_within_sane_bounds(self):
        for volts, want in (((800, 806.25, 812.5, 812.501, 818.75, 825), 6.25),
                            ((800, 825, 850, 875), 12.5),
                            ((800, 805, 810, 815, 820), 5.0),
                            ((800, 801, 802), 3.125)):
            with self.subTest(volts=volts):
                self.assertEqual(self.grain(volts), want)

    def test_the_step_rounds_half_to_even_on_the_exact_binary_value(self):
        self.assertEqual(round(6.125, 2), 6.12)  # an exact binary tie: to even
        self.assertEqual(self.grain((800.0, 806.125, 812.25, 818.375)), 6.12)
        self.assertEqual(self.grain((800.0, 806.375, 812.75)), 6.38)  # 6.375: to even, up

    def test_ties_between_steps_go_to_the_smallest(self):
        self.assertEqual(self.grain((800.0, 806.25, 812.5, 818.0, 823.5)), 5.5)

    def test_a_card_without_a_curve_has_no_grain(self):
        def scenario(side, fake, clock):
            side.seed()
            side.grain()
            side.grain()
        trace = self.run_both(scenario, **card())
        self.assertEqual([trace[0][1], trace[1][1]], [None, None])
        self.assertIsNone(trace[1][STATE_AT][0])


class RestoreTests(HoldParity):
    """restore_hold_headroom's texts on every branch."""

    def test_no_record(self):
        def scenario(side, fake, clock):
            side.seed()
            side.restore()
        self.assertEqual(self.results(scenario, **card())[0], (True, "no headroom to restore"))

    def test_unreadable_limits_keep_the_record(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.script["control"] = [-1] * 8
            side.restore()
        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (False, "cannot read the current limits to restore headroom"))
        self.assertIsNotNone(trace[1][HOLD])

    def test_a_record_for_a_rail_that_is_not_read(self):
        def scenario(side, fake, clock):
            side.seed(hold={"rail": 1, "prior_uv": {"vmin": 0}, "written_uv": {"vmin": 1}})
            side.restore()
        self.assertEqual(self.results(scenario, **card())[0],
                         (False, "cannot read the current limits to restore headroom"))

    def test_nothing_of_ours_left_drops_the_record(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.control[0] = [0, 0, 0, 0]
            fake.resync()
            side.restore()
        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (True, "headroom limits were changed since; nothing of ours to restore"))
        self.assertIsNone(trace[1][HOLD])

    def test_boost_unreadable_keeps_the_record(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.vboost_status = -1
            side.restore()
        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (False, "cannot preserve the current voltage boost; headroom left in place"))

    def test_not_seen_with_and_without_the_writers_text(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.scripts = [["unrelated"]]
            side.restore()
        trace = self.run_both(scenario, **card())
        self.assertTrue(trace[1][1][1].startswith(NOT_SEEN))

        def fallback(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.boost_fail_at = {fake.boost_reads + 2}  # restore's own read passes, the writer's fails
            side.restore()
        trace = self.run_both(fallback, **card())
        self.assertEqual(trace[1][1], (False, "the rails request was not seen"))

    def test_not_verified(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.store = lambda rail, values: [v + 1000 for v in values]
            side.restore()
        result = self.results(scenario, **card())[1]
        self.assertFalse(result[0])
        self.assertTrue(result[1].startswith("headroom restore not verified: rail 0 read-back disagrees"), result)

    def test_success_names_the_fields_in_the_records_order(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            side.restore()
        self.assertEqual(self.results(scenario, **card())[1],
                         (True, "core rail (NVVDD): reliability, alt-reliability restored after the hold"))

    def test_force_restores_a_field_that_landed_elsewhere(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)
            fake.control[0][1] += 6250
            fake.resync()
            side.restore(force=True)
        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (True, "core rail (NVVDD): reliability, alt-reliability restored after the hold"))
        self.assertEqual(trace[1][CONTROL][0], [0, 0, 0, 0])

    def test_a_record_set_from_outside_without_a_prior_raises_key_error(self):
        for hold, missing in (({"rail": 0, "prior_uv": {}, "written_uv": {"vmin": 0}}, "'vmin'"),
                              ({"rail": 0, "written_uv": {"vmin": 0}}, "'prior_uv'"),
                              ({"rail": 0, "prior_uv": {"vmin": 0}}, "'written_uv'")):
            with self.subTest(missing=missing):
                def scenario(side, fake, clock, hold=hold):
                    side.seed(hold=hold)
                    side.restore()
                result = self.results(scenario, **card())[0]
                self.assertEqual(result, ("raises", "KeyError", missing))


class StringAndOddInputTests(HoldParity):
    def test_hold_and_margin_are_converted_with_float(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply("1093.75", "25", label="text")
            side.restore()
            side.apply("x", 25.0)
            side.apply(1093.75, "x")
            side.apply(1093.75, None)
            side.apply(None, None)
            side.apply(True, 25.0)
        results = self.results(scenario, **card())
        self.assertTrue(results[0][0], results[0])
        self.assertEqual(results[2], (None, "hold voltage must be a number; no headroom applied"))
        self.assertEqual(results[3], (None, "hold, margin and bound must be numbers; no headroom applied"))
        self.assertEqual(results[4], results[3])
        self.assertEqual(results[5], results[2])
        self.assertTrue(results[6][0])  # 1.0 mV is below the ceiling: already clear

    def test_a_non_finite_hold(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(float("nan"), 25.0)
            side.apply(float("inf"), 25.0)
        for result in self.results(scenario, **card()):
            self.assertEqual(result, (None, "the current voltage ceiling cannot be estimated; no headroom applied"))

    def test_rails_without_the_fields_or_present(self):
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0, rail=1)
            side.apply(1093.75, 25.0, rail=7)
        results = self.results(scenario, **card())
        self.assertEqual(results, [(None, "the voltage limits could not be read right now; no headroom applied "
                                          "this time")] * 2)

    def test_live_readings_of_every_type(self):
        record = {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0, "raised_ceiling_mv": 1118.75,
                  "grain_mv": 6.25, "live_offset_mv": 0.0}

        def scenario(side, fake, clock):
            side.seed()
            for live in (True, 1120, 1130, 1121.875, "1130", float("nan"), -5.0, None):
                side.live_ok(record, live)
            side.live_ok(dict(record, rail=1.0))
            side.live_ok({k: v for k, v in record.items() if k != "rail"})
            side.live_ok(dict(record, grain_mv="x", live_offset_mv=5.0), 1125.0)
            side.live_ok(dict(record, grain_mv=0, live_offset_mv=None), 1121.875)
            side.live_ok(dict(record, raised_ceiling_mv="1110"), 1115.0)
            side.live_ok({"rail": 0, "hold_mv": "x"}, 1500.0)
            side.live_ok({"rail": 0, "hold_mv": 1000.0, "margin_mv": 10.0, "live_offset_mv": -3.0}, 1013.0)

        results = self.results(scenario, **card(nvvdd={0: 1200000}))
        self.assertEqual(results[0], (True, "live 1.00 mV"))
        self.assertEqual(results[1], (True, "live 1120.00 mV"))
        self.assertFalse(results[2][0])
        self.assertEqual(results[3], (True, "live 1121.88 mV"))
        self.assertEqual(results[4], (True, "no live rail reading"))
        self.assertEqual(results[5], (True, "no live rail reading"))
        self.assertEqual(results[6], (True, "no live rail reading"))
        self.assertFalse(results[7][0])  # the rail's own 1200 mV reading
        self.assertEqual(results[9], (True, "no live rail reading"))  # no "rail": no read, no reading
        self.assertEqual(results[10], (False, "the rail reads 1125.00 mV live, 6.25 mV above the 1118.75 mV "
                                              "ceiling the headroom raise set (this card's allowance is 3.12 mV)"))
        self.assertEqual(results[13], (True, "no bound to check against"))


class RailAvailabilityTests(HoldParity):
    def test_getters_that_answer_leave_nothing_to_report(self):
        def scenario(side, fake, clock):
            side.unavailable(0)
        self.assertIsNone(self.results(scenario, **card())[0])

    def test_getters_that_never_answered_on_this_adapter(self):
        def scenario(side, fake, clock):
            fake.script = {"control": [-104] * 40, "live": [-104] * 40}
            side.unavailable(0)
        kind, why = self.results(scenario, **card())[0]
        self.assertEqual(kind, "unanswered")
        self.assertIn("VoltRailsCtlGet status -104", why)
        self.assertIn("VoltRailsAbs status -104", why)

    def test_a_getter_not_exported_says_so(self):
        def scenario(side, fake, clock):
            side.unavailable(0)
        kind, why = self.results(scenario, with_control=False, **card())[0]
        self.assertEqual(kind, "unanswered")
        self.assertIn("VoltRailsCtlGet not exported", why)

    def test_getters_that_answered_earlier_and_fail_now_are_transient(self):
        def scenario(side, fake, clock):
            side.unavailable(0)
            fake.script = {"control": [-1] * 40, "live": [-1] * 40}
            side.unavailable(0)
        results = self.results(scenario, **card())
        self.assertIsNone(results[0])
        self.assertEqual(results[1][0], "transient")
        self.assertIn("status -1", results[1][1])

    def test_retry_detection_does_not_turn_a_failed_read_into_an_absent_rail(self):
        def scenario(side, fake, clock):
            side.unavailable(0)
            side.refresh()
            fake.script = {"control": [-1] * 40, "live": [-1] * 40}
            side.unavailable(0)
        self.assertEqual(self.results(scenario, **card())[2][0], "transient")

    def test_a_failed_read_is_retried_at_once_not_after_the_cadence(self):
        def scenario(side, fake, clock):
            fake.script = {"control": [-1] * 4, "live": []}
            side.supported(0)
            fake.script = {"control": [], "live": []}
            side.unavailable(0)
        self.assertIsNone(self.results(scenario, **card())[1])

    def test_other_rails_and_architectures(self):
        def scenario(side, fake, clock):
            side.unavailable(1)
            side.unavailable(7)
            side.unavailable(True)
        results = self.results(scenario, **card())
        self.assertEqual(results[0][0], "unanswered")
        options = card()
        options["card"] = {"arch": 99}
        unknown = self.results(lambda side, fake, clock: side.unavailable(0), **options)[0]
        self.assertEqual(unknown[0], "unanswered")  # no profile: never answered, not a transient


class AdoptForeignTests(HoldParity):
    def test_priors_replace_matching_raised_values_in_the_first_read(self):
        foreign = {"rail": 0, "written_uv": {"alt_reliability": 25000, "overvoltage": 5},
                   "prior_uv": {"alt_reliability": 0, "overvoltage": 0}}

        def scenario(side, fake, clock):
            side.seed()
            side.adopt(foreign)           # no first read yet: nothing
            side.limits()
            side.adopt(foreign)
            side.adopt({"rail": 0})
            side.adopt({"rail": 5, "written_uv": {}, "prior_uv": {}})
            side.reset(0)

        trace = self.run_both(scenario, **card(control={0: [0, 25000, 0, 0]}))
        self.assertEqual(trace[0][1], [])
        self.assertEqual(trace[2][1], ["alt_reliability"])
        self.assertEqual(trace[2][5]["poweron"][0], (0, 0, 0, 0))
        self.assertEqual([trace[3][1], trace[4][1]], [[], []])
        self.assertEqual(trace[5][CONTROL][0], [0, 0, 0, 0])  # Reset all goes below the other window's raise


# --------------------------------------------------------------------------------------------- #
class BindingContractTests(unittest.TestCase):
    """The native-only checks: the lock, the record round trip and the recorded divergences."""

    def setUp(self):
        self.rig = HoldRig(**card())
        self.addCleanup(self.rig.close)
        self.gpu = self.rig.native
        self.side = HoldNativeSide(self.gpu, self.rig)
        self.side.fake = self.rig.fake
        self.rig.fake.rig = self.rig

    def tearDown(self):
        native.hook_test_fail_protect(False)

    def test_constants_match_the_python_backend(self):
        self.assertEqual(nr.HOLD_HEADROOM_FIELDS, GPU.HOLD_HEADROOM_FIELDS)
        self.assertEqual(nr.HOLD_HEADROOM_FALLBACK_GRAIN_MV, GPU.HOLD_HEADROOM_FALLBACK_GRAIN_MV)

    def test_apply_and_restore_hold_the_handle_lock_through_the_write_and_the_hook(self):
        held = []

        def probe(fake, n):
            # another thread asking the core for anything on this handle waits for the call to end
            thread = threading.Thread(target=lambda: nr.hold_state(self.gpu))
            thread.start()
            thread.join(0.2)
            held.append(thread.is_alive())
            fake.threads.append(thread)
        self.rig.fake.after_set = probe
        self.rig.fake.threads = []
        self.side.seed()
        hook_held = []

        def hook(record):
            thread = threading.Thread(target=lambda: nr.hold_record(self.gpu))
            thread.start()
            thread.join(0.2)
            hook_held.append(thread.is_alive())
            self.rig.fake.threads.append(thread)
        self.assertTrue(self.side._armed(lambda: self.gpu.apply_hold_headroom(1093.75, 25.0, before_write=hook))[0])
        self.assertTrue(self.side._armed(self.gpu.restore_hold_headroom)[0])
        for thread in self.rig.fake.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual((held, hook_held), ([True, True], [True]))

    def test_the_fuller_record_round_trips_with_its_keys(self):
        full = {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0, "ceiling_mv": 1093.75, "boost_mv": 25.0,
                "live_offset_mv": 0.0, "grain_mv": None, "input_uv": {"reliability": 0, "alt_reliability": 0,
                                                                       "overvoltage": 0},
                "prior_uv": {"reliability": 0}, "written_uv": {"reliability": 25000}, "raised_ceiling_mv": 1118.75}
        for record in (full, {"rail": 0, "written_uv": {"vmin": 1}, "prior_uv": {"vmin": 0}},
                       {"rail": 1, "grain_mv": 5}):
            with self.subTest(keys=list(record)):
                self.gpu._hold_headroom = record
                got = self.gpu.hold_headroom_record()
                want = dict(record)
                if "grain_mv" in want and want["grain_mv"] is not None:
                    want["grain_mv"] = float(want["grain_mv"])
                self.assertEqual(set(got), set(want))
                self.assertEqual(got, want)
        self.assertEqual(list(self.gpu.hold_headroom_record()), ["rail", "grain_mv"])
        self.assertIsNot(self.gpu.hold_headroom_record(), self.gpu.hold_headroom_record())  # a new dict each call
        for bad in ({"rail": 0, "hold_mv": "1"}, {"rail": 0, "bogus": 1}, {"rail": 0, "input_uv": {"vmin": 1}},
                    {"rail": True}):
            with self.assertRaises(TypeError):
                self.gpu._hold_headroom = bad
        self.gpu._hold_headroom = None
        self.assertIsNone(self.gpu.hold_headroom_record())

    def test_the_state_attributes_round_trip(self):
        self.assertEqual(tuple(getattr(self.gpu, n) for n in STATE), STATE_DEFAULTS)
        self.gpu._hold_headroom_grain = 5
        self.gpu._hold_headroom_offset_mv = 12.5
        self.gpu._hold_headroom_unread = 1
        self.gpu._hold_headroom_live_trip = True
        self.assertEqual(tuple(getattr(self.gpu, n) for n in STATE), (5.0, 12.5, True, True))
        self.gpu._hold_headroom_live_trip = False   # as druta.py clears it
        self.gpu._hold_headroom_grain = None
        self.assertEqual(tuple(getattr(self.gpu, n) for n in STATE), (None, 12.5, True, False))

    def test_recorded_divergences(self):
        fake = self.rig.fake
        self.side.seed()
        # a rail equal to a rail but not a plain int is refused before any read
        for rail in (True, 1.0):
            fake.calls.clear()
            with self.assertRaises(TypeError):
                self.gpu.apply_hold_headroom(1093.75, 25.0, rail)
            self.assertEqual(fake.calls, [])
        # float(10 ** 400) raises OverflowError before any read (Python raises it after its reads)
        fake.calls.clear()
        with self.assertRaises(OverflowError):
            self.gpu.apply_hold_headroom(10 ** 400, 25.0)
        self.assertEqual(fake.calls, [])
        # the record's own rail's row without every field is refused (Python skips the missing keys)
        self.gpu._hold_headroom = {"rail": 0, "written_uv": {"vmin": 0}, "prior_uv": {"vmin": 0}}
        with self.assertRaises(TypeError):
            self.gpu.hold_headroom_overwritten({0: {"vmin": 0.0}})
        # a record set from outside keeps its keys but comes back in apply_hold_headroom's key order
        self.gpu._hold_headroom = {"rail": 0, "written_uv": {"vmin": 1}, "prior_uv": {"vmin": 0}}
        self.assertEqual(list(self.gpu.hold_headroom_record()), ["rail", "prior_uv", "written_uv"])
        # adopt_foreign_hold_headroom skips a prior that is not an int32 (Python would store it)
        nr.read_volt_rail_limits(self.gpu)
        self.assertEqual(self.gpu.adopt_foreign_hold_headroom(
            {"rail": 0, "written_uv": {"vmin": 0}, "prior_uv": {"vmin": 0.5}}), [])

    def test_abi_arguments(self):
        library = nr.load()
        handle = self.gpu._handle
        outcome, writes = nr._new(nr._HoldOutcome), nr._new(nr._SetterWrites)
        bad = nr._new(nr._HoldOutcome)
        bad.size = 4
        for status in (library.dc_rails_hold_apply(handle, 1.0, 1.0, 4, 0, 0, nr.BEFORE_WRITE(), None,
                                                   ctypes.byref(outcome), ctypes.byref(writes)),
                       library.dc_rails_hold_apply(handle, 1.0, 1.0, 0, 0, 0, nr.BEFORE_WRITE(), None,
                                                   ctypes.byref(bad), ctypes.byref(writes)),
                       library.dc_rails_hold_restore(handle, 0, None, ctypes.byref(writes)),
                       library.dc_rails_initial_get(handle, 2, ctypes.byref(ctypes.c_int32()),
                                                    (ctypes.c_int32 * 4)())):
            self.assertEqual(status, native.DC_E_ARGUMENT)
        record = nr._new(nr._HoldRecord)
        record.present, record.keys = 1, 0x200
        self.assertEqual(library.dc_rails_hold_record_set(handle, ctypes.byref(record)), native.DC_E_ARGUMENT)


# --------------------------------------------------------------------------------------------- #
# The review's fixes (R0 to R2, W0, A3) and its test additions (T0 to T6), each run on both sides.
FULL_ROW = {"reliability": 0.0, "alt_reliability": 0.0, "overvoltage": 0.0, "vmin": 0.0}
OUTSIDE = {"rail": 0, "prior_uv": {"reliability": 0}, "written_uv": {"reliability": 1}}
UNTRACKED = "the crash marker could not be written, so a crash could leave the raise untracked; no headroom applied"


class ReviewParityTests(HoldParity):
    def test_a_float_key_an_outside_record_lacks_reads_as_nan(self):
        """R0: live_ok on the ACTIVE record without hold_mv (or margin_mv) has no bound, as Python's
        KeyError gives; the record still reads back with exactly the keys it was given."""
        for given in (dict(OUTSIDE, margin_mv=25.0), dict(OUTSIDE, hold_mv=1093.75)):
            record = {"rail": 0}
            record.update((k, given[k]) for k in ("hold_mv", "margin_mv") if k in given)
            record.update(prior_uv=given["prior_uv"], written_uv=given["written_uv"])
            with self.subTest(keys=list(record)):
                def scenario(side, fake, clock, record=record):
                    side.seed(hold=record)
                    side.live_ok(None, 1100.0)
                    side.record()
                trace = self.run_both(scenario, **card())
                self.assertEqual(trace[0][1], (True, "no bound to check against"))
                self.assertEqual(trace[1][1], record)

    def test_live_ok_converts_a_records_numbers_in_pythons_order(self):
        """R1: hold_mv / margin_mv are converted only when raised_ceiling_mv is not finite, and the
        allowance's OverflowError is raised only when the allowance is consulted."""
        nan = float("nan")

        def scenario(side, fake, clock):
            side.seed()
            side.live_ok({"rail": 0, "raised_ceiling_mv": 1118.75, "hold_mv": 10 ** 400, "margin_mv": 1}, 1100.0)
            side.live_ok({"rail": 0, "raised_ceiling_mv": nan, "grain_mv": 10 ** 400}, None)
            side.live_ok({"rail": 0, "raised_ceiling_mv": 1118.75, "grain_mv": 10 ** 400}, 1100.0)
            side.live_ok({"rail": 0, "raised_ceiling_mv": 10 ** 400}, 1100.0)
            side.live_ok({"rail": 0, "raised_ceiling_mv": nan, "hold_mv": 1093.75, "margin_mv": 10 ** 400}, 1100.0)
            side.live_ok({"rail": 0, "raised_ceiling_mv": 1118.75, "live_offset_mv": 10 ** 400}, 0.0)

        results = self.results(scenario, **card())
        self.assertEqual(results[0], (True, "live 1100.00 mV"))
        self.assertEqual(results[1], (True, "no bound to check against"))
        for i in (2, 3, 4):
            self.assertEqual(results[i][:2], ("raises", "OverflowError"), i)
        self.assertEqual(results[5], (True, "no live rail reading"))

    def test_overwritten_looks_only_at_the_records_rail_in_pythons_order(self):
        """R2: a None row, other rails' rows (None or incomplete), an empty or non-dict limits, and
        no record at all answer False or are ignored, as Python does."""
        def scenario(side, fake, clock):
            side.seed(hold=OUTSIDE)
            side.overwritten({0: None})
            side.overwritten({0: FULL_ROW, 1: None})
            side.overwritten({0: FULL_ROW, 1: {"vmin": 0.0}})
            side.overwritten({0: dict(FULL_ROW, reliability=0.001), 1: {"vmin": 0.0}})
            side.overwritten({})
            side.overwritten(5)
            side.overwritten([None])
            side._set_hold(None)
            side.overwritten({0: None})
            side.overwritten({0: {"vmin": 0.0}})

        results = self.results(scenario, **card())
        self.assertEqual(results, [False, True, True, False, False, False, False, False, False])

    def test_an_incomplete_row_for_the_records_rail_is_a_recorded_divergence(self):
        """R2's remaining divergence: the record's own rail row missing fields. Python compares the
        fields it has (False here); the binding raises TypeError instead of guessing the rest."""
        def scenario(side, fake, clock):
            side.seed(hold=OUTSIDE)
            side.overwritten({0: {"vmin": 0.0}})

        rig = HoldRig(**card())
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertIs(python[0][1], False)
        self.assertEqual(core[0][1][:2], ("raises", "TypeError"))

    def test_a_plan_outside_int32_is_refused_without_the_hook(self):
        """W0: a corrupt reading puts the planned reliability beyond int32. Both sides refuse the
        write with the same text and keep no record; Python's hook sees the true int, the core does
        not call the hook (it would have seen a wrapped value)."""
        word = -2147400000
        control = deltas("turing", 1093.75, 1200.0, 1200.0, boost_mv=word / 1000)
        seen = {}

        def scenario(side, fake, clock):
            side.seed()
            name = type(side).__name__
            side.apply(1093.75, 100.0, answer=lambda record: seen.setdefault(name, []).append(copy.deepcopy(record)))

        rig = HoldRig(**card(control={0: control}, boost=word))
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        refusal = (False, "Could not preserve the current native rail record: rail 0 record values must be "
                          "signed int32 integers")
        for trace in (python, core):
            self.assertEqual(trace[0][1], refusal)
            self.assertIsNone(trace[0][HOLD])
            self.assertEqual(set_count(trace[0]), 0)
        self.assertEqual(python[0][6][0], core[0][6][0])  # the writer's error text
        self.assertEqual(python[0][STATE_AT], core[0][STATE_AT])
        self.assertNotIn("HoldNativeSide", seen)
        self.assertEqual(seen["HoldPythonSide"][0]["written_uv"], {"reliability": 2147525000})

    def test_adopt_skips_a_prior_the_first_read_cannot_hold(self):
        """A3: a corrupt foreign marker's prior (2**40) is not adopted natively and nothing raises;
        the good field is adopted on both sides."""
        foreign = {"rail": 0, "written_uv": {"reliability": 0, "alt_reliability": 25000},
                   "prior_uv": {"reliability": 2 ** 40, "alt_reliability": 0}}

        def scenario(side, fake, clock):
            side.seed()
            side.limits()
            side.adopt(foreign)

        rig = HoldRig(**card(control={0: [0, 25000, 0, 0]}))
        self.addCleanup(rig.close)
        python, core = rig.run(scenario)
        self.assertEqual(python[1][1], ["reliability", "alt_reliability"])
        self.assertEqual(python[1][5]["poweron"][0], (2 ** 40, 0, 0, 0))
        self.assertEqual(core[1][1], ["alt_reliability"])
        self.assertEqual(core[1][5]["poweron"][0], (0, 0, 0, 0))  # the first read keeps reliability

    def test_getters_that_answered_before_any_reference_are_transient(self):
        """T0: both getters answered once (before a reference was captured), then fail."""
        def scenario(side, fake, clock):
            fake.script = {"control": [None, None] + [-1] * 40, "live": [None] + [-1] * 40}
            side.unavailable(0)
            side.unavailable(0)

        for result in self.results(scenario, **card()):
            self.assertEqual(result[0], "transient")
            self.assertTrue(result[1].endswith("(VoltRailsCtlGet status -1; VoltRailsAbs status -1)"), result)

    def test_current_limits_unread_is_a_transient_refusal(self):
        """T1: the support check passes, then the plan's own limits read fails."""
        def scenario(side, fake, clock):
            side.seed()
            side.limits()
            fake.script["control"] = [None] + [-1] * 8
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card())
        self.assertEqual(trace[1][1], (None, "the current limits could not be read right now; no headroom applied"))
        self.assertEqual(trace[1][STATE_AT], (None, None, True, False))
        self.assertEqual(set_count(trace[1]), 0)

    def test_a_live_trip_whose_undo_fails_keeps_the_record(self):
        """T2: the live check trips and its rollback is refused: RESTORE FAILED, record kept."""
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, nvvdd_after={0: 1125000}, set_rms=[0, 0x1F]))
        ok, text = trace[0][1]
        self.assertIs(ok, False)
        self.assertTrue(text.startswith("the rail reads 1125.00 mV live, "), text)
        self.assertTrue(text.endswith("; RESTORE FAILED: headroom restore refused (NV_STATUS 0x1F)"), text)
        self.assertIsNotNone(trace[0][HOLD])
        self.assertTrue(trace[0][STATE_AT][3])

    def test_a_live_trip_forces_back_a_field_that_moved_before_the_rollback(self):
        """T2: between the verify and the rollback's read alt-reliability moves; the forced restore
        still puts every recorded field back."""
        state = {"n": 0}

        def on_read(fake):
            if fake.set_count == 1:
                state["n"] += 1
                if state["n"] == 2:
                    fake.control[0][1] += 6250
                    fake.resync()

        def scenario(side, fake, clock):
            state["n"] = 0
            side.seed()
            fake.on_read = on_read
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(nvvdd={0: 1093750}, nvvdd_after={0: 1125000}))
        ok, text = trace[0][1]
        self.assertIsNone(ok, text)
        self.assertTrue(text.startswith("the rail reads 1125.00 mV live, 6.25 mV above the 1118.75 mV ceiling"), text)
        self.assertTrue(text.endswith("; raise undone"), text)
        self.assertEqual(trace[0][CONTROL][0], [0, 0, 0, 0])
        self.assertIsNone(trace[0][HOLD])

    def test_the_ceiling_tolerance(self):
        """T3: a hold up to 1e-3 mV above the ceiling counts as at it."""
        c = ceiling_of("turing")
        for hold, want in ((c, True), (c + 0.0005, True), (c + 0.001, True), (c + 0.002, None), (c + 0.5, None)):
            with self.subTest(hold=hold):
                def scenario(side, fake, clock, hold=hold):
                    side.seed()
                    side.apply(hold, 25.0)
                self.assertIs(self.results(scenario, **card())[0][0], want)

    def test_an_offset_equal_to_the_margin_is_applied(self):
        """T4: the rail reads exactly the margin above its ceiling before the raise: applied, with
        the note; one microvolt more is refused."""
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1093.75, 25.0)

        ok, text = self.results(scenario, **card(nvvdd={0: 1118750}))[0]
        self.assertIs(ok, True, text)
        self.assertIn("25.00 mV above the ceiling before the raise", text)
        ok, text = self.results(scenario, **card(nvvdd={0: 1118751}))[0]
        self.assertIsNone(ok, text)

    def test_a_replan_over_a_record_whose_restore_raises_key_error(self):
        """T5: the re-plan's restore raises KeyError('prior_uv') on both sides, with the same reads."""
        def scenario(side, fake, clock):
            side.seed(hold={"rail": 0, "written_uv": {"reliability": 25000}})
            side.apply(1093.75, 25.0)

        trace = self.run_both(scenario, **card(control={0: [25000, 0, 0, 0]}))
        self.assertEqual(trace[0][1], ("raises", "KeyError", "'prior_uv'"))
        self.assertEqual(set_count(trace[0]), 0)

    def test_apply_live_check_overwritten_and_restore_on_rail_1(self):
        """T6: the MSVDD rail end to end."""
        def scenario(side, fake, clock):
            side.seed()
            side.apply(1000.0, 25.0, rail=1)
            side.live_ok()
            side.overwritten()
            side.restore()

        trace = self.run_both(scenario, **card("blackwell", nvvdd={1: 1000000}))
        ok, text = trace[0][1]
        self.assertIs(ok, True, text)
        self.assertTrue(text.startswith("MSVDD: reliability 1005.00 mV while holding 1000.00 mV"), text)
        self.assertEqual(trace[0][HOLD]["rail"], 1)
        self.assertEqual(trace[1][1], (True, "live 1000.00 mV"))
        self.assertIs(trace[2][1], False)
        self.assertEqual(trace[3][1][0], True)
        self.assertTrue(trace[3][1][1].startswith("MSVDD: reliability restored after the hold"), trace[3][1])
        self.assertEqual(trace[3][CONTROL][1], [-50000, 0, 0, 0])


class HookAndStateAbiTests(BindingContractTests):
    """A0, A2 and A1 through the raw ABI and the attributes (native only)."""

    def raw_apply(self, answer):
        """dc_rails_hold_apply(1093.75, 25.0) with a raw callback answering answer() (or raising)."""
        library = nr.load()
        self.side.seed()
        fake = self.rig.fake
        before = fake.set_count
        callback = nr.BEFORE_WRITE(lambda context, record, error, cap: answer())
        out, writes = nr._new(nr._HoldOutcome), nr._new(nr._SetterWrites)
        swallowed, hook = [], sys.unraisablehook
        sys.unraisablehook = lambda unraisable, swallowed=swallowed: swallowed.append(type(unraisable.exc_value))
        try:
            status = self.side._armed(lambda: library.dc_rails_hold_apply(
                self.gpu._handle, 1093.75, 25.0, 0, 0, 0, callback, None, ctypes.byref(out), ctypes.byref(writes)))
        finally:
            sys.unraisablehook = hook
        return status, out, fake.set_count - before, swallowed

    def test_the_hook_fails_closed(self):
        def raises():
            raise RuntimeError("the marker could not be written")
        for label, answer in (("raises", raises), ("0", lambda: 0), ("7", lambda: 7), ("-1", lambda: -1),
                              ("FALSE", lambda: 2)):
            with self.subTest(answer=label):
                status, out, sets, swallowed = self.raw_apply(answer)
                self.assertEqual(status, native.DC_OK)
                self.assertEqual((out.ok, out.message.decode()), (0, UNTRACKED))
                self.assertEqual(sets, 0)
                self.assertIsNone(self.gpu.hold_headroom_record())
                self.assertIs(self.gpu._hold_headroom_unread, True)
                self.assertEqual(swallowed, [RuntimeError] if label == "raises" else [])
        status, out, sets, _ = self.raw_apply(lambda: 1)  # PROCEED
        self.assertEqual((status, out.ok, sets), (native.DC_OK, 1, 1))
        self.assertIsNotNone(self.gpu.hold_headroom_record())

    def test_an_abort_reports_none(self):
        status, out, sets, _ = self.raw_apply(lambda: 4)
        self.assertEqual((status, out.ok, out.aborted, out.message), (native.DC_OK, 0, 1, b""))
        self.assertEqual(sets, 0)
        self.assertIsNone(self.gpu.hold_headroom_record())

    def test_one_state_attribute_leaves_the_other_three(self):
        values = {"_hold_headroom_grain": 5.0, "_hold_headroom_offset_mv": 12.5, "_hold_headroom_unread": True,
                  "_hold_headroom_live_trip": True}
        for name in STATE:
            with self.subTest(name=name):
                for other, value in values.items():
                    setattr(self.gpu, other, value)
                setattr(self.gpu, name, dict(zip(STATE, STATE_DEFAULTS))[name])
                for other, value in values.items():
                    self.assertEqual(getattr(self.gpu, other), dict(zip(STATE, STATE_DEFAULTS))[other]
                                     if other == name else value, other)
        state = nr._new(nr._HoldState)
        self.assertEqual(nr.load().dc_rails_hold_state_set(self.gpu._handle, 0x10, ctypes.byref(state)),
                         native.DC_E_ARGUMENT)

    def test_another_threads_update_is_not_written_back(self):
        """The verifier's interleaving: another thread changes offset and unread while an attribute
        is assigned (injected where the old read-modify-write read the state)."""
        library = nr.load()

        def other():
            value = nr._new(nr._HoldState)
            value.has_offset, value.offset_mv, value.unread = 1, 12.5, 1
            library.dc_rails_hold_state_set(self.gpu._handle, 0x2 | 0x4, ctypes.byref(value))

        real = nr.hold_state

        def interleaved(gpu):
            state = real(gpu)
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(5)
            return state

        with patch.object(nr, "hold_state", interleaved), patch.object(nr, "_new", wraps=nr._new) as made:
            self.gpu._hold_headroom_live_trip = True
            other()  # also between two assignments
            self.gpu._hold_headroom_live_trip = False
        self.assertTrue(made.called)
        self.assertEqual(tuple(getattr(self.gpu, n) for n in STATE), (None, 12.5, True, False))


if __name__ == "__main__":
    unittest.main()
