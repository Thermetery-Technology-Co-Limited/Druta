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
                                    ("refusing to write with warnings", 1)])
        self.assertEqual(rows[0].outcome, tw.TOOL_REFUSED)
        self.assertEqual(run.call_count, 3)

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


if __name__ == "__main__":
    unittest.main()
