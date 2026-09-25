# Druta - hardware-free tests for V/F hold headroom.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""A hold on the effective voltage ceiling delivers less clock than it
programs (measured on one TU102). These tests cover the software around the
fix: which ceiling terms are raised and to what, that nothing is ever lowered,
that the raise is recorded and undone exactly, and that a limit the user
changed in the meantime is left alone. Card values here are illustrative
fixtures, not generation-wide constants."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from druta import vfheadroom
from druta.nvbackend import GPU


def row(rel, alt, ov, vmin=643.75, boost=0.0, base=(1068.75, 1093.75, 1125.0, 643.75)):
    """One rail row as read_volt_rail_limits returns it: deltas against bases."""
    keys = GPU.VOLT_LIMIT_FIELDS
    absolute = dict(zip(keys, (rel, alt, ov, vmin)))
    bases = dict(zip(keys, base))
    out = {k: absolute[k] - bases[k] for k in keys}
    out.update(_base_mv=bases, _boost_mv=boost)
    return out


class FakeRails:
    """In-memory rail store standing in for the driver: writes land as exact
    microvolt deltas and read back unchanged unless told otherwise."""

    def __init__(self, gpu, first):
        self.gpu = gpu
        self.rows = {0: first}
        self.writes = []
        self.corrupt_next_write = False
        gpu.read_volt_rail_limits = self.read
        gpu._write_rail_records = self.write
        gpu._verify_rail_records = self.verify
        gpu.volt_rail_limits_supported = lambda rail=None: True
        gpu.volt_rail_limit_fields = lambda rail: GPU.VOLT_LIMIT_FIELDS
        gpu.read_voltage_boost = lambda: 100
        gpu.voltage_xoc_enabled = False

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

    def absolute(self, key):
        return GPU.abs_limit_mv(self.rows[0], key)


def bare_gpu():
    return GPU.__new__(GPU)


class PlannerTests(unittest.TestCase):
    def test_every_ceiling_term_is_raised_and_boost_is_not_counted_twice(self):
        # Stock TU102 first-read limits at 100 % boost: reliability 1068.75 +
        # 25 mV contribution is the binding term at 1093.75.
        targets, why = GPU.hold_headroom_targets(
            row(1068.75, 1093.75, 1125.0, boost=25.0), 1093.75, 25.0, 1200.0)
        self.assertIsNone(why)
        self.assertEqual(targets, {"reliability": 1093.75, "alt_reliability": 1118.75})

    def test_reliability_alone_binding_is_still_raised(self):
        # alt-reliability and overvoltage far above, reliability + boost at the
        # hold: the case that held 1093.75 while asked for 1125 and lost clock.
        targets, _ = GPU.hold_headroom_targets(
            row(1068.75, 1150.0, 1150.0, boost=25.0), 1125.0, 25.0, 1200.0)
        self.assertEqual(targets, {"reliability": 1125.0})

    def test_nothing_is_raised_when_the_ceiling_already_clears_the_hold(self):
        targets, why = GPU.hold_headroom_targets(
            row(1175.0, 1175.0, 1175.0), 1150.0, 25.0, 1200.0)
        self.assertEqual((targets, why), ({}, None))

    def test_a_field_already_higher_is_never_lowered(self):
        # alt-reliability is already above hold + margin and overvoltage sits
        # exactly on it: only reliability rises, and nothing moves down.
        targets, _ = GPU.hold_headroom_targets(
            row(1068.75, 1180.0, 1125.0), 1100.0, 25.0, 1200.0)
        self.assertEqual(targets, {"reliability": 1125.0})

    def test_plan_is_refused_above_the_bound_and_on_bad_input(self):
        for hold, margin, why_part in ((1190.0, 25.0, "exceeds"), (1100.0, 0.0, "positive"),
                                       (float("nan"), 25.0, "finite"), ("x", 25.0, "numbers")):
            with self.subTest(hold=hold, margin=margin):
                targets, why = GPU.hold_headroom_targets(
                    row(1068.75, 1093.75, 1125.0), hold, margin, 1200.0)
                self.assertIsNone(targets)
                self.assertIn(why_part, why)

    def test_missing_or_unreadable_term_refuses_instead_of_guessing(self):
        partial = row(1068.75, 1093.75, 1125.0)
        del partial["overvoltage"]
        self.assertIsNone(GPU.hold_headroom_targets(partial, 1100.0, 25.0, 1200.0)[0])
        nobase = row(1068.75, 1093.75, 1125.0)
        nobase["_base_mv"] = {}
        self.assertIsNone(GPU.hold_headroom_targets(nobase, 1100.0, 25.0, 1200.0)[0])

    def test_other_card_bases_are_used_as_read(self):
        # Blackwell-style fixture bases; nothing TU102-specific is assumed.
        base = (1040.0, 1060.0, 1200.0, 800.0)
        targets, _ = GPU.hold_headroom_targets(
            row(1040.0, 1060.0, 1200.0, vmin=800.0, base=base), 1060.0, 12.5, 1200.0)
        self.assertEqual(targets, {"reliability": 1072.5, "alt_reliability": 1072.5})


