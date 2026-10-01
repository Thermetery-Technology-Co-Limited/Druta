# Druta - hardware-free tests for V/F hold headroom.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""A hold on the effective voltage ceiling delivers less clock than it
programs (measured on one TU102). These tests cover the software around the
fix: which ceiling terms are raised and to what, that the voltage stays bounded
(holds above the ceiling are withheld, the raise always comes off while the
lock still holds the point, the live rail is watched against the raised
ceiling - it may use the driver's margin above the point, no more), that the raise
is recorded and undone exactly, and that crash evidence is kept, owned and
never mistaken for a live window's raise. Card values are fixtures for several
generations, not constants."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from druta import profiles, vfheadroom
from druta.nvbackend import GPU, PRIV_CONFIRMED

# (architecture, first-read bases rel/alt/ov/vmin, boost contribution at 100 %)
CARDS = {
    "turing": (GPU.ARCH_TURING, (1068.75, 1093.75, 1125.0, 643.75), 25.0),
    "pascal": (GPU.ARCH_PASCAL, (1068.75, 1093.75, 1200.0, 650.0), 6.25),
    "blackwell": (10, (1040.0, 1060.0, 1200.0, 800.0), 20.0),
}


def row(rel, alt, ov, vmin=643.75, boost=0.0, base=(1068.75, 1093.75, 1125.0, 643.75),
        effective=None):
    """One rail row as read_volt_rail_limits returns it: deltas against bases."""
    keys = GPU.VOLT_LIMIT_FIELDS
    absolute = dict(zip(keys, (rel, alt, ov, vmin)))
    bases = dict(zip(keys, base))
    out = {k: absolute[k] - bases[k] for k in keys}
    out.update(_base_mv=bases, _boost_mv=boost, _effective_mv=effective)
    return out


def stock_row(card):
    arch, base, boost = CARDS[card]
    return row(*base, boost=boost, base=base)


class FakeRails:
    """In-memory rail store standing in for the driver: writes land as exact
    microvolt deltas and read back unchanged unless told otherwise. Optionally
    two rails, a live NVVDD reading and a chosen architecture."""

    def __init__(self, gpu, first, arch=GPU.ARCH_TURING, second=None):
        self.gpu = gpu
        self.rows = {0: first}
        if second is not None:
            self.rows[1] = second
        self.writes = []
        self.live = None                  # the live NVVDD reading
        self.live_after = None            # ... once a write has landed, if different
        self.effective_after = None       # the card's own effective limit after a write
        self.corrupt_next_write = False
        gpu.read_volt_rail_limits = self.read
        gpu._write_rail_records = self.write
        gpu._verify_rail_records = self.verify
        gpu.volt_rail_limits_supported = lambda rail=None: True
        gpu.volt_rail_limit_fields = lambda rail: GPU.VOLT_LIMIT_FIELDS
        gpu.read_voltage_boost = lambda: 100
        gpu.read_rail_live_mv = self.read_live
        gpu.read_vf_curve = lambda: (None, "no curve in this fake")
        gpu.voltage_xoc_enabled = False
        gpu.arch = lambda: arch

    def read_live(self, rail):
        if rail != 0:
            return None
        return self.live_after if self.writes and self.live_after is not None else self.live

    def read(self):
        return {r: dict(v, _base_mv=dict(v["_base_mv"])) for r, v in self.rows.items()}

    def write(self, records):
        self.writes.append(records)
        for rail, values in records.items():
            values = list(values)
            if self.corrupt_next_write:
                values[1] += 6250
                self.corrupt_next_write = False
            for key, uv in zip(GPU.VOLT_LIMIT_FIELDS, values):
                self.rows[rail][key] = uv / 1000.0
            if self.effective_after is not None:
                self.rows[rail]["_effective_mv"] = self.effective_after
        return True, 0

    def verify(self, records, boost):
        back = self.read()
        for rail, wanted in records.items():
            got = [int(round(back[rail][k] * 1000)) for k in GPU.VOLT_LIMIT_FIELDS]
            if got != wanted:
                return None, f"rail {rail} read-back disagrees"
        return back, None

    def absolute(self, key, rail=0):
        return GPU.abs_limit_mv(self.rows[rail], key)

    def ceiling_terms(self, rail=0):
        return tuple(self.absolute(k, rail) for k in ("reliability", "alt_reliability", "overvoltage"))


def bare_gpu():
    gpu = GPU.__new__(GPU)
    gpu._lock = threading.RLock()                 # as GPU.__init__ gives every card
    return gpu


# --------------------------------------------------------------------------- #
class PlannerTests(unittest.TestCase):
    def test_every_ceiling_term_is_raised_and_boost_is_not_counted_twice(self):
        # Stock TU102 first-read limits at 100 % boost: reliability 1068.75 +
        # 25 mV contribution is the binding term at 1093.75.
        targets, why = GPU.hold_headroom_targets(
            row(1068.75, 1093.75, 1125.0, boost=25.0), 1093.75, 25.0, 1200.0)
        self.assertIsNone(why)
        self.assertEqual(targets, {"reliability": 1093.75, "alt_reliability": 1118.75})

    def test_reliability_alone_binding_is_still_raised(self):
        targets, _ = GPU.hold_headroom_targets(
            row(1068.75, 1150.0, 1150.0, boost=25.0), 1093.75, 25.0, 1200.0)
        self.assertEqual(targets, {"reliability": 1093.75})

    def test_nothing_is_raised_when_the_ceiling_already_clears_the_hold(self):
        self.assertEqual(GPU.hold_headroom_targets(row(1175.0, 1175.0, 1175.0), 1150.0, 25.0, 1200.0),
                         ({}, None))

    def test_a_field_already_higher_is_never_lowered(self):
        targets, _ = GPU.hold_headroom_targets(row(1068.75, 1180.0, 1125.0), 1100.0, 25.0, 1200.0)
        self.assertEqual(targets, {"reliability": 1125.0})

    def test_plan_is_refused_above_the_bound_and_on_bad_input(self):
        for hold, margin, why_part in ((1190.0, 25.0, "exceeds"), (1100.0, 0.0, "positive"),
                                       (float("nan"), 25.0, "finite"), ("x", 25.0, "numbers")):
            with self.subTest(hold=hold, margin=margin):
                targets, why = GPU.hold_headroom_targets(row(1068.75, 1093.75, 1125.0), hold, margin, 1200.0)
                self.assertIsNone(targets)
                self.assertIn(why_part, why)

    def test_missing_or_unreadable_term_refuses_instead_of_guessing(self):
        partial = row(1068.75, 1093.75, 1125.0)
        del partial["overvoltage"]
        self.assertIsNone(GPU.hold_headroom_targets(partial, 1093.75, 25.0, 1200.0)[0])
        nobase = row(1068.75, 1093.75, 1125.0)
        nobase["_base_mv"] = {}
        self.assertIsNone(GPU.hold_headroom_targets(nobase, 1093.75, 25.0, 1200.0)[0])

    def test_ceiling_is_the_lower_of_estimate_and_the_cards_effective_limit(self):
        self.assertEqual(GPU.hold_headroom_ceiling_mv(row(1068.75, 1093.75, 1125.0, boost=25.0)), 1093.75)
        self.assertEqual(GPU.hold_headroom_ceiling_mv(
            row(1068.75, 1093.75, 1125.0, boost=25.0, effective=1087.5)), 1087.5)
        # a higher 'effective' never lifts the estimate
        self.assertEqual(GPU.hold_headroom_ceiling_mv(
            row(1068.75, 1093.75, 1125.0, boost=25.0, effective=1100.0)), 1093.75)


# --------------------------------------------------------------------------- #
class ApplyRestoreTests(unittest.TestCase):
    """Rail-level behaviour, for every generation with rail control."""

    def make(self, card="turing", second=False):
        gpu = bare_gpu()
        arch, base, boost = CARDS[card]
        rails = FakeRails(gpu, stock_row(card), arch=arch,
                          second=stock_row(card) if second else None)
        return gpu, rails, base, boost

    def test_apply_raises_the_ceiling_and_restore_puts_it_back_on_each_generation(self):
        for card in CARDS:
            with self.subTest(card=card):
                gpu, rails, base, boost = self.make(card, second=True)
                ceiling = min(base[0] + boost, base[1], base[2])
                before = rails.ceiling_terms()
                ok, msg = gpu.apply_hold_headroom(ceiling, 25.0)
                self.assertTrue(ok, msg)
                self.assertEqual(GPU.rail_ceiling_mv(rails.rows[0]), ceiling + 25.0)
                self.assertEqual(rails.ceiling_terms(1), (base[0], base[1], base[2]))   # rail 1 untouched
                self.assertEqual(rails.absolute("vmin"), base[3])
                ok, msg = gpu.restore_hold_headroom()
                self.assertTrue(ok, msg)
                self.assertEqual(rails.ceiling_terms(), before)
                self.assertIsNone(gpu.hold_headroom_record())

    def test_hold_above_the_ceiling_is_withheld_on_each_generation(self):
        for card in CARDS:
            with self.subTest(card=card):
                gpu, rails, base, boost = self.make(card)
                ceiling = min(base[0] + boost, base[1], base[2])
                ok, msg = gpu.apply_hold_headroom(ceiling + 6.25, 25.0)
                self.assertIsNone(ok)
                self.assertIn("above the voltage ceiling", msg)
                self.assertEqual(rails.writes, [])

    def test_card_effective_limit_below_the_estimate_withholds(self):
        gpu, rails, *_ = self.make()
        rails.rows[0]["_effective_mv"] = 1087.5
        self.assertIsNone(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        self.assertEqual(rails.writes, [])

    def test_apply_does_not_require_the_free_form_limit_switch(self):
        gpu, *_ = self.make()
        gpu.volt_limits_write_enabled = False
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])

    def test_already_clear_ceiling_writes_nothing_and_records_nothing(self):
        gpu, rails, *_ = self.make()
        rails.rows[0] = row(1175.0, 1175.0, 1175.0)
        self.assertTrue(gpu.apply_hold_headroom(1100.0, 25.0)[0])
        self.assertEqual(rails.writes, [])
        self.assertIsNone(gpu.hold_headroom_record())

    def test_a_second_hold_replaces_the_first_raise_instead_of_stacking(self):
        gpu, rails, *_ = self.make()
        gpu.apply_hold_headroom(1093.75, 25.0)
        gpu.apply_hold_headroom(1087.5, 25.0)
        self.assertEqual((rails.absolute("reliability"), rails.absolute("alt_reliability")), (1087.5, 1112.5))
        gpu.restore_hold_headroom()
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

    def test_above_ceiling_hold_still_restores_a_previous_raise(self):
        gpu, rails, *_ = self.make()
        gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertIsNone(gpu.apply_hold_headroom(1125.0, 25.0)[0])
        self.assertIsNone(gpu.hold_headroom_record())
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

    def test_restore_first_step_is_retried_once(self):
        gpu, rails, *_ = self.make()
        gpu.apply_hold_headroom(1093.75, 25.0)
        real, calls = gpu.restore_hold_headroom, []
        def flaky(force=False):
            calls.append(1)
            return (False, "interrupted") if len(calls) == 1 else real(force)
        gpu.restore_hold_headroom = flaky
        self.assertTrue(gpu.apply_hold_headroom(1087.5, 25.0)[0])
        self.assertEqual(len(calls), 2)

    def test_record_carries_the_ceiling_and_boost_it_was_planned_against(self):
        gpu, *_ = self.make()
        gpu.apply_hold_headroom(1093.75, 25.0)
        rec = gpu.hold_headroom_record()
        self.assertEqual((rec["ceiling_mv"], rec["boost_mv"]), (1093.75, 25.0))

    def test_write_ahead_hook_sees_the_plan_before_the_set(self):
        gpu, rails, *_ = self.make()
        seen = []
        gpu.apply_hold_headroom(1093.75, 25.0, before_write=lambda r: seen.append((len(rails.writes), r)))
        self.assertEqual(seen[0][0], 0)
        self.assertEqual(seen[0][1]["written_uv"], gpu.hold_headroom_record()["written_uv"])

    def test_live_rail_above_the_raised_ceiling_undoes_the_raise(self):
        gpu, rails, *_ = self.make()
        rails.live, rails.live_after = 1093.75, 1125.0   # beyond hold + margin once raised
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertIsNone(ok)                            # undone: nothing of it is left
        self.assertIn("above the 1118.75 mV ceiling the headroom raise set", msg)
        self.assertIn("raise undone", msg)
        self.assertIsNone(gpu.hold_headroom_record())
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

    def test_rail_margin_above_the_old_ceiling_is_expected_and_kept(self):
        # observed on one TU102 (610.88): holding 1093.75 on the ceiling the rail
        # read 1093.75; under a 1118.75 ceiling it ran 1112.5
        gpu, rails, *_ = self.make()
        rails.live, rails.live_after = 1093.75, 1112.5
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        record = gpu.hold_headroom_record()
        self.assertEqual((record["raised_ceiling_mv"], record["live_offset_mv"]), (1118.75, 0.0))
        self.assertTrue(gpu.hold_headroom_live_ok(record, live_mv=1112.5)[0])

    def test_a_card_reading_above_its_own_ceiling_keeps_that_offset_only(self):
        # a live field that reads 12.5 mV above a binding limit (as a GA104's
        # did above its vmin) must not undo every raise - nor excuse more
        gpu, rails, *_ = self.make()
        rails.live, rails.live_after = 1106.25, 1131.25
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertTrue(ok, msg)
        self.assertIn("12.50 mV above the ceiling before the raise", msg)
        record = gpu.hold_headroom_record()
        self.assertEqual(record["live_offset_mv"], 12.5)
        self.assertFalse(gpu.hold_headroom_live_ok(record, live_mv=1131.25 + 6.25)[0])

    def test_an_offset_past_the_margin_is_not_raised_on(self):
        gpu, rails, *_ = self.make()
        rails.live = 1093.75 + 31.25
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertIsNone(ok)
        self.assertIn("could not be checked on this card", msg)
        self.assertEqual(rails.writes, [])

    def test_the_live_check_uses_this_cards_own_voltage_grain(self):
        gpu, rails, *_ = self.make()
        gpu.read_vf_curve = lambda: ([{"volt_mv": v} for v in (800.0, 805.0, 810.0, 815.0)], None)
        rails.live = 1093.75
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        record = gpu.hold_headroom_record()
        self.assertEqual(gpu.hold_headroom_allowance_mv(record), 2.5)
        self.assertTrue(gpu.hold_headroom_live_ok(record, live_mv=1118.75 + 2.5)[0])
        self.assertFalse(gpu.hold_headroom_live_ok(record, live_mv=1118.75 + 3.0)[0])

    def test_without_a_curve_the_fallback_grain_is_used_and_said(self):
        gpu, rails, *_ = self.make()
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertTrue(ok)
        self.assertIn("uses a 6.25 mV grain", msg)
        self.assertIn("no live rail reading", msg)
        self.assertEqual(gpu.hold_headroom_allowance_mv(), 3.125)

    def test_the_cards_own_enforced_limit_counts_as_holding(self):
        # an off-grid margin the card enforces one step up: its effective limit
        # says so, and a rail at that limit is not a clamp failure
        gpu, rails, *_ = self.make()
        rails.live, rails.live_after, rails.effective_after = 1093.75, 1106.25, 1106.25
        ok, msg = gpu.apply_hold_headroom(1093.75, 7.0)
        self.assertTrue(ok, msg)
        self.assertEqual(gpu.hold_headroom_record()["raised_ceiling_mv"], 1106.25)

    def test_a_zero_live_reading_is_no_reading(self):
        gpu, rails, *_ = self.make()
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        self.assertEqual(gpu.hold_headroom_live_ok(live_mv=0.0), (True, "no live rail reading"))

    def test_a_non_finite_raised_ceiling_falls_back_to_hold_plus_margin(self):
        gpu, rails, *_ = self.make()
        record = {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0,
                  "raised_ceiling_mv": float("nan")}
        self.assertEqual(gpu.hold_headroom_bound_mv(record), 1118.75)
        self.assertFalse(gpu.hold_headroom_live_ok(record, live_mv=1300.0)[0])

    def test_an_unread_absolute_state_is_not_planned_on(self):
        gpu, rails, *_ = self.make()
        rails.rows[0]["_absolute_ok"] = False        # boost and effective are placeholders
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertIsNone(ok)
        self.assertIn("absolute state could not be read", msg)
        self.assertEqual(rails.writes, [])

    def test_an_unread_absolute_state_on_read_back_bounds_at_hold_plus_margin(self):
        gpu, rails, *_ = self.make()
        rails.effective_after = 1200.0
        read = rails.read

        def read_back_without_state():
            rows = read()
            if rails.writes:
                rows[0]["_absolute_ok"] = False
            return rows
        rails.read = gpu.read_volt_rail_limits = read_back_without_state
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        self.assertEqual(gpu.hold_headroom_record()["raised_ceiling_mv"], 1118.75)

    def test_an_exception_after_the_set_keeps_nothing_raised_unrecorded(self):
        gpu, rails, *_ = self.make()
        write = rails.write

        def landed_then_raised(records):
            write(records)
            if len(rails.writes) == 1:
                raise OSError("escape callback failed after the SET")
            return True, 0
        gpu._write_rail_records = landed_then_raised
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("limits checked and restored", msg)
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))
        self.assertIsNone(gpu.hold_headroom_record())

    def test_an_exception_whose_restore_fails_keeps_the_record(self):
        gpu, rails, *_ = self.make()
        write = rails.write

        def always_raises(records):
            write(records)
            raise OSError("escape callback failed")
        gpu._write_rail_records = always_raises
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED", msg)
        self.assertIsNotNone(gpu.hold_headroom_record())     # release can still undo it
        gpu._write_rail_records = write
        self.assertTrue(gpu.restore_hold_headroom()[0])
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

    def test_a_write_the_driver_never_saw_leaves_no_record(self):
        gpu, rails, *_ = self.make()
        gpu._write_rail_records = lambda records: (False, None)
        self.assertFalse(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        self.assertIsNone(gpu.hold_headroom_record())

    def test_an_untracked_raise_is_not_made(self):
        gpu, rails, *_ = self.make()
        for hook in (lambda record: False,
                     lambda record: (_ for _ in ()).throw(OSError("disk full"))):
            with self.subTest(hook=hook):
                ok, msg = gpu.apply_hold_headroom(1093.75, 25.0, before_write=hook)
                self.assertIsNone(ok)
                self.assertIn("untracked", msg)
                self.assertEqual(rails.writes, [])
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0, before_write=lambda r: None)[0])

    def test_a_plan_past_the_bound_is_withheld_not_failed(self):
        gpu, rails, *_ = self.make()
        rails.rows[0] = row(1212.5, 1212.5, 1212.5)      # the user's own sliders
        ok, msg = gpu.apply_hold_headroom(1187.5, 25.0)
        self.assertIsNone(ok)
        self.assertIn("limit bound", msg)
        self.assertEqual(rails.writes, [])

    def test_the_raise_and_restore_hold_the_backend_lock(self):
        gpu, rails, *_ = self.make()
        write, held = rails.write, []

        def probe(records):
            got = []
            thread = threading.Thread(target=lambda: got.append(gpu._lock.acquire(timeout=0)))
            thread.start()
            thread.join()
            if got[0]:
                gpu._lock.release()
            held.append(not got[0])
            return write(records)
        gpu._write_rail_records = probe
        gpu.apply_hold_headroom(1093.75, 25.0)
        gpu.restore_hold_headroom()
        self.assertEqual(held, [True, True])

    def test_an_unsettled_reading_cannot_loosen_the_live_check(self):
        gpu, rails, *_ = self.make()
        rails.live = 1093.75                           # settled, at the ceiling: offset 0
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        rails.live = rails.live_after = 1112.5         # still up from the raise
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])      # a re-plan
        self.assertEqual(gpu.hold_headroom_record()["live_offset_mv"], 0.0)
        gpu.restore_hold_headroom()
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])      # a new raise
        self.assertEqual(gpu.hold_headroom_record()["live_offset_mv"], 0.0)   # only lowered

    def test_an_unsettled_low_reading_does_not_tighten_a_real_offset(self):
        # a card whose live field reads 12.5 above a binding limit: a re-plan's
        # reading right after the restore (lower, not settled) must not make
        # the check trip on that card's normal reading
        gpu, rails, *_ = self.make()
        rails.live, rails.live_after = 1106.25, 1131.25
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        rails.live = rails.live_after = 1093.75
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])      # a re-plan
        self.assertEqual(gpu.hold_headroom_record()["live_offset_mv"], 12.5)
        self.assertTrue(gpu.hold_headroom_live_ok(live_mv=1131.25)[0])

    def test_an_off_value_landing_that_then_raises_is_forced_back(self):
        gpu, rails, *_ = self.make()
        write = rails.write

        def lands_off_then_raises(records):
            if not rails.writes:
                rails.corrupt_next_write = True            # alt-reliability lands 6.25 off
                write(records)
                raise OSError("escape callback failed after the SET")
            return write(records)
        gpu._write_rail_records = lands_off_then_raises
        self.assertFalse(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))
        self.assertIsNone(gpu.hold_headroom_record())

    def test_an_issued_but_unconfirmed_write_is_forced_back(self):
        gpu, rails, *_ = self.make()
        write = rails.write

        def issued_not_confirmed(records):
            write(records)
            return (False, 0) if len(rails.writes) == 1 else (True, 0)
        gpu._write_rail_records = issued_not_confirmed
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("issued but not confirmed; limits restored", msg)
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

    def test_an_effective_limit_far_above_the_estimate_is_not_trusted(self):
        gpu, rails, *_ = self.make()
        rails.live, rails.effective_after = 1093.75, 1200.0
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        self.assertEqual(gpu.hold_headroom_record()["raised_ceiling_mv"], 1118.75 + 6.25)
        self.assertFalse(gpu.hold_headroom_live_ok(live_mv=1150.0)[0])

    def test_the_grain_is_the_cards_usual_step_within_sane_bounds(self):
        for volts, want in (((800, 806.25, 812.5, 812.501, 818.75, 825), 6.25),   # one odd point
                            ((800, 825, 850, 875), 12.5),                         # coarse: bounded
                            ((800, 805, 810, 815, 820), 5.0)):                    # this card's own
            with self.subTest(volts=volts):
                gpu = bare_gpu()
                gpu.read_vf_curve = lambda v=volts: ([{"volt_mv": x} for x in v], None)
                self.assertEqual(gpu.hold_headroom_grain_mv(), want)

    def test_raising_a_term_the_raise_did_not_touch_does_not_re_plan(self):
        gpu, rails, *_ = self.make()
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        rails.rows[0]["overvoltage"] = 1150.0 - 1125.0             # up: the plan still holds
        self.assertFalse(gpu.hold_headroom_overwritten())
        rails.rows[0]["overvoltage"] = 1100.0 - 1125.0             # down: plan again
        self.assertTrue(gpu.hold_headroom_overwritten())

    def test_live_voltage_at_the_hold_is_fine_and_missing_live_is_not_a_rise(self):
        gpu, rails, *_ = self.make()
        rails.live = 1093.75
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        gpu.restore_hold_headroom()
        rails.live = None
        self.assertTrue(gpu.apply_hold_headroom(1093.75, 25.0)[0])

    def test_restore_leaves_a_field_the_user_changed_since(self):
        gpu, rails, *_ = self.make()
        gpu.apply_hold_headroom(1093.75, 25.0)
        rails.rows[0]["alt_reliability"] = 1150.0 - 1093.75
        self.assertTrue(gpu.restore_hold_headroom()[0])
        self.assertEqual(rails.absolute("alt_reliability"), 1150.0)
        self.assertEqual(rails.absolute("reliability"), 1068.75)

    def test_overwritten_detects_a_profile_or_slider_write(self):
        gpu, rails, *_ = self.make()
        self.assertFalse(gpu.hold_headroom_overwritten())
        gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(gpu.hold_headroom_overwritten())
        rails.rows[0]["reliability"] = 0.0
        self.assertTrue(gpu.hold_headroom_overwritten())

    def test_unverified_write_is_rolled_back(self):
        gpu, rails, *_ = self.make()
        rails.corrupt_next_write = True
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("limits restored", msg)
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

    def test_driver_refused_write_is_rolled_back(self):
        gpu, rails, *_ = self.make()
        real, seen = rails.write, []
        def refuse_first(records):
            seen.append(1)
            real(records)
            return (True, 0x1F) if len(seen) == 1 else (True, 0)
        gpu._write_rail_records = refuse_first
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("NV_STATUS", msg)
        self.assertIsNone(gpu.hold_headroom_record())
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

    def test_rollback_that_also_fails_keeps_the_record_for_exit(self):
        gpu, rails, *_ = self.make()
        real = rails.write
        def always_refuse(records):
            real(records)
            return True, 0x1F
        gpu._write_rail_records = always_refuse
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED", msg)
        self.assertIsNotNone(gpu.hold_headroom_record())

    def test_write_not_seen_changes_nothing_and_says_so(self):
        gpu, rails, *_ = self.make()
        gpu._write_rail_records = lambda records: (False, None)
        ok, _ = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIsNone(gpu.hold_headroom_record())

    def test_transient_unreadable_limits_are_worded_as_transient(self):
        gpu, rails, *_ = self.make()
        gpu.volt_rail_limits_supported = lambda rail=None: False
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertIsNone(ok)                            # nothing was tried
        self.assertIn("right now", msg)
        self.assertEqual(rails.writes, [])

    def test_boost_unreadable_writes_nothing(self):
        gpu, rails, *_ = self.make()
        gpu.read_voltage_boost = lambda: None
        self.assertFalse(gpu.apply_hold_headroom(1093.75, 25.0)[0])
        self.assertEqual(rails.writes, [])

    def test_full_rail_reset_drops_the_record_single_field_reset_keeps_it(self):
        gpu, rails, *_ = self.make()
        gpu._volt_rail_profile = lambda: {"fields": {0: GPU.VOLT_LIMIT_FIELDS},
                                          "poweron": {0: [0, 0, 0, 0]}}
        gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertTrue(gpu.reset_volt_rail_limits(0, fields=("vmin",))[0])
        self.assertIsNotNone(gpu.hold_headroom_record())
        self.assertTrue(gpu.reset_volt_rail_limits(0)[0])
        self.assertIsNone(gpu.hold_headroom_record())

    def test_user_rail_limits_hides_only_our_raise(self):
        gpu, rails, *_ = self.make()
        gpu.apply_hold_headroom(1093.75, 25.0)
        user = gpu.user_rail_limits()[0]
        self.assertEqual(tuple(GPU.abs_limit_mv(user, k) for k in ("reliability", "alt_reliability", "overvoltage")),
                         (1068.75, 1093.75, 1125.0))
        self.assertEqual(GPU.abs_limit_mv(gpu.read_volt_rail_limits()[0], "alt_reliability"), 1118.75)

    def test_profile_capture_during_a_raise_records_the_users_limits(self):
        from types import SimpleNamespace
        from druta import profiles
        gpu, rails, *_ = self.make()
        gpu.apply_hold_headroom(1093.75, 25.0)
        view = SimpleNamespace(
            read_volt_rail_limits=gpu.read_volt_rail_limits, hold_headroom_record=gpu.hold_headroom_record,
            user_rail_limits=gpu.user_rail_limits, abs_limit_mv=GPU.abs_limit_mv,
            volt_rail_limit_fields=lambda r: GPU.VOLT_LIMIT_FIELDS if r == 0 else (),
            voltage_xoc_enabled=False)
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(view, state, None)
        self.assertEqual(state[profiles.INCOMPLETE_KEY], [])
        captured = state["rail_limits_mv"]["0"]
        self.assertEqual((captured["reliability"], captured["alt_reliability"], captured["overvoltage"]),
                         (1068.75, 1093.75, 1125.0))

    def test_architecture_is_three_way(self):
        gpu = bare_gpu()
        for arch, want in ((GPU.ARCH_TURING, True), (GPU.ARCH_PASCAL, True),
                           (GPU.ARCH_AMPERE, True), (10, True),
                           (8, False), (GPU.ARCH_MAXWELL, False), (None, None)):
            with self.subTest(arch=arch):
                gpu.arch = lambda a=arch: a
                self.assertIs(gpu.hold_headroom_architecture(), want)

    def test_headroom_follows_rail_control_rather_than_its_own_list(self):
        # A generation that gains rail control (as Ampere does once measured)
        # gets the headroom with it; one without rail control never does.
        gpu = bare_gpu()
        gpu.arch = lambda: 99
        self.assertFalse(gpu.hold_headroom_architecture())
        gpu._VOLT_RAIL_ARCHITECTURES = GPU._VOLT_RAIL_ARCHITECTURES + (99,)
        self.assertTrue(gpu.hold_headroom_architecture())


