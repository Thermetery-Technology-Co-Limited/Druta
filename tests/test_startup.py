# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Crash recovery and task construction without signing out or touching GPUs."""
import copy
import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from druta import startup


class StartupRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.boot = {"id": "boot-1", "clean": True, "reason": ""}
        self.registrar = Mock()
        self.profile = {"schema": 2, "device": {"uuid": "GPU-A"}, "vf_deltas": {"0": 0}}

    def manager(self, automatic=False):
        manager = startup.Startup(self.directory, lambda: dict(self.boot), self.registrar)
        self.addCleanup(manager.close)
        request = manager.begin(automatic)
        return manager, request

    def enable(self):
        manager, request = self.manager()
        self.assertIsNone(request)
        manager.enable("my tune", self.profile)
        manager.close(clean=True)
        return manager

    def test_opt_in_persists_a_fixed_copy_and_registers_only_when_selected(self):
        manager, _ = self.manager()
        self.registrar.assert_not_called()
        manager.enable("my tune", self.profile)
        self.registrar.assert_called_once_with(True)
        self.profile["vf_deltas"]["0"] = 9999
        saved = startup.read_json(manager.config_path)
        self.assertEqual(saved["profile"]["vf_deltas"], {"0": 0})

    def test_normal_exit_and_clean_windows_boot_allow_one_attempt(self):
        self.enable()
        self.boot["id"] = "boot-2"
        manager, request = self.manager(True)
        self.assertEqual(request["name"], "my tune")
        self.assertTrue(startup.read_json(manager.session_path)["running"])
        self.assertEqual(startup.read_json(manager.session_path)["attempted_boot"], "boot-2")
        manager.close(clean=True)
        again, request = self.manager(True)
        self.assertIsNone(request)
        self.assertIn("already attempted", again.reason)

    def test_manual_launch_does_not_apply_sign_in_profile(self):
        self.enable()
        manager, request = self.manager(False)
        self.assertIsNone(request)
        self.assertNotIn("attempted_boot", manager.session)

    def test_windows_power_loss_blocks_even_if_druta_had_closed_normally(self):
        self.enable()
        self.boot.update(id="boot-2", clean=False, reason="Windows abnormal shutdown")
        manager, request = self.manager(True)
        self.assertIsNone(request)
        self.assertEqual(manager.reason, "Windows abnormal shutdown")

    def test_crashed_druta_blocks_whole_next_boot_and_cannot_retry_by_reopening(self):
        self.enable()
        crashed, _ = self.manager(True)
        crashed.close(clean=False)
        self.boot["id"] = "boot-2"
        recovered, request = self.manager(True)
        self.assertIsNone(request)
        self.assertIn("did not exit normally", recovered.reason)
        recovered.close(clean=True)
        retry, request = self.manager(True)
        self.assertIsNone(request)
        self.assertEqual(retry.session["blocked_boot"], "boot-2")
        retry.close(clean=True)
        self.boot["id"] = "boot-3"
        normal, request = self.manager(True)
        self.assertIsNotNone(request)
        normal.close(clean=True)

    def test_unknown_windows_boot_status_fails_closed(self):
        self.enable()
        self.boot.update(id=None, clean=False, reason="log unavailable")
        manager, request = self.manager(True)
        self.assertIsNone(request)
        self.assertEqual(manager.reason, "log unavailable")

    def test_missing_or_corrupt_state_does_not_load(self):
        manager = self.enable()
        manager.session_path.unlink()
        missing, request = self.manager(True)
        self.assertIsNone(request)
        missing.close()
        manager.session_path.write_text("{truncated")
        corrupt, request = self.manager(True)
        self.assertIsNone(request)
        self.assertTrue(corrupt.failed)
        corrupt.close(clean=True)
        self.assertEqual(manager.session_path.read_text(), "{truncated")

    def test_attempt_marker_failure_prevents_application(self):
        self.enable()
        with patch("druta.startup.atomic_json", side_effect=OSError("disk full")):
            manager, request = self.manager(True)
        self.assertIsNone(request)
        self.assertTrue(manager.failed)

    def test_second_window_cannot_clear_first_windows_crash_marker(self):
        self.enable()
        first, _ = self.manager(True)
        second, request = self.manager(True)
        self.assertIsNone(request)
        self.assertIsNone(second.lock)
        second.close(clean=True)
        self.assertTrue(startup.read_json(first.session_path)["running"])
        with self.assertRaises(ValueError):
            second.enable("other", self.profile)

    def test_task_registration_failure_restores_previous_configuration(self):
        manager = self.enable()
        current, _ = self.manager()
        before = startup.read_json(manager.config_path)
        self.registrar.side_effect = OSError("access denied")
        with self.assertRaises(OSError):
            current.enable("new tune", dict(self.profile, xoc=True))
        self.assertEqual(startup.read_json(manager.config_path), before)

    def test_disabling_still_prevents_load_when_task_deletion_fails(self):
        self.enable()
        manager, _ = self.manager()
        self.registrar.side_effect = OSError("task locked")
        with self.assertRaises(OSError):
            manager.disable()
        manager.close(clean=True)
        _, request = self.manager(True)
        self.assertIsNone(request)

    def test_cancelled_shutdown_does_not_mark_clean(self):
        self.enable()
        manager, _ = self.manager()
        manager.end_session(False, 0)
        self.assertFalse(manager.ending)
        self.assertTrue(startup.read_json(manager.session_path)["running"])

    def test_confirmed_windows_shutdown_marks_idle_app_clean(self):
        self.enable()
        manager, _ = self.manager()
        manager.end_session(True, 0)
        self.assertTrue(manager.ending)
        self.assertFalse(startup.read_json(manager.session_path)["running"])

    def test_forced_shutdown_or_shutdown_during_i2c_stays_dirty(self):
        self.enable()
        for flags, idle in ((0x40000000, True), (0, False)):
            with self.subTest(flags=flags, idle=idle):
                manager, _ = self.manager()
                manager.end_session(True, flags, idle)
                manager.close(clean=True)
                self.assertTrue(startup.read_json(manager.session_path)["running"])

    def test_explicit_load_failure_blocks_retries_for_boot(self):
        self.enable()
        manager, _ = self.manager(True)
        manager.block("driver rejected voltage")
        manager.close(clean=True)
        another, request = self.manager(True)
        self.assertIsNone(request)
        self.assertEqual(another.reason, "driver rejected voltage")


