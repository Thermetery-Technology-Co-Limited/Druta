"""Low-power request profiles use owned live objects and manual application."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from druta import profiles
from druta.druta import Druta

SELECTIONS = [("difr", None), ("elpg", None), ("elcg", None), ("blcg", None),
              ("slcg", None), ("individual", 13)]
PANELS = {"difr": "difr_live_panel", "elpg": "gr_gating_live_panel",
          "elcg": "engine_clock_gating_panel", "blcg": "block_clock_gating_panel",
          "slcg": "second_level_clock_gating_panel", "individual": "individual_power_gating_panel"}


def state(kind, index):
    return {"schema": 2, "scope": "lowpower_requests", "device": {}, "incomplete": [],
            "lowpower_requests": {"kind": kind, "index": index, "mode": "disabled",
                                  "identity": {"slot": "1:0", "architecture": 1,
                                               "device_id": 2, "subsystem_id": 3}}}


@pytest.fixture
def app():
    ui = Druta.__new__(Druta)
    ui.gpu, ui.rail, ui._i2c_busy = Mock(), None, False
    ui.guard = Mock(return_value=True)
    ui.log, ui.profile_failure, ui.refresh_profile_list = Mock(), Mock(), Mock()
    ui.profile_capture_ready, ui.show_win = Mock(return_value=True), Mock()
    ui._finish_manual_voltage = Mock(side_effect=AssertionError("unrelated voltage"))
    ui.sync_risk_ui = Mock(side_effect=AssertionError("unrelated risk mode"))
    ui.i2c_verified = Mock(side_effect=AssertionError("unrelated I2C"))
    ui._registry_panel = SimpleNamespace(**{name: SimpleNamespace(read=Mock()) for name in PANELS.values()})
    return ui


@pytest.mark.parametrize("kind,index", SELECTIONS)
def test_named_save_routes_selected_live_context(app, kind, index):
    saved = state(kind, index)
    with (patch("druta.druta.dpg.does_item_exist", return_value=False),
          patch("druta.druta.dpg.get_value", return_value="named"),
          patch("druta.lowpower_profiles.capture", return_value=saved) as capture,
          patch("druta.druta.profiles.capture", side_effect=AssertionError("full capture")),
          patch("druta.druta.profiles.save", return_value="named.json") as save):
        app.open_save_profile(user_data=("lowpower_requests", kind, index))
        app.save_profile()
    capture.assert_called_once_with(app, kind, index)
    save.assert_called_once_with("named", saved)


@pytest.mark.parametrize("kind,index", SELECTIONS)
@pytest.mark.parametrize("failure", [None, "preflight", "capture", "save", "incomplete"])
def test_manual_load_captures_only_incoming_selection_before_restore(app, kind, index, failure):
    incoming, before = state(kind, index), state(kind, index)
    app.finish_profile_load = Mock()
    with (patch("druta.lowpower_profiles.preflight", return_value="not available" if failure == "preflight" else None) as preflight,
          patch("druta.lowpower_profiles.capture", side_effect=ValueError("capture failed") if failure == "capture" else None,
                return_value=before) as capture,
          patch("druta.druta.profiles.autosave_state", side_effect=OSError("disk full") if failure == "save" else None,
                return_value=("undo", "path", ["missing"] if failure == "incomplete" else [])) as autosave,
          patch("druta.druta.profiles.restore", side_effect=AssertionError("unrelated restore"))):
        app.begin_profile_load("named", incoming)
    preflight.assert_called_once_with(app, incoming)
    if failure == "preflight":
        capture.assert_not_called()
    else:
        capture.assert_called_once_with(app, kind, index)
    if failure in ("preflight", "capture"):
        autosave.assert_not_called()
    else:
        autosave.assert_called_once_with("load-named", before)
    if failure:
        app.finish_profile_load.assert_not_called()
        app.profile_failure.assert_called_once()
    else:
        app.finish_profile_load.assert_called_once_with("named", incoming, False)
        app.profile_failure.assert_not_called()


@pytest.mark.parametrize("kind,index", SELECTIONS)
@pytest.mark.parametrize("entry", ["begin_profile_load", "finish_profile_load"])
def test_automatic_refused_before_preflight_capture_or_restore(app, kind, index, entry):
    with (patch("druta.lowpower_profiles.preflight") as preflight,
          patch("druta.lowpower_profiles.capture") as capture,
          patch("druta.lowpower_profiles.restore") as restore,
          patch("druta.druta.profiles.autosave_state") as autosave):
        getattr(app, entry)("named", state(kind, index), automatic=True)
    for call in (preflight, capture, restore, autosave):
        call.assert_not_called()
    app.profile_failure.assert_called_once_with("low-power request profiles require manual loading", True)


@pytest.mark.parametrize("kind,index", SELECTIONS)
@pytest.mark.parametrize("outcome", ["success", "partial", "exception"])
def test_restore_uses_live_app_and_refreshes_only_selected_panel(app, kind, index, outcome):
    incoming = state(kind, index)
    result = [(True, "first"), (False, "failed")] if outcome == "partial" else [(True, "ok")]
    with (patch("druta.lowpower_profiles.restore", side_effect=RuntimeError("failed") if outcome == "exception" else None,
                return_value=result) as restore,
          patch("druta.druta.profiles.restore", side_effect=AssertionError("unrelated restore"))):
        app.finish_profile_load("named", incoming)
    restore.assert_called_once_with(app, incoming)
    for panel_kind, name in PANELS.items():
        read = getattr(app._registry_panel, name).read
        if panel_kind != kind:
            read.assert_not_called()
        elif kind == "individual":
            read.assert_called_once_with(user_data=index)
        else:
            read.assert_called_once_with()
    assert app.profile_failure.called == (outcome != "success")
    assert app._profile_applying is False


@pytest.mark.parametrize("kind,index", SELECTIONS)
def test_startup_selection_refuses_before_preflight_or_enable(app, kind, index):
    app._startup_manager = Mock()
    with (patch("druta.druta.is_admin", return_value=True),
          patch("druta.druta.profiles.load", return_value=state(kind, index)),
          patch("druta.druta.profiles.preflight") as preflight,
          patch("druta.lowpower_profiles.restore") as restore):
        app.set_startup_profile("named")
    preflight.assert_not_called()
    restore.assert_not_called()
    app._startup_manager.enable.assert_not_called()
    assert "require manual loading" in app.log.call_args.args[0]


def test_completeness_and_summary_validate_lowpower_scope():
    valid = state("individual", 13)
    assert profiles.incomplete(valid) == []
    assert "individual 13" in profiles.summarize(valid)
    assert "manual apply only" in profiles.summarize(valid)
    invalid = state("individual", 13)
    invalid["lowpower_requests"]["mode"] = "unknown"
    assert profiles.incomplete(invalid)
    assert "invalid low-power request" in profiles.summarize(invalid)

@pytest.mark.parametrize("entry", ["load", "undo"])
@pytest.mark.parametrize("unlocked", [False, True])
def test_load_and_undo_route_lowpower_before_gpu_only_preflight(app, entry, unlocked):
    incoming = state("individual", 13)
    app.guard.return_value = unlocked
    app.set_pending_load = Mock()
    app.begin_lowpower_profile_load = Mock()
    app.rail_for_profile = Mock(side_effect=AssertionError("unrelated rail selection"))
    with (patch("druta.druta.profiles.load", return_value=incoming) as load,
          patch("druta.druta.profiles.list_profiles", return_value=[("undo-name", None, None, True)]),
          patch("druta.druta.profiles.preflight", side_effect=AssertionError("GPU-only preflight")) as preflight,
          patch("druta.druta.profiles.device_mismatch", side_effect=AssertionError("GPU-only mismatch")) as mismatch):
        if entry == "load":
            app.load_profile("undo-name")
        else:
            app.undo_last_write()
    app.guard.assert_called_once_with()
    preflight.assert_not_called()
    mismatch.assert_not_called()
    app.rail_for_profile.assert_not_called()
    if unlocked:
        load.assert_called_once_with("undo-name")
        app.set_pending_load.assert_called_once_with(None)
        app.begin_lowpower_profile_load.assert_called_once_with("undo-name", incoming)
    else:
        load.assert_not_called()
        app.set_pending_load.assert_not_called()
        app.begin_lowpower_profile_load.assert_not_called()