# --------------------------------------------------------------------------- #
def make_app(gpu):
    from druta.druta import Druta
    app = Druta.__new__(Druta)
    app.gpu = gpu
    app.headroom_on, app.headroom_mv = True, 25.0
    app.log = Mock()
    app._once = {}
    app._clk_lock = None
    app.show_win = Mock()
    app.vf_recovery_pending = lambda: False
    app.unlocked = lambda: True                  # writes unlocked, as a hold needs
    return app


def vf_state(app, mv, **extra):
    state = {"kind": app.LOCK_VF, "idx": 103, "req_mv": mv, "domain": 6,
             "got_idx": 103, "got_mv": mv, "got_mhz": 2100.0}
    state.update(extra)
    return state


class AppTestCase(unittest.TestCase):
    """An app on a fake Turing card, with the crash-marker directory in a
    temporary folder so no test touches the real %LOCALAPPDATA%."""

    UUID = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        redirect = patch("druta.vfheadroom.active_dir", return_value=self.root)
        redirect.start()
        self.addCleanup(redirect.stop)
        exists = patch("druta.druta.dpg.does_item_exist", return_value=False)
        exists.start()
        self.addCleanup(exists.stop)
        self.gpu = bare_gpu()
        self.gpu.static = {"uuid": self.UUID} if self.UUID else {}
        self.rails = FakeRails(self.gpu, stock_row("turing"))
        self.app = make_app(self.gpu)

    def hold(self, mv, **extra):
        self.app.set_lock_state(vf_state(self.app, mv, **extra))

    def limits(self):
        return self.rails.ceiling_terms()

    def logged(self, text):
        return any(text in str(c) for c in self.app.log.call_args_list)


