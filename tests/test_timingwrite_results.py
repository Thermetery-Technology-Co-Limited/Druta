# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Timing outcomes must preserve process/readback failures; no EXE or GPU calls."""
import subprocess
import unittest
from unittest.mock import Mock, patch

from druta import timingwrite as tw


DRY_RUN = "CONFIG0 @0x10F290 0x0000002D -> 0x0000002E [would write]\n  RC 45 -> 46"
COMMIT = "CONFIG0 @0x10F290 0x0000002D -> 0x0000002E [write]\n  RC 45 -> 46"
SLOT = "0000:02:00.0"


class TimingWriteResultsTests(unittest.TestCase):
    def setUp(self):
        helper = tw.HelperContract("fake-nvtune.exe", (), ("--dry-run",),
                                   ("--commit",), True)
        patcher = patch.object(tw, "_helper", return_value=helper)
        patcher.start()
        self.addCleanup(patcher.stop)

    def apply(self, replies, assignments=None, **kwargs):
        with patch.object(tw, "_run", side_effect=replies) as run:
            result = tw.apply(assignments or {"RC": 46}, SLOT, **kwargs)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["slot"], SLOT)
        return result, run

    def test_success_and_unchanged_readback_remain_distinct(self):
        for after, expected in ((46, tw.LANDED), (45, tw.DROPPED), (47, tw.FAILED)):
            with self.subTest(after=after):
                (_, rows), _ = self.apply([("RC=45", 0), (DRY_RUN, 0),
                                          (COMMIT, 0), (f"RC={after}", 0)])
                self.assertEqual(rows[0].outcome, expected)
                self.assertEqual((rows[0].before, rows[0].after), (45, after))

    def test_commit_failure_is_not_reported_as_hardware_rejection(self):
        (_, rows), _ = self.apply([("RC=45", 0), (DRY_RUN, 0),
                                  ("access denied; nothing written", 5), ("RC=45", 0)])
        self.assertEqual(rows[0].outcome, tw.FAILED)
        self.assertIn("exited 5", rows[0].detail)
        self.assertIn("access denied", rows[0].detail)
        self.assertNotIn("reached the hardware", rows[0].detail)

    def test_nonzero_commit_preserves_partial_results_without_claiming_success(self):
        (_, rows), _ = self.apply([("RC=45 RFC=237", 0), (DRY_RUN, 0),
                                  ("first register changed; next write failed", 7),
                                  ("RC=46 RFC=237", 0)], {"RC": 46, "RFC": 238})
        self.assertEqual([r.outcome for r in rows], [tw.FAILED, tw.FAILED])
        self.assertEqual([r.after for r in rows], [46, 237])
        self.assertTrue(all("exited 7" in r.detail for r in rows))

    def test_failed_commit_and_unreadable_register_report_both_errors(self):
        (_, rows), _ = self.apply([("RC=45", 0), (DRY_RUN, 0),
                                  ("commit access denied", 5), ("device lost", 9)])
        self.assertEqual(rows[0].outcome, tw.FAILED)
        self.assertIsNone(rows[0].after)
        self.assertIn("commit access denied", rows[0].detail)
        self.assertIn("device lost", rows[0].detail)

    def test_unreadable_prewrite_value_stops_before_plan_or_commit(self):
        for reply in (("device lost", 9), ("no value", 0), OSError("cannot start helper")):
            with self.subTest(reply=reply):
                (plan, rows), run = self.apply([reply])
                self.assertFalse(plan.ok)
                self.assertEqual(rows[0].outcome, tw.FAILED)
                self.assertIn("nothing committed", rows[0].detail)
                self.assertEqual([c.args[0][0] for c in run.call_args_list], ["get"])

    def test_one_missing_prewrite_field_refuses_the_entire_batch(self):
        (plan, rows), run = self.apply([("RC=45", 0)], {"RC": 46, "RFC": 238})
        self.assertFalse(plan.ok)
        self.assertEqual([r.outcome for r in rows], [tw.FAILED, tw.FAILED])
        self.assertTrue(all("RFC" in r.detail for r in rows))
        run.assert_called_once()

    def test_postwrite_read_failure_cannot_be_called_a_dropped_write(self):
        for reply in (("device lost", 9), ("no value", 0), OSError("helper failed")):
            with self.subTest(reply=reply):
                (_, rows), _ = self.apply([("RC=45", 0), (DRY_RUN, 0), (COMMIT, 0), reply])
                self.assertEqual(rows[0].outcome, tw.FAILED)
                self.assertIsNone(rows[0].after)
                self.assertIn("read-back unavailable", rows[0].detail)

    def test_partial_postwrite_readback_keeps_the_known_value(self):
        (_, rows), _ = self.apply([("RC=45 RFC=237", 0), (DRY_RUN, 0),
                                  (COMMIT, 0), ("RC=46", 0)], {"RC": 46, "RFC": 238})
        self.assertEqual([r.outcome for r in rows], [tw.LANDED, tw.FAILED])
        self.assertEqual([r.after for r in rows], [46, None])

    def test_commit_timeout_does_not_reuse_before_as_observed_after(self):
        (_, rows), _ = self.apply([("RC=45", 0), (DRY_RUN, 0),
                                  subprocess.TimeoutExpired("fake-nvtune", 1)])
        self.assertEqual(rows[0].outcome, tw.FAILED)
        self.assertEqual(rows[0].before, 45)
        self.assertIsNone(rows[0].after)
        self.assertIn("unconfirmed", rows[0].detail)

    def test_precommit_guard_runs_after_dryrun_immediately_before_commit(self):
        events = []

        def run(args, *unused, **kwargs):
            self.assertEqual(kwargs["slot"], SLOT)
            if args[0] == "get":
                events.append("get")
                return ("RC=46" if "commit" in events else "RC=45"), 0
            if "--commit" in args:
                events.append("commit")
                return COMMIT, 0
            events.append("plan")
            return DRY_RUN, 0

        def guard():
            events.append("guard")
            return True, "top band still held"

        with patch.object(tw, "_run", side_effect=run):
            _, rows = tw.apply({"RC": 46}, SLOT, before_commit=guard)
        self.assertEqual(events, ["get", "plan", "guard", "commit", "get"])
        self.assertEqual(rows[0].outcome, tw.LANDED)

    def test_precommit_refusal_never_dispatches_write(self):
        guard = Mock(return_value=(False, "memory left the top band"))
        (_, rows), run = self.apply([("RC=45", 0), (DRY_RUN, 0)], before_commit=guard)
        guard.assert_called_once_with()
        self.assertEqual(rows[0].outcome, tw.TOOL_REFUSED)
        self.assertIn("memory left", rows[0].detail)
        self.assertTrue(all("--commit" not in c.args[0] for c in run.call_args_list))

    def test_precommit_exception_never_dispatches_write(self):
        guard = Mock(side_effect=RuntimeError("GPU read failed"))
        (_, rows), run = self.apply([("RC=45", 0), (DRY_RUN, 0)], before_commit=guard)
        self.assertEqual(rows[0].outcome, tw.FAILED)
        self.assertIn("GPU read failed", rows[0].detail)
        self.assertEqual(run.call_count, 2)

    def test_outstanding_warnings_refuse_without_force(self):
        guard = Mock()
        (_, rows), run = self.apply([("RC=45", 0), (DRY_RUN + "\n  range warning", 0)],
                                   before_commit=guard)
        self.assertEqual(rows[0].outcome, tw.TOOL_REFUSED)
        self.assertEqual(run.call_count, 2)
        guard.assert_not_called()

    def test_force_keeps_the_precommit_guard(self):
        guard = Mock(return_value=(False, "controls locked"))
        (_, rows), run = self.apply([("RC=45", 0), (DRY_RUN + "\n  range warning", 0)],
                                   force=True, before_commit=guard)
        guard.assert_called_once_with()
        self.assertEqual(rows[0].outcome, tw.TOOL_REFUSED)
        self.assertEqual(run.call_count, 2)

    def test_explicit_tool_warning_refusal_remains_refused(self):
        (_, rows), run = self.apply([("RC=45", 0), (DRY_RUN, 0),
                                    ("refusing to write with warnings", 1),
                                    ("RC=45", 0)])
        self.assertEqual(rows[0].outcome, tw.TOOL_REFUSED)
        self.assertEqual(rows[0].after, 45)
        self.assertEqual(run.call_count, 4)

    def test_warning_refusal_cannot_hide_a_partial_write(self):
        (_, rows), _ = self.apply([
            ("RC=45 FAW=24", 0), (DRY_RUN, 0),
            (COMMIT + "\nrefusing to write with warnings", 1),
            ("RC=46 FAW=24", 0)], {"RC": 46, "FAW": 1})
        self.assertEqual([r.outcome for r in rows], [tw.FAILED, tw.FAILED])
        self.assertEqual([r.after for r in rows], [46, 24])

    def test_warning_refusal_with_failed_readback_does_not_invent_unchanged_state(self):
        (_, rows), _ = self.apply([
            ("RC=45", 0), (DRY_RUN, 0),
            ("refusing to write with warnings", 1), ("device lost", 9)])
        self.assertEqual(rows[0].outcome, tw.FAILED)
        self.assertIsNone(rows[0].after)

    def test_empty_request_spawns_nothing(self):
        with patch.object(tw, "_run") as run:
            plan, rows = tw.apply({}, SLOT)
        self.assertTrue(plan.ok)
        self.assertEqual(rows, [])
        run.assert_not_called()

    def test_nonzero_read_status_cannot_be_hidden_by_parseable_output(self):
        with patch.object(tw, "_run", return_value=("RC=45\nfailed", 9)):
            with self.assertRaisesRegex(tw.WriteError, "failed"):
                tw.read_fields(["RC"], SLOT)


