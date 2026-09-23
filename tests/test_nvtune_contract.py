"""CLI contract regressions. All subprocesses are mocked; no GPU is opened.

These cover a helper that advertises --dry-run and --commit. Help discovery and
the other published command contracts are covered by test_nvtune_compatibility.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from druta import timings, timingwrite


SLOT = "0000:02:00.0"
HELP = """nvtune - NVIDIA FBPA memory timing tool
  set FIELD=VALUE...        write fields (add --dry-run to preview)
      --force           write even if range checks complain
      --dry-run         preview set/apply using read-only access; no writes
      --commit          explicitly commit set/apply/restore (the default)
"""
COMPLETE = "dry run complete: no registers written"
PREVIEW = """0000:02:00.0  GP102 (Pascal)
  [broadcast]
  CONFIG3 @0x10022c  0x2200194a -> 0x22001b4a  [would write]
      FAW             12 -> 13
"""
UNCHANGED = "  CONFIG0 @0x100220  unchanged (0x00000001)\n"


def response(output="", status=0, stderr=""):
    return subprocess.CompletedProcess([], status, stdout=output, stderr=stderr)


class HelperFixture:
    """Serve help from HELP and every other subprocess from queued replies."""
    HELP = HELP

    def setUp(self):
        # A real file, because the helper contract is pinned to its contents.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.exe = str(Path(folder.name) / "nvtune.exe")
        Path(self.exe).write_bytes(b"fake nvtune preview build; never executable")
        self.responses = []
        find_patch = patch.object(timings, "find_exe", return_value=self.exe)
        spawn_patch = patch("subprocess.run", side_effect=self.execute)
        self.find = find_patch.start()
        self.spawn = spawn_patch.start()
        self.addCleanup(find_patch.stop)
        self.addCleanup(spawn_patch.stop)

    def execute(self, argv, **kwargs):
        # Help is read-only and may be cached by executable fingerprint.
        if argv[1:] == ["--help"]:
            return response(self.HELP)
        if not self.responses:
            raise AssertionError("test attempted an unexpected subprocess: %r" % (argv,))
        reply = self.responses.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def respond(self, *responses):
        self.responses = list(responses)

    def argv(self):
        return [call.args[0] for call in self.spawn.call_args_list
                if call.args[0][1:] != ["--help"]]


class WriterContractTests(HelperFixture, unittest.TestCase):
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
        self.assertEqual(self.argv(), [[self.exe, "set", "-d", SLOT,
                                        "FAW=13", "--dry-run"]])

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
            [self.exe, "get", "-d", SLOT, "FAW"],
            [self.exe, "set", "-d", SLOT, "FAW=13", "--dry-run"],
            [self.exe, "set", "-d", SLOT, "FAW=13", "--commit"],
            [self.exe, "get", "-d", SLOT, "FAW"]])

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

    def test_incomplete_preview_prevents_commit_even_with_force(self):
        self.apply_responses(preview=response(PREVIEW))
        plan, results = timingwrite.apply({"FAW": 13}, SLOT, force=True)
        self.assertFalse(plan.ok)
        self.assertEqual(results[0].outcome, timingwrite.FAILED)
        self.assertEqual(len(self.argv()), 2)
        self.assertTrue(all("--commit" not in argv for argv in self.argv()))

    def test_failed_commit_is_not_a_hardware_rejection(self):
        self.apply_responses(commit=response(status=2, stderr="error: unsupported option"),
                             after=response(SLOT + "  FAW=12"))
        _, results = timingwrite.apply({"FAW": 13}, SLOT)
        self.assertEqual(results[0].outcome, timingwrite.FAILED)
        # The unchanged read-back is kept for diagnosis, not called a rejection.
        self.assertEqual(results[0].after, 12)
        self.assertIn("unsupported option", results[0].detail)
        self.assertNotIn("reached the hardware", results[0].detail)
        self.assertEqual(len(self.argv()), 4)

    def test_explicit_tool_refusal_is_preserved(self):
        self.apply_responses(commit=response(status=1, stderr="refusing to write with warnings outstanding"),
                             after=response(SLOT + "  FAW=12"))
        _, results = timingwrite.apply({"FAW": 13}, SLOT)
        self.assertEqual(results[0].outcome, timingwrite.TOOL_REFUSED)
        self.assertEqual(len(self.argv()), 4)

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
                self.assertIn("read-back unavailable", results[0].detail)

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
        backup = os.path.join(os.path.dirname(self.exe), "saved.json")
        Path(backup).write_text("{}", encoding="utf-8")
        self.assertEqual(timingwrite.restore(backup, SLOT), (True, "restored"))
        self.assertEqual(self.argv(), [[self.exe, "restore", "-d", SLOT,
                                        "-i", backup, "--commit"]])


class DefaultPreviewContractTests(HelperFixture, unittest.TestCase):
    """A helper whose bare set previews (help text of the nvtune fork's
    tool/src/core/cli.cpp) prints no completion marker; rows are still checked."""
    HELP = ("  set FIELD=VALUE...        write fields (dry run unless --commit)\n"
            "      --commit          actually write (set/apply)\n"
            "Everything defaults to a dry run; --commit is required to touch hardware.\n")

    def test_bare_preview_targets_one_card_without_a_marker(self):
        self.respond(response(PREVIEW + UNCHANGED))
        plan = timingwrite.plan({"FAW": 13, "RC": 1}, SLOT)
        self.assertTrue(plan.ok, plan.error)
        self.assertEqual(plan.touches, ["FAW"])
        self.assertFalse(plan.needs_force)
        self.assertEqual(self.argv(), [[self.exe, "set", "-d", SLOT, "FAW=13", "RC=1"]])

    def test_writing_or_unparseable_rows_are_still_not_a_plan(self):
        for output in ["", PREVIEW.replace("[would write]", "[write]"),
                       PREVIEW.replace("FAW             12 -> 13", "FAW 12 -> unknown"),
                       PREVIEW + "CONFIG0 @bad  unchanged (0x1)\n"]:
            with self.subTest(output=output):
                self.respond(response(output))
                self.assertFalse(timingwrite.plan({"FAW": 13}, SLOT).ok)


class ReaderContractTests(unittest.TestCase):
    def test_nonzero_fields_exit_with_usable_stdout_is_parsed_and_cached(self):
        exe = r"C:\nvtune\nvtune.exe"
        table = SimpleNamespace(fields=[])
        with patch.dict(timings._FT_CACHE, clear=True), \
                patch.object(timings, "find_exe", return_value=exe), \
                patch.object(timings, "_run", return_value=response("partial field table", status=1)) as run, \
                patch.object(timings, "parse_fields", return_value=table) as parse:
            self.assertIs(timings.field_table(refresh=True), table)
            parse.assert_called_once_with("partial field table")
            # The parsed table is cached like any other; no second `fields` run.
            self.assertIs(timings.field_table(), table)
            self.assertEqual(run.call_count, 1)

    def test_nonzero_fields_exit_without_stdout_raises_and_is_not_cached(self):
        exe = r"C:\nvtune\nvtune.exe"
        with patch.dict(timings._FT_CACHE, clear=True), \
                patch.object(timings, "find_exe", return_value=exe), \
                patch.object(timings, "_run", return_value=response("  \n", status=1,
                                                                   stderr="cannot open driver")), \
                patch.object(timings, "parse_fields") as parse:
            with self.assertRaises(timings.TimingsError) as raised:
                timings.field_table(refresh=True)
            self.assertIn("exit 1", str(raised.exception))
            self.assertIn("cannot open driver", str(raised.exception))
            parse.assert_not_called()
            self.assertNotIn(exe, timings._FT_CACHE)

    def test_failed_save_is_rejected_even_if_it_left_a_valid_file(self):
        exe = r"C:\nvtune\nvtune.exe"
        with tempfile.TemporaryDirectory() as directory:
            def partial_save(exe, subcommand, args, **kwargs):
                self.assertEqual(subcommand, "save")
                with open(args[1], "w", encoding="utf-8") as stream:
                    json.dump({"_format": "nvtune-backup-1", "slot": SLOT,
                               "registers": {"broadcast": {}}}, stream)
                return response(status=1, stderr="driver read failed")

            with patch.object(timings, "available", return_value=SimpleNamespace(ok=True, exe=exe)), \
                    patch.object(timings, "field_table", return_value=SimpleNamespace(fields=[])), \
                    patch.object(timings, "output_dir", return_value=directory), \
                    patch.object(timings, "_run", side_effect=partial_save):
                snapshot = timings.snapshot()
            self.assertFalse(snapshot.ok)
            self.assertIn("save exited 1", snapshot.error)
            self.assertIn("driver read failed", snapshot.error)
            self.assertTrue(os.listdir(directory))


if __name__ == "__main__":
    unittest.main()
