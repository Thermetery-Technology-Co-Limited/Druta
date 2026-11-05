"""Offline UI coverage for manual PCIe speed requests."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.pcie_speed_panel import PcieSpeedPanel


def snapshot(identity="gpu-a", target=1, available=True):
    return {"identity": identity, "available": available, "status": "Unavailable",
            "gpu_max_generation": 5, "root_max_generation": 4,
            "generation": target, "root_generation": target, "width": 16, "root_width": 16,
            "target_generation": target, "target_register": target, "allowed_generations": [1, 2, 3, 4]}


@pytest.fixture
def panel(monkeypatch):
    state = SimpleNamespace(read=Mock(return_value=snapshot()), set=Mock())
    class Controller:
        def __init__(self, gpu):
            self.gpu = gpu
        def read(self):
            return state.read()
        def set_generation(self, generation, expected_identity=None):
            return state.set(generation, expected_identity=expected_identity)
    monkeypatch.setitem(sys.modules, "druta.pcie_speed_live", SimpleNamespace(PcieSpeedController=Controller))
    from druta import profiles
    state.capture = Mock(side_effect=lambda gpu: {"pcie_speed": {"identity": state.read.return_value["identity"],
                           "target_generation": state.read.return_value["target_generation"]}})
    state.autosave = Mock(return_value=("undo", "path", []))
    monkeypatch.setattr(profiles, "capture_pcie_speed", state.capture, raising=False)
    monkeypatch.setattr(profiles, "autosave_pcie_speed", state.autosave, raising=False)
    dpg.create_context()
    app = SimpleNamespace(gpu=object(), s=lambda n: n, guard=Mock(return_value=True), log=Mock(),
                          open_save_profile=Mock())
    instance = PcieSpeedPanel(app)
    with dpg.window():
        instance.build()
    yield instance, state
    dpg.destroy_context()


def enabled(name):
    return dpg.get_item_configuration("pcie_speed_" + name)["enabled"]


def test_build_is_read_only_and_choices_follow_fresh_capabilities(panel):
    p, state = panel
    state.read.assert_not_called()
    assert not enabled("request") and not enabled("previous")
    p.read()
    assert dpg.get_item_configuration("pcie_speed_generation")["items"] == ["Gen 1", "Gen 2", "Gen 3", "Gen 4"]
    assert enabled("request")
    readback = dpg.get_value("pcie_speed_readback")
    assert "GPU link: Gen 1, x16; root link: Gen 1, x16" in readback
    assert "GPU Gen 5; root Gen 4" in readback
    narrower = snapshot()
    narrower["allowed_generations"] = [1, 2]
    state.read.return_value = narrower
    p.read()
    assert dpg.get_item_configuration("pcie_speed_generation")["items"] == ["Gen 1", "Gen 2"]
    state.read.return_value = snapshot(available=False)
    p.read()
    assert not enabled("request")
    assert dpg.get_item_configuration("pcie_speed_generation")["items"] == []


def test_failure_and_gpu_change_invalidate_requests(panel):
    p, state = panel
    p.read()
    state.read.side_effect = RuntimeError("transport")
    p.read()
    assert not enabled("request")
    assert "Historical readback" in dpg.get_value("pcie_speed_readback")
    p.request()
    state.set.assert_not_called()
    state.read.side_effect = None
    p.read()
    p.app.gpu = object()
    p.request()
    state.set.assert_not_called()
    assert not enabled("request")
    assert "Historical readback" in dpg.get_value("pcie_speed_readback")


def test_unverified_request_is_not_reported_as_changed(panel):
    p, state = panel
    p.read()
    result = snapshot(target=1)
    result.update(request_accepted=True, verification="unverified", before=snapshot())
    state.set.return_value = result
    dpg.set_value("pcie_speed_generation", "Gen 2")
    p.request()
    assert "not verified" in dpg.get_value("pcie_speed_status")
    assert not enabled("previous")
    assert not enabled("request")
    assert "unverified readback" in dpg.get_value("pcie_speed_readback")
    state.set.assert_called_once_with(2, expected_identity="gpu-a")


def test_manual_previous_request_requires_verified_changed_target(panel):
    p, state = panel
    p.read()
    after = snapshot(target=2)
    after.update(request_accepted=True, verification="target_and_link", before=snapshot())
    state.set.return_value = after
    dpg.set_value("pcie_speed_generation", "Gen 2")
    p.request()
    assert enabled("previous")
    state.read.return_value = snapshot(target=2)
    unchanged = snapshot(target=2)
    unchanged.update(request_accepted=False, verification="already_matches", before=snapshot(target=2))
    state.set.return_value = unchanged
    p.request()
    assert enabled("previous")
    assert state.autosave.call_count == 1
    restored = snapshot(target=1)
    restored.update(request_accepted=True, verification="target_and_link", before=snapshot(target=2))
    state.set.return_value = restored
    p.request_previous()
    assert state.set.call_args.args == (1,)
    assert state.set.call_args.kwargs == {"expected_identity": "gpu-a"}
    state.read.return_value = snapshot(identity="gpu-b")
    p.read()
    assert not enabled("previous")


def test_undo_capture_failure_prevents_request(panel):
    p, state = panel
    p.read()
    state.autosave.side_effect = OSError("disk full")
    dpg.set_value("pcie_speed_generation", "Gen 2")
    p.request()
    state.set.assert_not_called()
    assert "disk full" in dpg.get_value("pcie_speed_status")


def test_save_button_selects_pcie_scope(panel):
    p, _ = panel
    assert dpg.get_item_callback("pcie_speed_save_profile") == p.app.open_save_profile
    assert dpg.get_item_user_data("pcie_speed_save_profile") == ("pcie_speed", None, None)