class ApplyRestoreTests(unittest.TestCase):
    def setUp(self):
        self.gpu = bare_gpu()
        self.rails = FakeRails(self.gpu, row(1068.75, 1093.75, 1125.0, boost=25.0))

    def test_apply_writes_only_the_planned_fields_and_restore_puts_them_back(self):
        ok, msg = self.gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertTrue(ok, msg)
        self.assertEqual((self.rails.absolute("reliability"), self.rails.absolute("alt_reliability"),
                          self.rails.absolute("overvoltage"), self.rails.absolute("vmin")),
                         (1093.75, 1118.75, 1125.0, 643.75))
        self.assertTrue(self.gpu.hold_headroom_record())
        ok, msg = self.gpu.restore_hold_headroom()
        self.assertTrue(ok, msg)
        self.assertEqual((self.rails.absolute("reliability"), self.rails.absolute("alt_reliability")),
                         (1068.75, 1093.75))
        self.assertIsNone(self.gpu.hold_headroom_record())

    def test_apply_does_not_require_the_free_form_limit_switch(self):
        self.gpu.volt_limits_write_enabled = False
        self.assertTrue(self.gpu.apply_hold_headroom(1093.75, 25.0)[0])

    def test_already_clear_ceiling_writes_nothing_and_records_nothing(self):
        self.rails.rows[0] = row(1175.0, 1175.0, 1175.0)
        ok, _ = self.gpu.apply_hold_headroom(1100.0, 25.0)
        self.assertTrue(ok)
        self.assertEqual(self.rails.writes, [])
        self.assertIsNone(self.gpu.hold_headroom_record())

    def test_a_second_hold_replaces_the_first_raise_instead_of_stacking(self):
        self.gpu.apply_hold_headroom(1093.75, 25.0)
        self.gpu.apply_hold_headroom(1100.0, 25.0)
        self.assertEqual(self.rails.absolute("alt_reliability"), 1125.0)
        self.gpu.restore_hold_headroom()
        self.assertEqual((self.rails.absolute("reliability"), self.rails.absolute("alt_reliability"),
                          self.rails.absolute("overvoltage")), (1068.75, 1093.75, 1125.0))

    def test_restore_leaves_a_field_the_user_changed_since(self):
        self.gpu.apply_hold_headroom(1093.75, 25.0)
        self.rails.rows[0]["alt_reliability"] = 1150.0 - 1093.75   # user moved it
        ok, _ = self.gpu.restore_hold_headroom()
        self.assertTrue(ok)
        self.assertEqual(self.rails.absolute("alt_reliability"), 1150.0)
        self.assertEqual(self.rails.absolute("reliability"), 1068.75)
        self.assertIsNone(self.gpu.hold_headroom_record())

    def test_unverified_write_is_rolled_back(self):
        self.rails.corrupt_next_write = True
        ok, msg = self.gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("not verified", msg)
        self.assertIn("limits restored", msg)
        self.assertEqual((self.rails.absolute("reliability"), self.rails.absolute("alt_reliability")),
                         (1068.75, 1093.75))
        self.assertIsNone(self.gpu.hold_headroom_record())

    def test_unreadable_limits_apply_nothing(self):
        self.gpu.volt_rail_limits_supported = lambda rail=None: False
        ok, msg = self.gpu.apply_hold_headroom(1093.75, 25.0)
        self.assertFalse(ok)
        self.assertIn("no headroom applied", msg)
        self.assertEqual(self.rails.writes, [])

    def test_user_rail_limits_hides_only_our_raise(self):
        self.gpu.apply_hold_headroom(1093.75, 25.0)
        user = self.gpu.user_rail_limits()[0]
        self.assertEqual((GPU.abs_limit_mv(user, "reliability"), GPU.abs_limit_mv(user, "alt_reliability"),
                          GPU.abs_limit_mv(user, "overvoltage")), (1068.75, 1093.75, 1125.0))
        live = self.gpu.read_volt_rail_limits()[0]
        self.assertEqual(GPU.abs_limit_mv(live, "alt_reliability"), 1118.75)


