"""Explicit Undo trial cannot be confused with display-only capture."""

import json
from unittest.mock import Mock

import pytest

from druta import druta
from tests.test_lpwr_rows_ui_cli import _launch_stubs, _no_launch


@pytest.mark.parametrize("extra", [[], ["--startup-profile", "--recovery-result", "receipt"],
                                    ["--ui-capture-recovery", "other", "--recovery-result", "receipt"]])
def test_invalid_undo_combination_rejected_before_app(monkeypatch, tmp_path, extra):
    app, startup, _, _ = _no_launch(monkeypatch)
    assert druta.main(["--ui-capture-recovery-undo", str(tmp_path / "trial"), *extra]) == 2
    app.assert_not_called()
    startup.assert_not_called()


def test_initial_and_resume_keep_kind_and_recovery_result(monkeypatch, tmp_path):
    app, _, _, _ = _launch_stubs(monkeypatch)
    app.gpu.slot = Mock(return_value="slot")
    monkeypatch.setattr(druta.devicerecovery, "read_result", Mock(return_value={"ok": False}))
    root = tmp_path / "trial"
    assert druta.main(["--ui-capture-recovery-undo", str(root), "--recovery-result", "receipt"]) == 0
    assert app._ui_registry_trial_kind == "recovery-undo" and not app._ui_pstates_trial_resume
    root.mkdir()
    (root / "trial.json").write_text(json.dumps({"kind": "recovery-undo", "phase": "restoring", "receipt": "r"}))
    assert druta.main(["--ui-pstates-trial-resume", str(root), "--recovery-result", "success"]) == 0
    assert app._ui_registry_trial_kind == "recovery-undo" and app._ui_pstates_trial_resume
