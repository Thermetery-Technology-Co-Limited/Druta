# Druta - hardware-free tests for V/F hold headroom.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""A hold on the effective voltage ceiling delivers less clock than it
programs (measured on one TU102). These tests cover the software around the
fix: which ceiling terms are raised and to what, that the running voltage is
never raised (holds above the ceiling are withheld, the raise always comes off
while the lock still pins the rail, the live rail is watched), that the raise
is recorded and undone exactly, and that crash evidence is kept, owned and
never mistaken for a live window's raise. Card values are fixtures for several
generations, not constants."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from druta import vfheadroom
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
        self.live = None
        self.corrupt_next_write = False
        gpu.read_volt_rail_limits = self.read
        gpu._write_rail_records = self.write
        gpu._verify_rail_records = self.verify
        gpu.volt_rail_limits_supported = lambda rail=None: True
        gpu.volt_rail_limit_fields = lambda rail: GPU.VOLT_LIMIT_FIELDS
        gpu.read_voltage_boost = lambda: 100
        gpu.read_rail_live_mv = lambda rail: self.live if rail == 0 else None
        gpu.voltage_xoc_enabled = False
        gpu.arch = lambda: arch

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
    return GPU.__new__(GPU)


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

    def test_live_voltage_above_the_old_ceiling_undoes_the_raise(self):
        gpu, rails, *_ = self.make()
        rails.live = 1112.5                          # the rail climbed into the raise
        ok, msg = gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("voltage rose", msg)
        self.assertIsNone(gpu.hold_headroom_record())
        self.assertEqual(rails.ceiling_terms(), (1068.75, 1093.75, 1125.0))

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
        self.assertFalse(ok)
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
        for arch, want in ((GPU.ARCH_TURING, True), (GPU.ARCH_PASCAL, True), (10, True),
                           (7, False), (8, False), (GPU.ARCH_MAXWELL, False), (None, None)):
            with self.subTest(arch=arch):
                gpu.arch = lambda a=arch: a
                self.assertIs(gpu.hold_headroom_architecture(), want)


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
        self.gpu.arch = lambda: 7                    # Ampere
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.app.log.assert_not_called()

    def test_unread_architecture_is_a_visible_retryable_failure(self):
        self.gpu.arch = lambda: None
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.assertEqual(self.app._headroom_note[0], "failed")
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
        self.gpu._write_rail_records = lambda records: (False, None)
        self.app.set_lock_state(None)
        self.hold(1087.5)
        self.assertEqual(self.app._headroom_note[0], "failed")

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


class OrderingTests(AppTestCase):
    """The raise comes off while the lock still pins the rail - never after."""

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
    def test_live_voltage_above_the_old_ceiling_undoes_the_raise(self):
        self.hold(1093.75)
        self.app.watch_hold_headroom({"vcore_mv": 1112.5})
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))
        self.assertTrue(self.logged("voltage rose"))

    def test_voltage_at_the_hold_keeps_the_raise(self):
        self.hold(1093.75)
        self.gpu.read_vf_lock_status = lambda domain=None: ({"volt_uV": 1093750}, None)
        for _ in range(8):
            self.app.watch_hold_headroom({"vcore_mv": 1093.75})
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

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
class ClockGapCheckTests(unittest.TestCase):
    def setUp(self):
        from druta.druta import Druta
        self.app = Druta.__new__(Druta)
        self.app.log = Mock()
        self.app._clk_lock = None
        self.app.step_khz = Mock(return_value=15000)

    def tick(self, prog, meas, util=99, name="GPC", grade=PRIV_CONFIRMED, n=1):
        for _ in range(n):
            self.app.check_clock_gap({"util_gpu": util, "clk_domains": [
                {"domain": 0, "name": name, "grade": grade, "prog_mhz": prog, "meas_mhz": meas}]})

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
