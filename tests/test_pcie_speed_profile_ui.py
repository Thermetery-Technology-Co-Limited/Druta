"""Manual PCIe speed profile selection and dispatch."""
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg

from druta import profiles
from druta.druta import Druta


def test_named_profile_selection_and_capture(monkeypatch):
    dpg.create_context()
    try:
        with dpg.window():
            dpg.add_input_text(tag="prof_name", default_value="link")
        state = {"scope": "pcie_speed"}
        capture = Mock(return_value=state)
        save = Mock(return_value="link.json")
        monkeypatch.setattr(profiles, "capture_pcie_speed", capture)
        monkeypatch.setattr(profiles, "save", save)
        monkeypatch.setattr(profiles, "incomplete", lambda _: [])
        monkeypatch.setattr(profiles, "summarize", lambda _: "link")
        gpu = SimpleNamespace(read=lambda: {}, clkdom_controls_for_ui=lambda _: [], clkdom_domains=lambda: [])
        app = SimpleNamespace(gpu=gpu, DOMAIN_KNOBS=[], log=Mock(), profile_capture_ready=lambda: True,
                              refresh_profile_list=Mock(), _profile_save_scope="pcie_speed")
        assert Druta.ordinary_profile_contents(app)["PCIe speed"] == ("pcie_speed", None, None)
        Druta.save_profile(app)
        capture.assert_called_once_with(gpu)
        save.assert_called_once_with("link", state)
    finally:
        dpg.destroy_context()


def test_manual_profile_load_undo_and_refresh(monkeypatch):
    state = {"scope": "pcie_speed"}
    monkeypatch.setattr(profiles, "preflight", lambda *_: None)
    autosave = Mock(return_value=("undo", "path", []))
    monkeypatch.setattr(profiles, "autosave_pcie_speed", autosave)
    app = SimpleNamespace(gpu=object(), guard=lambda: True, _i2c_busy=False,
                          profile_failure=Mock(), log=Mock(), finish_profile_load=Mock())
    Druta.begin_voltage_request_profile_load(app, "link", state)
    autosave.assert_called_once_with(app.gpu, "load-link", state)
    app.finish_profile_load.assert_called_once_with("link", state, False)
    app.finish_profile_load.reset_mock()
    autosave.side_effect = OSError("disk full")
    Druta.begin_voltage_request_profile_load(app, "link", state)
    app.finish_profile_load.assert_not_called()
    assert "nothing changed" in app.profile_failure.call_args.args[0]
    panel = SimpleNamespace(read=Mock())
    app._registry_panel = SimpleNamespace(pcie_speed_panel=panel)
    app.refresh_profile_list = Mock()
    Druta.refresh_scoped_profile(app, state)
    panel.read.assert_called_once_with()


def test_automatic_profile_routes_reject_before_writes(monkeypatch):
    state = {"scope": "pcie_speed"}
    app = SimpleNamespace(profile_failure=Mock())
    app.begin_voltage_request_profile_load = lambda *args: Druta.begin_voltage_request_profile_load(app, *args)
    Druta.begin_profile_load(app, "link", state, automatic=True)
    assert "manual" in app.profile_failure.call_args.args[0]
    restore = Mock()
    monkeypatch.setattr(profiles, "restore", restore)
    Druta.finish_profile_load(app, "link", state, automatic=True)
    restore.assert_not_called()
    assert "manual" in app.profile_failure.call_args.args[0]


def test_combined_profile_refreshes_pcie_panel(monkeypatch):
    from druta import combined_profiles
    state = {"scope": "combined"}
    child = {"scope": "pcie_speed"}
    monkeypatch.setattr(combined_profiles, "restore", lambda *_: [(True, "done")])
    monkeypatch.setattr(combined_profiles, "validate", lambda _: [{"state": child}])
    panel = SimpleNamespace(read=Mock())
    app = SimpleNamespace(_registry_panel=SimpleNamespace(pcie_speed_panel=panel), rail=None,
                          log=Mock(), profile_failure=Mock(), refresh_profile_list=Mock())
    app.refresh_scoped_profile = lambda data: Druta.refresh_scoped_profile(app, data)
    Druta.finish_combined_profile_load(app, "combined", state)
    panel.read.assert_called_once_with()


def test_select_save_scope_from_panel():
    dpg.create_context()
    try:
        app = SimpleNamespace(profile_capture_ready=lambda: True, show_win=Mock())
        Druta.open_save_profile(app, user_data=("pcie_speed", None, None))
        assert (app._profile_save_scope, app._profile_save_kind, app._profile_save_index) == ("pcie_speed", None, None)
    finally:
        dpg.destroy_context()