class LockStateHookTests(AppTestCase):
    def test_confirmed_hold_raises_and_release_restores(self):
        self.hold(1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.app.set_lock_state(None)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_plan_uses_the_lock_request_not_the_curve_point(self):
        # the lock resolved to a lower point on a flat run, but it can run up to
        # its request: the gate and the plan follow the request
        self.hold(1125.0, got_mv=1093.75)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertEqual(self.app._headroom_note[0], "withheld")

    def test_same_hold_again_writes_once(self):
        self.hold(1093.75)
        self.hold(1093.75)
        self.assertEqual(len(self.rails.writes), 1)

    def test_moving_the_hold_replans_from_the_users_limits(self):
        self.hold(1093.75)
        self.hold(1087.5)
        self.assertEqual(self.limits(), (1087.5, 1112.5, 1125.0))
        self.app.set_lock_state(None)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_boost_change_replans_even_without_the_replan_flag(self):
        self.hold(1093.75)
        self.rails.rows[0]["_boost_mv"] = 50.0      # a profile or Undo moved boost
        self.app.sync_hold_headroom()
        self.assertEqual(self.limits(), (1068.75, 1118.75, 1125.0))

    def test_unconfirmed_state_for_the_same_request_keeps_the_raise(self):
        self.hold(1093.75)
        self.hold(1093.75, verified=False)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_unconfirmed_state_for_a_different_request_takes_the_raise_off(self):
        self.hold(1093.75)
        self.hold(1125.0, verified=False)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_above_ceiling_hold_warns_and_leaves_the_limits(self):
        self.hold(1125.0)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertEqual(self.app._headroom_note[0], "withheld")
        self.assertTrue(self.logged("WARNING"))
        self.hold(1093.75)
        self.assertIsNone(self.app._headroom_note)

    def test_setting_off_means_no_raise(self):
        self.app.headroom_on = False
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])

    def test_nvml_frequency_lock_gets_no_headroom(self):
        self.app.set_lock_state({"kind": self.app.LOCK_NVML, "lo": 2100, "hi": 2100})
        self.assertEqual(self.rails.writes, [])

    def test_generation_without_rail_control_is_silent(self):
        self.gpu.arch = lambda: 8                    # Ada: no rail control
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.app.log.assert_not_called()

    def test_unread_architecture_is_a_visible_retryable_failure(self):
        self.gpu.arch = lambda: None
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.assertEqual(self.app._headroom_note[0], "unread")      # no write was tried
        self.assertTrue(self.logged("could not be read"))

    def test_backend_without_the_feature_is_left_alone(self):
        self.app.gpu = Mock()
        self.hold(1093.75)
        self.app.gpu.apply_hold_headroom.assert_not_called()

    def test_a_failed_apply_is_retried_once_then_marked_on_the_banner(self):
        calls = []
        real = self.gpu._write_rail_records
        def not_seen_once(records):
            calls.append(1)
            return (False, None) if len(calls) == 1 else real(records)
        self.gpu._write_rail_records = not_seen_once
        self.hold(1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.app.set_lock_state(None)
        self.gpu._write_rail_records = lambda records: (False, None)
        self.hold(1087.5)
        self.assertEqual(self.app._headroom_note[0], "failed")
        self.assertIsNone(self.gpu.hold_headroom_record())      # nothing left raised

    def test_failed_restore_is_retried_once(self):
        self.hold(1093.75)
        calls, real = [], self.gpu.restore_hold_headroom
        def flaky(force=False):
            calls.append(1)
            return (False, "interrupted") if len(calls) == 1 else real(force)
        self.gpu.restore_hold_headroom = flaky
        self.app.set_lock_state(None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_rail_write_during_the_hold_replans_from_the_new_values(self):
        self.hold(1093.75)
        self.rails.rows[0]["reliability"] = 1080.0 - 1068.75     # e.g. a profile load
        self.app.refresh_volt_limits = type(self.app).refresh_volt_limits.__get__(self.app)
        with patch.object(type(self.app), "rail_limit_diagnostics", return_value={}, create=True), \
                patch.object(type(self.app), "rail_limit_support", return_value={}, create=True), \
                patch.object(type(self.app), "update_rail_limit_diagnostic_ui", create=True), \
                patch.object(type(self.app), "volt_limits_cells", return_value=None, create=True):
            self.gpu.read_volt_rail_state = lambda: None
            self.app.refresh_volt_limits()
        self.assertTrue(self.logged("re-planning"))
        self.assertEqual(GPU.rail_ceiling_mv(self.rails.rows[0]), 1118.75)


class RailGetters:
    """The two NVAPI rail getters the backend reads, answering like a Turing
    card's (NVVDD at its first-read limits) or with a status every time."""

    def __init__(self, gpu, status=0, exported=("VoltRailsCtlGet", "VoltRailsAbs")):
        self.status = status
        gpu.nvapi = type("Api", (), {"ok": True, "gpu": None})()
        gpu.static = {}
        gpu.arch = lambda: GPU.ARCH_TURING
        for name in exported:
            setattr(gpu.nvapi, name, getattr(self, name))

    def VoltRailsCtlGet(self, handle, pointer):
        return self.status                       # deltas all zero: stock

    def VoltRailsAbs(self, handle, pointer):
        if self.status:
            return self.status
        import ctypes
        words = ctypes.cast(pointer._obj, ctypes.POINTER(ctypes.c_uint32))
        base = GPU.LIVE_RAIL_BASE // 4
        for n, value in enumerate((1, 900000, 1068750, 1093750, 1125000, 1068750, 643750)):
            words[base + n] = value
        return 0


class RailAvailabilityTests(unittest.TestCase):
    """A supported generation whose adapter never answered its rail getters is
    told apart from one whose answered getters failed this time."""

    def test_getters_that_answer_leave_nothing_to_report(self):
        gpu = bare_gpu()
        RailGetters(gpu)
        self.assertIsNone(gpu.hold_headroom_rail_unavailable(0))

    def test_getters_that_never_answered_on_this_adapter(self):
        gpu = bare_gpu()
        RailGetters(gpu, status=-104)
        kind, why = gpu.hold_headroom_rail_unavailable(0)
        self.assertEqual(kind, "unanswered")
        self.assertIn("VoltRailsCtlGet status -104", why)
        self.assertIn("VoltRailsAbs status -104", why)

    def test_a_getter_not_exported_says_so(self):
        gpu = bare_gpu()
        RailGetters(gpu, exported=("VoltRailsAbs",))
        kind, why = gpu.hold_headroom_rail_unavailable(0)
        self.assertEqual(kind, "unanswered")
        self.assertIn("VoltRailsCtlGet not exported", why)

    def test_getters_that_answered_earlier_and_fail_now_are_transient(self):
        gpu = bare_gpu()
        getters = RailGetters(gpu)
        self.assertIsNone(gpu.hold_headroom_rail_unavailable(0))
        getters.status = -1
        kind, why = gpu.hold_headroom_rail_unavailable(0)
        self.assertEqual(kind, "transient")
        self.assertIn("status -1", why)

    def test_retry_detection_does_not_turn_a_failed_read_into_an_absent_rail(self):
        gpu = bare_gpu()
        getters = RailGetters(gpu)
        self.assertIsNone(gpu.hold_headroom_rail_unavailable(0))
        gpu._refresh_volt_rail_capabilities()                # empties the getters' cache
        getters.status = -1
        self.assertEqual(gpu.hold_headroom_rail_unavailable(0)[0], "transient")

    def test_a_failed_read_is_retried_at_once_not_after_the_cadence(self):
        gpu = bare_gpu()
        getters = RailGetters(gpu, status=-1)
        gpu.volt_rail_limits_supported(0)           # fails; the rail waits 2 s
        getters.status = 0                          # the driver answers now
        self.assertIsNone(gpu.hold_headroom_rail_unavailable(0))


class RailAnswerAppTests(AppTestCase):
    def unavailable(self, kind, why="the driver did not return this rail's control settings"):
        self.gpu.hold_headroom_rail_unavailable = lambda rail=0: (kind, why) if kind else None

    def banner(self):
        shown = {}
        with patch("druta.druta.dpg.does_item_exist", side_effect=lambda tag: tag == "hold_info"), \
                patch("druta.druta.dpg.set_value", side_effect=shown.__setitem__), \
                patch("druta.druta.dpg.configure_item"):
            self.app.draw_hold_banner()
        return shown.get("hold_info", "")

    def test_a_card_that_never_answered_is_reported_once_not_on_every_hold(self):
        self.unavailable("unanswered")
        for _ in range(3):
            self.app.set_lock_state(None)
            self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.assertIsNone(self.app._headroom_note)
        notes = [c for c in self.app.log.call_args_list if "not available on this card" in str(c)]
        self.assertEqual(len(notes), 1)
        self.assertIsNone(notes[0].args[1])                  # a warning, not a failure
        self.assertNotIn("HEADROOM", self.banner())

    def test_a_later_answer_gets_its_headroom_and_re_arms_the_note(self):
        self.unavailable("unanswered")
        self.hold(1093.75)
        self.app.set_lock_state(None)
        self.unavailable(None)
        self.hold(1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.assertIsNone(self.app._headroom_unanswered)
        self.app.set_lock_state(None)
        self.unavailable("unanswered")
        self.hold(1093.75)
        notes = [c for c in self.app.log.call_args_list if "not available on this card" in str(c)]
        self.assertEqual(len(notes), 2)

    def test_a_transient_read_failure_stays_visible_and_says_what_failed(self):
        self.unavailable("transient", "the driver did not return a valid rail status")
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.assertEqual(self.app._headroom_note[0], "unread")
        text = self.banner()
        self.assertIn("HEADROOM NOT APPLIED", text)
        self.assertIn("could not read what it needs this time", text)
        self.assertNotIn("write did not succeed", text)

    def test_a_raise_already_on_the_card_is_not_second_guessed(self):
        self.hold(1093.75)
        self.unavailable("unanswered")
        self.hold(1087.5)                                    # re-plan: apply restores first
        self.assertEqual(self.limits(), (1087.5, 1112.5, 1125.0))

    def test_the_clock_check_names_a_card_that_never_answered(self):
        self.unavailable("unanswered", "VoltRailsCtlGet status -104")
        self.hold(1093.75)
        self.app.step_khz = lambda: 15000
        self.gpu.step_is_measured = lambda: True
        clock_reads(self.app, 20, 2100.0, 2070.0)
        self.assertTrue(self.logged("has not returned its voltage limits"))


class OrderingTests(AppTestCase):
    """The raise comes off while the lock still holds the point - never after."""

    def events(self):
        order = []
        real_restore = self.gpu.restore_hold_headroom
        def restore(force=False):
            order.append("restore")
            return real_restore(force)
        self.gpu.restore_hold_headroom = restore
        self.gpu.clear_vf_lock = lambda domain=None, expected_uv=None: (order.append("unlock") or (True, "released"))
        self.app.guard = lambda: True
        return order

    def test_release_restores_before_unlocking(self):
        self.hold(1093.75)
        order = self.events()
        self.app.release_lock()
        self.assertEqual(order[:2], ["restore", "unlock"])
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_release_is_refused_when_the_raise_cannot_come_off(self):
        self.hold(1093.75)
        order = self.events()
        self.gpu.restore_hold_headroom = lambda force=False: (order.append("restore") or (False, "busy"))
        self.app.release_lock()
        self.assertNotIn("unlock", order)
        self.assertEqual(self.app._clk_lock["kind"], self.app.LOCK_VF)
        self.assertTrue(self.logged("NOT done"))

    def test_handover_to_another_lock_restores_first(self):
        self.hold(1093.75)
        order = self.events()
        self.assertTrue(self.app.handover(self.app.LOCK_NVML))
        self.assertEqual(order[:2], ["restore", "unlock"])

    def test_exit_restores_before_unlocking_and_keeps_the_lock_if_it_cannot(self):
        self.hold(1093.75)
        order = self.events()
        self.app.release_on_exit()
        self.assertEqual(order[:2], ["restore", "unlock"])
        self.hold(1093.75)
        order.clear()
        self.gpu.restore_hold_headroom = lambda force=False: (order.append("restore") or (False, "busy"))
        self.app.release_on_exit()
        self.assertNotIn("unlock", order)

    def test_exit_restores_a_raise_behind_a_non_vf_lock(self):
        self.hold(1093.75)
        self.gpu.restore_hold_headroom = lambda force=False: (False, "busy")
        self.app.set_lock_state({"kind": self.app.LOCK_NVML, "lo": 2100, "hi": 2100})
        self.assertIsNotNone(self.gpu.hold_headroom_record())
        del self.gpu.restore_hold_headroom            # back to the real method
        self.gpu.reset_gpu_clocks = lambda: (True, "reset")
        self.app.release_on_exit()
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_moving_a_hold_restores_the_old_raise_before_the_new_lock(self):
        self.hold(1093.75)
        order = self.events()
        self.gpu.set_vf_lock = lambda uv, domain=None: (order.append(("lock", uv)) or (True, "set"))
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1087500, "domain": 6, "volt_mv": 1087.5}, None)
        pt = {"idx": 102, "volt_mv": 1087.5, "freq_mhz": 2085.0}
        self.app.vf_sel, self.app.vf_by_idx, self.app.vf_points = 102, {102: pt}, [pt]
        self.app.vf_work, self.app.vf_orig = {}, {}
        self.app.hold_point()
        self.assertEqual(order[0], "restore")
        self.assertEqual(order[1], ("lock", 1087500))

    def test_switching_cards_is_refused_while_a_raise_is_left(self):
        self.hold(1093.75)
        self.app._clk_lock = None
        self.assertFalse(self.app.swap_gpu("0000:02:00.0"))
        self.app.log.reset_mock()
        self.app.gpu_list = [{"slot": "0000:02:00.0", "name": "other"}]
        self.gpu.slot = lambda: "0000:01:00.0"
        self.app._switch_armed = None
        with patch.object(type(self.app), "swap_gpu") as swap:
            self.app.switch_gpu(user_data="0000:02:00.0")
            swap.assert_not_called()
        self.assertTrue(self.logged("not switching to"))

    def test_restore_now_retries_without_touching_the_setting(self):
        self.hold(1093.75)
        self.app._clk_lock = None
        self.app.restore_raised_limits_now()
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertTrue(self.app.headroom_on)


class WatchTests(AppTestCase):
    def test_live_rail_above_the_raised_ceiling_undoes_the_raise(self):
        self.hold(1093.75)
        self.app.watch_hold_headroom({"nvvdd_live_mv": 1125.0})
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertTrue(self.logged("above the 1118.75 mV ceiling the headroom raise set"))

    def test_rail_margin_under_the_raised_ceiling_keeps_the_raise(self):
        self.hold(1093.75)
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1093750}, None)
        for _ in range(8):
            self.app.watch_hold_headroom({"nvvdd_live_mv": 1112.5, "vcore_mv": 1093.75})
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_the_v_f_point_voltage_is_not_what_is_judged(self):
        self.hold(1093.75)
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1093750}, None)
        self.app.watch_hold_headroom({"vcore_mv": 1200.0})        # not the rail reading
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_poll_snapshot_carries_the_rail_reading_only_during_a_raise(self):
        gpu = bare_gpu()
        gpu._lock = threading.RLock()
        FakeRails(gpu, stock_row("turing")).live = 1112.5
        for name in ("_priv_clocks", "_read_clocks", "_read_temps", "_read_power", "_read_fan",
                     "_read_util", "_read_throttle", "_read_pcie", "_read_misc"):
            setattr(gpu, name, lambda *a, **k: None)
        gpu.clkdom_is_blackwell = lambda: False
        self.assertNotIn("nvvdd_live_mv", gpu.read())
        gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertEqual(gpu.read()["nvvdd_live_mv"], 1112.5)

    def test_a_replaced_lock_takes_the_raise_off(self):
        self.hold(1093.75)
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1150000}, None)   # another tool
        for _ in range(4):
            self.app.watch_hold_headroom({"vcore_mv": 1093.75})
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertIsNone(self.app._clk_lock)

    def test_an_unreadable_lock_is_not_taken_as_gone(self):
        self.hold(1093.75)
        self.gpu.read_vf_lock_status = lambda domain=None: (None, "busy")
        for _ in range(8):
            self.app.watch_hold_headroom({"vcore_mv": 1093.75})
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))


