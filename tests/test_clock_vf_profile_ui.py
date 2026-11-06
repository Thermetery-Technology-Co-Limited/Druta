"""Clock VF profiles are manual and use scoped undo on every load route."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from druta.druta import Druta


@pytest.fixture
def app():
    ui = Druta.__new__(Druta)
    ui.gpu, ui.rail, ui._i2c_busy, ui._pending_load = Mock(), None, False, None
    ui.guard = Mock(return_value=True)
    ui.log, ui.profile_failure, ui.refresh_profile_list = Mock(), Mock(), Mock()
    ui.profile_capture_ready, ui.show_win, ui.set_pending_load = Mock(return_value=True), Mock(), Mock()
    ui.rail_for_profile = Mock(return_value=None)
    ui.i2c_verified = Mock(side_effect=AssertionError("unrelated I2C"))
    ui._registry_panel = SimpleNamespace(refresh_clock_vf_live=Mock(), invalidate=Mock(),
                                         refresh_voltage_gate=Mock(), voltage_requests_panel=Mock())
    return ui


def state():
    return {"schema": 2, "scope": "clock_vf", "clock_vf": {"monotonicity": True}}


def test_named_save_routes_to_vf_capture(app):
    incoming = state()
    with patch("druta.druta.dpg.does_item_exist", return_value=False), \
         patch("druta.druta.dpg.get_value", return_value="named"), \
         patch("druta.druta.profiles.capture_clock_vf", return_value=incoming) as capture, \
         patch("druta.druta.profiles.capture", side_effect=AssertionError("full capture")), \
         patch("druta.druta.profiles.save", return_value="named.json") as save:
        app.open_save_profile(user_data="clock_vf")
        app.save_profile()
    capture.assert_called_once_with(app.gpu)
    save.assert_called_once_with("named", incoming)


@pytest.mark.parametrize("entry", ["load", "undo"])
@pytest.mark.parametrize("failure", [None, "save", "incomplete"])
def test_load_and_undo_take_selected_undo_before_restoring(app, entry, failure):
    incoming = state()
    with patch("druta.druta.profiles.load", return_value=incoming), \
         patch("druta.druta.profiles.list_profiles", return_value=[("named", None, None, True)]), \
         patch("druta.druta.profiles.preflight", return_value=None) as preflight, \
         patch("druta.druta.profiles.device_mismatch", return_value=None), \
         patch("druta.druta.profiles.autosave_clock_vf", side_effect=OSError("disk full") if failure == "save" else None,
                return_value=("undo", "path", ["missing"] if failure == "incomplete" else [])) as autosave, \
         patch("druta.druta.profiles.restore", return_value=[(True, "restored")]) as restore:
        if entry == "load":
            app.load_profile("named")
        else:
            app.undo_last_write()
    assert preflight.call_count == 2
    autosave.assert_called_once_with(app.gpu, "load-named", incoming)
    if failure:
        restore.assert_not_called()
        app.profile_failure.assert_called_once()
        app._registry_panel.refresh_clock_vf_live.assert_not_called()
    else:
        restore.assert_called_once_with(app.gpu, incoming)
        app._registry_panel.refresh_clock_vf_live.assert_called_once_with()
        app._registry_panel.invalidate.assert_called_once_with()
        app._registry_panel.refresh_voltage_gate.assert_not_called()
        app._registry_panel.voltage_requests_panel.refresh.assert_not_called()
        app.profile_failure.assert_not_called()


@pytest.mark.parametrize("entry", ["begin_profile_load", "finish_profile_load"])
def test_automatic_application_refused_before_any_profile_operations(app, entry):
    with patch("druta.druta.profiles.preflight") as preflight, \
         patch("druta.druta.profiles.autosave_clock_vf") as autosave, \
         patch("druta.druta.profiles.restore") as restore:
        getattr(app, entry)("named", state(), automatic=True)
    for operation in (preflight, autosave, restore):
        operation.assert_not_called()
    assert "manual loading" in app.profile_failure.call_args.args[0]


def test_startup_refused_before_preflight_or_enable(app):
    app._startup_manager = Mock()
    with patch("druta.druta.is_admin", return_value=True), \
         patch("druta.druta.profiles.load", return_value=state()), \
         patch("druta.druta.profiles.preflight") as preflight:
        app.set_startup_profile("named")
    preflight.assert_not_called()
    app._startup_manager.enable.assert_not_called()
    assert "manual loading" in app.log.call_args.args[0]
