"""Offline adaptive policy panel behavior."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.adaptive_power_panel import AdaptivePowerPanel


class FakeController:
    calls = []

    def __init__(self, gpu, index):
        self.gpu, self.index = gpu, index

    def read(self):
        self.calls.append(("read", self.index))
        if self.gpu.error:
            raise RuntimeError(self.gpu.error)
        available = self.index not in self.gpu.unavailable
        return {"index": self.index, "name": str(self.index), "available": available,
                "enabled": self.gpu.enabled if available else None,
                "status": "available" if available else "unsupported"}

    def set_allowed(self, value):
        self.calls.append(("set", self.index, value))
        if self.gpu.error:
            raise RuntimeError(self.gpu.error)
        state = self.read()
        return {**state, "requested_allowed": value, "request_accepted": True,
                "before_enabled": state["enabled"], "verification": self.gpu.verification}


@pytest.fixture
def panel(monkeypatch):
    FakeController.calls.clear()
    monkeypatch.setitem(sys.modules, "druta.adaptive_power_live",
                        SimpleNamespace(AdaptivePowerController=FakeController))
    dpg.create_context()
    gpu = SimpleNamespace(error=None, enabled=True, unavailable=set(), verification="aggregate_transition")
    app = SimpleNamespace(gpu=gpu, s=lambda value: value, guard=Mock(return_value=True), log=Mock())
    view = AdaptivePowerPanel(app)
    with dpg.window():
        view.build()
    try:
        yield view
    finally:
        dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration(tag)["enabled"]


def select(panel, value):
    dpg.set_value("adaptive_power_controller", value)
    panel._selection_changed()


def test_build_and_selection_do_not_call_backend(panel):
    assert not enabled("adaptive_power_allow") and not enabled("adaptive_power_disable")
    select(panel, "NVDEC")
    assert FakeController.calls == []
    assert "shared" in dpg.get_value("adaptive_power_explainer")


@pytest.mark.parametrize("name,index", [("Graphics (GR)", 0), ("Memory subsystem (MS)", 2),
                                        ("NVDEC", 4), ("NVENC", 5), ("NVJPG", 6), ("OFA", 7)])
def test_selected_controller_requires_refresh_and_routes_both_actions(panel, name, index):
    select(panel, name)
    panel.read()
    assert enabled("adaptive_power_allow") and enabled("adaptive_power_disable")
    assert "enabled" in dpg.get_value("adaptive_power_readback")
    panel.disable()
    panel.allow()
    assert [(c[1], c[2]) for c in FakeController.calls if c[0] == "set"] == [(index, False), (index, True)]


def test_unavailable_role_does_not_block_other_role(panel):
    panel.app.gpu.unavailable.add(2)
    select(panel, "Memory subsystem (MS)")
    panel.read()
    assert not enabled("adaptive_power_allow") and "unsupported" in dpg.get_value("adaptive_power_readback")
    select(panel, "NVDEC")
    assert not enabled("adaptive_power_disable")
    panel.read()
    assert enabled("adaptive_power_disable")


def test_aggregate_only_never_claims_individual_request_effect(panel):
    panel.read()
    panel.app.gpu.verification = "aggregate_only"
    panel.disable()
    status = dpg.get_value("adaptive_power_status")
    assert "accepted" in status and "aggregate policy is enabled" in status
    assert "not independently verified" in status


def test_already_allowed_reports_no_request(panel):
    panel.read()
    panel.app.gpu.verification = "already_allowed"
    panel.allow()
    assert "no request was needed" in dpg.get_value("adaptive_power_status")


def test_guard_selection_and_gpu_switch_prevent_stale_write(panel):
    panel.read()
    panel.app.guard.return_value = False
    panel.disable()
    assert not [c for c in FakeController.calls if c[0] == "set"]
    panel.app.guard.return_value = True
    dpg.set_value("adaptive_power_controller", "NVENC")
    panel.allow()
    assert not enabled("adaptive_power_allow")
    panel.read()
    old = panel.app.gpu
    panel.app.gpu = SimpleNamespace(error=None, enabled=True, unavailable=set(), verification="aggregate_transition")
    panel.allow()
    assert old is not panel.app.gpu and not enabled("adaptive_power_allow")
    assert not [c for c in FakeController.calls if c[0] == "set"]


def test_failed_or_unknown_state_requires_fresh_read(panel):
    panel.read()
    panel.app.gpu.error = "temporary failure"
    panel.disable()
    assert not enabled("adaptive_power_disable")
    assert "temporary failure" in dpg.get_value("adaptive_power_status")
    panel.app.log.assert_called_once()
    panel.app.gpu.error = None
    panel.app.gpu.enabled = None
    panel.read()
    assert not enabled("adaptive_power_allow") and "unknown" in dpg.get_value("adaptive_power_readback")


def test_mismatched_readback_never_enables(panel, monkeypatch):
    monkeypatch.setattr(FakeController, "read", lambda self: {"index": 7, "available": True, "enabled": True})
    panel.read()
    assert not enabled("adaptive_power_allow") and "does not match" in dpg.get_value("adaptive_power_status")
