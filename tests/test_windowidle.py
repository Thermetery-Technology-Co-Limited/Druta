# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Window pause owns only its viewport and leaves restoration/close responsive."""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import windowidle


def message(kind, *, key=0, detail=0, hwnd=None):
    return SimpleNamespace(message=kind, wParam=key, lParam=detail, hwnd=hwnd)


class FakeWindows:
    def __init__(self):
        self.ui_thread = threading.get_ident()
        self.windows = {12: (123, self.ui_thread, "Druta test", "Druta test")}
        self.iconic = set()
        self.client_sizes = {12: (420, 240)}
        self.messages = queue.Queue()
        self.hotkeys = queue.Queue()
        self.events = []
        self.registration_error = None
        self.registered = False
        self.dispatched = []
        self.dispatched_items = []
        self.dispatch_hook = None

    def thread_id(self):
        return threading.get_ident()

    def process_id(self):
        return 123

    def thread_windows(self, thread_id):
        return list(self.windows)

    def identity(self, hwnd):
        return self.windows.get(hwnd, (0, 0, "", ""))

    def valid(self, hwnd):
        return hwnd in self.windows

    def minimized(self, hwnd):
        return hwnd in self.iconic

    def client_size(self, hwnd):
        return self.client_sizes.get(hwnd)

    def minimize(self, hwnd):
        self.events.append(("minimize", hwnd))
        self.iconic.add(hwnd)

    def restore(self, hwnd):
        self.events.append(("restore", hwnd))
        self.iconic.discard(hwnd)

    def peek(self):
        try:
            return self.messages.get_nowait()
        except queue.Empty:
            return None

    def ensure_queue(self):
        self.events.append(("queue", threading.get_ident()))

    def get_message(self):
        return self.hotkeys.get(timeout=3)

    def dispatch(self, item):
        self.dispatched.append(item.message)
        self.dispatched_items.append(item)
        if self.dispatch_hook:
            self.dispatch_hook(item)

    def post(self, thread_id, kind):
        (self.messages if thread_id == self.ui_thread else self.hotkeys).put(message(kind))

    def register_hotkey(self):
        if self.registration_error:
            raise self.registration_error
        self.events.append(("register", threading.get_ident()))
        self.registered = True

    def unregister_hotkey(self):
        self.events.append(("unregister", threading.get_ident()))
        self.registered = False

    def wait(self, timeout):
        self.events.append(("wait", timeout))


class WindowIdleTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeWindows()
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()

    def attach(self):
        controller = windowidle.WindowIdle.attach("Druta test", _native=self.native)
        self.controllers.append(controller)
        return controller

    def test_attach_matches_current_process_thread_class_and_title(self):
        self.native.windows.update({
            20: (999, self.native.ui_thread, "Druta test", "Druta test"),
            21: (123, 99, "Druta test", "Druta test"),
            22: (123, self.native.ui_thread, "Other class", "Druta test"),
            23: (123, self.native.ui_thread, "Druta test", "Other title"),
        })
        controller = self.attach()
        self.assertTrue(controller.pause())
        self.assertEqual(self.native.iconic, {12})

    def test_missing_or_ambiguous_viewport_does_not_register_a_hotkey(self):
        self.native.windows[20] = self.native.windows[12]
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            self.attach()
        self.native.windows.clear()
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            self.attach()
        self.assertFalse(self.native.events)

    def test_regular_minimize_and_taskbar_restore_need_no_hotkey(self):
        controller = self.attach()
        self.assertFalse(controller.poll())
        self.native.iconic.add(12)
        self.assertTrue(controller.poll())
        self.native.iconic.clear()
        self.assertFalse(controller.poll())
        self.assertFalse(controller.closed)

    def test_owned_input_changes_activity_immediately_without_rendering(self):
        controller = self.attach()
        self.assertEqual(controller.activity_serial, 0)
        for kind in (0x0100, 0x0102, 0x0200, 0x0201, 0x020A, 0x020E,
                     0x0006, 0x0007, 0x0008, 0x001C, 0x010F):
            before = controller.activity_serial
            self.native.messages.put(message(kind, hwnd=12))
            self.assertFalse(controller.poll())
            self.assertEqual(controller.activity_serial, before + 1)

    def test_background_noise_and_other_windows_do_not_extend_activity(self):
        controller = self.attach()
        for item in (message(0, hwnd=12), message(0x000F, hwnd=12),
                     message(windowidle._WM_TIMER, hwnd=12, key=2),
                     message(0x0200, hwnd=999), message(0x0100, hwnd=None),
                     message(0x0005, hwnd=12)):
            self.native.messages.put(item)
        self.assertFalse(controller.poll())
        self.assertEqual(controller.activity_serial, 0)
        self.assertEqual(len(self.native.dispatched), 6)

    def test_synchronous_size_and_minimize_transitions_are_observed(self):
        controller = self.attach()
        self.native.client_sizes[12] = (620, 400)
        self.assertFalse(controller.poll())
        self.assertEqual(controller.activity_serial, 1)
        self.assertFalse(controller.poll())
        self.assertEqual(controller.activity_serial, 1)
        self.native.iconic.add(12)
        self.assertTrue(controller.poll())
        self.assertEqual(controller.activity_serial, 2)
        self.native.iconic.clear()
        self.assertFalse(controller.poll())
        self.assertEqual(controller.activity_serial, 3)

    def test_unknown_size_preserves_last_valid_state_until_recovery(self):
        controller = self.attach()
        self.native.client_sizes[12] = None
        self.assertFalse(controller.poll())
        self.assertEqual(controller.activity_serial, 0)
        self.native.client_sizes[12] = (420, 240)
        self.assertFalse(controller.poll())
        self.assertEqual(controller.activity_serial, 0)
        self.native.client_sizes[12] = (620, 400)
        self.assertFalse(controller.poll())
        self.assertEqual(controller.activity_serial, 1)

    def test_foreign_reused_handle_cannot_supply_activity(self):
        controller = self.attach()
        self.native.windows[12] = (999, self.native.ui_thread, "Druta test", "Druta test")
        self.native.messages.put(message(0x0200, hwnd=12))
        self.assertFalse(controller.poll())
        self.assertTrue(controller.closed)
        self.assertEqual(controller.activity_serial, 0)
        self.assertEqual(self.native.dispatched, [])

    def test_hotkey_restores_minimized_window_once_and_does_not_toggle(self):
        controller = self.attach()
        controller.pause()
        hotkey = message(windowidle._WM_HOTKEY, key=windowidle._HOTKEY_ID,
                         detail=(windowidle._HOTKEY_VK << 16) | windowidle._HOTKEY_MODIFIERS)
        self.native.hotkeys.put(hotkey)
        self.assertTrue(controller._hotkey.resume.wait(1))
        self.assertFalse(controller.poll())
        self.native.hotkeys.put(hotkey)
        self.assertTrue(controller._hotkey.resume.wait(1))
        self.assertFalse(controller.poll())
        self.assertEqual(self.native.events.count(("restore", 12)), 1)

    def test_registration_conflict_keeps_minimize_and_taskbar_restore(self):
        self.native.registration_error = OSError("hotkey already registered")
        controller = self.attach()
        self.assertFalse(controller.hotkey_available)
        self.assertIn("taskbar", controller.hotkey_error)
        self.assertTrue(controller.pause())
        self.assertTrue(controller.poll())
        self.native.iconic.clear()
        self.assertFalse(controller.poll())

    def test_pumps_close_without_a_render_frame(self):
        controller = self.attach()
        controller.pause()
        self.native.messages.put(message(0x0010))  # WM_CLOSE

        def close_window(item):
            self.assertEqual(item.message, 0x0010)
            self.native.windows.pop(12)
            self.native.messages.put(message(windowidle._WM_QUIT))

        self.native.dispatch_hook = close_window
        self.assertFalse(controller.poll())
        self.assertTrue(controller.closed)
        self.assertEqual(self.native.dispatched, [0x0010])

    def test_quit_is_not_dispatched_and_marks_controller_closed(self):
        controller = self.attach()
        self.native.messages.put(message(windowidle._WM_QUIT))
        self.assertFalse(controller.poll())
        self.assertTrue(controller.closed)
        self.assertEqual(self.native.dispatched, [])

    def test_minimized_drops_only_its_known_callback_free_resize_timer(self):
        controller = self.attach()
        controller.pause()
        discarded = message(windowidle._WM_TIMER, hwnd=12, key=1)
        preserved = [message(windowidle._WM_TIMER, hwnd=13, key=1),
                     message(windowidle._WM_TIMER, hwnd=12, key=2),
                     message(windowidle._WM_TIMER, hwnd=12, key=1, detail=12345),
                     message(0x000F, hwnd=12)]  # WM_PAINT must still be dispatched.
        for item in [discarded] + preserved:
            self.native.messages.put(item)
        self.assertTrue(controller.poll())
        self.assertEqual(self.native.dispatched_items, preserved)

    def test_visible_viewport_keeps_its_resize_timer(self):
        controller = self.attach()
        timer = message(windowidle._WM_TIMER, hwnd=12, key=1)
        self.native.messages.put(timer)
        self.assertFalse(controller.poll())
        self.assertEqual(self.native.dispatched_items, [timer])

    def test_title_change_preserves_target_but_owner_change_rejects_it(self):
        controller = self.attach()
        self.native.windows[12] = (123, self.native.ui_thread, "Druta test", "New card title")
        self.assertTrue(controller.pause())
        self.native.windows[12] = (999, self.native.ui_thread, "Druta test", "New card title")
        controller._hotkey.resume.set()
        self.assertFalse(controller.poll())
        self.assertTrue(controller.closed)
        self.assertNotIn(("restore", 12), self.native.events)

    def test_pause_refuses_stale_target(self):
        controller = self.attach()
        self.native.windows.clear()
        with self.assertRaisesRegex(RuntimeError, "no longer available"):
            controller.pause()
        self.assertTrue(controller.closed)
        self.assertNotIn(("minimize", 12), self.native.events)

    def test_message_wait_is_bounded_and_skipped_for_pending_restore(self):
        controller = self.attach()
        controller.wait(0.1)
        self.assertIn(("wait", 0.1), self.native.events)
        controller._hotkey.resume.set()
        controller.wait(0.2)
        self.assertNotIn(("wait", 0.2), self.native.events)

    def test_native_wait_rounds_positive_deadlines_up_and_preserves_nonblocking_zero(self):
        native = windowidle._Windows.__new__(windowidle._Windows)
        wait = Mock(return_value=0)
        native.user = SimpleNamespace(MsgWaitForMultipleObjectsEx=wait)
        for seconds, milliseconds in ((0, 0), (-0.1, 0), (0.000001, 1),
                                      (0.0009, 1), (0.001, 1), (0.0011, 2), (10, 1000)):
            with self.subTest(seconds=seconds):
                native.wait(seconds)
                self.assertEqual(wait.call_args.args[2], milliseconds)

    def test_wrong_thread_cannot_operate_viewport(self):
        controller = self.attach()
        errors = []

        def wrong_thread():
            try:
                controller.pause()
            except RuntimeError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=wrong_thread)
        worker.start()
        worker.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIn("UI thread", errors[0])
        self.assertNotIn(("minimize", 12), self.native.events)

    def test_close_unregisters_on_registering_thread_and_is_idempotent(self):
        controller = self.attach()
        self.assertTrue(controller.hotkey_available)
        controller.close()
        controller.close()
        registrations = [event for event in self.native.events if event[0] == "register"]
        removals = [event for event in self.native.events if event[0] == "unregister"]
        self.assertEqual(len(removals), 1)
        self.assertEqual(registrations[0][1], removals[0][1])
        self.assertNotEqual(registrations[0][1], self.native.ui_thread)
        self.assertFalse(self.native.registered)
        self.assertFalse(controller._hotkey.thread.is_alive())
        self.assertFalse(controller.hotkey_available)


if __name__ == "__main__":
    unittest.main()
