"""Manual rail-request profiles and undo do not touch unrelated tuning paths."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from druta.druta import Druta


@pytest.fixture
def app():
    ui = Druta.__new__(Druta)
    ui.gpu = Mock()
    ui.rail = None
    ui._i2c_busy = False
    ui.guard = Mock(return_value=True)
    ui.log = Mock()
    ui.profile_failure = Mock()
    ui._finish_manual_voltage = Mock(side_effect=AssertionError("unrelated manual voltage"))
    ui.sync_risk_ui = Mock(side_effect=AssertionError("unrelated modes"))
    ui.sync_sliders_from_gpu = Mock(side_effect=AssertionError("unrelated sliders"))
    ui.vf_read = Mock(side_effect=AssertionError("unrelated V/F"))
    ui.i2c_verified = Mock(side_effect=AssertionError("unrelated I2C"))
    ui.refresh_profile_list = Mock()
    return ui


def test_manual_load_saves_only_incoming_fields_and_bypasses_other_controls(app):
    state = {"schema": 2, "scope": "voltage_requests"}
    app.finish_profile_load = Mock()
    app._held_startup_request = {"name": "old automatic request"}
    with (
        patch("druta.druta.profiles.preflight", return_value=None),
        patch("druta.druta.profiles.autosave_voltage_requests", return_value=("undo", "p", [])) as save,
    ):
        app.begin_profile_load("requests", state)
    save.assert_called_once_with(app.gpu, "load-requests", state)
    app.finish_profile_load.assert_called_once_with("requests", state, False)
    assert app._held_startup_request is None


def test_missing_undo_blocks_manual_load(app):
    app.finish_profile_load = Mock()
    with (
        patch("druta.druta.profiles.preflight", return_value=None),
        patch("druta.druta.profiles.autosave_voltage_requests", side_effect=OSError("disk full")),
    ):
        app.begin_profile_load("requests", {"scope": "voltage_requests"})
    app.finish_profile_load.assert_not_called()
    assert "disk full" in app.profile_failure.call_args.args[0]


@pytest.mark.parametrize("entry", ["begin_profile_load", "finish_profile_load"])
def test_automatic_application_is_rejected_before_reads_or_writes(app, entry):
    with (
        patch("druta.druta.profiles.restore") as restore,
        patch("druta.druta.profiles.preflight") as preflight,
        patch("druta.druta.profiles.autosave_voltage_requests") as save,
    ):
        getattr(app, entry)("requests", {"scope": "voltage_requests"}, automatic=True)
    restore.assert_not_called()
    preflight.assert_not_called()
    save.assert_not_called()
    app.profile_failure.assert_called_once_with("voltage-request profiles require manual loading", True)


@pytest.mark.parametrize("success", [True, False])
def test_finishing_only_refreshes_request_panel_and_reports_partial_failure(app, success):
    state = {"scope": "voltage_requests"}
    app._registry_panel = SimpleNamespace(voltage_requests_panel=Mock())
    results = [(True, "REL restored")]
    if not success:
        results.append((False, "OV write suppressed"))
    with patch("druta.druta.profiles.restore", return_value=results) as restore:
        app.finish_profile_load("requests", state)
    restore.assert_called_once_with(app.gpu, state)
    app._registry_panel.voltage_requests_panel.refresh.assert_called_once()
    assert app.profile_failure.called is (not success)
    assert app._profile_applying is False


def test_scoped_profiles_cannot_be_selected_for_startup(app):
    app._startup_manager = Mock()
    with (
        patch("druta.druta.is_admin", return_value=True),
        patch("druta.druta.profiles.load", return_value={"scope": "voltage_requests"}),
        patch("druta.druta.profiles.preflight") as preflight,
    ):
        app.set_startup_profile("requests")
    preflight.assert_not_called()
    app._startup_manager.enable.assert_not_called()
    assert "require manual loading" in app.log.call_args.args[0]


def test_save_uses_scoped_capture(app):
    app.profile_capture_ready = Mock(return_value=True)
    app._profile_save_scope = "voltage_requests"
    state = {"scope": "voltage_requests"}
    with (
        patch("druta.druta.dpg.get_value", return_value="requests"),
        patch("druta.druta.dpg.does_item_exist", return_value=False),
        patch("druta.druta.profiles.capture_voltage_requests", return_value=state) as capture,
        patch("druta.druta.profiles.capture", side_effect=AssertionError("full capture")),
        patch("druta.druta.profiles.save", return_value="requests.json") as save,
        patch("druta.druta.profiles.summarize", return_value="rail requests"),
    ):
        app.save_profile()
    capture.assert_called_once_with(app.gpu)
    save.assert_called_once_with("requests", state)