class LockStateHookTests(unittest.TestCase):
    """set_lock_state is the one place every lock change passes through; the
    limits must follow the V/F hold recorded there."""

    def setUp(self):
        from druta.druta import Druta
        self.app = Druta.__new__(Druta)
        self.app.gpu = bare_gpu()
        self.rails = FakeRails(self.app.gpu, row(1068.75, 1093.75, 1125.0, boost=25.0))
        self.app.headroom_on, self.app.headroom_mv = True, 25.0
        self.app.log = Mock()
        self.app._clk_lock = None
        self.dpg = patch("druta.druta.dpg.does_item_exist", return_value=False)
        self.dpg.start()
        self.addCleanup(self.dpg.stop)

    def hold(self, mv, **extra):
        state = {"kind": self.app.LOCK_VF, "idx": 103, "req_mv": mv, "domain": 6,
                 "got_idx": 103, "got_mv": mv, "got_mhz": 2100.0}
        state.update(extra)
        self.app.set_lock_state(state)

    def limits(self):
        return tuple(self.rails.absolute(k) for k in ("reliability", "alt_reliability", "overvoltage"))

    def test_confirmed_hold_raises_and_release_restores(self):
        self.hold(1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.app.set_lock_state(None)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_same_hold_again_writes_once(self):
        self.hold(1093.75)
        self.hold(1093.75)
        self.assertEqual(len(self.rails.writes), 1)

    def test_moving_the_hold_replans_from_the_users_limits(self):
        self.hold(1093.75)
        self.hold(1100.0)
        self.assertEqual(self.limits(), (1100.0, 1125.0, 1125.0))
        self.app.set_lock_state(None)
        self.assertEqual(self.limits(), (1068.75, 1093.75, 1125.0))

    def test_boost_change_replans_the_reliability_term(self):
        self.hold(1093.75)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))
        self.rails.rows[0]["_boost_mv"] = 50.0     # boost now contributes more
        self.app.sync_hold_headroom(replan=True)
        # reliability 1068.75 + 50 already clears 1118.75: it goes back to the
        # user's value instead of keeping a raise the new boost makes redundant
        self.assertEqual(self.limits(), (1068.75, 1118.75, 1125.0))

    def test_unconfirmed_hold_keeps_the_existing_raise(self):
        self.hold(1093.75)
        self.hold(1093.75, verified=False)
        self.assertEqual(self.limits(), (1093.75, 1118.75, 1125.0))

    def test_setting_off_means_no_raise(self):
        self.app.headroom_on = False
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])

    def test_nvml_frequency_lock_gets_no_headroom(self):
        self.app.set_lock_state({"kind": self.app.LOCK_NVML, "lo": 2100, "hi": 2100})
        self.assertEqual(self.rails.writes, [])

    def test_backend_without_the_feature_is_left_alone(self):
        self.app.gpu = Mock()
        self.hold(1093.75)
        self.app.gpu.apply_hold_headroom.assert_not_called()

    def test_a_backend_failure_is_logged_not_raised(self):
        self.app.gpu.volt_rail_limits_supported = lambda rail=None: False
        self.hold(1093.75)
        self.assertEqual(self.rails.writes, [])
        self.assertTrue(any("no headroom applied" in str(c) for c in self.app.log.call_args_list))


