# Druta - hardware-free tests for physical clock versus raw V/F deltas.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise editor staging with no GPU, driver initialization, or GUI context."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.druta import Druta
from druta.nvbackend import GPU


def editor(freq_div, step_khz, points, gfx_max=2300):
    app = Druta.__new__(Druta)
    app.gpu = SimpleNamespace(
        static={"gfx_max": gfx_max},
        clock_step_khz=Mock(return_value=step_khz),
        vfp_layout=Mock(return_value=SimpleNamespace(freq_div=freq_div)))
    app.vf_points = copy.deepcopy(points)
    app.vf_by_idx = {p["idx"]: p for p in app.vf_points}
    app.vf_orig = {p["idx"]: p["delta_khz"] for p in points}
    app.vf_work = dict(app.vf_orig)
    app._plan_note = None
    app.push_undo = Mock()
    app.sync_sel_inputs = Mock()
    app.vf_redraw = Mock()
    app.log = Mock()
    return app


class VfStagingTests(unittest.TestCase):
    CARDS = (("Turing", 1, 15000), ("Pascal", 2, 12657),
             ("Blackwell", 1, 15000))

    def test_ramp_callback_can_raise_a_stock_peak_above_the_clock_list(self):
        for name, divisor, step in self.CARDS:
            with self.subTest(card=name):
                points = [dict(idx=i, volt_mv=987.5 + i * 12.5,
                               freq_mhz=1911, delta_khz=0) for i in range(8)]
                app = editor(divisor, step, points, gfx_max=1911)
                with patch("druta.druta.dpg.get_value", side_effect={
                        "rfloor": 1000, "vcap": 1062.5}.__getitem__):
                    app.vf_ramp()
                    self.assertIsNotNone(app.apply_plan())
                self.assertEqual(app.wf(6), 1911000 + 5 * step)
                self.assertEqual(app.vf_work[0], 0)  # Below the floor.
                self.assertEqual(app.vf_work[7], 0)  # Above the cap.
                self.assertTrue(all(app.wf(i) >= 1911000 for i in range(8)))
                rephase, _ = GPU.compute_rephase(
                    app.vf_work, app.vf_delta_step_khz())
                self.assertEqual(rephase, {})

    def test_pascal_ramp_callback_raises_rounded_1911_mhz_plateau(self):
        clocks = [1860, 1873, 1885.5, 1898, 1911, 1911, 1911, 1911]
        points = [dict(idx=i, volt_mv=1000 + i * 12.5,
                       freq_mhz=f, delta_khz=0) for i, f in enumerate(clocks)]
        app = editor(2, 12657, points, gfx_max=1911)
        with patch("druta.druta.dpg.get_value", side_effect={
                "rfloor": 1000, "vcap": 1093.75}.__getitem__):
            app.vf_ramp()
        self.assertEqual(app.wf(7), 1911000 + 3 * 12657)
        self.assertTrue(all(app.wf(i) >= f * 1000 for i, f in enumerate(clocks)))
        self.assertEqual(GPU.compute_rephase(app.vf_work, 2 * 12657)[0], {})

    def test_max_it_applies_the_ramp_above_the_stock_clock_list_peak(self):
        points = [dict(idx=i, volt_mv=1000 + i * 12.5,
                       freq_mhz=1911, delta_khz=0) for i in range(8)]
        app = editor(2, 12657, points, gfx_max=1911)
        app.guard = Mock(return_value=True)
        app.gpu.static["pl_max_mw"] = 300000
        for method in ("set_fan", "set_power_limit_mw", "set_voltage_boost"):
            setattr(app.gpu, method, Mock(return_value=(True, "fixture")))
        app.autosave_before = Mock()
        applied = []
        app.vf_apply = Mock(side_effect=lambda **kw: applied.append(dict(app.vf_work)))
        app.hold_cap_point = Mock()
        with patch("druta.druta.dpg.get_value", side_effect={
                "rfloor": 1000, "vcap": 1093.75}.__getitem__), \
                patch("druta.druta.dpg.does_item_exist", return_value=False):
            app.oc_max()
        self.assertEqual(applied[0][7], 7 * 12657 * 2)
        self.assertEqual(app.wf(7), 1911000 + 7 * 12657)
        app.vf_apply.assert_called_once_with(autosave=False)
        app.autosave_before.assert_called_once_with("one-click max")
        app.hold_cap_point.assert_called_once_with(1093.75)

    def test_pascal_ramp_preview_survives_apply_rephase(self):
        # Measured R472 TITAN Xp frequencies: nominal 12.657 MHz bins
        # appear as rounded 12.5/13 MHz gaps in the evaluated curve.
        clocks = [1847.5, 1860, 1860, 1873, 1885.5, 1898, 1911, 1911]
        points = [dict(idx=i, volt_mv=975 + i * 12.5,
                       freq_mhz=f, delta_khz=0) for i, f in enumerate(clocks)]
        app = editor(2, 12657, points, gfx_max=1911)
        changes, _, _, _ = GPU.compute_ramp(
            app.work_pts(), 975, 1094, max_khz=1911000, step_khz=12657)
        self.assertTrue(changes)
        app.stage_curve_changes(changes)
        rephase, _ = GPU.compute_rephase(app.vf_work, app.vf_delta_step_khz())
        self.assertEqual(rephase, {})
        for p in points:
            self.assertGreaterEqual(app.wf(p['idx']), p['freq_mhz'] * 1000)
            self.assertLessEqual(app.wf(p['idx']), 1911000)

    def test_pascal_ceiling_does_not_preview_fractional_bin_edits(self):
        clocks = [1860, 1873, 1885.5, 1898, 1911, 1911]
        points = [dict(idx=i, volt_mv=1000 + i * 12.5,
                       freq_mhz=f, delta_khz=0) for i, f in enumerate(clocks)]
        changes, _, _, meta = GPU.compute_ramp(
            points, 1000, 1094, max_khz=1911000, step_khz=12657)
        self.assertEqual(changes, [])
        self.assertTrue(meta['clamped'])
        self.assertFalse(meta['unique'])

    def test_ramp_never_demotes_points_already_above_clock_list_max(self):
        points = [dict(idx=i, volt_mv=900 + i * 12.5,
                       freq_mhz=2000, delta_khz=0) for i in range(3)]
        changes, _, _, _ = GPU.compute_ramp(
            points, 900, 1000, max_khz=1911000, step_khz=12657)
        self.assertEqual(changes, [])

    def test_working_frequency_and_raw_grid_follow_the_card(self):
        for name, divisor, step in self.CARDS:
            with self.subTest(card=name):
                app = editor(divisor, step, [dict(
                    idx=4, volt_mv=900, freq_mhz=1800, delta_khz=64000)])
                app.vf_work[4] += 2 * step * divisor
                self.assertEqual(app.wf(4), 1800000 + 2 * step)
                self.assertEqual(app.work_pts()[0]["delta_khz"],
                                 64000 + 2 * step * divisor)
                self.assertEqual(app.vf_delta_step_khz(), step * divisor)

    def test_deflatten_adds_one_physical_bin_to_an_already_edited_curve(self):
        for name, divisor, step in self.CARDS:
            with self.subTest(card=name):
                points = [dict(idx=i, volt_mv=800 + 100 * i,
                               freq_mhz=1800, delta_khz=60000 + i * 20000)
                          for i in range(3)]
                app = editor(divisor, step, points)
                # Existing edits and unequal raw baselines must survive;
                # multiplying the entire planner delta would corrupt both.
                app.vf_work = {i: d + 2 * step * divisor
                               for i, d in app.vf_work.items()}
                before = dict(app.vf_work)
                with patch("druta.druta.dpg.get_value", return_value=900):
                    app.vf_deflatten()
                self.assertEqual(app.vf_work[0], before[0])
                for index in (1, 2):
                    self.assertEqual(app.vf_work[index],
                                     before[index] + step * divisor)
                    self.assertEqual(app.wf(index), 1800000 + 3 * step)
                app.push_undo.assert_called_once_with("limited de-flatten")

                # The preview now has a unique boundary maximum; repeating the
                # operation must not stack another offset or reinterpret raw units.
                staged = dict(app.vf_work)
                with patch("druta.druta.dpg.get_value", return_value=900):
                    app.vf_deflatten()
                self.assertEqual(app.vf_work, staged)
                app.push_undo.assert_called_once()

    def test_deflatten_at_hardware_max_scales_only_the_lowered_shadow(self):
        for name, divisor, step in self.CARDS:
            with self.subTest(card=name):
                points = [dict(idx=i, volt_mv=800 + 100 * i,
                               freq_mhz=1850 if i == 0 else 1911,
                               delta_khz=23000 + i * 21000)
                          for i in range(4)]
                app = editor(divisor, step, points, gfx_max=1911)
                before = dict(app.vf_work)
                with patch("druta.druta.dpg.get_value", return_value=1000):
                    app.vf_deflatten()
                expected = dict(before)
                expected[1] -= step * divisor
                self.assertEqual(app.vf_work, expected)
                self.assertEqual(app.wf(1), 1911000 - step)
                self.assertEqual(app.wf(2), 1911000)

    def test_set_mhz_round_trip_uses_raw_delta_units_without_moving_baseline(self):
        for name, divisor, step in self.CARDS:
            for bins in (-2, 3):
                with self.subTest(card=name, bins=bins):
                    app = editor(divisor, step, [dict(
                        idx=4, volt_mv=900, freq_mhz=1800, delta_khz=64000)])
                    target = 1800000 + bins * step
                    app.set_work_freq(4, target)
                    self.assertEqual(app.vf_work[4],
                                     64000 + bins * step * divisor)
                    self.assertEqual(app.wf(4), target)
                    self.assertEqual(app.vf_orig[4], 64000)

    def test_rephase_removes_half_physical_bin_and_preserves_whole_bins(self):
        for name, divisor, step in self.CARDS:
            with self.subTest(card=name):
                points = [dict(idx=i, volt_mv=800 + 100 * i,
                               freq_mhz=1800, delta_khz=0)
                          for i in range(4)]
                app = editor(divisor, step, points)
                raw_step = step * divisor
                app.vf_work[1] = raw_step + raw_step // 2
                app.vf_work[2] = 2 * raw_step
                app.vf_rephase()
                self.assertEqual(app.vf_work,
                                 {0: 0, 1: raw_step, 2: 2 * raw_step, 3: 0})
                self.assertEqual(app.wf(1), 1800000 + step)
                self.assertEqual(app.wf(2), 1800000 + 2 * step)
                app.push_undo.assert_called_once_with("re-phase")


if __name__ == "__main__":
    unittest.main()
