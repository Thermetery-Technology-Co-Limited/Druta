"""Real panel Undo dispatch and helper continuation without hardware."""

import copy
import json
from dataclasses import dataclass
from unittest.mock import Mock

from druta import recovery_undo_ui_trial as trial
from druta.registry_panel import RegistryPanel
from tests.test_recovery_ui_capture import setup
from tests.test_ui_capture import run


@dataclass
class Target:
    slot: str = "test"


def fixture(monkeypatch):
    pending = {"path": "receipt", "state": "recovery-required", "target": {"slot": "test"},
               "parent": None, "id": "same", "before": {}, "after": {"x": 1}, "plan": {}}
    restored = dict(pending, state="restored", ok=True)
    state = {"active": pending, "receipt": pending}
    monkeypatch.setattr(trial.overrides, "bind_target", lambda gpu: Target())
    monkeypatch.setattr(trial.overrides, "Target", Target)
    monkeypatch.setattr(trial.overrides, "load_receipt", lambda path: copy.deepcopy(state["receipt"]))
    monkeypatch.setattr(trial.overrides, "active_receipt", lambda target: copy.deepcopy(state["active"]))
    return pending, restored, state


def prepare_app(pending):
    dpg, app, panel = setup(copy.deepcopy(pending))
    app._closing = False
    app._recovery_result["message"] = "failed\nDetails: receipt"
    dpg.values["log"] = "[!!] Device recovery: failed\nDetails: receipt"
    panel._launch = Mock(side_effect=lambda receipt, restore: setattr(app, "_closing", True))
    panel.restore = Mock(side_effect=lambda: RegistryPanel.restore(panel))
    return dpg, app, panel


def test_panel_undo_handoff_and_real_success_resume(monkeypatch, tmp_path):
    pending, restored, state = fixture(monkeypatch)
    dpg, app, panel = prepare_app(pending)
    root = tmp_path / "trial"
    capture = trial.RecoveryUndoUiTrial(dpg, app, root)
    run(capture, dpg)
    panel.restore.assert_called_once_with()
    panel._launch.assert_called_once_with(pending, True)
    assert app._registry_ui_trial == str(root)
    assert capture.manifest["state"] == "handoff"
    saved = json.loads((root / "trial.json").read_text())
    assert saved["phase"] == "restoring" and saved["receipt"] == "receipt"
    assert not (root / "trial-result.json").exists()
    state.update(active=None, receipt=restored)
    dpg, app, panel = setup(None)
    app._closing = False
    app._recovery_result.update(ok=True, message="restored\nDetails: receipt")
    dpg.values["log"] = "[ok] Device recovery: restored\nDetails: receipt"
    capture = trial.RecoveryUndoUiTrial(dpg, app, root, resume=True)
    run(capture, dpg)
    assert json.loads((root / "trial-result.json").read_text())["phase"] == "complete"
    assert app._registry_ui_trial is None
    panel.restore.assert_not_called()


def test_callback_noop_and_failed_resume_preserve_receipt(monkeypatch, tmp_path):
    pending, restored, state = fixture(monkeypatch)
    dpg, app, panel = prepare_app(pending)
    panel.restore = Mock()
    root = tmp_path / "trial"
    capture = trial.RecoveryUndoUiTrial(dpg, app, root)
    run(capture, dpg)
    failed = json.loads((root / "initial" / "failed-result.json").read_text())
    assert failed["phase"] == "attention" and failed["receipt"] == "receipt"
    assert state["active"] == pending and app._registry_ui_trial is None
    # A helper's failed result cannot qualify a restoration.
    saved = json.loads((root / "trial.json").read_text())
    saved.update(phase="restoring", error=None)
    (root / "trial.json").write_text(json.dumps(saved))
    state.update(active=None, receipt=restored)
    dpg, app, _ = setup(None)
    app._recovery_result["message"] = "failed\nDetails: receipt"
    dpg.values["log"] = "[!!] Device recovery: failed\nDetails: receipt"
    capture = trial.RecoveryUndoUiTrial(dpg, app, root, resume=True)
    assert capture.manifest["state"] == "failed"
    assert not (root / "trial-result.json").exists()


def test_different_receipt_details_refuses_before_undo(monkeypatch, tmp_path):
    pending, _, state = fixture(monkeypatch)
    dpg, app, panel = prepare_app(pending)
    app._recovery_result["message"] = "failure\nDetails: receipt-other"
    dpg.values["log"] = "[!!] Device recovery: failure\nDetails: receipt-other"
    capture = trial.RecoveryUndoUiTrial(dpg, app, tmp_path / "trial")
    assert capture.manifest["state"] == "failed"
    panel.restore.assert_not_called()
    assert state["active"] == pending
