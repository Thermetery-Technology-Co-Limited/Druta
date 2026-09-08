# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Blackwell timing/profile contracts, using saved clock shapes and fakes only."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import profiles, timings, timingwrite
from tests.test_tune_profiles import hardware
from tests.test_timingwrite_results import DRY_RUN


SLOT = "0000:01:00.0"
MEMORY_STATES = [405, 810, 7001, 14801, 15001]


class BlackwellTimingTests(unittest.TestCase):
    def test_unknown_blackwell_band_requires_highest_nominal_clock(self):
        for state in (0, 2):
            for offset in (0, -100, 100):
                with self.subTest(state=state, offset=offset):
                    self.assertTrue(timings.in_performance_band(
                        15001 + offset, state, MEMORY_STATES, "GB203", offset))
                    self.assertFalse(timings.in_performance_band(
                        14801 + offset, state, MEMORY_STATES, "GB203", offset))
        self.assertEqual(timings.performance_band_floor(
            MEMORY_STATES[::-1] + [15001], "GB203"), 15001)

    def test_p1_or_idle_never_inherits_titan_performance_state(self):
        for state in (1, 8):
            self.assertFalse(timings.in_performance_band(
                15001, state, MEMORY_STATES, "GB203", 0))
        self.assertFalse(timings.in_performance_band(
            15001, 2, MEMORY_STATES, "GB203", 15001 - 405))

    def capture(self, status, stderr="", codename="GB203", fields=()):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        gpu = SimpleNamespace(static={"mem_div": None, "mem_type": "RAM type 16",
                                      "mem_clocks": MEMORY_STATES},
                              slot=lambda: SLOT,
                              read=lambda: {"mem_off": 200, "mem": 15101, "pstate": 0})

        def save(exe, command, args, **kwargs):
            self.assertEqual(command, "save")
            self.assertEqual(kwargs["slot"], SLOT)
            Path(args[1]).write_text(json.dumps({"codename": codename, "slot": SLOT,
                "registers": {"broadcast": {"CONFIG0": "0x2D"}}}), encoding="utf-8")
            return SimpleNamespace(returncode=status, stdout="", stderr=stderr)

        with patch.object(timings, "available", return_value=SimpleNamespace(ok=True, exe="fake")), \
                patch.object(timings, "field_table", return_value=SimpleNamespace(fields=fields)), \
                patch.object(timings, "output_dir", return_value=folder.name), \
                patch.object(timings, "_run", side_effect=save):
            return timings.snapshot(gpu)

    def test_unknown_divisor_keeps_nanoseconds_unavailable(self):
        snap = self.capture(0)
        self.assertTrue(snap.ok, snap.error)
        self.assertTrue(snap.perf_band)
        self.assertEqual(snap.mem_offset, 100)
        self.assertEqual(snap.band_floor, 15001)
        self.assertIsNone(snap.mem_true_mhz)
        self.assertFalse(snap.ns_trustworthy)

    def test_unknown_decoder_keeps_raw_words_but_cannot_authorize_writes(self):
        field = SimpleNamespace(name="FAW", register="CONFIG0", structural=False,
                                inferred=False, max_value=255)
        snap = self.capture(0, codename="UNKNOWN_1B3", fields=[field])
        self.assertTrue(snap.ok)
        self.assertFalse(snap.layout_known)
        self.assertTrue(snap.perf_band)  # a good clock cannot validate a decoder
        self.assertEqual(snap.registers, {"broadcast": {"CONFIG0": 45}})
        self.assertIsNone(snap.readings[0].cycles)
        self.assertIsNone(snap.readings[0].ns)
        self.assertTrue(any("raw registers only" in w for w in snap.warnings))
        snap.mem_div = 4
        self.assertFalse(snap.ns_trustworthy)
        problems = timingwrite.check({"FAW": 46}, SimpleNamespace(by_name=lambda _: field), snap)
        self.assertTrue(any("no confirmed timing layout" in p for p in problems))

    def test_failed_save_cannot_promote_a_partial_file_to_valid_capture(self):
        for status in (1, 5, 9):
            with self.subTest(status=status):
                snap = self.capture(status, "failed to read partition")
                self.assertFalse(snap.ok)
                self.assertEqual(snap.registers, {})
                self.assertIn(f"exited {status}", snap.error)
                self.assertIn("failed to read partition", snap.error)
                self.assertTrue(Path(snap.json_path).is_file())

    def test_failed_partial_plan_stops_before_commit_even_with_force(self):
        for status, suffix in ((1, ""), (1, "\nfailed to read partition"),
                               (5, ""), (5, "\nfailed to read partition")):
            for force in (False, True):
                with self.subTest(status=status, suffix=suffix, force=force):
                    with patch.object(timingwrite, "_run", side_effect=[
                            ("RC=45", 0), (DRY_RUN + suffix, status)]) as run:
                        plan, results = timingwrite.apply({"RC": 46}, SLOT, force=force)
                    self.assertFalse(plan.ok)
                    self.assertEqual(results[0].outcome, timingwrite.FAILED)
                    self.assertIn(f"exited {status}", results[0].detail)
                    self.assertTrue(all("--commit" not in c.args[0] for c in run.call_args_list))


