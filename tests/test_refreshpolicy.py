# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Visible idle pacing stays responsive without catch-up rendering bursts."""

import unittest

from druta.refreshpolicy import FramePacer


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FramePacerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pacer = FramePacer(clock=self.clock)

    def become_idle(self):
        self.clock.advance(1.0)

    def test_initial_grace_leaves_interactive_frames_to_vsync(self):
        self.assertEqual(self.pacer.delay(), 0.0)
        self.pacer.rendered()
        self.assertEqual(self.pacer.delay(), 0.0)
        self.clock.advance(0.74)
        self.pacer.rendered()
        self.assertEqual(self.pacer.delay(), 0.0)
        self.clock.advance(0.02)
        self.assertAlmostEqual(self.pacer.delay(), 0.08)

    def test_idle_frames_wait_until_tenth_second_after_completion(self):
        self.become_idle()
        self.assertEqual(self.pacer.delay(), 0.0)
        self.pacer.rendered()
        self.assertAlmostEqual(self.pacer.delay(), 0.1)
        self.clock.advance(0.04)
        self.assertAlmostEqual(self.pacer.delay(), 0.06)
        self.clock.advance(0.06)
        self.assertAlmostEqual(self.pacer.delay(), 0.0)

    def test_activity_immediately_wakes_and_renews_grace(self):
        self.become_idle()
        self.pacer.rendered()
        self.clock.advance(0.01)
        self.assertGreater(self.pacer.delay(), 0.0)
        self.pacer.note_activity()
        self.assertEqual(self.pacer.delay(), 0.0)
        self.pacer.rendered()
        self.clock.advance(0.74)
        self.pacer.rendered()
        self.assertEqual(self.pacer.delay(), 0.0)
        self.clock.advance(0.02)
        self.assertAlmostEqual(self.pacer.delay(), 0.08)

    def test_continuous_refresh_bypasses_cap_without_renewing_grace(self):
        self.become_idle()
        self.pacer.rendered()
        self.assertEqual(self.pacer.delay(continuous=True), 0.0)
        self.clock.advance(0.01)
        self.pacer.rendered()
        self.assertEqual(self.pacer.delay(continuous=True), 0.0)
        self.assertAlmostEqual(self.pacer.delay(continuous=False), 0.1)

    def test_overdue_frames_never_return_a_negative_wait(self):
        self.become_idle()
        self.pacer.rendered()
        self.clock.advance(60.0)
        self.assertEqual(self.pacer.delay(), 0.0)
        self.assertEqual(self.pacer.delay(), 0.0)
        self.pacer.rendered()
        self.assertAlmostEqual(self.pacer.delay(), 0.1)

    def test_slow_frames_do_not_schedule_catch_up_bursts(self):
        self.become_idle()
        self.pacer.rendered()
        self.clock.advance(0.1)
        self.assertAlmostEqual(self.pacer.delay(), 0.0)
        # Rendering takes longer than several idle intervals.
        self.clock.advance(0.35)
        self.pacer.rendered()
        self.assertAlmostEqual(self.pacer.delay(), 0.1)
        self.clock.advance(0.05)
        self.assertAlmostEqual(self.pacer.delay(), 0.05)

    def test_custom_rate_and_zero_grace(self):
        pacer = FramePacer(clock=self.clock, idle_fps=5.0, active_grace=0.0)
        self.assertEqual(pacer.delay(), 0.0)
        pacer.rendered()
        self.assertAlmostEqual(pacer.delay(), 0.2)
        pacer.note_activity()
        self.assertEqual(pacer.delay(), 0.0)
        pacer.rendered()
        self.assertAlmostEqual(pacer.delay(), 0.2)

    def test_invalid_pacing_configuration_is_rejected(self):
        for fps in (0.0, -1.0, float("inf"), float("nan")):
            with self.subTest(idle_fps=fps), self.assertRaises(ValueError):
                FramePacer(clock=self.clock, idle_fps=fps)
        for grace in (-1.0, float("inf"), float("nan")):
            with self.subTest(active_grace=grace), self.assertRaises(ValueError):
                FramePacer(clock=self.clock, active_grace=grace)


if __name__ == "__main__":
    unittest.main()