# --------------------------------------------------------------------------- #
DEAD = {"pid": 2 ** 30, "created": 1}


class CrashMarkerTests(AppTestCase):
    UUID = "GPU-test-0001"

    def path(self):
        return vfheadroom.marker_path(self.UUID, self.root)

    def set_owner(self, owner):
        data = json.loads(self.path().read_text(encoding="utf-8"))
        data["owner"] = owner
        self.path().write_text(json.dumps(data), encoding="utf-8")

    def next_session(self):
        nxt = make_app(self.gpu)
        self.gpu._hold_headroom = None               # a new process has no record
        return nxt

    def crash(self):
        self.hold(1093.75)
        self.set_owner(DEAD)
        return self.next_session()

    def test_marker_follows_the_raise_and_records_its_owner(self):
        self.hold(1093.75)
        record = vfheadroom.active_record(self.UUID)
        self.assertEqual(record["owner"]["pid"], vfheadroom.current_owner()["pid"])
        self.assertEqual(record["version"], vfheadroom.MARKER_VERSION)
        self.app.set_lock_state(None)
        self.assertIsNone(vfheadroom.active_record(self.UUID))

    def test_marker_exists_before_the_limits_move(self):
        seen = []
        real = self.gpu._write_rail_records
        def write(records):
            seen.append(vfheadroom.active_record(self.UUID))
            return real(records)
        self.gpu._write_rail_records = write
        self.hold(1093.75)
        self.assertIsNotNone(seen[0])

    def test_crash_leaves_a_marker_the_next_session_reports(self):
        nxt = self.crash()
        nxt.check_stale_headroom()
        self.assertIsNotNone(nxt._stale_headroom)

    def test_an_unanswered_stale_marker_withholds_new_raises(self):
        nxt = self.crash()
        nxt.check_stale_headroom()
        nxt.set_lock_state(vf_state(nxt, 1093.75))
        self.assertEqual(nxt._headroom_note[0], "withheld")
        self.assertEqual(vfheadroom.active_record(self.UUID)["owner"], DEAD)   # evidence kept

    def test_marker_is_dropped_silently_once_the_limits_are_back(self):
        nxt = self.crash()
        self.rails.rows[0] = stock_row("turing")
        nxt.check_stale_headroom()
        self.assertIsNone(getattr(nxt, "_stale_headroom", None))
        self.assertIsNone(vfheadroom.active_record(self.UUID))

    def test_a_partial_read_keeps_the_evidence(self):
        nxt = self.crash()
        nxt.gpu.read_volt_rail_limits = lambda: {1: stock_row("turing")}
        nxt.check_stale_headroom()
        self.assertIsNotNone(vfheadroom.active_record(self.UUID))
        self.assertTrue(any("cannot be read" in str(c) for c in nxt.log.call_args_list))

    def test_live_foreign_owner_is_not_a_crash_and_blocks_this_windows_raise(self):
        self.hold(1093.75)
        with patch("druta.vfheadroom.owner_state", return_value="alive"):
            other = self.next_session()
            other.check_stale_headroom()
            self.assertIsNone(getattr(other, "_stale_headroom", None))
            other.show_win.assert_not_called()
            self.assertIn("another Druta window", other.headroom_blocker())
            self.assertTrue(vfheadroom.clear_active(self.UUID, force=True)[0])
        self.assertIsNotNone(vfheadroom.active_record(self.UUID))

    def test_no_existing_foreign_marker_is_overwritten(self):
        self.hold(1093.75)
        self.set_owner(DEAD)
        ok, msg = vfheadroom.mark_active(self.UUID, {"rail": 0, "written_uv": {"reliability": 1},
                                                    "prior_uv": {"reliability": 0}})
        self.assertFalse(ok)
        self.assertEqual(vfheadroom.active_record(self.UUID)["owner"], DEAD)

    def test_an_unreadable_marker_is_kept_and_blocks_raises(self):
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text("{not json", encoding="utf-8")
        self.assertEqual(vfheadroom.marker_state(self.UUID)[0], "unreadable")
        self.assertFalse(vfheadroom.clear_active(self.UUID, force=True)[0])
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.assertTrue(self.path().exists())

    def test_reused_pid_and_version_1_owners_count_as_dead(self):
        me = vfheadroom.current_owner()
        for owner in ({"pid": me["pid"], "created": (me["created"] or 0) + 1}, None):
            with self.subTest(owner=owner):
                self.rails.rows[0] = stock_row("turing")   # a fresh card each time
                self.gpu._hold_headroom, self.app._clk_lock = None, None
                self.hold(1093.75)
                self.set_owner(owner)
                nxt = self.next_session()
                nxt.check_stale_headroom()
                self.assertIsNotNone(nxt._stale_headroom)
                nxt.stale_headroom_dismiss()

    def test_owner_from_before_this_boot_is_dead_even_if_access_is_denied(self):
        with patch("druta.vfheadroom.process_created", return_value="unknown"), \
                patch("druta.vfheadroom.boot_filetime", return_value=10_000):
            self.assertEqual(vfheadroom.owner_state({"pid": 4242, "created": 5}), "dead")
            self.assertEqual(vfheadroom.owner_state({"pid": 4242, "created": 20_000}), "alive")

    def test_launched_restart_keeps_the_marker_and_rechecks_first(self):
        nxt = self.crash()
        nxt.check_stale_headroom()
        nxt._closing = False
        nxt.open_device_restart = lambda: setattr(nxt, "_closing", True)
        with patch("druta.vfheadroom.other_instances", return_value=0):
            nxt.stale_headroom_restart()
        self.assertTrue(nxt._closing)
        self.assertIsNotNone(vfheadroom.active_record(self.UUID))

    def test_restart_is_skipped_when_the_raise_is_already_gone(self):
        nxt = self.crash()
        nxt.check_stale_headroom()
        self.rails.rows[0] = stock_row("turing")
        nxt.open_device_restart = Mock()
        nxt.stale_headroom_restart()
        nxt.open_device_restart.assert_not_called()
        self.assertIsNone(vfheadroom.active_record(self.UUID))

    def test_restart_is_refused_while_another_druta_window_is_open(self):
        nxt = self.crash()
        nxt.check_stale_headroom()
        nxt.open_device_restart = Mock()
        with patch("druta.vfheadroom.other_instances", return_value=1):
            nxt.stale_headroom_restart()
        nxt.open_device_restart.assert_not_called()

    def test_dismissing_a_dead_owners_marker_removes_it(self):
        nxt = self.crash()
        nxt.check_stale_headroom()
        nxt.stale_headroom_dismiss()
        self.assertIsNone(vfheadroom.active_record(self.UUID))
        self.assertIsNone(nxt._stale_headroom)

    def test_startup_profile_waits_for_the_answer(self):
        nxt = self.crash()
        nxt.check_stale_headroom()
        nxt._held_startup_request = {"name": "p", "profile": {}}
        nxt.begin_profile_load = Mock()
        nxt.stale_headroom_dismiss()
        nxt.begin_profile_load.assert_called_once_with("p", {}, automatic=True)

    def test_a_new_session_does_not_clear_an_old_marker_before_checking(self):
        vfheadroom.mark_active(self.UUID, {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0,
                                           "written_uv": {"alt_reliability": 25000},
                                           "prior_uv": {"alt_reliability": 0}})
        self.app.mirror_headroom_marker()
        self.assertIsNotNone(vfheadroom.active_record(self.UUID))

    def test_markers_for_different_cards_are_separate_files(self):
        self.assertNotEqual(vfheadroom.marker_path("GPU-a", self.root), vfheadroom.marker_path("GPU-b", self.root))
        self.assertEqual(vfheadroom.marker_path("GPU/../x", self.root).parent, self.root)


