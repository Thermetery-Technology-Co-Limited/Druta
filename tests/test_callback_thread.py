# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""UI callbacks finish before refresh, with cleanup even when callbacks fail."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta import druta
from druta.druta import Druta


class CallbackThreadTests(unittest.TestCase):
    def run_app(self, callback, *, paused_first=False, close_paused=False):
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
        events = []
        for name in ("load_fonts", "build_ui", "relayout", "poll_loop",
                     "sync_lock_ui", "log", "vf_read", "timings_capture",
                     "clear_once", "set_stale", "drag_watchdog",
                     "poll_tab_tear", "tick_gate_flashes", "layout_torn_pages",
                     "update_vf_corner"):
            setattr(app, name, Mock())
        app.poll_profile_load = Mock(side_effect=lambda: events.append("refresh"))
        app.release_on_exit = Mock(side_effect=lambda: events.append("release"))
        app.idle_maintenance = Mock(side_effect=lambda: events.append("maintenance"))
        idle = Mock(hotkey_available=True, closed=False)
        idle.poll.side_effect = [True, False] if paused_first else [False]
        def idle_wait(_timeout):
            events.append("wait")
            if close_paused:
                running[0] = False
        idle.wait.side_effect = idle_wait
        idle.close.side_effect = lambda: events.append("hotkey closed")
        running = [True]

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
                     "render_dearpygui_frame", "destroy_context"):
            p = patch("druta.druta.dpg." + name)
            mocks[name] = p.start()
            patches.append(p)
        mocks["window"].return_value = MagicMock()
        mocks["does_item_exist"].return_value = False
        mocks["is_dearpygui_running"].side_effect = lambda: running[0]
        mocks["get_callback_queue"].side_effect = [[[queued]], []]
        mocks["render_dearpygui_frame"].side_effect = render
        mocks["destroy_context"].side_effect = lambda: events.append("destroy")
        error = None
        try:
            with patch("druta.druta.threading.Thread"), \
                    patch("druta.druta.windowidle.WindowIdle.attach", return_value=idle):
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


if __name__ == "__main__":
    unittest.main()
