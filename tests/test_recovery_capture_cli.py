"""Explicit recovery framebuffer mode validates before application startup."""

from unittest.mock import Mock

import pytest

from druta import druta
from tests.test_lpwr_rows_ui_cli import _launch_stubs, _no_launch


@pytest.mark.parametrize("extra", [[], ["--startup-profile", "--recovery-result", "receipt"],
                                    ["--ui-capture", "other", "--recovery-result", "receipt"],
                                    ["--ui-capture-pstates-trial", "other", "--recovery-result", "receipt"]])
def test_invalid_modes_fail_before_app(monkeypatch, tmp_path, extra):
    app_factory, startup_factory, _, gpu_factory = _no_launch(monkeypatch)
    assert druta.main(["--ui-capture-recovery", str(tmp_path / "new"), *extra]) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()
    gpu_factory.assert_not_called()


def test_existing_output_rejected_before_app(monkeypatch, tmp_path):
    app_factory, startup_factory, _, _ = _no_launch(monkeypatch)
    assert druta.main(["--ui-capture-recovery", str(tmp_path), "--recovery-result", "receipt"]) == 2
    app_factory.assert_not_called()
    startup_factory.assert_not_called()


def test_accepted_recovery_result_is_forwarded(monkeypatch, tmp_path):
    application, _, _, _ = _launch_stubs(monkeypatch)
    application.gpu.slot = Mock(return_value="slot")
    result = {"slot": "slot", "ok": False, "reboot_required": False, "message": "rolled back"}
    reader = Mock(return_value=result)
    monkeypatch.setattr(druta.devicerecovery, "read_result", reader)
    assert druta.main(["--ui-capture-recovery", str(tmp_path / "new"), "--recovery-result", "receipt"]) == 0
    reader.assert_called_once_with("receipt", "slot")
    assert application._ui_capture_recovery is True and application._recovery_result_valid is True
    assert application._recovery_result == result