class NoUuidTests(AppTestCase):
    def test_no_uuid_means_no_marker_and_says_so(self):
        self.hold(1093.75)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(self.logged("no UUID"))


class StillRaisedTests(unittest.TestCase):
    RECORD = {"rail": 0, "written_uv": {"alt_reliability": 25000}, "prior_uv": {"alt_reliability": 0}}

    def test_three_way(self):
        raised = {0: {"alt_reliability": 25.0}}
        gone = {0: {"alt_reliability": 0.0}}
        self.assertIs(vfheadroom.still_raised(self.RECORD, raised), True)
        self.assertIs(vfheadroom.still_raised(self.RECORD, gone), False)
        self.assertIsNone(vfheadroom.still_raised(self.RECORD, {1: {}}))
        self.assertIsNone(vfheadroom.still_raised(self.RECORD, {0: {}}))


# --------------------------------------------------------------------------- #
class VerificationFollowUpTests(AppTestCase):
    """The second review round: dead ends, lost headroom and noisy retries."""

    UUID = "GPU-test-0002"

    def test_recovery_release_does_not_apply_a_raise_before_unpinning(self):
        prev = vf_state(self.app, 1093.75)
        self.app._clk_lock = {"kind": self.app.LOCK_VF, "verified": False, "recovery": True,
                              "req_mv": 1087.5, "domain": 6, "previous_lock": prev}
        self.app.vf_recovery_pending = lambda: True
        self.gpu._vf_lock_recovery = {}
        def recovered():
            self.app.vf_recovery_pending = lambda: False
            return True, "recovered"
        self.gpu.recover_vf_lock = recovered
        self.gpu.clear_vf_lock = lambda domain=None, expected_uv=None: (True, "released")
        self.app.release_current()
        self.assertEqual(self.rails.writes, [])         # no raise applied on the way out

    def test_restore_now_works_under_a_hold_and_then_release_goes_through(self):
        self.hold(1093.75)
        self.app.restore_raised_limits_now()
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.app.guard = lambda: True
        self.gpu.clear_vf_lock = lambda domain=None, expected_uv=None: (True, "released")
        self.app.release_lock()
        self.assertIsNone(self.app._clk_lock)

    def test_failed_release_after_a_restore_puts_the_raise_back(self):
        self.hold(1093.75)
        self.app.guard = lambda: True
        self.gpu.clear_vf_lock = lambda domain=None, expected_uv=None: (False, "driver refused")
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1093750}, None)
        self.app.release_lock()
        self.assertEqual(self.app._clk_lock["kind"], self.app.LOCK_VF)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))   # still held: raised again

    def test_a_release_sent_but_unverified_does_not_raise_an_unlocked_card(self):
        for status, want_lock in ((({}, None), None),                       # reads back unlocked
                                  ((None, "verification read failed"), "unconfirmed")):
            with self.subTest(status=status):
                self.app.set_lock_state(None)
                self.gpu._hold_headroom = None
                self.rails.rows[0] = stock_row("turing")
                self.hold(1093.75)
                self.app.guard = lambda: True
                self.gpu.clear_vf_lock = lambda domain=None, expected_uv=None: (
                    False, "V/F lock release was sent, but the verification read failed")
                self.gpu.read_vf_lock_status = lambda domain=None, s=status: (
                    (None, None) if s[0] == {} else s)
                self.app.release_lock()
                self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))  # not raised again
                if want_lock is None:
                    self.assertIsNone(self.app._clk_lock)
                else:
                    self.assertFalse(self.app._clk_lock["verified"])

    def test_failed_re_hold_re_plans_the_hold_still_in_force(self):
        self.hold(1093.75)
        self.gpu.set_vf_lock = lambda uv, domain=None: (False, "refused")
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1093750, "domain": 6}, None)
        self.gpu.vf_lock_recovery_pending = lambda: False
        pt = {"idx": 102, "volt_mv": 1087.5, "freq_mhz": 2085.0}
        self.app.vf_sel, self.app.vf_by_idx, self.app.vf_points = 102, {102: pt}, [pt]
        self.app.guard = lambda: True
        self.app.hold_point()
        self.assertEqual(self.app._clk_lock["req_mv"], 1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_watch_backs_off_after_a_failed_restore_and_logs_once(self):
        self.hold(1093.75)
        calls = []
        self.gpu.restore_hold_headroom = lambda force=False: (calls.append(1) or (False, "busy"))
        for _ in range(3):
            self.app.watch_hold_headroom({"nvvdd_live_mv": 1125.0})
        self.assertEqual(len(calls), 2)                 # one restore (with its retry), then back off
        fails = [c for c in self.app.log.call_args_list if "RESTORE FAILED" in str(c)]
        self.assertEqual(len(fails), 1)

    def test_watch_ignores_a_snapshot_taken_before_the_raise(self):
        self.hold(1093.75)
        applied = self.app._headroom_applied_t
        self.app.watch_hold_headroom({"nvvdd_live_mv": 1125.0}, snap_t=applied - 1.0)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.app.watch_hold_headroom({"nvvdd_live_mv": 1125.0}, snap_t=applied + 1.0)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_watch_makes_no_driver_call_when_the_snapshot_has_no_live_reading(self):
        self.hold(1093.75)
        self.gpu.read_rail_live_mv = Mock(side_effect=AssertionError("driver call from the tick"))
        self.app.watch_hold_headroom({})
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_exit_still_releases_the_lock_when_the_restore_raises(self):
        self.app.set_lock_state({"kind": self.app.LOCK_NVML, "lo": 2100, "hi": 2100})
        self.gpu._hold_headroom = {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0,
                                   "written_uv": {"reliability": 1}, "prior_uv": {"reliability": 0}}
        def boom(force=False):
            raise RuntimeError("driver gone")
        self.gpu.restore_hold_headroom = boom
        released = []
        self.gpu.reset_gpu_clocks = lambda: (released.append(1) or (True, "reset"))
        self.app.release_on_exit()
        self.assertEqual(released, [1])

    def test_rail_stock_reset_during_a_hold_plans_the_headroom_again(self):
        self.hold(1093.75)
        self.gpu._volt_rail_profile = lambda: {"fields": {0: GPU.VOLT_LIMIT_FIELDS},
                                               "poweron": {0: [0, 0, 0, 0]}}
        self.app.refresh_volt_limits = Mock()
        self.app.apply_vlim_reset()
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_unjudged_dead_marker_withholds_and_the_check_is_retried(self):
        vfheadroom.mark_active(self.UUID, {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0,
                                           "written_uv": {"alt_reliability": 25000},
                                           "prior_uv": {"alt_reliability": 0}})
        data = json.loads(vfheadroom.marker_path(self.UUID, self.root).read_text(encoding="utf-8"))
        data["owner"] = DEAD
        vfheadroom.marker_path(self.UUID, self.root).write_text(json.dumps(data), encoding="utf-8")
        real = self.gpu.read_volt_rail_limits
        self.gpu.read_volt_rail_limits = lambda: None
        self.app.check_stale_headroom()
        self.assertTrue(self.app._stale_recheck)
        self.gpu.read_volt_rail_limits = real
        self.hold(1093.75)
        self.assertEqual(self.app._headroom_note[0], "withheld")
        self.assertEqual(self.rails.writes, [])
        self.app._clk_lock = None
        for _ in range(4):
            self.app.watch_hold_headroom({})
        self.assertFalse(self.app._stale_recheck)
        self.assertIsNone(vfheadroom.active_record(self.UUID))   # judged: values not there

    def test_a_failed_marker_clear_is_retried_on_the_tick(self):
        self.hold(1093.75)
        with patch("druta.vfheadroom.clear_active", return_value=(False, "disk full")):
            self.app.set_lock_state(None)
        self.assertIsNotNone(vfheadroom.active_record(self.UUID))
        for _ in range(4):
            self.app.watch_hold_headroom({})
        self.assertIsNone(vfheadroom.active_record(self.UUID))

    def test_pnp_blocker_is_checked_on_each_click(self):
        with patch("druta.vfheadroom.other_instances", return_value=1):
            self.assertIn("other Druta window", self.app.pnp_blocker())
        with patch("druta.vfheadroom.other_instances", return_value=0):
            self.assertIsNone(self.app.pnp_blocker())
        with patch("druta.vfheadroom.other_instances", side_effect=ValueError("bad output")):
            self.gpu.read_vf_lock = lambda: None
            self.assertIsNone(self.app.pnp_blocker())

    def test_setting_off_restores_and_on_again_reapplies(self):
        self.hold(1093.75)
        self.app.guard = lambda: True
        with patch("druta.druta.dpg.get_value", side_effect=lambda tag: {"hr_on": False, "hr_mv": 25.0}[tag]), \
                patch("druta.druta.dpg.set_value"), \
                patch("druta.druta.dpg.does_item_exist", side_effect=lambda tag: tag in ("hr_on", "hr_mv")), \
                patch("druta.vfheadroom.save", return_value=(True, "saved")):
            self.app.headroom_changed()
            self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        with patch("druta.druta.dpg.get_value", side_effect=lambda tag: {"hr_on": True, "hr_mv": 25.0}[tag]), \
                patch("druta.druta.dpg.set_value"), \
                patch("druta.druta.dpg.does_item_exist", side_effect=lambda tag: tag in ("hr_on", "hr_mv")), \
                patch("druta.vfheadroom.save", return_value=(True, "saved")):
            self.app.headroom_changed()
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_restore_with_unreadable_limits_keeps_record_and_marker(self):
        self.hold(1093.75)
        self.gpu.read_volt_rail_limits = lambda: None
        self.app.guard = lambda: True
        self.app.release_lock()
        self.assertIsNotNone(self.gpu.hold_headroom_record())
        self.assertIsNotNone(vfheadroom.active_record(self.UUID))
        self.assertEqual(self.app._clk_lock["kind"], self.app.LOCK_VF)   # not unpinned

    def test_an_exception_inside_apply_is_logged_and_the_hold_survives(self):
        def boom(*a, **k):
            raise RuntimeError("escape hook")
        self.gpu.apply_hold_headroom = boom
        self.hold(1093.75)
        self.assertEqual(self.app._clk_lock["kind"], self.app.LOCK_VF)
        self.assertEqual(self.app._headroom_note[0], "failed")


class OtherGenerationAppTests(unittest.TestCase):
    """The app-level sync on Pascal and Blackwell bases, not only Turing."""

    def test_hold_raises_and_release_restores_on_pascal_and_blackwell(self):
        for card in ("pascal", "blackwell"):
            with self.subTest(card=card), tempfile.TemporaryDirectory() as d, \
                    patch("druta.vfheadroom.active_dir", return_value=Path(d)), \
                    patch("druta.druta.dpg.does_item_exist", return_value=False):
                arch, base, boost = CARDS[card]
                gpu = bare_gpu()
                gpu.static = {}
                rails = FakeRails(gpu, stock_row(card), arch=arch)
                app = make_app(gpu)
                ceiling = min(base[0] + boost, base[1], base[2])
                app.set_lock_state(vf_state(app, ceiling))
                self.assertEqual(GPU.rail_ceiling_mv(rails.rows[0]), ceiling + 25.0)
                app.set_lock_state(None)
                self.assertEqual(rails.ceiling_terms(), base[:3])


@unittest.skipUnless(__import__("os").name == "nt", "Windows process identity")
class RealProcessOwnerTests(unittest.TestCase):
    def test_a_child_process_is_alive_then_dead_after_it_exits(self):
        import subprocess
        import sys
        import time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            deadline = time.time() + 5
            created = None
            while created is None and time.time() < deadline:
                created = vfheadroom.process_created(child.pid)
            owner = {"pid": child.pid, "created": created}
            self.assertEqual(vfheadroom.owner_state(owner), "alive")
            self.assertEqual(vfheadroom.owner_state({"pid": child.pid, "created": 1}), "dead")
        finally:
            child.kill()
            child.wait()
        self.assertEqual(vfheadroom.owner_state(owner), "dead")


# --------------------------------------------------------------------------- #
def clock_reads(app, n, prog, meas, util=99, name="GPC", grade=PRIV_CONFIRMED, scale=1):
    """n snapshots through the core tile and then the log check, as the UI
    loop does: B (in MHz) jitters by 8 kHz like a counter, so it earns trust."""
    from druta.nvbackend import PRIV_FREQ
    t = getattr(app, "_test_t", 0.0)
    with patch("druta.druta.dpg.set_value"), patch("druta.druta.dpg.configure_item"):
        for _ in range(n):
            t += 1.05
            i = app._test_i = getattr(app, "_test_i", 0) + 1
            a_khz, b_khz = int(round(prog * 1000)), int(round(meas * 1000)) + (i % 2) * 8
            d = {"core": round(prog / scale), "util_gpu": util, "pstate": 0, "clk_domains": [
                {"domain": 0, "name": name, "grade": grade, "kind": PRIV_FREQ,
                 "prog_khz": a_khz, "meas_khz": b_khz, "scale": scale,
                 "prog_mhz": a_khz / 1000.0, "meas_mhz": b_khz / 1000.0}]}
            app.refresh_real_clocks(d, now=t)
            app.check_clock_gap(d)
    app._test_t = t


class ClockGapCheckTests(unittest.TestCase):
    def setUp(self):
        from druta.druta import Druta
        self.app = Druta.__new__(Druta)
        self.app.log = Mock()
        self.app._clk_lock = None
        self.app._stale = False
        self.app.step_khz = Mock(return_value=15000)

    def tick(self, prog, meas, util=99, name="GPC", grade=PRIV_CONFIRMED, n=1):
        clock_reads(self.app, n, prog, meas, util=util, name=name, grade=grade,
                    scale=2 if name.endswith("2CLK") else 1)

    def warnings(self):
        return [c for c in self.app.log.call_args_list if "below the" in str(c)]

    def recoveries(self):
        return [c for c in self.app.log.call_args_list if "clock it shows again" in str(c)]

    def test_sustained_at_ceiling_gap_warns_once_and_reports_recovery(self):
        self.tick(2115, 2083, n=40)
        self.assertEqual(len(self.warnings()), 1)
        self.tick(2115, 2114, n=20)
        self.assertEqual(len(self.recoveries()), 1)

    def test_a_moving_clock_is_not_reported_as_recovery(self):
        self.tick(2115, 2083, n=20)
        for i in range(20):
            self.tick(2100 + 15 * (i % 2), 2070)
        self.assertEqual(self.recoveries(), [])

    def test_noise_short_gap_low_load_and_moving_clock_do_not_warn(self):
        self.tick(2115, 2112, n=40)
        self.tick(2115, 2083, n=10)
        self.tick(2115, 2083, util=60, n=40)
        for i in range(40):
            self.tick(2100 + 15 * (i % 2), 2070)
        self.assertEqual(self.warnings(), [])

    def test_unconfirmed_or_legacy_rows_are_not_checked(self):
        self.tick(2115, 2083, name="", n=40)
        self.tick(2115, 2083, grade="likely", n=40)
        self.tick(3822, 3760, name="GPC2CLK", n=40)
        self.assertEqual(self.warnings(), [])

    def test_threshold_follows_this_cards_clock_bin(self):
        self.app.step_khz = Mock(return_value=12657)
        self.tick(1911, 1900, n=40)
        self.assertEqual(self.warnings(), [])
        self.tick(1911, 1898, n=40)
        self.assertEqual(len(self.warnings()), 1)


class SecondReviewAppTests(AppTestCase):
    """The whole-branch review's findings, each pinned where the user sees it:
    the card's limits, the banner, the log and the marker file."""

    UUID = "GPU-test-0002"

    def path(self):
        return vfheadroom.marker_path(self.UUID, self.root)

    def set_owner(self, owner):
        data = json.loads(self.path().read_text(encoding="utf-8"))
        data["owner"] = owner
        self.path().write_text(json.dumps(data), encoding="utf-8")

    def banner(self):
        shown = {}
        with patch("druta.druta.dpg.does_item_exist", side_effect=lambda tag: tag == "hold_info"), \
                patch("druta.druta.dpg.set_value", side_effect=shown.__setitem__), \
                patch("druta.druta.dpg.configure_item"):
            self.app.draw_hold_banner()
        return shown.get("hold_info", "")

    def refresh(self):
        """The real rail readout refresh, which reconciles a raise with what
        a rail write left on the card."""
        self.app.refresh_volt_limits = type(self.app).refresh_volt_limits.__get__(self.app)
        with patch.object(type(self.app), "rail_limit_diagnostics", return_value={}, create=True), \
                patch.object(type(self.app), "rail_limit_support", return_value={}, create=True), \
                patch.object(type(self.app), "update_rail_limit_diagnostic_ui", create=True), \
                patch.object(type(self.app), "volt_limits_cells", return_value=None, create=True):
            self.gpu.read_volt_rail_state = lambda: None
            self.app.refresh_volt_limits()

    def ticks(self, n=1, d=None):
        for _ in range(n * self.app.HEADROOM_LOCK_CHECK_TICKS):
            self.app.watch_hold_headroom(d or {})

    def test_lowering_a_term_the_raise_did_not_touch_is_planned_again(self):
        self.hold(1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))   # overvoltage untouched
        self.rails.rows[0]["overvoltage"] = 1075.0 - 1125.0           # the user's slider
        self.refresh()
        # the hold now sits above the ceiling: the raise comes off and it says so
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1075.0))
        self.assertEqual(self.app._headroom_note[0], "withheld")
        self.assertIn("HEADROOM WITHHELD", self.banner())

    def test_a_failed_watch_restore_says_the_limits_are_still_raised(self):
        self.hold(1093.75)
        self.gpu.restore_hold_headroom = lambda force=False: (False, "interrupted")
        self.app.watch_hold_headroom({"nvvdd_live_mv": 1200.0})
        self.assertEqual(self.app._headroom_note[0], "raised")
        self.assertIn("VOLTAGE LIMITS STILL RAISED", self.banner())

    def test_a_live_trip_is_not_undone_by_a_re_plan(self):
        self.hold(1093.75)
        self.app.watch_hold_headroom({"nvvdd_live_mv": 1200.0})
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.rails.rows[0]["_boost_mv"] = 50.0                        # re-plans the headroom
        self.app.sync_hold_headroom(replan=True)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertIn("stays off until the hold changes", self.app._headroom_note[1])
        self.rails.rows[0]["_boost_mv"] = 25.0
        self.hold(1087.5)                                  # the hold moved, no release
        self.assertEqual(self.limits(), (1087.5, 1112.5, 1125.0))
        self.app.watch_hold_headroom({"nvvdd_live_mv": 1200.0})         # trips this one too
        self.app.set_lock_state(None)                                   # released
        self.hold(1087.5)                                               # the same point again
        self.assertEqual(self.limits(), (1087.5, 1112.5, 1125.0))

    def test_an_unreadable_re_plan_keeps_the_raise_for_the_same_hold(self):
        self.hold(1093.75)
        self.gpu.hold_headroom_rail_unavailable = lambda rail=0: ("transient", "status -1")
        self.app.sync_hold_headroom(replan=True)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.assertIsNotNone(self.gpu.hold_headroom_record())
        self.assertEqual(self.app._headroom_note[0], "unread")
        self.assertTrue(self.logged("the raise made for this hold stays"))
        self.assertFalse(self.logged("headroom: not applied"))

    def test_a_re_plan_whose_old_raise_will_not_come_off_says_it_is_still_raised(self):
        self.hold(1093.75)
        self.gpu.restore_hold_headroom = lambda force=False: (False, "interrupted")
        self.app.headroom_mv = 50.0                                     # re-plans this hold
        self.app.sync_hold_headroom()
        self.assertEqual(self.app._headroom_note[0], "raised")
        self.assertIn("VOLTAGE LIMITS STILL RAISED", self.banner())

    def test_an_unread_boost_is_not_a_moved_boost(self):
        self.hold(1093.75)
        writes = len(self.rails.writes)
        self.rails.rows[0].update(_absolute_ok=False, _boost_mv=0.0)  # a placeholder
        self.app.sync_hold_headroom()
        self.assertEqual(len(self.rails.writes), writes)

    def test_a_refusal_before_any_write_is_withheld_not_a_failed_write(self):
        self.rails.rows[0] = row(1212.5, 1212.5, 1212.5)
        self.hold(1187.5)
        self.assertEqual(self.rails.writes, [])
        self.assertEqual(self.app._headroom_note[0], "withheld")
        self.assertNotIn("write did not succeed", self.banner())

    def test_a_restore_that_raises_is_reported_not_raised(self):
        self.hold(1093.75)

        def boom(force=False):
            raise OSError("driver went away")
        self.gpu.restore_hold_headroom = boom
        ok, msg = self.app.restore_headroom()
        self.assertFalse(ok)
        self.assertIn("driver went away", msg)
        self.app.guard = lambda: True
        self.app.release_lock()                                        # refused, no exception
        self.assertEqual(self.app._clk_lock["kind"], self.app.LOCK_VF)

    def test_turned_off_with_a_failed_restore_is_retried_by_the_watch(self):
        self.hold(1093.75)
        real = self.gpu.restore_hold_headroom
        self.gpu.restore_hold_headroom = lambda force=False: (False, "interrupted")
        self.app.headroom_on = False
        self.app.sync_hold_headroom()
        self.assertEqual(self.app._headroom_note[0], "raised")
        self.gpu.restore_hold_headroom = real
        self.ticks()
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertIsNone(self.app._headroom_note)

    def test_a_failed_read_at_hold_time_is_retried_by_the_watch(self):
        calls = []

        def unavailable(rail=0):
            calls.append(1)
            return ("transient", "status -1") if len(calls) < 3 else None
        self.gpu.hold_headroom_rail_unavailable = unavailable
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.ticks(4)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_a_read_that_fails_inside_the_apply_is_retried_too(self):
        read, calls = self.rails.read, []

        def absolute_state_missing_once():
            rows = read()
            calls.append(1)
            if len(calls) == 1:
                rows[0]["_absolute_ok"] = False
            return rows
        self.gpu.read_volt_rail_limits = absolute_state_missing_once
        self.hold(1093.75)
        self.assertEqual(self.app._headroom_note[0], "unread")
        self.assertEqual(self.rails.writes, [])
        self.ticks(2)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_those_retries_are_bounded(self):
        self.gpu.hold_headroom_rail_unavailable = lambda rail=0: ("transient", "status -1")
        self.hold(1093.75)
        before = self.app.log.call_count
        self.ticks(20)
        self.assertEqual(self.app.log.call_count - before, self.app.HEADROOM_UNREAD_RETRIES)

    def test_no_raise_is_made_when_its_marker_cannot_be_written(self):
        with patch("druta.vfheadroom.mark_active", return_value=(False, "disk full")):
            self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.assertEqual(self.app._headroom_note[0], "unread")          # an I/O failure: retried
        self.ticks()
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.assertIsNotNone(vfheadroom.active_record(self.UUID))

    def test_a_stale_marker_is_rewritten_on_the_tick(self):
        self.hold(1093.75)
        record = self.gpu.hold_headroom_record()
        self.app._headroom_marker_last[self.UUID] = ("an older raise",)
        self.ticks()
        self.assertTrue(self.app.headroom_marker_current(self.UUID, record))

    def test_a_dead_marker_found_at_a_hold_is_judged_by_the_watch(self):
        self.hold(1093.75)
        self.set_owner(DEAD)                     # the window that made it has gone
        other = make_app(self.gpu)
        self.gpu._hold_headroom = None
        other.set_lock_state(vf_state(other, 1087.5))
        self.assertEqual(other._headroom_note[0], "withheld")
        for _ in range(other.HEADROOM_LOCK_CHECK_TICKS):
            other.watch_hold_headroom({})
        self.assertIsNotNone(other._stale_headroom)                   # the dialog's state

    def test_a_judged_marker_whose_delete_failed_does_not_block_holds(self):
        self.hold(1093.75)
        self.set_owner(DEAD)
        nxt = make_app(self.gpu)
        self.gpu._hold_headroom = None
        nxt.check_stale_headroom()
        with patch("druta.vfheadroom.clear_active", return_value=(False, "in use")):
            nxt.stale_headroom_dismiss()
        self.assertIsNone(nxt.headroom_blocker())
        self.assertTrue(any("could not be removed" in str(c) for c in nxt.log.call_args_list))
        for _ in range(nxt.HEADROOM_LOCK_CHECK_TICKS):
            nxt.watch_hold_headroom({})
        self.assertFalse(self.path().exists())

    def test_a_hold_withheld_for_an_unjudged_marker_is_raised_once_it_is_judged(self):
        self.hold(1093.75)
        self.set_owner(DEAD)
        self.rails.rows[0] = stock_row("turing")            # the old raise is gone already
        other = make_app(self.gpu)
        self.gpu._hold_headroom = None
        other.set_lock_state(vf_state(other, 1093.75))
        self.assertEqual(other._headroom_note[0], "withheld")
        for _ in range(other.HEADROOM_LOCK_CHECK_TICKS):
            other.watch_hold_headroom({})
        self.assertFalse(self.path().exists() and vfheadroom.active_record(self.UUID)["owner"] == DEAD)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.assertIsNone(other._headroom_note)

    def test_still_raised_is_retried_while_the_hold_stays(self):
        self.hold(1093.75)
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1093750}, None)
        real, calls = self.gpu.restore_hold_headroom, []

        def fails_twice(force=False):
            calls.append(1)
            return (False, "interrupted") if len(calls) <= 2 else real(force)
        self.gpu.restore_hold_headroom = fails_twice
        self.app.headroom_mv = 50.0
        self.app.sync_hold_headroom()
        self.assertEqual(self.app._headroom_note[0], "raised")
        self.ticks(2)
        self.assertIsNone(self.app._headroom_note)
        self.assertEqual(self.limits(), (1118.75, 1143.75, 1143.75))    # the new margin
                                                                        # (reliability + 25 boost)

    def test_a_pending_recovery_keeps_its_record_after_a_failed_release(self):
        lock = dict(vf_state(self.app, 1093.75), verified=False, recovery=True)
        self.app._clk_lock = lock
        self.app.vf_recovery_pending = lambda: True
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1150000}, None)
        self.app.reconfirm_hold_after_failed_release()
        self.assertTrue(self.app._clk_lock["recovery"])
        self.assertEqual(self.rails.writes, [])

    def test_a_restore_that_half_landed_is_not_re_planned_by_its_own_refresh(self):
        self.hold(1093.75)
        applies = []
        apply = self.gpu.apply_hold_headroom
        self.gpu.apply_hold_headroom = lambda *a, **k: applies.append(a) or apply(*a, **k)
        self.rails.rows[0]["alt_reliability"] = 1100.0 - 1093.75       # one field landed
        self.gpu.restore_hold_headroom = lambda force=False: (False, "verify failed")
        self.app.refresh_volt_limits = type(self.app).refresh_volt_limits.__get__(self.app)
        with patch("druta.druta.dpg.does_item_exist", side_effect=lambda tag: tag == "vlim_txt0"), \
                patch.object(type(self.app), "rail_limit_diagnostics", return_value={}, create=True), \
                patch.object(type(self.app), "rail_limit_support", return_value={}, create=True), \
                patch.object(type(self.app), "update_rail_limit_diagnostic_ui", create=True), \
                patch.object(type(self.app), "volt_limits_cells", return_value=None, create=True):
            self.gpu.read_volt_rail_state = lambda: None
            ok, _msg = self.app.restore_headroom()
        self.assertFalse(ok)
        self.assertEqual(applies, [])

    def test_a_parked_startup_profile_runs_once_there_is_nothing_to_judge(self):
        for state in ("missing", "unreadable", "alive"):
            with self.subTest(state=state):
                self.app._held_startup_request = {"name": "p", "profile": {}}
                self.app.begin_profile_load = Mock()
                if state == "missing":
                    marker = ("missing", None)
                elif state == "unreadable":
                    marker = ("unreadable", None)
                else:
                    marker = ("ok", {"rail": 0, "written_uv": {"alt_reliability": 1},
                                     "prior_uv": {"alt_reliability": 0}, "owner": DEAD})
                with patch("druta.vfheadroom.marker_state", return_value=marker), \
                        patch("druta.vfheadroom.owner_state", return_value="alive"):
                    self.app.check_stale_headroom()
                self.app.begin_profile_load.assert_called_once_with("p", {}, automatic=True)

    def test_a_second_window_does_not_take_a_live_raise_as_the_cards_start(self):
        self.hold(1093.75)                                             # window A raises
        gpu_b = bare_gpu()
        gpu_b.static = {"uuid": self.UUID}
        rails_b = FakeRails(gpu_b, stock_row("turing"))
        rails_b.rows = self.rails.rows                                 # the same card
        raised = tuple(int(round(self.rails.rows[0][k] * 1000)) for k in GPU.VOLT_LIMIT_FIELDS)
        gpu_b._volt_rail_initial_uv = {0: raised}                      # B's first read
        b = make_app(gpu_b)
        with patch("druta.vfheadroom.owner_state", return_value="alive"):
            b.check_stale_headroom()
        first = dict(zip(GPU.VOLT_LIMIT_FIELDS, gpu_b._volt_rail_initial_uv[0]))
        self.assertEqual((first["reliability"], first["alt_reliability"]), (0, 0))
        user = gpu_b.user_rail_limits()[0]                             # what B's profiles save
        self.assertEqual(GPU.abs_limit_mv(user, "alt_reliability"), 1093.75)
        self.assertTrue(any("another Druta window" in str(c) for c in b.log.call_args_list))