def boot_event(provider, event_id, record, stamp, **fields):
    event = ET.Element("Event", xmlns=startup.NS[1:-1])
    system = ET.SubElement(event, "System")
    ET.SubElement(system, "Provider", Name=provider)
    ET.SubElement(system, "EventID").text = str(event_id)
    ET.SubElement(system, "EventRecordID").text = str(record)
    ET.SubElement(system, "TimeCreated", SystemTime=stamp)
    data = ET.SubElement(event, "EventData")
    for key, value in fields.items():
        ET.SubElement(data, "Data", Name=key).text = value
    return ET.tostring(event, encoding="unicode")


class WindowsIntegration(unittest.TestCase):
    def test_boot_xml_requires_explicit_positive_evidence_from_current_boot(self):
        start = boot_event("Microsoft-Windows-Kernel-General", 12, 100, "2026-09-06T12:00:00.0000000Z")
        def status(**fields):
            return boot_event("Microsoft-Windows-Kernel-Boot", 20, 103,
                              "2026-09-06T12:00:00.0000091Z", **fields)
        self.assertTrue(startup.parse_boot(start, status(LastShutdownGood="true", LastBootGood="true"))["clean"])
        for fields in ({}, {"LastShutdownGood": "false", "LastBootGood": "true"},
                       {"LastShutdownGood": "true", "LastBootGood": "false"}):
            self.assertFalse(startup.parse_boot(start, status(**fields))["clean"])
        stale = boot_event("Microsoft-Windows-Kernel-Boot", 20, 50,
                           "2026-09-05T12:00:00Z", LastShutdownGood="true", LastBootGood="true")
        with self.assertRaises(ValueError):
            startup.parse_boot(start, stale)

    def test_task_is_per_user_interactive_elevated_with_no_password_or_retry(self):
        command = [r"C:\Druta & tuning\Druta.exe", "--startup-profile"]
        xml = startup.task_xml(command, "S-1-5-21-123")
        root = ET.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        def get(path):
            return root.findtext(path, namespaces=ns)
        self.assertEqual(get("t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(get("t:Principals/t:Principal/t:RunLevel"), "HighestAvailable")
        self.assertEqual(get("t:Triggers/t:LogonTrigger/t:UserId"), "S-1-5-21-123")
        self.assertEqual(get("t:Actions/t:Exec/t:Command"), command[0])
        self.assertEqual(get("t:Actions/t:Exec/t:Arguments"), "--startup-profile")
        self.assertNotIn("Password", xml)
        self.assertNotIn("RestartOnFailure", xml)

    def test_frozen_launch_uses_current_executable(self):
        with patch("druta.startup.sys.frozen", True, create=True), \
                patch("druta.startup.sys.executable", r"C:\portable Druta\Druta.exe"):
            self.assertEqual(startup.launch_command(), [r"C:\portable Druta\Druta.exe", "--startup-profile"])

    def test_real_process_owned_window_receives_shutdown_and_cancellation(self):
        # Deliver only to our own test window; do not broadcast, sign out, or reboot.
        callback = Mock()
        listener = startup.ShutdownListener(callback)
        listener.start()
        try:
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            user.SendMessageW.restype = ctypes.c_ssize_t
            self.assertEqual(user.SendMessageW(listener.hwnd, 0x11, 0, 0), 1)
            callback.assert_not_called()
            user.SendMessageW(listener.hwnd, 0x16, 0, 0)
            callback.assert_called_once_with(False, 0)
            user.SendMessageW(listener.hwnd, 0x16, 1, 0x80000000)
            callback.assert_called_with(True, 0x80000000)
        finally:
            listener.stop()
        self.assertFalse(listener.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