# Rows as nvtune's print_ops() in tool/src/cli.cpp writes them: two-space
# indents, hex(offset, 6) and hex(word, 8) as "0x" plus uppercase digits, and
# change rows padded by setw(12), setw(6) and setw(6). The --dry-run builds
# print "[would write]" and commits print "[write]". Register words are
# derived from GP102 words in nvtune's own capture transcript
# (tool/tests/manual/profile-roundtrip-20260923); no GPU was accessed.
def op_row(reg, offset, old, new, mode):
    return f"  {reg} @0x{offset:06X}  0x{old:08X} -> 0x{new:08X}  [{mode}]"


def change_row(name, old, new):
    return f"      {name:<12}{old:>6} -> {new:<6}"


def unchanged_row(reg, offset, word):
    return f"  {reg} @0x{offset:06X}  unchanged (0x{word:08X})"


def warning_row(text):
    return f"      ! {text}"


def nvtune_output(rows, footer="dry run complete: no registers written"):
    return "\n".join([f"{SLOT}  GP102 (Pascal)", "  [broadcast]"] + list(rows) + [footer])


MODES = ("would write", "write")
HALVED = "FAW more than halved (24 -> 12); step in small increments instead."


def rfc_write(mode):
    return [op_row("CONFIG0", 0x9A0290, 0x16489D3A, 0x16489E3A, mode),
            change_row("RFC", 157, 158)]