class ReleaseReviewAppTests(AppTestCase):
    """The 1.7.0 release review's findings in the headroom app code."""

    UUID = "GPU-test-0170"

    def ticks(self, n=1, d=None):
        for _ in range(n * self.app.HEADROOM_LOCK_CHECK_TICKS):
            self.app.watch_hold_headroom(d or {})

    def test_read_only_holds_back_a_raise_still_owed_and_resumes_on_unlock(self):
        calls = []

        def unavailable(rail=0):
            calls.append(1)
            return ("transient", "status -1") if len(calls) < 2 else None
        self.gpu.hold_headroom_rail_unavailable = unavailable
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.app.unlocked = lambda: False                      # 'Unlock controls' unticked
        self.ticks(4)
        self.assertEqual(self.rails.writes, [])                # nothing written while read-only
        self.app.unlocked = lambda: True
        self.ticks(4)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_read_only_still_takes_a_raise_off(self):
        self.hold(1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.app.unlocked = lambda: False
        self.app._clk_lock = None                              # the hold is gone
        self.ticks(1)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_a_second_windows_profiles_save_the_users_limits(self):
        self.hold(1093.75)                                     # window A raises
        gpu_b = bare_gpu()
        gpu_b.static = {"uuid": self.UUID}
        rails_b = FakeRails(gpu_b, stock_row("turing"))
        rails_b.rows = self.rails.rows                         # the same card
        raised = tuple(int(round(self.rails.rows[0][k] * 1000)) for k in GPU.VOLT_LIMIT_FIELDS)
        gpu_b._volt_rail_initial_uv = {0: raised}
        b = make_app(gpu_b)
        with patch("druta.vfheadroom.owner_state", return_value="alive"):
            b.check_stale_headroom()
        self.assertIsNone(gpu_b.hold_headroom_record())        # B has no raise of its own
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(gpu_b, state, None)
        captured = state["rail_limits_mv"][0] if 0 in state["rail_limits_mv"] \
            else state["rail_limits_mv"]["0"]
        self.assertEqual((captured["reliability"], captured["alt_reliability"]), (1068.75, 1093.75))

    def test_a_hand_load_drops_a_held_startup_profile(self):
        from druta.druta import Druta
        app = Druta.__new__(Druta)
        app.gpu, app.rail, app._i2c_busy = Mock(), None, False
        app.guard = Mock(return_value=True)
        app.rail_for_profile = Mock(return_value=None)
        app.autosave_before = Mock(return_value=True)
        app.log, app.profile_failure, app.finish_profile_load = Mock(), Mock(), Mock()
        app._held_startup_request = {"name": "sign-in", "profile": {"schema": 2}}
        with patch("druta.druta.profiles.preflight", return_value=None):
            app.begin_profile_load("mine", {"schema": 2, "scope": "fan"})
        self.assertIsNone(app._held_startup_request)
        app.begin_profile_load = Mock()
        app.resume_startup_profile()                           # the marker judged later
        app.begin_profile_load.assert_not_called()

    def test_a_card_switch_drops_a_held_startup_profile(self):
        from types import SimpleNamespace
        from druta.druta import Druta
        app = Druta.__new__(Druta)
        app.gpu = SimpleNamespace(static={}, slot=lambda: "0000:02:00.0")
        app._tim_lock = threading.Lock()
        app.log = Mock()
        app._held_startup_request = {"name": "sign-in", "profile": {}}
        app._stale_recheck = True
        app.reset_card_state()
        self.assertIsNone(app._held_startup_request)
        self.assertFalse(app._stale_recheck)
        self.assertTrue(any("not applied to this one" in str(c) for c in app.log.call_args_list))

    def test_the_margin_box_holds_the_curve_shortcuts(self):
        with patch("druta.druta.dpg.does_item_exist", return_value=True), \
                patch("druta.druta.dpg.is_item_active", return_value=False), \
                patch("druta.druta.dpg.is_item_focused", side_effect=lambda tag: tag == "hr_mv"):
            self.assertTrue(self.app.typing())


class AmpereHeadroomTests(unittest.TestCase):
    """Ampere gets headroom because #30 gave it rail control: through the
    rail getters of a card whose first read matches one RTX 3070 Ti
    (reliability 1081.25, alt-reliability and overvoltage 1100, boost 0),
    with the MSVDD mask rejected as there. Those values are that card's,
    not Ampere's: this pins the path, not the numbers."""

    def gpu(self):
        from tests.test_volt_rails import fake_gpu
        gpu = fake_gpu("turing", architecture=GPU.ARCH_AMPERE,
                       name="NVIDIA GeForce RTX 3070 Ti", devid=0x2482)
        gpu.nvapi.bases = (1081250, 1100000, 1100000, 662500)
        gpu.nvapi.sync_live()
        gpu.read_voltage_boost = Mock(return_value=0)
        gpu._lock = threading.RLock()
        return gpu

    def terms(self, gpu):
        row = gpu.read_volt_rail_limits()[0]
        return tuple(GPU.abs_limit_mv(row, k) for k in ("reliability", "alt_reliability", "overvoltage"))

    def test_a_hold_on_the_ceiling_is_raised_checked_and_restored(self):
        gpu = self.gpu()
        self.assertTrue(gpu.hold_headroom_architecture())
        self.assertIsNone(gpu.hold_headroom_rail_unavailable(0))
        self.assertEqual(self.terms(gpu), (1081.25, 1100.0, 1100.0))
        ok, message = gpu.apply_hold_headroom(1081.25, 25.0)
        self.assertTrue(ok, message)
        self.assertEqual(self.terms(gpu), (1106.25, 1106.25, 1106.25))
        record = gpu.hold_headroom_record()
        self.assertEqual(record["raised_ceiling_mv"], 1106.25)
        allowance = gpu.hold_headroom_allowance_mv(record)
        self.assertTrue(gpu.hold_headroom_live_ok(record, live_mv=1106.25)[0])   # as that card read
        self.assertFalse(gpu.hold_headroom_live_ok(record, live_mv=1106.25 + allowance + 1.0)[0])
        self.assertTrue(gpu.restore_hold_headroom()[0])
        self.assertEqual(self.terms(gpu), (1081.25, 1100.0, 1100.0))
        self.assertEqual(gpu.nvapi.control[0], [0, 0, 0, 0])


class MarkerFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.record = {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0,
                       "written_uv": {"alt_reliability": 25000}, "prior_uv": {"alt_reliability": 0}}

    def test_the_field_names_are_the_backends(self):
        self.assertEqual(vfheadroom.LIMIT_FIELDS, GPU.VOLT_LIMIT_FIELDS)

    def test_a_marker_that_appears_meanwhile_is_not_replaced(self):
        path = vfheadroom.marker_path("GPU-X", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"other": "window"}', encoding="utf-8")
        with patch("druta.vfheadroom.marker_state", return_value=("missing", None)):
            ok, msg = vfheadroom.mark_active("GPU-X", self.record, root=self.root)
        self.assertFalse(ok)
        self.assertIn("appeared", msg)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"other": "window"}')
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_a_clock_step_does_not_turn_a_live_owner_dead(self):
        with patch("druta.vfheadroom.process_created", return_value=5), \
                patch("druta.vfheadroom.boot_filetime", return_value=10_000):
            self.assertEqual(vfheadroom.owner_state({"pid": 4242, "created": 5}), "alive")
            self.assertEqual(vfheadroom.owner_state({"pid": 4242, "created": 6}), "dead")

    def test_unknown_versions_fields_and_deep_nesting_are_unreadable_not_errors(self):
        path = vfheadroom.marker_path("GPU-X", self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        for text in (json.dumps(dict(self.record, version=99)),
                     json.dumps(dict(self.record, written_uv={"alt_rel": 1}, prior_uv={"alt_rel": 0})),
                     "[" * 200000):
            with self.subTest(text=text[:40]):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(vfheadroom.marker_state("GPU-X", self.root), ("unreadable", None))
        settings = self.root / "vf-headroom.json"
        settings.write_text("[" * 200000, encoding="utf-8")
        self.assertEqual(vfheadroom.load(settings), (True, 25.0))


class SettingTests(unittest.TestCase):
    def test_default_is_on_with_25_mv(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(vfheadroom.load(Path(d) / "missing.json"), (True, 25.0))

    def test_round_trip_and_clamping(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "vf-headroom.json"
            self.assertTrue(vfheadroom.save(False, 500, path)[0])
            self.assertEqual(vfheadroom.load(path), (False, vfheadroom.MAX_MARGIN_MV))

    def test_malformed_file_keeps_protection_on(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "vf-headroom.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(vfheadroom.load(path), (True, 25.0))
            path.write_text('{"enabled": "no", "margin_mv": "abc"}', encoding="utf-8")
            self.assertEqual(vfheadroom.load(path), (True, 25.0))


if __name__ == "__main__":
    unittest.main()
