"""Manual profile routing for power-policy feedback bounds."""
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta import profiles, startup
from druta.druta import Druta


def test_named_profile_choice_captures_all_bounds(monkeypatch):
    dpg.create_context()
    try:
        with dpg.window():
            dpg.add_input_text(tag="prof_name", default_value="bounds")
        state = {"scope": "power_integral"}
        capture = Mock(return_value=state)
        save = Mock(return_value="bounds.json")
        monkeypatch.setattr(profiles, "capture_power_integral", capture)
        monkeypatch.setattr(profiles, "save", save)
        monkeypatch.setattr(profiles, "incomplete", lambda _state: [])
        monkeypatch.setattr(profiles, "summarize", lambda _state: "bounds")
        gpu = SimpleNamespace(read=lambda: {}, clkdom_controls_for_ui=lambda _: [],
                              clkdom_domains=lambda: [])
        app = SimpleNamespace(gpu=gpu, DOMAIN_KNOBS=[], log=Mock(),
                              profile_capture_ready=lambda: True, refresh_profile_list=Mock(),
                              _profile_save_scope="power_integral")
        assert Druta.ordinary_profile_contents(app)["Power-policy feedback bounds"] == (
            "power_integral", None, None)
        Druta.save_profile(app)
        capture.assert_called_once_with(gpu)
        save.assert_called_once_with("bounds", state)
    finally:
        dpg.destroy_context()


def test_automatic_profile_load_and_startup_refused(tmp_path):
    app = SimpleNamespace(profile_failure=Mock())
    app.begin_voltage_request_profile_load = lambda *args: Druta.begin_voltage_request_profile_load(app, *args)
    Druta.begin_profile_load(app, "bounds", {"scope": "power_integral"}, automatic=True)
    assert "manual" in app.profile_failure.call_args.args[0]
    manager = startup.Startup(tmp_path, lambda: {"id": "boot", "clean": True, "reason": ""}, Mock())
    try:
        with pytest.raises(ValueError, match="manual"):
            manager.enable("bounds", {"scope": "power_integral"})
    finally:
        manager.close()


def test_profile_refresh_uses_panel_read():
    panel = SimpleNamespace(read=Mock())
    app = SimpleNamespace(_power_integral_panel=panel, refresh_profile_list=Mock())
    Druta.refresh_scoped_profile(app, {"scope": "power_integral"})
    panel.read.assert_called_once_with()


def test_manual_load_takes_selected_field_undo_before_restore(monkeypatch):
    state = {"scope": "power_integral", "power_integral": {"policies": [{"policy": 7, "values": {"lower_raw": 10}}]}}
    monkeypatch.setattr(profiles, "preflight", lambda *_: None)
    autosave = Mock(return_value=("undo", "path", []))
    monkeypatch.setattr(profiles, "autosave_power_integral", autosave)
    app = SimpleNamespace(gpu=object(), guard=lambda: True, _i2c_busy=False,
                          profile_failure=Mock(), log=Mock(), finish_profile_load=Mock())
    Druta.begin_voltage_request_profile_load(app, "bounds", state)
    autosave.assert_called_once_with(app.gpu, "load-bounds", state)
    app.finish_profile_load.assert_called_once_with("bounds", state, False)
    app.finish_profile_load.reset_mock()
    autosave.side_effect = OSError("disk full")
    Druta.begin_voltage_request_profile_load(app, "bounds", state)
    app.finish_profile_load.assert_not_called()
    assert "nothing changed" in app.profile_failure.call_args.args[0]
