"""Scoped voltage-control profiles remain manual and touch only their control."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from druta.druta import Druta

KINDS = ("gate", "rel_bounds", "constants", "switch_delay")
REFRESH = {
    "gate": "refresh_voltage_gate",
    "rel_bounds": "refresh_rel_live",
    "constants": "refresh_constants_live",
    "switch_delay": "refresh_voltage_devices",
}


@pytest.fixture
def app():
    ui = Druta.__new__(Druta)
    ui.gpu, ui.rail, ui._i2c_busy = Mock(), None, False
    ui.guard = Mock(return_value=True)
    ui.log, ui.profile_failure, ui.refresh_profile_list = Mock(), Mock(), Mock()
    ui._finish_manual_voltage = Mock(side_effect=AssertionError("unrelated voltage"))
    ui.sync_risk_ui = Mock(side_effect=AssertionError("unrelated modes"))
    ui.i2c_verified = Mock(side_effect=AssertionError("unrelated I2C"))
    return ui


def state(kind):
    return {"scope": "voltage_controls", "voltage_controls": {"kind": kind}}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("entry", ["begin_profile_load", "finish_profile_load"])
def test_automatic_refused_without_capture_or_restore(app, kind, entry):
    with patch("druta.druta.profiles.preflight") as preflight, patch("druta.druta.profiles.restore") as restore:
        getattr(app, entry)("named", state(kind), automatic=True)
    preflight.assert_not_called()
    restore.assert_not_called()
    app.profile_failure.assert_called_once_with("voltage-request profiles require manual loading", True)


@pytest.mark.parametrize("kind", KINDS)
def test_named_save_uses_selected_kind(app, kind):
    app.profile_capture_ready = Mock(return_value=True)
    app.show_win = Mock()
    with (
        patch("druta.druta.dpg.does_item_exist", return_value=False),
        patch("druta.druta.dpg.get_value", return_value="named"),
        patch("druta.druta.profiles.capture_voltage_controls", return_value=state(kind), create=True) as capture,
        patch("druta.druta.profiles.capture", side_effect=AssertionError("full capture")),
        patch("druta.druta.profiles.save", return_value="named.json") as save,
        patch("druta.druta.profiles.summarize", return_value="voltage"),
    ):
        app.open_save_profile(user_data=kind)
        app.save_profile()
    capture.assert_called_once_with(app.gpu, kind)
    save.assert_called_once_with("named", state(kind))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("failure", [False, True])
def test_restore_only_refreshes_selected_control(app, kind, failure):
    panel = SimpleNamespace(**{name: Mock() for name in REFRESH.values()})
    panel.voltage_requests_panel = SimpleNamespace(refresh=Mock())
    app._registry_panel = panel
    results = [(not failure, "result")]
    with patch("druta.druta.profiles.restore", return_value=results) as restore:
        app.finish_profile_load("named", state(kind))
    restore.assert_called_once_with(app.gpu, state(kind))
    for name in REFRESH.values():
        assert getattr(panel, name).call_count == (name == REFRESH[kind])
    panel.voltage_requests_panel.refresh.assert_not_called()
    assert app.profile_failure.called is failure
    assert app._profile_applying is False


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("failure", [None, "exception", "incomplete"])
def test_load_saves_incoming_selection_before_restore(app, kind, failure):
    incoming = state(kind)
    app.finish_profile_load = Mock()
    result = ("undo", "path", ["missing"] if failure == "incomplete" else [])
    save = Mock(side_effect=OSError("disk full") if failure == "exception" else None, return_value=result)
    with (
        patch("druta.druta.profiles.preflight", return_value=None),
        patch("druta.druta.profiles.autosave_voltage_controls", save, create=True),
    ):
        app.begin_profile_load("named", incoming)
    save.assert_called_once_with(app.gpu, "load-named", incoming)
    if failure:
        app.finish_profile_load.assert_not_called()
        assert "undo" in app.profile_failure.call_args.args[0]
    else:
        app.finish_profile_load.assert_called_once_with("named", incoming, False)


@pytest.mark.parametrize("kind", KINDS)
def test_cannot_select_for_startup(app, kind):
    app._startup_manager = Mock()
    with (
        patch("druta.druta.is_admin", return_value=True),
        patch("druta.druta.profiles.load", return_value=state(kind)),
        patch("druta.druta.profiles.preflight") as preflight,
    ):
        app.set_startup_profile("named")
    preflight.assert_not_called()
    app._startup_manager.enable.assert_not_called()
    assert "require manual loading" in app.log.call_args.args[0]
