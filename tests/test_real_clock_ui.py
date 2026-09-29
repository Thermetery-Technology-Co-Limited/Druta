# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The measured line under the Monitor's clock tiles: what it says, in which
colour, for the states a card really goes through. Drawn values are asserted
through the DearPyGui calls; nothing touches hardware."""
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.druta import BAD, DIM, TEXT, WARN, Druta
from druta.nvbackend import PRIV_CONFIRMED, PRIV_FREQ, PRIV_LIKELY, PRIV_UNNAMED, PRIV_UNPOPULATED


def row(dom, name, a_khz, b_khz, grade=PRIV_CONFIRMED, scale=1):
    return {"domain": dom, "name": name, "grade": grade, "kind": PRIV_FREQ,
            "prog_khz": a_khz, "meas_khz": b_khz, "flags": 0, "srcid": 32, "scale": scale,
            "prog_mhz": a_khz / 1000.0, "meas_mhz": b_khz / 1000.0,
            "delta_mhz": (b_khz - a_khz) / 1000.0 if a_khz and b_khz else None}


def jitter(base, i):
    return base + (i % 3) * 8


class RealClockCase(unittest.TestCase):
    def setUp(self):
        app = Druta.__new__(Druta)
        app.gpu = SimpleNamespace(static={"mem_div": 2, "mem_type": "GDDR6"})
        app.step_khz = Mock(return_value=15000)
        app._stale = False
        self.app = app
        self.values, self.colour = {}, {}
        self.colour_calls = []

        def configure(tag, **kw):
            if "color" in kw:
                self.colour[tag] = kw["color"]
                self.colour_calls.append(tag)
        for target, effect in (("druta.druta.dpg.set_value", self.values.__setitem__),
                               ("druta.druta.dpg.configure_item", configure)):
            p = patch(target, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)
        self.t = 0.0

    def snap(self, core=2115, a=2115000, b=2087100, util=99, rows=None, **extra):
        """One NEW snapshot dict, as the poll thread builds one per read."""
        d = {"core": core, "xbar": 2025, "mem": 6801, "util_gpu": util, "pstate": 0,
             "clk_domains": rows if rows is not None else [
                 row(0, "GPC", a, b), row(1, "XBAR", 2025000, 2011100),
                 row(4, "MEM", 6801000, 6794200)]}
        d.update(extra)
        return d

    def feed(self, d):
        self.t += 1.05
        self.app.refresh_real_clocks(d, now=self.t)
        return d

    def hold_on_the_ceiling(self, n=9, **kw):
        for i in range(n):
            self.feed(self.snap(b=jitter(2087100, i), rows=None, **kw))


class MeasuredLineTests(RealClockCase):
    def test_a_ceiling_hold_shows_the_measured_clock_and_turns_amber(self):
        self.hold_on_the_ceiling()
        self.assertEqual(self.values["r_core"], "measured 2087  Δ -27.9")
        self.assertEqual(self.colour["r_core"], WARN)
        self.assertEqual(self.values["s_core"], "P0 · steady at load")
        self.assertIn("-1.86 bins of 15.000 MHz", self.values["rt_core"])

    def test_the_clock_units_say_programmed(self):
        units = {key: unit for key, _l, unit, _c in Druta.TILES}
        self.assertEqual([units[k] for k in ("core", "xbar", "mem")], ["MHz programmed"] * 3)

    def test_one_snapshot_redrawn_is_one_reading(self):
        d = self.snap()
        for _ in range(20):
            self.app.refresh_real_clocks(d, now=self.t)
        self.assertEqual(self.values["r_core"], "measured: checking B")
        self.assertEqual(self.app._real.last("core").samples, 1)

    def test_stale_telemetry_is_dimmed_and_not_fed(self):
        self.hold_on_the_ceiling()
        samples = self.app._real.last("core").samples
        self.app._stale = True
        self.feed(self.snap(b=2087200))
        self.assertEqual(self.colour["r_core"], DIM)
        self.assertTrue(self.values["s_core"].endswith(" · stale"))
        self.assertEqual(self.app._real.last("core").samples, samples)

    def test_a_card_whose_B_copies_A_never_prints_a_measured_number(self):
        for a in (648000, 648000, 4232000, 4232000, 4440000, 4440000, 648000, 4232000):
            self.feed(self.snap(core=a // 1000, rows=[row(0, "GPC", a, a)]))
            self.assertIsNone(re.match(r"^measured \d", self.values["r_core"]))
            self.assertEqual(self.colour["r_core"], DIM)
        self.assertEqual(self.values["r_core"], "measured: none (B = A)")

    def test_a_B_that_never_moves_on_its_own_is_labelled_unproven(self):
        for _ in range(6):
            self.feed(self.snap(rows=[row(0, "GPC", 2115000, 2087100)]))
        self.assertTrue(self.values["r_core"].startswith("unproven 2087"))
        self.assertEqual(self.colour["r_core"], DIM)

    def test_an_idle_card_is_shown_dim_with_its_load(self):
        self.hold_on_the_ceiling()
        self.feed(self.snap(util=3, b=1300000))
        self.assertEqual(self.values["s_core"], "P0 · load 3 %")
        self.assertEqual(self.colour["r_core"], DIM)
        self.assertIn("Not judged below 90 % load", self.values["rt_core"])
        self.assertIn("drops below 85 %", self.values["rt_core"])

    def test_settling_says_when_the_load_stops_counting(self):
        self.hold_on_the_ceiling(n=5)
        self.feed(self.snap(core=2100, a=2100000, b=2072100))       # A moved: settle again
        self.assertEqual(self.values["s_core"], "P0 \u00b7 settling")
        self.assertIn("drops below 85 %", self.values["rt_core"])

    def test_the_private_getter_down_reads_n_a_everywhere(self):
        self.feed({"core": 2115, "xbar": None, "mem": 6801, "clk_domains": None,
                   "clk_domains_err": "private NvAPI_GPU_GetAllClocks did not answer"})
        self.assertEqual([self.values[f"r_{k}"] for k in ("core", "xbar", "mem")],
                         ["measured: n/a"] * 3)
        self.assertIn("the only source these tiles use", self.values["rt_core"])

    def test_the_power_monitor_snapshot_draws_without_a_clock_reading(self):
        self.feed({"power_w": 50, "pl_now_mw": 200000})
        self.assertEqual(self.values["r_core"], "measured: --")

    def test_only_a_confirmed_core_row_feeds_the_core_line(self):
        for bad in (row(0, "GPC", 2115000, 2087100, grade=PRIV_LIKELY),
                    row(0, "", 2115000, 2087100, grade=PRIV_UNNAMED),
                    row(0, "GPC", 2115000, 0)):
            with self.subTest(row=bad):
                self.feed(self.snap(rows=[bad]))
                self.assertEqual(self.values["r_core"], "measured: --")

    def test_pascal_2clk_is_halved_to_core_mhz_and_banded_on_its_bins(self):
        self.app.step_khz.return_value = 12657
        for i in range(9):
            self.feed(self.snap(core=1898, xbar=None, rows=[
                row(15, "GPC2CLK", 3797100, jitter(3771100, i), scale=2)]))
        self.assertEqual(self.values["r_core"], "measured 1886  Δ -13.0")
        self.assertEqual(self.colour["r_core"], WARN)
        self.assertIn("= 2 x", self.values["rt_core"])

    def test_the_xbar_line_follows_the_tiles_own_slot(self):
        for name, grade in (("Crossbar", PRIV_CONFIRMED), ("", PRIV_UNNAMED)):
            with self.subTest(name=name):
                self.feed(self.snap(rows=[row(1, name, 2025000, 2011100, grade=grade)]))
                self.assertNotEqual(self.values["r_xbar"], "measured: --")
        self.feed(self.snap(rows=[row(1, "", 0, 0, grade=PRIV_UNPOPULATED),
                                  row(16, "XBAR2CLK", 4050000, 4022000)]))
        self.assertEqual(self.values["r_xbar"], "measured: --")

    def test_memory_is_dim_until_its_B_is_shown_to_be_measured(self):
        self.feed(self.snap(rows=[row(4, "MEM", 6801000, 6794200)]))
        self.assertEqual(self.values["r_mem"], "measured: checking B")
        self.assertEqual(self.colour["r_mem"], DIM)
        for _ in range(6):
            self.feed(self.snap(rows=[row(4, "MEM", 6801000, 6794200)]))
        self.assertTrue(self.values["r_mem"].startswith("unproven"))
        self.assertEqual(self.colour["r_mem"], DIM)

    def test_memory_is_in_the_tiles_units_and_never_colour_graded(self):
        for i in range(8):
            self.feed(self.snap(rows=[row(4, "MEM", 6801000, jitter(6794200, i))]))
        self.assertEqual(self.values["r_mem"], "measured 3397  Δ -3.4")
        self.assertEqual(self.colour["r_mem"], DIM)                  # shown, never judged
        self.assertIn("fixed -6.8 MHz", self.values["rt_mem"])
        self.assertNotIn("Plain = within one clock bin", self.values["rt_mem"])

    def test_a_row_that_is_not_the_tiles_clock_is_named_and_never_coloured(self):
        self.hold_on_the_ceiling(core=2100)
        self.assertEqual(self.values["r_core"], "measured 2087  \u0394 -27.9")   # no longer
        self.assertEqual(self.values["s_core"], "P0 \u00b7 vs A 2115")       # the detail moves
        self.assertEqual(self.colour["r_core"], DIM)

    def test_the_xbar_line_is_shown_but_never_coloured(self):
        for i in range(8):
            self.feed(self.snap(rows=[row(0, "GPC", 2115000, jitter(2087100, i)),
                                      row(1, "XBAR", 2025000, jitter(1985000, i))]))
        self.assertTrue(self.values["r_xbar"].startswith("measured 1985"))
        self.assertEqual(self.colour["r_xbar"], DIM)
        self.assertIn("Shown, never judged", self.values["rt_xbar"])

    def test_a_B_equal_to_A_at_one_steady_clock_says_so_without_a_number(self):
        for _ in range(7):
            self.feed(self.snap(rows=[row(0, "GPC", 2115000, 2115000)]))
        self.assertEqual(self.values["r_core"], "measured: same as A")
        self.assertEqual(self.values["s_core"], "P0 · same as A")
        self.assertNotIn("B differs from A", self.values["rt_core"])
        self.assertIn("cannot yet be told apart from a copy of A", self.values["rt_core"])

    def test_an_unproven_B_that_equals_A_right_now_is_not_said_to_differ(self):
        for _ in range(6):
            self.feed(self.snap(rows=[row(0, "GPC", 2115000, 2087100)]))
        for _ in range(2):
            self.feed(self.snap(rows=[row(0, "GPC", 2115000, 2115000)]))
        self.assertTrue(self.values["r_core"].startswith("unproven"))
        self.assertIn("B equals A right now", self.values["rt_core"])
        self.assertNotIn("B differs from A", self.values["rt_core"])

    def test_one_failed_private_read_keeps_the_last_reading_dimmed(self):
        self.hold_on_the_ceiling()
        self.feed({"core": 2115, "xbar": 2025, "mem": 6801, "util_gpu": 99, "pstate": 0,
                   "clk_domains": None, "clk_domains_err": "did not answer"})
        self.assertEqual(self.values["r_core"], "last 2087  Δ -27.9")
        self.assertEqual(self.colour["r_core"], DIM)
        self.assertEqual(self.values["s_core"], "P0 · read failed")
        self.hold_on_the_ceiling(n=1)
        self.assertEqual(self.app._real.last("core").state, "judged")   # continuity kept

    def test_a_failed_read_marks_every_tile_and_expires(self):
        for i in range(9):
            self.feed(self.snap(rows=[row(0, "GPC", 2115000, jitter(2087100, i)),
                                      row(1, "XBAR", 2025000, jitter(1985000, i)),
                                      row(4, "MEM", 6801000, jitter(6794200, i))]))
        # the XBAR figure comes from the private getter too, so it goes with it
        failed = {"core": 300, "mem": 405, "util_gpu": 0, "pstate": 8,
                  "clk_domains": None, "clk_domains_err": "did not answer"}
        self.feed(failed)
        self.assertEqual([self.values[f"r_{k}"][:5] for k in ("core", "xbar", "mem")],
                         ["last "] * 3)
        self.assertNotIn("None", self.values["rt_xbar"])
        for _ in range(3):                          # the UI redraws the same failed snapshot
            self.t += 1.05
            self.app.refresh_real_clocks(failed, now=self.t)
        self.assertEqual([self.values[f"r_{k}"] for k in ("core", "xbar", "mem")],
                         ["measured: n/a"] * 3)
        self.assertEqual(self.values["s_core"], "P8")

    def test_a_row_not_yet_read_is_not_called_missing(self):
        self.app._stale = True
        self.feed(self.snap())
        self.assertEqual(self.values["r_core"], "measured: not read yet")
        self.assertNotIn("No row on this card", self.values["rt_core"])

    def test_no_colour_is_claimed_against_a_borrowed_clock_step(self):
        self.app.gpu = SimpleNamespace(static={}, step_is_measured=lambda: False)
        self.hold_on_the_ceiling(n=9)
        self.assertEqual(self.colour["r_core"], DIM)
        self.assertIn("could not be measured", self.values["rt_core"])

    def test_a_fault_leaves_no_old_verdict_behind(self):
        app = self.app
        self.hold_on_the_ceiling()
        self.assertEqual(self.colour["r_core"], WARN)
        app.refresh_domains = Mock()
        app.mem_fmt = lambda value: ("--", "")
        app._bar_band, app._bar_themes = {}, {}
        app.gpu = SimpleNamespace(static={}, mem_offset_scale=lambda: (1, "MHz"))
        app.shunt = SimpleNamespace(active=False)
        app.log_once = Mock()
        app.refresh_real_clocks = Mock(side_effect=RuntimeError("boom"))
        app.refresh_monitor({"power_w": 50, "pl_now_mw": 200000, "pstate": 0})
        self.assertEqual((self.values["r_core"], self.colour["r_core"]), ("measured: --", DIM))
        self.assertNotIn("Judged", self.values["rt_core"])
        self.assertIn("could not be drawn", self.values["rt_core"])
        self.assertEqual(self.values["s_core"], "P0")

    def test_a_card_switch_starts_its_evidence_over(self):
        self.hold_on_the_ceiling()
        app = self.app
        app.gpu = SimpleNamespace(static={}, slot=lambda: "0000:01:00.0")
        app._tim_lock = __import__("threading").Lock()
        try:
            app.reset_card_state()
        except AttributeError as exc:            # the rest of the app state is not built here
            self.skipTest(f"reset_card_state needs more of the app: {exc}")
        self.feed(self.snap(rows=[row(0, "GPC", 1950000, 1950000)]))
        self.assertEqual(self.values["r_core"], "measured: checking B")

    def test_a_colour_held_on_its_edge_says_so(self):
        for i in range(12):
            self.feed(self.snap(b=2100300 + (i % 2) * 8))              # -14.7: amber
        for i in range(20):
            self.feed(self.snap(b=2100800 + (i % 2) * 8))              # -14.2
        self.assertEqual(self.colour["r_core"], WARN)
        self.assertIn("Amber is HELD", self.values["rt_core"])
        self.assertIn("held until the median falls clearly back", self.values["rt_core"])

    def test_a_three_bin_loss_is_red(self):
        for i in range(9):
            self.feed(self.snap(b=jitter(2069000, i)))
        self.assertEqual(self.colour["r_core"], BAD)

    def test_the_colour_is_only_reconfigured_when_it_changes(self):
        self.hold_on_the_ceiling(n=12)
        # dim while checking/settling, then amber: two changes for the core line
        self.assertEqual(self.colour_calls.count("r_core"), 2)

    def test_a_fault_in_the_measured_line_does_not_stop_the_monitor(self):
        app = self.app
        app.refresh_domains = Mock()
        app.mem_fmt = lambda value: ("--", "")
        app._bar_band, app._bar_themes = {}, {}
        app.gpu = SimpleNamespace(static={}, mem_offset_scale=lambda: (1, "MHz"))
        app.shunt = SimpleNamespace(active=False)
        app.log_once = Mock()
        app.refresh_real_clocks = Mock(side_effect=RuntimeError("boom"))
        app.refresh_monitor({"power_w": 50, "pl_now_mw": 200000, "temp_edge": 40})
        self.assertEqual((self.values["t_pwr"], self.values["t_edge"]), ("50", "40"))
        app.log_once.assert_called_once()
        self.assertEqual(app.log_once.call_args.args[0], "real_clock")


class SharedRuleTests(unittest.TestCase):
    def test_the_core_row_is_shared_with_the_log_check(self):
        rows = [row(15, "GPC2CLK", 3797100, 3771100, scale=2)]
        self.assertIsNone(Druta.core_clock_row(rows))                  # the log: GPC only
        self.assertIs(Druta.real_clock_row(rows, "core"), rows[0])     # the tile: 2CLK too

    def test_the_domain_table_bands_carry_the_same_tolerance(self):
        app = Druta.__new__(Druta)
        app.step_khz = Mock(return_value=15000)
        self.assertEqual([app.dom_band(v) for v in (14.9, 14.4, 44.5, -28.0)],
                         ["warn", "ok", "bad", "warn"])
        app.step_khz.return_value = 12657
        self.assertEqual(app.dom_band(24.5, scale=2), "warn")



class LogCheckTests(RealClockCase):
    def setUp(self):
        super().setUp()
        self.app.log = Mock()
        self.app._clk_lock = None

    def run_log(self, n, a, b_of, util=99, redraws=4, core=None):
        for i in range(n):
            d = self.snap(core=a // 1000 if core is None else core, util=util,
                          rows=[row(0, "GPC", a, b_of(i))])
            self.feed(d)
            for _ in range(redraws):                # the UI loop redraws each snapshot
                self.app.check_clock_gap(d)

    def warned(self):
        return [c for c in self.app.log.call_args_list if "below the" in str(c)]

    def test_the_log_says_nothing_about_a_B_the_tile_does_not_trust(self):
        self.run_log(30, 1950000, lambda i: 1935000)             # never moves: unproven
        self.assertEqual(self.warned(), [])
        self.run_log(30, 1950000, lambda i: jitter(1935000, i))  # a counter: measured
        self.assertEqual(len(self.warned()), 1)

    def test_the_log_counts_exactly_the_tiles_load_gate(self):
        self.run_log(40, 1950000, lambda i: jitter(1935000, i), util=89)
        self.assertEqual(self.warned(), [])
        self.run_log(25, 1950000, lambda i: jitter(1935000, i), util=90)
        self.assertEqual(len(self.warned()), 1)

    def test_the_log_keeps_the_tiles_load_release(self):
        self.run_log(6, 1950000, lambda i: jitter(1935000, i), util=95)
        self.run_log(20, 1950000, lambda i: jitter(1935000, i + 6), util=87)
        self.assertEqual(self.colour["r_core"], WARN)                # the tile still judges
        self.assertEqual(len(self.warned()), 1)

    def test_the_log_says_nothing_while_the_tile_refuses_to_judge(self):
        cases = {
            "not refreshing": dict(b_of=lambda i: jitter(1935000, i) if i < 6 else 1935000),
            "unpaired": dict(b_of=lambda i: jitter(1935000, i), core=1935),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.setUp()
                self.run_log(40, 1950000, **kw)
                self.assertEqual(self.warned(), [])

    def test_the_log_says_nothing_against_a_borrowed_clock_step(self):
        self.app.gpu = SimpleNamespace(static={}, step_is_measured=lambda: False)
        self.run_log(40, 1950000, lambda i: jitter(1935000, i))
        self.assertEqual(self.warned(), [])

    def test_a_stale_snapshot_redrawn_is_no_reading_for_the_log(self):
        self.run_log(9, 1965000, lambda i: jitter(1964000, i))       # plain: no warning
        d = self.snap(core=1965, rows=[row(0, "GPC", 1965000, 1905000)])   # a lagging read
        self.feed(d)
        self.app._stale = True
        for _ in range(40):                             # 10 s of redraws of that one read
            self.t += 0.25
            self.app.refresh_real_clocks(d, now=self.t)
            self.app.check_clock_gap(d)
        self.assertEqual(self.warned(), [])

    def test_the_log_reports_only_a_clock_below_the_one_shown(self):
        self.run_log(30, 1950000, lambda i: jitter(1965000, i))      # one bin ABOVE
        self.assertEqual(self.colour["r_core"], WARN)                # the tile: either sign
        self.assertEqual(self.warned(), [])

    def test_one_plain_reading_is_not_a_recovery(self):
        self.run_log(30, 1950000, lambda i: jitter(1935000, i))
        self.assertEqual(len(self.warned()), 1)
        def recovered():
            return [c for c in self.app.log.call_args_list if "clock it shows again" in str(c)]
        for i in range(30):                                        # the gap closes ...
            self.run_log(1, 1950000, lambda _i, i=i: jitter(1949000, i))
            if self.colour["r_core"] == TEXT:
                break
        self.assertEqual(self.colour["r_core"], TEXT)              # ... the tile turns plain
        self.assertEqual(recovered(), [])                          # one reading: not yet
        self.run_log(5, 1950000, lambda i: jitter(1949008, i))
        self.assertEqual(len(recovered()), 1)

    def test_the_log_carries_the_tiles_tolerance(self):
        self.app.step_khz.return_value = 12657
        self.run_log(30, 1898000, lambda i: jitter(1885800, i))  # -12.2 on a 12.657 bin
        self.assertEqual(len(self.warned()), 1)


class TileHeightTests(RealClockCase):
    def test_the_tile_does_not_change_height_as_the_line_changes_state(self):
        app = self.app
        app.s = lambda v: v
        # a narrow tile: "P0 · steady at load" wraps where "P0 · checking" does not
        app.text_h = lambda txt, font, wrap=-1.0: 19 * (1 + len(txt or "") // 18)
        heights = []

        def measure():
            with patch("druta.druta.dpg.does_item_exist",
                       side_effect=lambda tag: tag[:2] in ("s_", "r_")), \
                    patch("druta.druta.dpg.get_value",
                          side_effect=lambda tag: self.values.get(tag, "")):
                heights.append(app.tile_height(180))
        self.feed(self.snap())                                  # checking
        measure()
        self.hold_on_the_ceiling()                              # measured, amber
        measure()
        self.feed(self.snap(util=3, b=1300000))                 # light
        measure()
        app._stale = True
        self.feed(self.snap())                                  # stale
        measure()
        self.assertEqual(len(set(heights)), 1, heights)

    def test_a_wider_sign_or_a_state_line_does_not_grow_the_tile(self):
        # "+" is wider than "-" in the UI font: here it takes a character more,
        # so "measured 0000  \u0394 +00.0" wraps where the "-" line does not
        app = self.app
        app.s = lambda v: v
        app.text_h = lambda txt, font, wrap=-1.0: 19 * (1 + (len(txt or "")
                                                             + (txt or "").count("+")) // 23)
        heights = []

        def measure():
            with patch("druta.druta.dpg.does_item_exist",
                       side_effect=lambda tag: tag[:2] in ("s_", "r_")), \
                    patch("druta.druta.dpg.get_value",
                          side_effect=lambda tag: self.values.get(tag, "")):
                heights.append(app.tile_height(180))
        self.feed(self.snap())                                  # checking
        measure()
        for i in range(9):
            self.feed(self.snap(b=jitter(2129900, i)))          # one bin above: +14.9
        self.assertIn("+14.9", self.values["r_core"])
        measure()
        self.assertEqual(len(set(heights)), 1, heights)


if __name__ == "__main__":
    unittest.main()
