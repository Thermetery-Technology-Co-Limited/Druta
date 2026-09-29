# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""When the second private clock array (B) may be shown as a measured clock, and
when its distance from the programmed one (A) is judged. Traces follow what one
TITAN RTX showed (B never on the grid, jitter of a few kHz, ~1 s refresh) and
what GK104/GM107 showed (B identical to A); neither is assumed of any card."""
import unittest

from druta import realclock as rc

BIN = 15.0


class Feeder:
    def __init__(self, key="core", dom=0, name="GPC", scale=1, bin_mhz=BIN):
        self.ev = rc.ClockEvidence()
        self.key, self.dom, self.name, self.scale, self.bin = key, dom, name, scale, bin_mhz
        self.t = 0.0

    def __call__(self, a_khz, b_khz, util=99, paired=True, gap=1.05, graded=True, dom=None,
                 key=None):
        self.t += gap
        return self.ev.feed(key or self.key, self.dom if dom is None else dom, self.name,
                            a_khz, b_khz, self.scale, util, paired, self.t, self.bin,
                            graded=graded)


def jitter(base_khz, i):
    return base_khz + (i % 3) * 8                  # an 8 kHz quantum, never repeating twice


class BandTests(unittest.TestCase):
    def test_bands_are_this_cards_bins_with_a_counter_tolerance(self):
        for gap, band in ((14.4, "ok"), (14.5, "warn"), (14.94, "warn"), (-28.0, "warn"),
                          (44.4, "warn"), (44.5, "bad"), (None, None)):
            with self.subTest(gap=gap):
                self.assertEqual(rc.gap_band(gap, BIN), band)
        self.assertEqual(rc.gap_band(12.2, 12.657), "warn")          # GP102's grid
        self.assertEqual(rc.gap_band(12.1, 12.657), "ok")

    def test_a_window_is_coloured_only_when_every_reading_agrees(self):
        self.assertEqual(rc.window_verdict([-28.1, -27.9, -28.0], BIN), "warn")
        self.assertEqual(rc.window_verdict([-2.0, 1.0, -0.5], BIN), "ok")
        self.assertEqual(rc.window_verdict([-14.0, -15.2, -14.1], BIN), "ok")    # median -14.1
        self.assertEqual(rc.window_verdict([-14.8, -14.8, -14.8, -14.8, -265.0], BIN), "varying")
        self.assertEqual(rc.window_verdict([-16.0, 16.0], BIN), "varying")   # both signs
        self.assertEqual(rc.window_verdict([-46.0, -47.0], BIN), "bad")
        self.assertIsNone(rc.window_verdict([], BIN))


class EvidenceTests(unittest.TestCase):
    def test_a_counter_under_a_fixed_target_is_measured_then_judged(self):
        # one TITAN RTX holding 2115 MHz on its voltage ceiling read B ~2087
        feed = Feeder()
        seen = [feed(2115000, jitter(2087000, i)) for i in range(8)]
        self.assertEqual([r.trust for r in seen[:3]], [rc.CHECKING] * 3)
        self.assertEqual(seen[3].trust, rc.MEASURED)
        self.assertEqual([r.state for r in seen[3:7]], ["settling"] * 4)
        self.assertEqual((seen[7].state, seen[7].verdict), ("judged", "warn"))
        self.assertTrue(-29 < seen[7].delta_mhz < -27)
        self.assertEqual(len(seen[7].window), rc.WINDOW_READS)

    def test_identical_arrays_are_a_mirror_and_never_measured(self):
        # GK104/GM107: B identical to A through idle, boost and held P0
        feed = Feeder(dom=15, name="GPC2CLK", scale=2)
        seen = [feed(a, a) for a in (648000, 648000, 4232000, 4232000, 4440000, 4440000, 648000)]
        self.assertEqual([r.trust for r in seen[:4]], [rc.CHECKING] * 4)
        self.assertTrue(all(r.trust == rc.MIRROR for r in seen[4:]))
        self.assertTrue(all(r.verdict is None for r in seen))

    def test_equality_at_one_fixed_target_is_not_a_mirror(self):
        # a real counter on a fixed clock reads exactly its target half the
        # time (one TU102's idle XBAR): that proves nothing either way
        feed = Feeder(key="xbar", dom=1, name="XBAR")
        for i in range(4):
            feed(1875000, 1861000 + i * 8)                   # measured first
        seen = [feed(540000, 540000) for _ in range(8)]
        self.assertTrue(all(r.trust != rc.MIRROR for r in seen))

    def test_a_lagged_copy_of_A_earns_no_evidence(self):
        feed = Feeder()
        a = [1950000, 1965000, 1980000, 1980000, 1980000, 1995000, 1995000, 1995000]
        b = [1950000, 1950000, 1950000, 1965000, 1980000, 1980000, 1980000, 1995000]
        seen = [feed(x, y) for x, y in zip(a, b)]
        self.assertEqual(seen[-1].events, 0)
        self.assertNotEqual(seen[-1].trust, rc.MEASURED)

    def test_a_constant_B_that_differs_from_A_is_unproven(self):
        # GP102: control 1 moved A 3442 -> 3493 while B sat at 3290.3
        feed = Feeder(key="xbar", dom=16, name="")
        seen = [feed(3442000 if i < 3 else 3493000, 3290300) for i in range(7)]
        self.assertEqual(seen[-1].trust, rc.UNPROVEN)
        self.assertIsNone(seen[-1].verdict)

    def test_five_exact_matches_through_a_change_withdraw_trust_and_it_returns(self):
        feed = Feeder()
        for i in range(4):
            feed(1950000, jitter(1949900, i))
        self.assertEqual(feed(1950000, 1949910).trust, rc.MEASURED)
        copies = [feed(a, a) for a in (1950000, 1950000, 1965000, 1965000, 1965000)]
        self.assertEqual(copies[-1].trust, rc.MIRROR)
        self.assertEqual(feed(1965000, 1964900).trust, rc.MEASURED)

    def test_a_clock_change_transient_never_enters_the_window(self):
        feed = Feeder()
        before = [feed(1350000, jitter(1364940, i)) for i in range(8)]
        self.assertEqual(before[-1].verdict, "warn")          # one bin ABOVE, at a 1350 lock
        after = [feed(1950000, 2550000), feed(1950000, 1650000)]
        after += [feed(1950000, jitter(1949900, i)) for i in range(6)]
        self.assertTrue(all(r.verdict in (None, "ok") for r in after))
        first = next(i for i, r in enumerate(after) if r.verdict is not None)
        self.assertEqual(first, 6)
        self.assertTrue(all(abs(d) < 1 for d in after[first].window))

    def test_the_first_loaded_reading_is_load_start(self):
        feed = Feeder()
        for i in range(4):
            feed(1950000, jitter(1949900, i), util=5)
        self.assertEqual(feed(1950000, 1949910, util=99).state, "load start")
        self.assertEqual(feed(1950000, 1949902, util=99).state, "settling")

    def test_idle_gating_builds_trust_but_is_never_judged(self):
        feed = Feeder()
        seen = [feed(1350000, b, util=2) for b in (470000, 573000, 512000, 498000, 560000, 481000)]
        self.assertEqual(seen[-1].trust, rc.MEASURED)
        self.assertTrue(all(r.state in ("checking", "light") and r.verdict is None for r in seen))

    def test_a_gap_on_a_band_edge_holds_its_colour(self):
        # a ceiling hold on one TU102 read about -14.7: 0.2 MHz from the edge
        feed = Feeder()
        seen = [feed(1950000, 1950000 - (14250 if i % 2 else 14950) + (i % 3) * 8)
                for i in range(30)]
        judged = [r.verdict for r in seen if r.state == "judged"]
        first = judged.index("warn")
        self.assertEqual(set(judged[first:]), {"warn"})           # no flicker once amber

    def test_hysteresis_releases_a_band_once_clearly_past_its_edge(self):
        self.assertEqual(rc.window_verdict([-14.2] * 5, BIN, previous="warn"), "warn")
        self.assertEqual(rc.window_verdict([-13.9] * 5, BIN, previous="warn"), "ok")
        self.assertEqual(rc.window_verdict([-44.2] * 5, BIN, previous="bad"), "bad")
        self.assertEqual(rc.window_verdict([-43.9] * 5, BIN, previous="bad"), "warn")
        self.assertEqual(rc.window_verdict([-14.2] * 5, BIN, previous="ok"), "ok")

    def test_one_outlier_inside_the_window_is_varying_not_steady(self):
        feed = Feeder()
        for i in range(8):
            feed(1950000, jitter(1935200, i))
        r = feed(1950000, 1685000)                                  # the load ending
        self.assertEqual(r.verdict, "varying")

    def test_no_band_is_claimed_without_a_usable_bin(self):
        for bin_mhz in (None, 0, float("nan"), -15.0):
            with self.subTest(bin_mhz=bin_mhz):
                self.assertIsNone(rc.gap_band(-300.0, bin_mhz))
                self.assertIsNone(rc.window_verdict([-300.0] * 5, bin_mhz))
        feed = Feeder(bin_mhz=None)
        seen = [feed(2115000, jitter(2087000, i)) for i in range(9)]
        self.assertEqual((seen[-1].state, seen[-1].verdict), ("judged", None))

    def test_a_B_equal_to_A_at_one_steady_clock_is_same_not_unproven(self):
        feed = Feeder(dom=15, name="GPC2CLK", scale=2)
        seen = [feed(2168000, 2168000) for _ in range(8)]
        self.assertEqual([r.trust for r in seen[:4]], [rc.CHECKING] * 4)
        self.assertTrue(all(r.trust == rc.SAME for r in seen[4:]))

    def test_the_catch_up_after_a_change_of_A_is_no_evidence(self):
        # B = A - 6.8 MHz, one read late: it only ever moves once after A does
        feed = Feeder()
        a = [1950000, 1950000, 1965000, 1965000, 1980000, 1980000, 1995000, 1995000]
        b = [1943200, 1943200, 1943200, 1958200, 1958200, 1973200, 1973200, 1988200]
        seen = [feed(x, y) for x, y in zip(a, b)]
        self.assertEqual(seen[-1].events, 0)
        self.assertNotEqual(seen[-1].trust, rc.MEASURED)

    def test_a_copy_of_A_lagging_three_reads_earns_no_evidence(self):
        # B = A three reads late, through two staircases of A: B moves twice
        # while A holds, the second time to an old value of A
        feed = Feeder()
        a = [1950000, 1965000, 1980000, 1995000, 1995000, 1995000, 1995000,
             2010000, 2025000, 2040000, 2040000, 2040000, 2040000]
        b = [1950000, 1950000, 1950000, 1950000, 1965000, 1980000, 1995000,
             1995000, 1995000, 1995000, 2010000, 2025000, 2040000]
        seen = [feed(x, y) for x, y in zip(a, b)]
        self.assertEqual(seen[-1].events, 0)
        self.assertNotEqual(seen[-1].trust, rc.MEASURED)

    def test_a_row_returned_to_settles_again_before_it_is_judged(self):
        feed = Feeder()
        for i in range(9):
            feed(2115000, jitter(2087000, i))
        feed(2115000, 2087000, dom=3)
        seen = [feed(2115000, jitter(2087100, i)) for i in range(10)]
        first = next(i for i, r in enumerate(seen) if r.state == "judged")
        self.assertGreaterEqual(first, rc.FRESH_NEEDED + rc.WINDOW_READS - 1)

    def test_a_B_that_stops_refreshing_is_not_judged(self):
        feed = Feeder()
        for i in range(9):
            feed(2115000, jitter(2087000, i))
        seen = [feed(2115000, 2087000) for _ in range(6)]
        self.assertEqual(seen[-1].state, "stuck")
        self.assertIsNone(seen[-1].verdict)

    def test_a_target_that_keeps_moving_is_said_so(self):
        feed = Feeder()
        for i in range(6):
            feed(1950000, jitter(1935000, i))
        seen = []
        for i in range(12):
            a = 1950000 if (i // 2) % 2 else 1935000
            seen.append(feed(a, jitter(a - 12000, i)))
        self.assertIn("moving", {r.state for r in seen})
        self.assertTrue(all(r.verdict is None for r in seen))

    def test_the_load_gate_has_hysteresis(self):
        feed = Feeder()
        for i in range(9):
            feed(2115000, jitter(2087000, i), util=99)
        dip = feed(2115000, 2087016, util=88)
        self.assertEqual(dip.state, "judged")                     # still loaded above 85
        self.assertEqual(feed(2115000, 2087000, util=84).state, "light")
        self.assertEqual(feed(2115000, 2087008, util=89).state, "light")   # not back yet
        self.assertEqual(feed(2115000, 2087016, util=90).state, "load start")

    def test_after_a_miss_nothing_is_judged_before_a_full_settle_and_window(self):
        feed = Feeder()
        for i in range(9):
            feed(2115000, jitter(2087000, i))
        feed.ev.miss("core")
        seen = [feed(2115000, jitter(2087000, i)) for i in range(10)]
        first = next(i for i, r in enumerate(seen) if r.state == "judged")
        self.assertGreaterEqual(first, rc.FRESH_NEEDED + rc.WINDOW_READS - 1)

    def test_2clk_is_in_core_units_and_this_cards_bins(self):
        feed = Feeder(dom=15, name="GPC2CLK", scale=2, bin_mhz=12.657)
        seen = [feed(3797100, 3771000 + (i % 2) * 8) for i in range(8)]
        self.assertAlmostEqual(seen[-1].a_mhz, 1898.55)
        self.assertEqual(seen[-1].verdict, "warn")             # -13.05 core MHz
        feed = Feeder(dom=15, name="GPC2CLK", scale=2, bin_mhz=12.657)
        seen = [feed(3797100, 3773000 + (i % 2) * 8) for i in range(8)]
        self.assertEqual(seen[-1].verdict, "ok")               # -12.05 core MHz

    def test_a_read_gap_drops_continuity_but_keeps_trust(self):
        feed = Feeder()
        for i in range(8):
            feed(2115000, jitter(2087000, i))
        r = feed(2115000, 2087016, gap=4.0)
        self.assertEqual((r.trust, r.state, r.window), (rc.MEASURED, "load start", ()))

    def test_miss_clears_the_last_reading_and_the_window(self):
        feed = Feeder()
        for i in range(8):
            feed(2115000, jitter(2087000, i))
        feed.ev.miss("core")
        self.assertIsNone(feed.ev.last("core"))
        r = feed(2115000, 2087008)
        self.assertEqual((r.trust, r.state), (rc.MEASURED, "load start"))   # continuity restarts

    def test_zero_words_are_never_evidence(self):
        feed = Feeder()
        feed(2115000, 2087000)
        self.assertIsNone(feed(2115000, 0))
        self.assertIsNone(feed(0, 2087008))
        self.assertEqual(feed(2115000, 2087016).samples, 2)

    def test_evidence_is_per_tile_and_domain_and_a_moved_row_restarts(self):
        feed = Feeder()
        for i in range(8):
            feed(2115000, jitter(2087000, i))
        other = feed(1875000, 1875000, key="xbar", dom=1)
        self.assertEqual((other.trust, other.samples), (rc.CHECKING, 1))
        moved = feed(2115000, 2087000, dom=3)                  # the core row moved domain
        self.assertEqual((moved.trust, moved.samples, moved.window), (rc.CHECKING, 1, ()))

    def test_returning_to_an_earlier_row_restarts_its_continuity(self):
        feed = Feeder()
        for i in range(8):
            feed(2115000, jitter(2087000, i))
        feed(2115000, 2087000, dom=3)                          # a different row, once
        back = feed(2115000, 2087024)                          # and the first one again
        self.assertEqual((back.trust, back.state), (rc.MEASURED, "load start"))

    def test_unpaired_is_never_judged(self):
        feed = Feeder()
        seen = [feed(2115000, jitter(2087000, i), paired=False) for i in range(8)]
        self.assertTrue(all(r.verdict is None for r in seen))
        self.assertEqual(seen[-1].state, "unpaired")

    def test_ungraded_has_no_verdict(self):
        feed = Feeder(key="mem", dom=4, name="MEM")
        seen = [feed(6801000, jitter(6794200, i), graded=False) for i in range(8)]
        self.assertEqual((seen[-1].trust, seen[-1].state, seen[-1].verdict),
                         (rc.MEASURED, "ungraded", None))


if __name__ == "__main__":
    unittest.main()
