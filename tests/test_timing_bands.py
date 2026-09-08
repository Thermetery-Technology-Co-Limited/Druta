# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Timing-band classification for measured Maxwell/Pascal clocks, no hardware."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import timings, timingwrite


def snapshot(memory, pstate, *, codename="GM107", states=(405, 900), offset=0):
    return timings.Snapshot(ok=True, codename=codename, mem_states=list(states),
                            mem_before=memory, mem_after=memory,
                            pstate_before=pstate, pstate_after=pstate,
                            mem_offset=offset, mem_div=1)


class TimingBandTests(unittest.TestCase):
    def test_snapshot_producer_preserves_unknown_offsets(self):
        for offset in (None, float("nan"), float("inf"), True, "0", RuntimeError("read failed"), 0, -50):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as folder:
                gpu = SimpleNamespace(static={"mem_div": 1, "mem_type": "DDR3", "mem_clocks": [405, 900]},
                                      slot=lambda: "0000:02:00.0")
                bracket = {"mem": 900, "pstate": 0}
                gpu.read = Mock(side_effect=[offset if isinstance(offset, Exception) else {"mem_off": offset},
                                             bracket, bracket])

                def save(exe, command, args, **kwargs):
                    self.assertEqual(command, "save")
                    self.assertEqual(kwargs["slot"], "0000:02:00.0")
                    Path(args[1]).write_text(json.dumps({"codename": "GM107", "slot": "0000:02:00.0",
                                                       "registers": {"broadcast": {"CONFIG0": "0x2D"}}}),
                                             encoding="utf-8")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with patch.object(timings, "available", return_value=SimpleNamespace(ok=True, exe="fake")), \
                        patch.object(timings, "field_table", return_value=SimpleNamespace(fields=[])), \
                        patch.object(timings, "output_dir", return_value=folder), \
                        patch.object(timings, "_run", side_effect=save):
                    snap = timings.snapshot(gpu)
                self.assertTrue(snap.ok, snap.error)
                if type(offset) is int:
                    self.assertEqual(snap.mem_offset, offset / 2)
                    self.assertTrue(snap.perf_band)
                else:
                    self.assertIsNone(snap.mem_offset)
                    self.assertIsNone(snap.perf_band)
                    self.assertIsNone(snap.at_p0)
                    self.assertIsNone(snap.matched_state)
                    self.assertIn("MEMORY STATE UNKNOWN", snap.state_headline)
                    self.assertTrue(any("offset" in w for w in snap.warnings))

    def test_actual_gtx745_idle_clock_list_is_not_a_performance_band(self):
        snap = snapshot(405, 8)
        self.assertEqual(snap.band_floor, 900)
        self.assertFalse(snap.perf_band)
        self.assertFalse(snap.at_p0)
        field = SimpleNamespace(structural=False, inferred=False, max_value=255)
        problems = timingwrite.check({"RC": 46}, SimpleNamespace(by_name=lambda _: field), snap)
        self.assertTrue(any("not in its top memory band" in p for p in problems))

    def test_low_band_cycles_and_nanoseconds_remain_readable(self):
        snap = snapshot(405, 8)
        self.assertFalse(snap.perf_band)
        self.assertTrue(snap.ns_trustworthy)
        self.assertEqual(snap.mem_true_mhz, 405)
        self.assertEqual(snap.state_tag, "idle")

    def test_gtx745_p0_is_valid_with_positive_or_negative_offset(self):
        for offset in (0, 10, -25):
            with self.subTest(offset=offset):
                snap = snapshot(900 + offset, 0, offset=offset)
                self.assertTrue(snap.perf_band)
                self.assertTrue(snap.at_p0)
                self.assertEqual(snap.state_tag, "P0")

    def test_order_and_duplicate_clocks_cannot_create_an_extra_band(self):
        for states in ([900, 405], [900, 900, 405, 405], [405, 900, 900]):
            with self.subTest(states=states):
                self.assertEqual(timings.performance_band_floor(states, "GM107"), 900)
                self.assertFalse(timings.in_performance_band(405, 2, states, "GM107"))

    def test_pascal_p2_and_p0_use_the_measured_nearby_pair(self):
        states = (405, 810, 5505, 5705)
        for pstate, nominal in ((2, 5505), (0, 5705)):
            for offset in (0, -50, 50):
                with self.subTest(pstate=pstate, offset=offset):
                    snap = snapshot(nominal + offset, pstate, codename="GP102",
                                    states=states, offset=offset)
                    self.assertEqual(snap.band_floor, 5505)
                    self.assertTrue(snap.perf_band)
                    self.assertEqual(snap.at_p0, pstate == 0)

    def test_turing_nearby_pair_remains_supported(self):
        snap = snapshot(7228, 2, codename="TU102", states=(405, 810, 6801, 7001), offset=428)
        self.assertEqual(snap.band_floor, 6801)
        self.assertTrue(snap.perf_band)
        self.assertFalse(snap.at_p0)

    def test_unknown_chip_does_not_inherit_measured_p2_equivalence(self):
        states = (405, 810, 5505, 5705)
        for codename in ("", "GP104", "GM204"):
            with self.subTest(codename=codename):
                self.assertEqual(timings.performance_band_floor(states, codename), 5705)
                self.assertFalse(timings.in_performance_band(5505, 2, states, codename))
                self.assertTrue(timings.in_performance_band(5705, 0, states, codename))

    def test_distinct_clock_bands_on_known_chip_use_only_highest(self):
        for states in ((405, 900), (405, 810, 4000, 5705)):
            with self.subTest(states=states):
                self.assertEqual(timings.performance_band_floor(states, "GP102"), max(states))

    def test_positive_idle_offset_cannot_authorize_a_write(self):
        self.assertFalse(timings.in_performance_band(1000, 8, (405, 900), "GM107", 595))
        self.assertFalse(timings.in_performance_band(1000, 2, (405, 900), "GM107", 595))

    def test_both_bracketing_performance_states_are_required(self):
        for pstate, expected in ((8, False), (None, None), (True, None)):
            with self.subTest(pstate=pstate):
                snap = snapshot(900, 0)
                snap.pstate_after = pstate
                self.assertIs(snap.perf_band, expected)
                self.assertIs(snap.at_p0, expected)

    def test_missing_bracketing_clock_is_unverified(self):
        snap = snapshot(900, 0)
        snap.mem_after = None
        self.assertIsNone(snap.perf_band)
        self.assertFalse(snap.ns_trustworthy)

    def test_unknown_or_invalid_inputs_do_not_confirm_a_band(self):
        for memory, state, offset in ((None, 0, 0), (float("nan"), 0, 0),
                                      (True, 0, 0), (900, None, 0),
                                      (900, 0, None), (900, 0, float("inf")),
                                      (900, 0, False)):
            with self.subTest(memory=memory, state=state, offset=offset):
                self.assertIsNone(timings.in_performance_band(memory, state, (405, 900),
                                                             "GM107", offset))
        for states in ([], [405, None], [0, 900], [405, float("inf")], [True, 900]):
            with self.subTest(states=states):
                self.assertIsNone(timings.performance_band_floor(states, "GM107"))

    def test_bounded_integer_clock_quantization_is_tolerated(self):
        self.assertTrue(timings.in_performance_band(895, 0, (405, 900), "GM107"))
        self.assertFalse(timings.in_performance_band(894, 0, (405, 900), "GM107"))


if __name__ == "__main__":
    unittest.main()