class CrashMarkerTests(unittest.TestCase):
    """A raise that outlives the process must be recognisable next session,
    and only while the card still carries exactly what Druta wrote."""

    UUID = "GPU-test-0001"

    def setUp(self):
        from druta.druta import Druta
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "vf-headroom-active.json"
        redirect = patch("druta.vfheadroom.active_path", return_value=self.path)
        redirect.start()
        self.addCleanup(redirect.stop)
        self.app = Druta.__new__(Druta)
        self.app.gpu = bare_gpu()
        self.app.gpu.static = {"uuid": self.UUID}
        self.rails = FakeRails(self.app.gpu, row(1068.75, 1093.75, 1125.0, boost=25.0))
        self.app.headroom_on, self.app.headroom_mv = True, 25.0
        self.app.log, self.app.log_once, self.app.show_win = Mock(), Mock(), Mock()
        self.app._clk_lock = None
        exists = patch("druta.druta.dpg.does_item_exist", return_value=False)
        exists.start()
        self.addCleanup(exists.stop)

    def hold(self, mv):
        self.app.set_lock_state({"kind": self.app.LOCK_VF, "idx": 103, "req_mv": mv, "domain": 6,
                                 "got_idx": 103, "got_mv": mv, "got_mhz": 2100.0})

    def test_marker_follows_the_raise(self):
        self.hold(1093.75)
        self.assertIsNotNone(vfheadroom.active_record(self.UUID, self.path))
        self.app.set_lock_state(None)
        self.assertIsNone(vfheadroom.active_record(self.UUID, self.path))

    def test_crash_leaves_a_marker_the_next_session_reports(self):
        self.hold(1093.75)                          # then the process dies
        from druta.druta import Druta
        nxt = Druta.__new__(Druta)
        nxt.gpu, nxt.log, nxt.show_win = self.app.gpu, Mock(), Mock()
        nxt.gpu._hold_headroom = None               # a new process has no record
        nxt.check_stale_headroom()
        self.assertIsNotNone(nxt._stale_headroom)
        self.assertTrue(any("still raised" in str(c) for c in nxt.log.call_args_list))

    def test_marker_is_dropped_silently_once_the_limits_are_back(self):
        self.hold(1093.75)
        self.app.gpu._hold_headroom = None
        self.rails.rows[0] = row(1068.75, 1093.75, 1125.0, boost=25.0)   # e.g. after PnP
        self.app.check_stale_headroom()
        self.assertIsNone(getattr(self.app, "_stale_headroom", None))
        self.assertIsNone(vfheadroom.active_record(self.UUID, self.path))

    def test_a_new_session_does_not_clear_an_old_marker_before_checking(self):
        vfheadroom.mark_active(self.UUID, {"rail": 0, "hold_mv": 1093.75, "margin_mv": 25.0,
                                           "written_uv": {"alt_reliability": 25000},
                                           "prior_uv": {"alt_reliability": 0}}, self.path)
        self.app.mirror_headroom_marker()           # no record this session
        self.assertIsNotNone(vfheadroom.active_record(self.UUID, self.path))

    def test_dismiss_clears_and_a_refused_restart_keeps_the_marker(self):
        self.hold(1093.75)
        self.app._closing = False
        self.app.open_device_restart = Mock()       # refused: _closing stays False
        self.app.stale_headroom_restart()
        self.assertIsNotNone(vfheadroom.active_record(self.UUID, self.path))
        self.app.stale_headroom_dismiss()
        self.assertIsNone(vfheadroom.active_record(self.UUID, self.path))

    def test_no_uuid_means_no_marker(self):
        self.app.gpu.static = {}
        self.hold(1093.75)
        self.assertFalse(self.path.exists())

    def test_malformed_marker_is_ignored(self):
        self.path.write_text('{"%s": {"rail": "0", "written_uv": [], "prior_uv": {}}}' % self.UUID,
                             encoding="utf-8")
        self.assertIsNone(vfheadroom.active_record(self.UUID, self.path))


class ClockGapCheckTests(unittest.TestCase):
    def setUp(self):
        from druta.druta import Druta
        self.app = Druta.__new__(Druta)
        self.app.log = Mock()
        self.app._clk_lock = None
        self.app.step_khz = Mock(return_value=15000)

    def tick(self, prog, meas, util=99, name="GPC", n=1):
        for _ in range(n):
            self.app.check_clock_gap({"util_gpu": util, "clk_domains": [
                {"domain": 0, "name": name, "prog_mhz": prog, "meas_mhz": meas}]})

    def warnings(self):
        return [c for c in self.app.log.call_args_list if "below the" in str(c)]

    def test_sustained_at_ceiling_gap_warns_once_and_reports_recovery(self):
        self.tick(2115, 2083, n=40)
        self.assertEqual(len(self.warnings()), 1)
        self.tick(2115, 2114, n=20)
        self.assertTrue(any("clock it shows again" in str(c) for c in self.app.log.call_args_list))

    def test_noise_sized_gap_is_not_a_warning(self):
        self.tick(2115, 2112, n=40)
        self.assertEqual(self.warnings(), [])

    def test_short_gap_low_load_and_moving_clock_do_not_warn(self):
        self.tick(2115, 2083, n=10)                       # under 5 s
        self.tick(2115, 2083, util=60, n=40)              # not at load
        for i in range(40):                               # clock still moving
            self.tick(2100 + 15 * (i % 2), 2070)
        self.assertEqual(self.warnings(), [])

    def test_unclassified_domain_is_not_checked(self):
        self.tick(2115, 2083, name="", n=40)
        self.assertEqual(self.warnings(), [])

    def test_threshold_follows_this_cards_clock_bin(self):
        self.app.step_khz = Mock(return_value=12657)      # GP102-style grid
        self.tick(1911, 1900, n=40)                       # 11 MHz: under one bin
        self.assertEqual(self.warnings(), [])
        self.tick(1911, 1898, n=40)                       # 13 MHz: a full bin
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
