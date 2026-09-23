# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from druta import devicerecovery, devicereset, druta
from tests.test_arch_ui_regressions import FakeUiTest


@dataclass
class Result:
    ok: bool = True
    reboot_required: bool = False
    message: str = "Device restarted"
    returncode: int = 0


class RecoveryProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.target = SimpleNamespace(slot="0000:03:00.0", instance_id="PCI\\EXACT", name="GPU")
        self.backend = SimpleNamespace(resolve_target=Mock(return_value=self.target),
                                       restart_target=Mock(return_value=Result()))
        # Real JSON receipts and command orchestration; only hardware/process
        # boundaries are substituted so the failure branches cannot reset a GPU.
        import druta as package
        for p in (
            patch.dict("sys.modules", {"druta.devicereset": self.backend}),
            patch.object(package, "devicereset", self.backend, create=True),
            patch.object(devicerecovery.startup, "state_dir", return_value=self.state),
        ):
            p.start()
            self.addCleanup(p.stop)

    def receipt(self):
        files = list((self.state / "device-recovery").glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_waits_for_parent_before_restart_then_reopens_without_profile(self):
        order = []
        self.backend.resolve_target.side_effect = lambda _: (order.append("resolve"), self.target)[1]
        self.backend.restart_target.side_effect = lambda _: (order.append("restart"), Result())[1]
        with patch.object(devicerecovery, "wait_for_exit", side_effect=lambda _: order.append("exit")), \
                patch.object(devicerecovery.subprocess, "Popen") as spawn:
            result = devicerecovery.run(self.target.slot, parent_pid=123,
                                       expected_instance=self.target.instance_id)
        self.assertEqual(result, 0)
        self.assertEqual(order, ["exit", "resolve", "restart"])
        command = spawn.call_args.args[0]
        self.assertIn("--recovery-result", command)
        self.assertIn("--gpu", command)
        self.assertNotIn("--startup-profile", command)
        self.assertTrue(self.receipt()["ok"])

    def test_parent_timeout_never_touches_device_or_opens_second_window(self):
        with patch.object(devicerecovery, "wait_for_exit", side_effect=TimeoutError("still running")), \
                patch.object(devicerecovery.subprocess, "Popen") as spawn:
            self.assertEqual(devicerecovery.run(self.target.slot, parent_pid=123), 1)
        self.backend.resolve_target.assert_not_called()
        self.backend.restart_target.assert_not_called()
        spawn.assert_not_called()
        self.assertIn("still running", self.receipt()["message"])

    def test_changed_instance_refuses_restart_and_reopens_with_failure(self):
        with patch.object(devicerecovery.subprocess, "Popen") as spawn:
            self.assertEqual(devicerecovery.run(self.target.slot, expected_instance="PCI\\OTHER"), 1)
        self.backend.restart_target.assert_not_called()
        spawn.assert_called_once()
        self.assertFalse(self.receipt()["ok"])

    def test_reboot_required_is_recorded_without_reboot_or_profile(self):
        self.backend.restart_target.return_value = Result(False, True, "Reboot required", 3010)
        with patch.object(devicerecovery.subprocess, "Popen") as spawn:
            self.assertEqual(devicerecovery.run(self.target.slot), 1)
        self.assertTrue(self.receipt()["reboot_required"])
        self.assertNotIn("--startup-profile", spawn.call_args.args[0])

    def test_helper_launch_binds_current_target_and_parent(self):
        with patch.object(devicerecovery.subprocess, "Popen") as spawn:
            devicerecovery.launch_helper(self.target)
        command = spawn.call_args.args[0]
        self.assertIn(self.target.instance_id, command)
        self.assertIn("--parent-pid", command)
        self.assertIn("--restart-gpu", command)
        self.assertNotIn("--startup-profile", command)

    def test_recovery_receipt_rejects_wrong_slot_or_outside_directory(self):
        with patch.object(devicerecovery.subprocess, "Popen"):
            devicerecovery.run(self.target.slot)
        path = next((self.state / "device-recovery").glob("*.json"))
        self.assertTrue(devicerecovery.read_result(path, self.target.slot)["ok"])
        with self.assertRaises(ValueError):
            devicerecovery.read_result(path, "0000:04:00.0")
        elsewhere = self.state / "other.json"
        elsewhere.write_text(path.read_text())
        with self.assertRaises(ValueError):
            devicerecovery.read_result(elsewhere, self.target.slot)


class DeviceRestartUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.gpu = SimpleNamespace(slot=lambda: "0000:03:00.0")
        self.target = SimpleNamespace(slot="0000:03:00.0", instance_id="PCI\\EXACT", name="GPU")
        self.app._stop = Mock()
        self.app._closing = False

    def test_action_launches_helper_immediately_before_closing_without_applying_profile(self):
        with patch.object(druta, "is_admin", return_value=True), \
                patch.object(devicereset, "resolve_target", return_value=self.target) as resolve, \
                patch.object(devicerecovery, "launch_helper") as helper:
            self.app.open_device_restart()
        resolve.assert_called_once_with(self.target.slot)
        helper.assert_called_once_with(self.target)
        self.ui.stop_dearpygui.assert_called_once()
        self.assertTrue(self.app._closing)
        self.app.autosave_before.assert_not_called()

    def test_failed_helper_launch_keeps_window_open(self):
        with patch.object(druta, "is_admin", return_value=True), \
                patch.object(devicereset, "resolve_target", return_value=self.target), \
                patch.object(devicerecovery, "launch_helper", side_effect=OSError("launch failed")):
            self.app.open_device_restart()
        self.ui.stop_dearpygui.assert_not_called()
        self.app._stop.set.assert_not_called()
        self.assertFalse(self.app._closing)

    def test_active_writer_missing_admin_or_closing_refuses_restart(self):
        with patch.object(druta, "is_admin", return_value=True), \
                patch.object(devicereset, "resolve_target") as resolve, \
                patch.object(devicerecovery, "launch_helper") as helper:
            self.app._i2c_busy = True
            self.app.open_device_restart()
            self.app._i2c_busy = False
            self.app._profile_pending = {"profile": 1}
            self.app.open_device_restart()
            self.app._profile_pending = None
            self.app._profile_applying = True
            self.app.open_device_restart()
            self.app._profile_applying = False
            self.app._closing = True
            self.app.open_device_restart()
            helper.assert_not_called()
            resolve.assert_not_called()
        self.app._closing = False
        with patch.object(druta, "is_admin", return_value=False), \
                patch.object(devicereset, "resolve_target") as resolve, \
                patch.object(devicerecovery, "launch_helper") as helper:
            self.app.open_device_restart()
            helper.assert_not_called()
            resolve.assert_not_called()
        self.ui.stop_dearpygui.assert_not_called()

    def test_resolve_failure_never_starts_helper_and_no_confirm_entrypoint_remains(self):
        with patch.object(druta, "is_admin", return_value=True), \
                patch.object(devicereset, "resolve_target", side_effect=RuntimeError("missing target")), \
                patch.object(devicerecovery, "launch_helper") as helper:
            self.app.open_device_restart()
        helper.assert_not_called()
        self.assertFalse(hasattr(druta.Druta, "confirm_device_restart"))

    def test_cli_recovery_dispatches_before_gpu_or_startup_initialization(self):
        with patch.object(devicerecovery, "cli", return_value=0) as cli, \
                patch.object(druta, "Druta") as app, \
                patch.object(druta.startup, "Startup") as startup:
            self.assertEqual(druta.main(["--restart-gpu", self.target.slot]), 0)
        cli.assert_called_once_with([self.target.slot], druta._tell)
        app.assert_not_called()
        startup.assert_not_called()

    def test_recovery_relaunch_suppresses_explicit_startup_profile_flag(self):
        manager = Mock()
        manager.begin.return_value = None
        app = Mock()
        app.gpu.slot.return_value = self.target.slot
        app.gpu.available.return_value = True
        app.shutdown_is_clean.return_value = True
        with patch.object(druta.startup, "Startup", return_value=manager), \
                patch.object(druta, "Druta", return_value=app), \
                patch.object(devicerecovery, "read_result", return_value={
                    "ok": True, "reboot_required": False, "message": "restarted"}):
            self.assertEqual(druta.main(["--gpu", self.target.slot,
                                         "--startup-profile", "--recovery-result", "receipt.json"]), 0)
        manager.begin.assert_called_once_with(automatic=False)
        self.assertIsNone(app._startup_request)
        app.run.assert_called_once()
