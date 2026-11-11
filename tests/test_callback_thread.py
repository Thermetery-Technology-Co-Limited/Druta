# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""UI callbacks finish before refresh, with cleanup even when callbacks fail."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta import druta
from druta.druta import Druta


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CallbackThreadTests(unittest.TestCase):
    def run_app(self, callback, *, paused_first=False, close_paused=False,
                always_refresh=False, pacing_delays=None, clock=None):
        app = Druta.__new__(Druta)
        app.gpu = SimpleNamespace(available=lambda: True, status_line=lambda: "test")
        app.gpu_list = []
        app.s = lambda value: value
        app.menu_h = lambda: 20
        app._startup_request = app._startup_manager = None
        app._stop = threading.Event()
        app._lock = threading.Lock()
        app._snap = app._snap_err = app._snap_t = None
        app._once = {}
        app._always_refresh = always_refresh
        app._monitoring_paused = False
        self.app = app
        events = []
        for name in ("load_fonts", "build_ui", "relayout", "poll_loop",
                     "sync_lock_ui", "log", "vf_read", "timings_capture",
                     "clear_once", "set_stale", "drag_watchdog",
                     "poll_tab_tear", "tick_gate_flashes", "layout_torn_pages",
                     "update_vf_corner", "refresh_periodic"):
            setattr(app, name, Mock())
        app.poll_profile_load = Mock(side_effect=lambda: events.append("refresh"))
        app.release_on_exit = Mock(side_effect=lambda: events.append("release"))
        app.idle_maintenance = Mock(side_effect=lambda: events.append("maintenance"))
        idle = Mock(hotkey_available=True, closed=False, activity_serial=0)
        self.idle = idle
        idle.poll.return_value = False
        if paused_first:
            idle.poll.side_effect = [True, False]
        idle.pause.return_value = True
        def idle_wait(_timeout):
            events.append("wait")
            if clock is not None:
                clock.advance(_timeout)
            if close_paused:
                running[0] = False
        idle.wait.side_effect = idle_wait
        idle.close.side_effect = lambda: events.append("hotkey closed")
        running = [True]
        pacer = Mock()
        pacer.delay.return_value = 0.0
        if pacing_delays is not None:
            pacer.delay.side_effect = pacing_delays
        self.pacer = pacer

        def render():
            events.append("render")
            running[0] = False

        def queued():
            events.append(("callback", threading.get_ident()))
            callback(running)

        patches = []
        mocks = {}
        for name in ("create_context", "configure_app", "create_viewport",
                     "window", "add_spacer", "setup_dearpygui", "show_viewport",
                     "set_primary_window", "set_viewport_resize_callback",
                     "does_item_exist", "configure_item",
                     "is_dearpygui_running", "get_callback_queue",
                     "render_dearpygui_frame", "is_mouse_button_down", "destroy_context"):
            p = patch("druta.druta.dpg." + name)
            mocks[name] = p.start()
            patches.append(p)
        mocks["window"].return_value = MagicMock()
        mocks["does_item_exist"].return_value = False
        mocks["is_dearpygui_running"].side_effect = lambda: running[0]
        callback_queue = [[[queued]]]
        mocks["get_callback_queue"].side_effect = (
            lambda: callback_queue.pop(0) if callback_queue else [])
        mocks["render_dearpygui_frame"].side_effect = render
        mocks["is_mouse_button_down"].return_value = False
        mocks["destroy_context"].side_effect = lambda: events.append("destroy")
        if clock is not None:
            p = patch("druta.druta.time.perf_counter", side_effect=clock)
            p.start()
            patches.append(p)
        error = None
        try:
            with patch("druta.druta.threading.Thread"), \
                    patch("druta.druta.windowidle.WindowIdle.attach", return_value=idle), \
                    patch("druta.druta.refreshpolicy.FramePacer", return_value=pacer):
                try:
                    app.run()
                except Exception as exc:
                    error = exc
            mocks["configure_app"].assert_called_once_with(manual_callback_management=True)
            app.release_on_exit.assert_called_once()
            self.assertTrue(app._stop.is_set())
        finally:
            for p in reversed(patches):
                p.stop()
        return events, error

    def test_callbacks_run_on_render_thread_before_per_frame_work(self):
        events, error = self.run_app(lambda _running: None)
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()),
                                  "refresh", "render", "release", "hotkey closed", "destroy"])

    def test_callback_error_still_releases_owned_state_before_destroy(self):
        def fail(_running):
            raise RuntimeError("callback failed")
        events, error = self.run_app(fail)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), "callback failed")
        self.assertEqual(events, [("callback", threading.get_ident()), "release", "hotkey closed", "destroy"])

    def test_exit_callback_skips_refresh_and_still_cleans_up(self):
        def stop(running):
            running[0] = False
        events, error = self.run_app(stop)
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()), "release", "hotkey closed", "destroy"])

    def test_paused_loop_maintains_owned_work_without_refresh_or_render_then_resumes(self):
        events, error = self.run_app(lambda _running: None, paused_first=True)
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()), "maintenance", "wait",
                                  "refresh", "render", "release", "hotkey closed", "destroy"])

    def test_exit_while_paused_releases_owned_state_without_rendering(self):
        events, error = self.run_app(lambda _running: None, paused_first=True, close_paused=True)
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()), "maintenance", "wait",
                                  "release", "hotkey closed", "destroy"])

    def test_always_refresh_renders_while_minimized_and_keeps_monitoring_enabled(self):
        events, error = self.run_app(lambda _running: None, paused_first=True,
                                     always_refresh=True)
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()),
                                  "refresh", "render", "release", "hotkey closed", "destroy"])
        self.assertTrue(self.app._window_minimized)
        self.assertFalse(self.app._monitoring_paused)
        self.pacer.delay.assert_called_once_with(continuous=True)
        self.pacer.rendered.assert_called_once_with()
        self.app.idle_maintenance.assert_not_called()
        self.idle.wait.assert_not_called()

    def test_default_refresh_obeys_idle_pacing_before_rendering(self):
        events, error = self.run_app(lambda _running: None,
                                     pacing_delays=[0.06, 0.0])
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()), "refresh", "wait",
                                  "refresh", "render", "release", "hotkey closed", "destroy"])
        self.assertEqual(self.pacer.delay.call_count, 2)
        for call in self.pacer.delay.call_args_list:
            self.assertEqual(call.kwargs, {"continuous": False})
        self.idle.wait.assert_called_once_with(0.06)
        self.pacer.rendered.assert_called_once_with()

    def test_idle_frame_wait_wakes_for_the_quarter_second_service_deadline(self):
        clock = Clock()
        service_times = []

        def publish_snapshot(_running):
            self.app._snap = {"core": 900}
            self.app._snap_t = clock()
            self.app.refresh_periodic.side_effect = (
                lambda *_args: service_times.append(clock()))

        events, error = self.run_app(publish_snapshot, clock=clock,
                                     pacing_delays=[0.1, 0.1, 0.1, 0.1, 0.0])
        self.assertIsNone(error)
        self.assertEqual(service_times, [10.0, 10.25])
        waits = [call.args[0] for call in self.idle.wait.call_args_list]
        self.assertEqual(len(waits), 4)
        for actual, expected in zip(waits, [0.1, 0.1, 0.05, 0.1]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(events.count("refresh"), 5,
                         "pending work must progress while frame rendering waits")
        self.assertEqual(events.count("render"), 1)
        self.pacer.rendered.assert_called_once_with()

    def test_explicit_pause_skips_rendering_even_with_always_refresh_enabled(self):
        events, error = self.run_app(lambda _running: self.app.pause_display(),
                                     always_refresh=True, close_paused=True)
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()), "maintenance", "wait",
                                  "release", "hotkey closed", "destroy"])
        self.assertTrue(self.app._always_refresh)
        self.assertTrue(self.app._manual_display_pause)
        self.assertTrue(self.app._monitoring_paused)
        self.pacer.delay.assert_not_called()
        self.pacer.rendered.assert_not_called()


if __name__ == "__main__":
    unittest.main()
