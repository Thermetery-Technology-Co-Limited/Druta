# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in real DPG/Win32 pause checks with a synthetic, GPU-backend-free window.

Run with DRUTA_TEST_WINDOWIDLE_RENDER=1. This opens only its own test viewport
and temporarily registers the restore shortcut. It never imports a tuning
backend. WM_HOTKEY notification injection tests routing, not physical keys.
"""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENABLED = (sys.platform == "win32" and os.environ.get("DRUTA_TEST_WINDOWIDLE_RENDER") == "1"
           and importlib.util.find_spec("dearpygui") is not None)


@unittest.skipUnless(ENABLED, "set DRUTA_TEST_WINDOWIDLE_RENDER=1 with Windows DearPyGui installed")
class WindowIdleRenderTests(unittest.TestCase):
    def test_native_minimize_restore_close_and_hotkey_lifecycle_without_frames(self):
        script = textwrap.dedent(
            """
            import ctypes
            from collections import Counter
            import json
            import os
            import time
            import uuid
            import dearpygui.dearpygui as dpg
            from druta import windowidle

            title = 'Druta idle synthetic ' + uuid.uuid4().hex
            controller = None
            report = {'physical_hotkey_tested': False}
            dpg.create_context()
            try:
                dpg.configure_app(manual_callback_management=True)
                dpg.create_viewport(title=title, width=420, height=240)
                with dpg.window(tag='root'):
                    dpg.add_text('Synthetic rendering pause test: no GPU tuning backend')
                dpg.setup_dearpygui()
                dpg.show_viewport()
                dpg.set_primary_window('root', True)
                for _ in range(4):
                    dpg.render_dearpygui_frame()
                report['dpg_version'] = dpg.get_dearpygui_version()
                controller = windowidle.WindowIdle.attach(title)
                if not controller.hotkey_available:
                    raise AssertionError(controller.hotkey_error)
                hwnd = controller._hwnd
                native = controller._native
                user = native.user
                message_counts = Counter()
                native_dispatch = native.dispatch
                def counted_dispatch(item):
                    message_counts[hex(item.message)] += 1
                    native_dispatch(item)
                native.dispatch = counted_dispatch
                user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                             ctypes.c_size_t, ctypes.c_ssize_t]
                user.PostMessageW.restype = ctypes.c_int

                def post_window(kind, wparam=0, lparam=0):
                    if not user.PostMessageW(hwnd, kind, wparam, lparam):
                        raise ctypes.WinError(ctypes.get_last_error())

                def pump_until(predicate, seconds=2):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        controller.poll()
                        if predicate():
                            return
                        controller.wait(0.01)
                    raise AssertionError('Native window state did not converge')

                if not controller.pause() or not user.IsIconic(hwnd):
                    raise AssertionError('Window did not become minimized')
                before = dpg.get_frame_count()
                started = time.monotonic()
                cpu_started = time.process_time()
                pump_count = 0
                wait_seconds = 0.0
                while time.monotonic() - started < 0.6:
                    if not controller.poll():
                        raise AssertionError('Minimized viewport unexpectedly resumed')
                    wait_started = time.monotonic()
                    controller.wait(0.05)
                    wait_seconds += time.monotonic() - wait_started
                    pump_count += 1
                after = dpg.get_frame_count()
                report['minimized'] = {'before': before, 'after': after,
                                       'seconds': time.monotonic()-started, 'pumps': pump_count,
                                       'wait_seconds': wait_seconds,
                                       'process_cpu_seconds': time.process_time()-cpu_started,
                                       'dispatched_messages': dict(message_counts)}
                if after != before:
                    raise AssertionError(('Minimized pump rendered frames', before, after))

                # DPG owns timer 1 for modal move/resize. Observe the treatment of
                # a queued timer after minimization separately from the idle loop.
                post_window(0x0113, 1)
                timer_before = dpg.get_frame_count()
                controller.poll()
                report['queued_resize_timer_frames'] = dpg.get_frame_count() - timer_before
                if report['queued_resize_timer_frames']:
                    raise AssertionError('Queued resize timer rendered while minimized')
                other_timers_before = message_counts['0x113']
                post_window(0x0113, 2)
                controller.poll()
                report['other_timer_dispatched'] = message_counts['0x113'] == other_timers_before + 1
                if not report['other_timer_dispatched']:
                    raise AssertionError('Unrelated timer was swallowed')

                details = (windowidle._HOTKEY_VK << 16) | windowidle._HOTKEY_MODIFIERS
                if not user.PostThreadMessageW(controller._hotkey.thread_id,
                        windowidle._WM_HOTKEY, windowidle._HOTKEY_ID, details):
                    raise ctypes.WinError(ctypes.get_last_error())
                pump_until(lambda: not user.IsIconic(hwnd))
                if controller._hwnd != hwnd or not user.IsWindow(hwnd):
                    raise AssertionError('Hotkey restore replaced the HWND')
                restored_before = dpg.get_frame_count()
                dpg.render_dearpygui_frame()
                if dpg.get_frame_count() <= restored_before:
                    raise AssertionError('Restored viewport did not render')
                report['hotkey_notification_restored_same_hwnd'] = True
                # The same DPG resize timer must still render while visible;
                # otherwise the new filter would break normal window dragging.
                visible_timer_before = dpg.get_frame_count()
                post_window(0x0113, 1)
                controller.poll()
                report['visible_resize_timer_frames'] = dpg.get_frame_count() - visible_timer_before
                if report['visible_resize_timer_frames'] != 1:
                    raise AssertionError('Visible resize timer was not dispatched normally')

                if not controller.pause():
                    raise AssertionError('Second minimization failed')
                controller.poll()
                # SW_RESTORE is the native restoration mechanism used by the
                # taskbar, exercised directly without touching the user's shell.
                user.ShowWindow(hwnd, 9)
                pump_until(lambda: not user.IsIconic(hwnd))
                if controller._hwnd != hwnd:
                    raise AssertionError('Native restore replaced the HWND')
                report['native_restore_same_hwnd'] = True
                dpg.render_dearpygui_frame()

                # Release and reacquire on a new hotkey thread. A leaked global
                # registration would make this fail even though thread IDs differ.
                controller.close()
                if controller._hotkey.thread.is_alive():
                    raise AssertionError('First hotkey thread still alive')
                controller = windowidle.WindowIdle.attach(title)
                if not controller.hotkey_available:
                    raise AssertionError(('Could not reacquire restore key', controller.hotkey_error))
                report['hotkey_reacquired'] = True

                controller.pause()
                close_before = dpg.get_frame_count()
                post_window(0x0010)  # WM_CLOSE
                pump_until(lambda: controller.closed)
                report['closed_without_frames'] = dpg.get_frame_count() == close_before
                if not report['closed_without_frames'] or user.IsWindow(hwnd):
                    raise AssertionError('WM_CLOSE did not close without a render')
                if any(name in __import__('sys').modules for name in
                       ('druta.nvbackend', 'druta.native', 'druta.gpu_backend')):
                    raise AssertionError('Synthetic test loaded a GPU tuning backend')
                report['gpu_tuning_backend_loaded'] = False
            finally:
                if controller is not None:
                    controller.close()
                dpg.destroy_context()
            report['hotkey_thread_stopped'] = not controller._hotkey.thread.is_alive()
            print(json.dumps(report))
            """
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=environment,
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        print(json.dumps(report))
        self.assertEqual(report["minimized"]["before"], report["minimized"]["after"])
        self.assertGreaterEqual(report["minimized"]["seconds"], 0.6)
        self.assertEqual(report["queued_resize_timer_frames"], 0)
        self.assertTrue(report["other_timer_dispatched"])
        self.assertEqual(report["visible_resize_timer_frames"], 1)
        self.assertTrue(report["hotkey_notification_restored_same_hwnd"])
        self.assertTrue(report["native_restore_same_hwnd"])
        self.assertTrue(report["hotkey_reacquired"])
        self.assertTrue(report["closed_without_frames"])
        self.assertTrue(report["hotkey_thread_stopped"])
        self.assertFalse(report["physical_hotkey_tested"])
        self.assertFalse(report["gpu_tuning_backend_loaded"])


if __name__ == "__main__":
    unittest.main()
