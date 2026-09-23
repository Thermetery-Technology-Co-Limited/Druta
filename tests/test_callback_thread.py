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
    def run_app(self, callback):
        app = Druta.__new__(Druta)
        app.gpu = SimpleNamespace(available=lambda: True, status_line=lambda: "test")
        app.gpu_list = []
        app.s = lambda value: value
        app.menu_h = lambda: 20
        app._startup_request = app._startup_manager = None
        app._stop = threading.Event()
        app._lock = threading.Lock()
        app._snap = app._snap_err = app._snap_t = None
        events = []
        for name in ("load_fonts", "build_ui", "relayout", "poll_loop",
                     "sync_lock_ui", "log", "vf_read", "timings_capture",
                     "clear_once", "set_stale", "drag_watchdog", "update_vf_corner"):
            setattr(app, name, Mock())
        app.poll_profile_load = Mock(side_effect=lambda: events.append("refresh"))
        app.release_on_exit = Mock(side_effect=lambda: events.append("release"))
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
                     "is_dearpygui_running", "get_callback_queue",
                     "render_dearpygui_frame", "destroy_context"):
            p = patch("druta.druta.dpg." + name)
            mocks[name] = p.start()
            patches.append(p)
        mocks["window"].return_value = MagicMock()
        mocks["is_dearpygui_running"].side_effect = lambda: running[0]
        mocks["get_callback_queue"].return_value = [[queued]]
        mocks["render_dearpygui_frame"].side_effect = render
        mocks["destroy_context"].side_effect = lambda: events.append("destroy")
        error = None
        try:
            with patch("druta.druta.threading.Thread"):
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
                                  "refresh", "render", "release", "destroy"])

    def test_callback_error_still_releases_owned_state_before_destroy(self):
        def fail(_running):
            raise RuntimeError("callback failed")
        events, error = self.run_app(fail)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), "callback failed")
        self.assertEqual(events, [("callback", threading.get_ident()), "release", "destroy"])

    def test_exit_callback_skips_refresh_and_still_cleans_up(self):
        def stop(running):
            running[0] = False
        events, error = self.run_app(stop)
        self.assertIsNone(error)
        self.assertEqual(events, [("callback", threading.get_ident()), "release", "destroy"])


if __name__ == "__main__":
    unittest.main()
