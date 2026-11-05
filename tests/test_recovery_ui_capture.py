"""The recovery capture reads real widgets and never dispatches writers."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta.recovery_ui_capture import RecoveryUiCapture
from tests.test_ui_capture import FakeDpg, app, run


class RecoveryDpg(FakeDpg):
    def __init__(self):
        super().__init__()
        self.values.update({"registry_header": False, "reg_rel_live_header": False,
                            "reg_constants_live_header": False, "reg_rel_live_select": "",
                            "reg_constants_live_select": "", "reg_status": "Read fields",
                            "reg_last_recovery": "Last device recovery (historical): rollback completed",
                            "log": "[!!] Device recovery: rollback completed"})
        self.config["reg_restore_button"] = {"enabled": False}
        self.stop_dearpygui = Mock()

    def get_item_state(self, tag):
        if tag == "page_control":
            return {"pos": (8, 200)}
        return {"rect_min": (8, 800 if tag == "registry_header" else 1800)}


def setup(active):
    dpg, application = RecoveryDpg(), app()
    application._recovery_result_valid = True
    application._recovery_result = {"slot": "test", "message": "rollback completed", "ok": False,
                                    "reboot_required": False}
    panel = SimpleNamespace(active=active, rel_live_choices={}, constants_live_choices={},
                            prepare=Mock(), apply=Mock(), restore=Mock(),
                            apply_rel_live=Mock(), apply_constants_live=Mock())

    def recover():
        dpg.config["reg_restore_button"]["enabled"] = active is not None
        if active:
            dpg.values["reg_status"] = "Saved override transaction: " + active["state"]

    panel.recover = Mock(side_effect=recover)
    application._registry_panel = panel
    return dpg, application, panel


@pytest.mark.parametrize("active", [None, {"path": "saved-receipt", "state": "recovery-required"}])
def test_real_log_and_receipt_state_are_captured_without_writers(tmp_path, active):
    dpg, application, panel = setup(active)
    capture = RecoveryUiCapture(dpg, application, tmp_path / "capture")
    run(capture, dpg)
    assert capture.manifest["state"] == "complete"
    assert [x["stage"] for x in capture.manifest["captures"]] == ["recovery-panel", "recovery-log"]
    for view in capture.manifest["views"].values():
        assert view["undo_enabled"] is (active is not None)
        assert view["receipt"] == active
        assert view["log"] == "[!!] Device recovery: rollback completed"
        assert view["last_recovery"] == "Last device recovery (historical): rollback completed"
    assert capture.manifest["recovery_result"] == application._recovery_result
    assert capture.manifest["process_id"] == os.getpid()
    assert dpg.values["registry_header"] is False and dpg.values["tabs"] == "tab_monitor"
    assert dpg.scroll == 23
    for writer in (panel.prepare, panel.apply, panel.restore, panel.apply_rel_live, panel.apply_constants_live,
                   application.gpu.set_power_limit_mw):
        writer.assert_not_called()
    dpg.stop_dearpygui.assert_called_once()


def test_failed_receipt_discovery_and_interruption_restore_view(tmp_path):
    dpg, application, panel = setup(None)
    panel.recover.side_effect = lambda: dpg.set_value("reg_status", "Could not discover saved undo: unavailable")
    capture = RecoveryUiCapture(dpg, application, tmp_path / "failed")
    assert capture.manifest["state"] == "failed" and "Could not discover" in capture.manifest["errors"][0]
    assert dpg.values["registry_header"] is False and dpg.scroll == 23
    dpg, application, panel = setup(None)
    capture = RecoveryUiCapture(dpg, application, tmp_path / "interrupted")
    capture._finish(RuntimeError("interrupted"))
    assert capture.manifest["state"] == "failed" and dpg.scroll == 23
    assert dpg.values["registry_header"] is False
    panel.restore.assert_not_called()


def test_unaccepted_result_cannot_qualify(tmp_path):
    dpg, application, _ = setup(None)
    application._recovery_result_valid = False
    capture = RecoveryUiCapture(dpg, application, tmp_path / "bad-result")
    assert capture.manifest["state"] == "failed" and not capture.manifest["captures"]


def test_missing_actual_recovery_log_fails_without_synthesizing_text(tmp_path):
    dpg, application, _ = setup(None)
    dpg.values["log"] = "unrelated log"
    capture = RecoveryUiCapture(dpg, application, tmp_path / "missing-log")
    assert capture.manifest["state"] == "failed" and not capture.manifest["captures"]
    assert dpg.values["log"] == "unrelated log"