def faw_halved_write(mode):
    return [op_row("CONFIG3", 0x9A029C, 0x2200314A, 0x2200194A, mode),
            change_row("FAW", 24, 12), warning_row(HALVED)]


FAW_ALREADY_25 = unchanged_row("CONFIG3", 0x9A029C, 0x2200334A)


class UnchangedRegisterRowTests(unittest.TestCase):
    """A register that already holds the request is not a preview warning."""

    def setUp(self):
        helper = tw.HelperContract("fake-nvtune.exe", (), ("--dry-run",),
                                   ("--commit",), True)
        patcher = patch.object(tw, "_helper", return_value=helper)
        patcher.start()
        self.addCleanup(patcher.stop)

    def preview(self, rows, assignments):
        with patch.object(tw, "_run", return_value=(nvtune_output(rows), 0)):
            plan = tw.plan(assignments, SLOT)
        self.assertTrue(plan.ok, plan.error)
        return plan

    def test_unchanged_row_after_a_write_is_not_a_warning(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                plan = self.preview(rfc_write(mode) + [FAW_ALREADY_25],
                                    {"RFC": 158, "FAW": 25})
                self.assertEqual(plan.warnings, [])
                self.assertFalse(plan.needs_force)
                self.assertEqual([op["reg"] for op in plan.ops], ["CONFIG0"])
                self.assertEqual(plan.touches, ["RFC"])

    def test_commit_with_an_unchanged_register_is_sent_without_force(self):
        commit = nvtune_output(
            rfc_write("write") + [FAW_ALREADY_25],
            footer="      applied and verified\n  reminder: the driver reprograms "
                   "these on p-state changes. Use 'nvtune daemon' to hold them.")
        with patch.object(tw, "_run", side_effect=[
                (f"{SLOT}  RFC=157  FAW=25  ", 0),
                (nvtune_output(rfc_write("would write") + [FAW_ALREADY_25]), 0),
                (commit, 0), (f"{SLOT}  RFC=158  FAW=25  ", 0)]) as run:
            plan, rows = tw.apply({"RFC": 158, "FAW": 25}, SLOT)
        self.assertFalse(plan.needs_force)
        self.assertEqual([r.outcome for r in rows], [tw.LANDED, tw.LANDED])
        sent = [c.args[0] for c in run.call_args_list]
        self.assertEqual([args for args in sent if "--commit" in args],
                         [["set", "RFC=158", "FAW=25", "--commit"]])

    def test_warning_after_an_unchanged_row_still_requires_force(self):
        rfc_already_158 = unchanged_row("CONFIG0", 0x9A0290, 0x16489E3A)
        cl_already_19 = unchanged_row("CONFIG1", 0x9A0294, 0x31260393)
        for mode in MODES:
            cases = (
                ("unchanged first", [rfc_already_158] + faw_halved_write(mode),
                 {"RFC": 158, "FAW": 12}, ["FAW"]),
                ("between writes", rfc_write(mode) + [cl_already_19] + faw_halved_write(mode),
                 {"RFC": 158, "CL": 19, "FAW": 12}, ["RFC", "FAW"]),
            )
            for label, rows, assignments, touches in cases:
                with self.subTest(mode=mode, order=label):
                    plan = self.preview(rows, assignments)
                    self.assertEqual(plan.warnings, ["! " + HALVED])
                    self.assertTrue(plan.needs_force)
                    self.assertEqual(plan.touches, touches)

    def test_real_warning_beside_an_unchanged_register_still_refuses_commit(self):
        rows = rfc_write("would write") + [
            unchanged_row("CONFIG1", 0x9A0294, 0x31260393)] + faw_halved_write("would write")
        with patch.object(tw, "_run", side_effect=[
                (f"{SLOT}  RFC=157  CL=19  FAW=24  ", 0), (nvtune_output(rows), 0)]) as run:
            _, results = tw.apply({"RFC": 158, "CL": 19, "FAW": 12}, SLOT)
        self.assertEqual({r.outcome for r in results}, {tw.TOOL_REFUSED})
        self.assertTrue(all("--commit" not in c.args[0] for c in run.call_args_list))

    def test_stray_line_after_an_unchanged_row_is_still_a_warning(self):
        for mode in MODES:
            for stray in ("      unexpected helper output",
                          "stderr text appended after the report"):
                with self.subTest(mode=mode, stray=stray):
                    plan = self.preview(rfc_write(mode) + [FAW_ALREADY_25, stray],
                                        {"RFC": 158, "FAW": 25})
                    self.assertEqual(plan.warnings, [stray.strip()])
                    self.assertTrue(plan.needs_force)

    def test_preview_with_only_unchanged_registers_is_the_existing_no_op(self):
        plan = self.preview([unchanged_row("CONFIG0", 0x9A0290, 0x16489E3A), FAW_ALREADY_25],
                            {"RFC": 158, "FAW": 25})
        self.assertEqual((plan.ops, plan.warnings, plan.touches), ([], [], []))
        self.assertFalse(plan.needs_force)
        self.assertTrue(plan.summary().startswith("nothing to write"))

    def test_text_resembling_the_unchanged_row_is_still_a_warning(self):
        near_misses = (
            FAW_ALREADY_25 + " - the driver reprogrammed it",
            "    " + FAW_ALREADY_25,
            FAW_ALREADY_25.replace("0x9A029C", "0x9a029c").replace("0x2200334A", "0x2200334a"),
            FAW_ALREADY_25.replace("  unchanged", " unchanged"),
            "  CONFIG3 @0x9A029C  unchanged",
            "  CONFIG3 @0x9A029C  unchanged (0x2200334)",
            "      FAW left unchanged by the driver; verify before writing",
            warning_row("CONFIG3 unchanged (0x2200334A) but its readback differs"),
        )
        for mode in MODES:
            for line in near_misses:
                with self.subTest(mode=mode, line=line):
                    plan = self.preview(rfc_write(mode) + [line], {"RFC": 158, "FAW": 25})
                    self.assertEqual(plan.warnings, [line.strip()])
                    self.assertTrue(plan.needs_force)


if __name__ == "__main__":
    unittest.main()
