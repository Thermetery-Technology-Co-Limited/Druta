"""CLI contract regressions. All subprocesses are mocked; no GPU is opened."""
import json
import os
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import timings
import timingwrite


SLOT = "0000:02:00.0"
EXE = r"C:\nvtune\nvtune.exe"
COMPLETE = "dry run complete: no registers written"
PREVIEW = """0000:02:00.0  GP102 (Pascal)
  [broadcast]
  CONFIG3 @0x10022c  0x2200194a -> 0x22001b4a  [would write]
      FAW             12 -> 13
"""
UNCHANGED = "  CONFIG0 @0x100220  unchanged (0x00000001)\n"


def response(output="", status=0, stderr=""):
    return subprocess.CompletedProcess([], status, stdout=output, stderr=stderr)


class WriterContractTests(unittest.TestCase):
    def setUp(self):
        find_patch = patch("timings.find_exe", return_value=EXE)
        spawn_patch = patch("subprocess.run", side_effect=AssertionError(
            "test attempted an unexpected subprocess"))
        self.find = find_patch.start()
        self.spawn = spawn_patch.start()
        self.addCleanup(find_patch.stop)
        self.addCleanup(spawn_patch.stop)

    def respond(self, *responses):
        self.spawn.side_effect = list(responses)

    def argv(self):
        return [call.args[0] for call in self.spawn.call_args_list]

    def apply_responses(self, preview=None, commit=None, after=None):
        self.respond(response(SLOT + "  FAW=12"),
                     preview if preview is not None else response(PREVIEW + COMPLETE),
                     commit if commit is not None else response("applied and verified"),
                     after if after is not None else response(SLOT + "  FAW=13"))

    def test_preview_explicitly_requests_dry_run_and_one_card(self):
        self.respond(response(PREVIEW + COMPLETE))
        plan = timingwrite.plan({"FAW": 13}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertFalse(plan.needs_force)
        self.assertEqual(plan.touches, ["FAW"])
        self.assertEqual(self.argv(), [[EXE, "set", "-d", SLOT,
                                        "--dry-run", "FAW=13"]])

    def test_old_tool_rejection_never_retries_bare_set(self):
        self.respond(response(status=2, stderr="error: unknown option '--dry-run'"))
        plan = timingwrite.plan({"FAW": 13}, SLOT)
        self.assertFalse(plan.ok)
        self.assertIn("--dry-run", plan.error)
        self.assertEqual(len(self.argv()), 1)

    def test_partial_output_with_failed_status_is_not_a_plan(self):
        self.respond(response(PREVIEW + COMPLETE, status=1))
        self.assertFalse(timingwrite.plan({"FAW": 13}, SLOT).ok)

    def test_successful_status_requires_complete_parseable_preview(self):
        outputs = ["", PREVIEW, COMPLETE,
                   PREVIEW.replace("[would write]", "[write]") + COMPLETE,
                   PREVIEW.replace("FAW             12 -> 13", "FAW 12 -> unknown") + COMPLETE,
                   PREVIEW + "CONFIG0 @bad  unchanged (0x1)\n" + COMPLETE,
                   PREVIEW + COMPLETE + "\nerror: interrupted"]
        for output in outputs:
            with self.subTest(output=output):
                self.respond(response(output))
                self.assertFalse(timingwrite.plan({"FAW": 13}, SLOT).ok)

    def test_unchanged_only_preview_requires_mode_acknowledgement(self):
        self.respond(response(UNCHANGED + COMPLETE))
        plan = timingwrite.plan({"RC": 1}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(plan.ops, [])
        self.respond(response(UNCHANGED))
        self.assertFalse(timingwrite.plan({"RC": 1}, SLOT).ok)

    def test_unchanged_rows_and_completion_are_not_warnings(self):
        self.respond(response(PREVIEW + UNCHANGED + COMPLETE))
        plan = timingwrite.plan({"FAW": 13, "RC": 1}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertFalse(plan.needs_force)

    def test_warning_requires_force_before_commit(self):
        self.apply_responses(preview=response(PREVIEW + "      ! FAW exceeds guide\n" + COMPLETE))
        plan, results = timingwrite.apply({"FAW": 13}, SLOT)
        self.assertTrue(plan.needs_force)
        self.assertEqual(results[0].outcome, timingwrite.TOOL_REFUSED)
        self.assertEqual(len(self.argv()), 2)
        self.assertNotIn("--commit", self.argv()[-1])

    def test_real_warning_arrow_is_not_a_malformed_change(self):
        # Gpu::field_warnings uses an arrow inside its explanatory warning.
        warning = "      ! FAW more than halved (12 -> 5); step in small increments instead.\n"
        self.respond(response(PREVIEW.replace("12 -> 13", "12 -> 5") + warning + COMPLETE))
        plan = timingwrite.plan({"FAW": 5}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(plan.warnings, [warning.strip()])

    def test_commit_follows_successful_preview_and_readback(self):
        self.apply_responses()
        plan, results = timingwrite.apply({"FAW": 13}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(results[0].outcome, timingwrite.LANDED)
        self.assertEqual(self.argv(), [
            [EXE, "get", "-d", SLOT, "FAW"],
            [EXE, "set", "-d", SLOT, "--dry-run", "FAW=13"],
            [EXE, "set", "-d", SLOT, "FAW=13", "--commit"],
            [EXE, "get", "-d", SLOT, "FAW"]])

    def test_force_is_only_added_to_commit_after_explicit_preview(self):
        self.apply_responses(preview=response(PREVIEW + "      ! warning\n" + COMPLETE))
        _, results = timingwrite.apply({"FAW": 13}, SLOT, force=True)
        self.assertEqual(results[0].outcome, timingwrite.LANDED)
        self.assertNotIn("--force", self.argv()[1])
        self.assertEqual(self.argv()[2][-2:], ["--commit", "--force"])

    def test_failed_preview_prevents_commit_even_with_force(self):
        self.apply_responses(preview=response(PREVIEW + COMPLETE, status=1))
        plan, results = timingwrite.apply({"FAW": 13}, SLOT, force=True)
        self.assertFalse(plan.ok)
        self.assertEqual(results[0].outcome, timingwrite.FAILED)
        self.assertEqual(len(self.argv()), 2)

    def test_failed_commit_is_not_a_hardware_rejection(self):
        self.apply_responses(commit=response(status=2, stderr="error: unsupported option"))
        _, results = timingwrite.apply({"FAW": 13}, SLOT)
        self.assertEqual(results[0].outcome, timingwrite.FAILED)
        self.assertIsNone(results[0].after)
        self.assertIn("unsupported option", results[0].detail)
        self.assertEqual(len(self.argv()), 3)

    def test_explicit_tool_refusal_is_preserved(self):
        self.apply_responses(commit=response(status=1, stderr="refusing to write with warnings outstanding"))
        _, results = timingwrite.apply({"FAW": 13}, SLOT)
        self.assertEqual(results[0].outcome, timingwrite.TOOL_REFUSED)
        self.assertEqual(len(self.argv()), 3)

    def test_failed_or_missing_before_read_prevents_any_set(self):
        for output in [response("FAW=12", status=1), response("no values")]:
            with self.subTest(output=output):
                self.spawn.reset_mock()
                self.respond(output)
                plan, results = timingwrite.apply({"FAW": 13}, SLOT)
                self.assertFalse(plan.ok)
                self.assertEqual(results[0].outcome, timingwrite.FAILED)
                self.assertEqual(len(self.argv()), 1)

    def test_failed_or_missing_after_read_is_not_hardware_rejection(self):
        for output in [response("FAW=12", status=1), response("no values")]:
            with self.subTest(output=output):
                self.apply_responses(after=output)
                _, results = timingwrite.apply({"FAW": 13}, SLOT)
                self.assertEqual(results[0].outcome, timingwrite.FAILED)
                self.assertIsNone(results[0].after)
                self.assertIn("readback failed", results[0].detail)

    def test_subprocess_error_returns_failed_result(self):
        self.spawn.side_effect = OSError("not a valid Win32 application")
        plan, results = timingwrite.apply({"FAW": 13}, SLOT)
        self.assertFalse(plan.ok)
        self.assertEqual(results[0].outcome, timingwrite.FAILED)

    def test_missing_slot_and_empty_assignments_never_spawn(self):
        self.assertFalse(timingwrite.plan({"FAW": 13}, None).ok)
        plan, results = timingwrite.apply({"FAW": 13}, None)
        self.assertFalse(plan.ok)
        self.assertEqual(results[0].outcome, timingwrite.FAILED)
        self.assertEqual(timingwrite.apply({}, SLOT)[1], [])
        self.spawn.assert_not_called()

    def test_restore_names_one_card_and_explicit_commit(self):
        self.respond(response("restored"))
        with patch("os.path.exists", return_value=True):
            self.assertEqual(timingwrite.restore(r"C:\saved.json", SLOT),
                             (True, "restored"))
        self.assertEqual(self.argv(), [[EXE, "restore", "-d", SLOT,
                                        "-i", r"C:\saved.json", "--commit"]])


class ReaderContractTests(unittest.TestCase):
    def test_failed_fields_output_is_never_cached_as_valid(self):
        with patch("timings.find_exe", return_value=EXE), \
                patch("timings._run", return_value=response("partial field table", status=1)), \
                patch("timings.parse_fields") as parse:
            with self.assertRaises(timings.TimingsError):
                timings.field_table(refresh=True)
            parse.assert_not_called()

    def test_failed_save_is_rejected_even_if_it_left_a_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            def partial_save(exe, subcommand, args, **kwargs):
                self.assertEqual(subcommand, "save")
                with open(args[1], "w", encoding="utf-8") as stream:
                    json.dump({"_format": "nvtune-backup-1", "slot": SLOT,
                               "registers": {"broadcast": {}}}, stream)
                return response(status=1, stderr="driver read failed")

            with patch("timings.available", return_value=SimpleNamespace(ok=True, exe=EXE)), \
                    patch("timings.field_table", return_value=SimpleNamespace(fields=[])), \
                    patch("timings.output_dir", return_value=directory), \
                    patch("timings._run", side_effect=partial_save):
                snapshot = timings.snapshot()
            self.assertFalse(snapshot.ok)
            self.assertIn("save failed (exit 1)", snapshot.error)
            self.assertTrue(os.listdir(directory))


if __name__ == "__main__":
    unittest.main()