class MultiRailProfileTests(unittest.TestCase):
    def setUp(self):
        self.gpu, _rail, self.calls = hardware()
        self.gpu.static.update(name="NVIDIA GeForce RTX 5080", driver="580.97",
                               vbios="98.03.3b.c0.6f")
        self.gpu.mem_offset_scale.return_value = 2, "MHz reported"
        self.gpu.clkdom_controls_for_ui.return_value = [1, 2, 3, 4]
        self.gpu.read_clk_domain_offsets.return_value = (
            {i: {"freq_khz": 25000 * i} for i in (1, 2, 3, 4)}, None)

    def test_partial_two_rail_read_is_explicitly_incomplete(self):
        self.gpu.read_volt_rail_limits.return_value.pop(1)
        state = profiles.capture(self.gpu)
        self.assertEqual(set(state["rail_limits_mv"]), {"0"})
        self.assertTrue(any("rail 1" in why for why in profiles.incomplete(state)))
        self.assertIn("INCOMPLETE", profiles.summarize(state))
        self.assertEqual(self.calls, [])

    def test_absent_reads_name_both_known_writers(self):
        self.gpu.read_volt_rail_limits.return_value = None
        state = profiles.capture(self.gpu)
        for index in (0, 1):
            self.assertTrue(any(f"rail {index}" in why for why in profiles.incomplete(state)))

    def test_unsupported_second_rail_is_not_a_missing_capture(self):
        self.gpu.read_volt_rail_limits.return_value.pop(1)
        fields = self.gpu.volt_rail_limit_fields.return_value
        self.gpu.volt_rail_limit_fields.side_effect = lambda index: fields if index == 0 else ()
        self.assertEqual(profiles.incomplete(profiles.capture(self.gpu)), [])

    def test_json_roundtrip_replays_both_rails_and_all_blackwell_controls(self):
        self.gpu.read.return_value["mem_off"] = -100
        state = json.loads(json.dumps(profiles.capture(self.gpu)))
        self.assertEqual(state["mem_off_true_mhz"], -50)
        self.assertEqual(set(state["clock_domain_offsets_mhz"]), {"1", "2", "3", "4"})
        results = profiles.restore(self.gpu, state)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.gpu.set_clock_offset.assert_any_call(2, -50)
        for index in (0, 1):
            self.gpu.set_volt_rail_limits.assert_any_call(index, **state["rail_limits_mv"][str(index)])
        for index in (1, 2, 3, 4):
            self.gpu.set_clk_domain_offset.assert_any_call(index, 25 * index)
        self.assertEqual(self.calls[-1][0], "apply_vf_deltas")

    def test_other_driver_or_vbios_private_profile_is_refused_before_writes(self):
        state = profiles.capture(self.gpu)
        for key in ("driver", "vbios"):
            changed = copy.deepcopy(state)
            changed["device"][key] = "other"
            results = profiles.restore(self.gpu, changed)
            self.assertFalse(results[0][0])
            self.assertIn(key, results[0][1])
            self.assertEqual(self.calls, [])

    def test_requested_power_roundtrip_does_not_capture_lagged_enforced_limit(self):
        self.gpu.static.update(pl_min_mw=250000, pl_max_mw=450000)
        self.gpu.read.return_value["pl_now_mw"] = 360000
        self.gpu.read_power_limit_mw = Mock(return_value=349200)
        state = json.loads(json.dumps(profiles.capture(self.gpu)))
        self.assertEqual(state["power_limit_mw"], 349200)
        results = profiles.restore(self.gpu, state)
        self.assertTrue(all(ok for ok, _ in results), results)
        self.gpu.set_power_limit_mw.assert_called_once_with(349200)

    def test_saved_power_summary_preserves_fractional_watts(self):
        for milliwatts, watts in ((349200, "349.2"), (350001, "350.001"), (360000, "360")):
            with self.subTest(milliwatts=milliwatts):
                state = profiles.capture(self.gpu)
                state["power_limit_mw"] = milliwatts
                self.assertIn(f"PL {watts} W", profiles.summarize(state))

    def test_unreadable_requested_power_is_incomplete_without_enforced_fallback(self):
        self.gpu.static.update(pl_min_mw=250000, pl_max_mw=450000)
        for error in (None, RuntimeError("read failed")):
            self.gpu.read_power_limit_mw = Mock(return_value=None, side_effect=error)
            state = profiles.capture(self.gpu)
            self.assertIsNone(state["power_limit_mw"])
            self.assertTrue(any("requested power limit" in why for why in profiles.incomplete(state)))

    def test_fractional_milliwatt_profile_is_refused_before_rail_writes(self):
        state = profiles.capture(self.gpu)
        state["power_limit_mw"] = 349200.5
        results = profiles.restore(self.gpu, state)
        self.assertFalse(results[0][0])
        self.assertIn("power limit must be an integer", results[0][1])
        self.assertEqual(self.calls, [])

    def test_blackwell_measured_offset_grid_is_checked_before_voltage_writes(self):
        self.gpu.memory_offset_step_units = Mock(return_value=2)
        state = profiles.capture(self.gpu)
        for memory in (-0.5, 0.5, 1.5):
            state["mem_off_true_mhz"] = memory
            results = profiles.restore(self.gpu, state)
            self.assertFalse(results[0][0])
            self.assertIn("offset grid", results[0][1])
            self.assertEqual(self.calls, [])


class NvtunePreviewContractTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.exe = Path(folder.name) / "fake-helper.exe"
        self.exe.write_bytes(b"fake; never executable")
        self.commands = []

    def run_plan(self, help_text, help_status=0, change_during_help=False):
        def execute(argv, **kwargs):
            self.commands.append(argv)
            if argv[1] == "--help":
                if change_during_help:
                    self.exe.write_bytes(b"replacement helper bytes")
                return SimpleNamespace(returncode=help_status, stdout=help_text, stderr="")
            return SimpleNamespace(returncode=0, stdout=DRY_RUN +
                                   "\ndry run complete: no registers written", stderr="")
        with patch.object(timings, "find_exe", return_value=str(self.exe)), \
                patch.object(timingwrite.subprocess, "run", side_effect=execute):
            return timingwrite.plan({"RC": 46}, SLOT)

    def test_modern_helper_gets_explicit_dryrun_and_completion_is_not_a_warning(self):
        plan = self.run_plan("  --dry-run  preview without writing\n--commit commits")
        self.assertTrue(plan.ok, plan.error)
        self.assertFalse(plan.needs_force)
        self.assertIn("--dry-run", self.commands[-1])
        self.assertNotIn("--commit", self.commands[-1])

    def test_known_legacy_dryrun_contract_stays_compatible(self):
        plan = self.run_plan("Everything defaults to a dry run; --commit is required to touch hardware.")
        self.assertTrue(plan.ok, plan.error)
        self.assertNotIn("--dry-run", self.commands[-1])
        self.assertNotIn("--commit", self.commands[-1])

    def test_unknown_or_failed_help_never_runs_set(self):
        for help_text, status in (("set modifies registers", 0), ("  --dry-run preview", 1)):
            self.commands.clear()
            plan = self.run_plan(help_text, status)
            self.assertFalse(plan.ok)
            self.assertEqual([c[1] for c in self.commands], ["--help"])

    def test_contract_cache_is_invalidated_when_helper_changes(self):
        self.assertTrue(self.run_plan("  --dry-run preview").ok)
        self.assertTrue(self.run_plan("unread help would fail").ok)
        self.assertEqual(sum(c[1] == "--help" for c in self.commands), 1)
        self.exe.write_bytes(b"new helper, different size")
        plan = self.run_plan("set commits; no known preview mode")
        self.assertFalse(plan.ok)
        self.assertEqual(sum(c[1] == "--help" for c in self.commands), 2)

    def test_helper_changed_during_help_is_refused(self):
        plan = self.run_plan("  --dry-run preview", change_during_help=True)
        self.assertFalse(plan.ok)
        self.assertIn("changed", plan.error)
        self.assertEqual([c[1] for c in self.commands], ["--help"])

    def test_same_size_helper_replacement_with_preserved_mtime_rechecks_contract(self):
        legacy = "Everything defaults to a dry run; --commit is required to touch hardware."
        self.assertTrue(self.run_plan(legacy).ok)
        old = self.exe.stat()
        self.exe.write_bytes(b"M" * old.st_size)
        os.utime(self.exe, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertTrue(self.run_plan("  --dry-run preview").ok)
        self.assertEqual(sum(c[1] == "--help" for c in self.commands), 2)
        self.assertIn("--dry-run", self.commands[-1])


if __name__ == "__main__":
    unittest.main()
