# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Always refresh is opt-in, with explicit display pause taking precedence."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta.druta import Druta


class AlwaysRefreshTests(unittest.TestCase):
    def make_app(self):
        app = Druta.__new__(Druta)
        app._lock = threading.Lock()
        app._stop = threading.Event()
        app._poll_wake = Mock()
        app._poll_wake.wait.side_effect = lambda _timeout: app._stop.set()
        app._monitoring_paused = False
        app._monitoring_epoch = 0
        app._manual_display_pause = False
        app._window_minimized = False
        app._maintenance_snap = None
        app._snap = app._snap_err = app._snap_t = None
        app._gpu_gen = 1
        app._rebuilding = False
        app._clock_capability_recovery_token = 0
        app._frame_pacer = Mock()
        app._window_idle = Mock()
        app._window_idle.pause.return_value = True
        app.log = Mock()
        app.gpu = SimpleNamespace(
            read=Mock(return_value={"core": 900}),
            retry_clock_capabilities=Mock(return_value=False),
        )
        return app

    def test_default_off_minimize_pauses_ordinary_telemetry(self):
        app = self.make_app()
        # Exercise the same false default used before any toggle interaction.
        app.update_display_pause(True)
        self.assertTrue(app._monitoring_paused)
        app.poll_loop()
        app.gpu.read.assert_not_called()
        app.gpu.retry_clock_capabilities.assert_not_called()
        self.assertIsNone(app._snap)
        app._poll_wake.wait.assert_called_once_with(1.0)

    def test_always_refresh_minimized_keeps_ordinary_telemetry_running(self):
        app = self.make_app()
        app.set_always_refresh(None, True)
        app.update_display_pause(True)
        self.assertFalse(app._monitoring_paused)
        app.poll_loop()
        app.gpu.read.assert_called_once_with()
        self.assertEqual(app._snap, {"core": 900})
        app.gpu.retry_clock_capabilities.assert_called_once_with()
        app._frame_pacer.note_activity.assert_called_once_with()

    def test_toggle_on_resumes_ordinary_minimize_and_toggle_off_pauses_it(self):
        app = self.make_app()
        app.update_display_pause(True)
        paused_epoch = app._monitoring_epoch
        app.set_always_refresh(None, True)
        self.assertFalse(app._monitoring_paused)
        self.assertGreater(app._monitoring_epoch, paused_epoch)
        self.assertTrue(app._poll_wake.set.called)
        self.assertTrue(app._snap_err, "resume must await fresh telemetry")
        app.set_always_refresh(None, False)
        self.assertTrue(app._monitoring_paused)
        self.assertFalse(app._manual_display_pause)

    def test_explicit_pause_overrides_enabled_always_refresh(self):
        app = self.make_app()
        app.set_always_refresh(None, True)
        app.pause_display()
        self.assertTrue(app._always_refresh)
        self.assertTrue(app._manual_display_pause)
        self.assertTrue(app._window_minimized)
        self.assertTrue(app._monitoring_paused)
        app.update_display_pause(True)
        app.poll_loop()
        app.gpu.read.assert_not_called()

    def test_turning_on_always_refresh_cannot_cancel_explicit_pause(self):
        app = self.make_app()
        app.pause_display()
        paused_epoch = app._monitoring_epoch
        app.set_always_refresh(None, True)
        self.assertTrue(app._always_refresh)
        self.assertTrue(app._manual_display_pause)
        self.assertTrue(app._monitoring_paused)
        self.assertEqual(app._monitoring_epoch, paused_epoch)
        app.update_display_pause(True)
        self.assertTrue(app._monitoring_paused)

    def test_restore_clears_manual_override_and_preserves_refresh_preference(self):
        app = self.make_app()
        app.set_always_refresh(None, True)
        app.pause_display()
        app.update_display_pause(False)
        self.assertFalse(app._manual_display_pause)
        self.assertFalse(app._window_minimized)
        self.assertFalse(app._monitoring_paused)
        self.assertTrue(app._always_refresh)
        app.update_display_pause(True)
        self.assertFalse(app._monitoring_paused)

    def test_failed_minimize_does_not_leave_a_manual_pause_override(self):
        for failure in (False, RuntimeError("minimize failed")):
            with self.subTest(failure=failure):
                app = self.make_app()
                app.set_always_refresh(None, True)
                if isinstance(failure, Exception):
                    app._window_idle.pause.side_effect = failure
                else:
                    app._window_idle.pause.return_value = failure
                app.pause_display()
                self.assertFalse(app._manual_display_pause)
                self.assertFalse(app._window_minimized)
                self.assertFalse(app._monitoring_paused)
                self.assertTrue(app._always_refresh)
                app.log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
