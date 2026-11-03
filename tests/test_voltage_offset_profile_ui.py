"""Voltage offset profiles are manual and use scoped undo."""

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
    ui._registry_panel = SimpleNamespace(voltage_offsets_panel=Mock(), invalidate=Mock(),
                                         refresh_voltage_gate=Mock(), voltage_requests_panel=Mock())
    return ui


def state():
    return {"schema": 2, "scope": "voltage_offsets", "voltage_offsets": {"values": {"voltage.0.REL.offset": -5000}}}


@pytest.mark.parametrize("entry", ["load", "undo"])
@pytest.mark.parametrize("failure", [None, "save", "incomplete"])
def test_load_and_undo_take_selected_undo_before_restoring(app, entry, failure):
    incoming = state()
    with (patch("druta.druta.profiles.load", return_value=incoming),
          patch("druta.druta.profiles.list_profiles", return_value=[("named", None, None, True)]),
          patch("druta.druta.profiles.preflight", return_value=None) as preflight,
          patch("druta.druta.profiles.device_mismatch", return_value=None) as mismatch,
          patch("druta.druta.profiles.autosave_voltage_offsets", side_effect=OSError("disk full") if failure == "save" else None,
                return_value=("undo", "path", ["missing"] if failure == "incomplete" else [])) as autosave,
          patch("druta.druta.profiles.restore", return_value=[(True, "restored")]) as restore):
        if entry == "load":
            app.load_profile("named")
        else:
            app.undo_last_write()
    preflight.assert_called_once_with(app.gpu, incoming)
    mismatch.assert_not_called()
    app.rail_for_profile.assert_not_called()
    app.set_pending_load.assert_called_once_with(None)
    autosave.assert_called_once_with(app.gpu, "load-named", incoming)
    if failure:
        restore.assert_not_called()
        app.profile_failure.assert_called_once()
        app._registry_panel.voltage_offsets_panel.refresh.assert_not_called()
    else:
        restore.assert_called_once_with(app.gpu, incoming)
        app._registry_panel.voltage_offsets_panel.refresh.assert_called_once_with()
        app._registry_panel.invalidate.assert_called_once_with()
        app._registry_panel.refresh_voltage_gate.assert_not_called()
        app._registry_panel.voltage_requests_panel.refresh.assert_not_called()
        app.profile_failure.assert_not_called()


@pytest.mark.parametrize("entry", ["begin_profile_load", "finish_profile_load"])
def test_automatic_application_refused_before_any_profile_operations(app, entry):
    with (patch("druta.druta.profiles.preflight") as preflight,
          patch("druta.druta.profiles.autosave_voltage_offsets") as autosave,
          patch("druta.druta.profiles.restore") as restore):
        getattr(app, entry)("named", state(), automatic=True)
    for operation in (preflight, autosave, restore):
        operation.assert_not_called()
    assert "manual loading" in app.profile_failure.call_args.args[0]


def test_startup_refused_before_preflight_or_enable(app):
    app._startup_manager = Mock()
    with (patch("druta.druta.is_admin", return_value=True),
          patch("druta.druta.profiles.load", return_value=state()),
          patch("druta.druta.profiles.preflight") as preflight):
        app.set_startup_profile("named")
    preflight.assert_not_called()
    app._startup_manager.enable.assert_not_called()
    assert "manual loading" in app.log.call_args.args[0]


def test_descriptive_metadata_difference_does_not_block_manual_load(app):
    slot = "0000:01:00.0"
    app.gpu.slot.return_value = slot
    app.gpu.static = {"name": "current", "vbios": "new", "uuid": None}
    incoming = {"schema": 2, "scope": "voltage_offsets",
                "device": {"name": "old", "vbios": "old", "uuid": None},
                "voltage_offsets": {"slot": slot, "values": {"voltage.0.REL.offset": -5000}}}
    snapshot = {"slot": slot, "values": {"voltage.0.REL.offset": 0}}
    with (patch("druta.druta.profiles.load", return_value=incoming),
          patch("druta.voltage_offsets_live.read", return_value=snapshot),
          patch("druta.druta.profiles.autosave_voltage_offsets", return_value=("undo", "path", [])),
          patch("druta.druta.profiles.restore", return_value=[(True, "restored")]) as restore):
        app.load_profile("named")
    restore.assert_called_once_with(app.gpu, incoming)
    app.profile_failure.assert_not_called()
    app.rail_for_profile.assert_not_called()
