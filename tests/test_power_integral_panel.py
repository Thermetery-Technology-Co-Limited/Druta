"""Offline feedback adjustment UI behavior."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.power_integral_panel import PowerIntegralPanel


class FakeController:
    calls = []
    instances = []

    def __init__(self, gpu):
        self.gpu = gpu
        self.instances.append(self)

    def read(self):
        self.calls.append(("read", self.gpu))
        if self.gpu.error:
            raise ValueError(self.gpu.error)
        return {"identity": {"slot": self.gpu.slot}, "policies": [
            {"policy": 7, "type": 2, "channel": 4, "unit": "mW", "available": True,
             "lower_raw": self.gpu.lower, "upper_raw": self.gpu.upper, "default_upper_raw": 101,
             "default_lower_raw": 70, "past_samples": 43, "next_samples": 2,
             "default_past_samples": 42, "default_next_samples": 3,
             "requested": 450000, "effective": 370000, "pmu_output": 360000},
            {"policy": 8, "type": 2, "channel": 5, "unit": "mA", "available": True,
             "lower_raw": 70, "upper_raw": 95, "default_upper_raw": 100, "default_lower_raw": 60,
             "requested": 45000, "effective": 37000, "pmu_output": 36000},
            {"policy": 3, "type": 1, "channel": 1, "unit": "mA", "available": False}]}

    def set_upper(self, policy, raw, *, expected_identity=None):
        self.calls.append(("set", policy, raw, expected_identity))
        self.gpu.upper = raw
        return self.read()

    def set_lower(self, policy, raw, *, expected_identity=None):
        self.calls.append(("set_lower", policy, raw, expected_identity))
        if raw > self.gpu.upper:
            raise ValueError("lower exceeds upper")
        self.gpu.lower = raw
        return self.read()


@pytest.fixture
def panel(monkeypatch):
    FakeController.calls.clear()
    FakeController.instances.clear()
    monkeypatch.setitem(sys.modules, "druta.power_integral_runtime", SimpleNamespace(
        PowerIntegralController=FakeController, percent_to_raw=lambda text: int(text),
        raw_to_percent=lambda raw: str(raw)))
    dpg.create_context()
    app = SimpleNamespace(gpu=SimpleNamespace(slot="A", error=None, upper=103, lower=75),
                          s=lambda n: n, guard=Mock(return_value=True), log=Mock())
    view = PowerIntegralPanel(app)
    with dpg.window():
        view.build()
    yield view
    dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration(tag)["enabled"]


def test_manual_read_and_apply_preserve_input_and_verify_identity(panel):
    assert not FakeController.calls and not enabled("integral_apply")
    dpg.set_value("integral_input", "98")
    panel.read()
    assert enabled("integral_apply") and dpg.get_value("integral_input") == "98"
    assert len(dpg.get_item_configuration("integral_policy")["items"]) == 2
    display = dpg.get_value("integral_readback")
    assert all(text in display for text in ("103%", "101%", "75%", "450 W", "360 W", "370 W"))
    panel.apply()
    assert ("set", 7, 98, {"slot": "A"}) in FakeController.calls
    assert len(FakeController.instances) == 1
    assert dpg.get_value("integral_input") == "98"


def test_default_guard_and_failed_read_keep_stale_display(panel):
    panel.read()
    previous = dpg.get_value("integral_readback")
    panel.app.guard.return_value = False
    panel.use_default()
    assert not any(call[0] == "set" for call in FakeController.calls)
    panel.app.guard.return_value = True
    panel.app.gpu.error = "temporary failure"
    panel.read()
    assert dpg.get_value("integral_readback") == previous
    assert "stale" in dpg.get_value("integral_status") and not enabled("integral_default")
    panel.app.gpu.error = None
    panel.read()
    panel.use_default()
    assert ("set", 7, 101, {"slot": "A"}) in FakeController.calls


def test_adapter_change_and_fresh_identity_change_prevent_write(panel):
    panel.read()
    panel.app.gpu.slot = "B"
    panel.apply()
    assert not any(call[0] == "set" for call in FakeController.calls)
    assert not enabled("integral_apply")
    panel.read()
    panel.app.gpu = SimpleNamespace(slot="C", error=None, upper=105, lower=75)
    panel.use_default()
    assert not any(call[0] == "set" for call in FakeController.calls)
    assert dpg.get_value("integral_readback") == ""
    panel.read()
    assert len(FakeController.instances) == 2


def test_initial_input_selection_and_invalid_input(panel):
    panel.read()
    assert dpg.get_value("integral_input") == "103"
    dpg.set_value("integral_input", "pending")
    panel.read()
    assert dpg.get_value("integral_input") == "pending"
    panel.apply()
    assert not any(call[0] == "set" for call in FakeController.calls)
    panel.read()
    dpg.set_value("integral_policy", "Policy 8 (type 2, channel 5)")
    panel._selection_changed()
    assert dpg.get_value("integral_input") == "95"
    assert "45 A" in dpg.get_value("integral_readback")


def test_read_respects_guard(panel):
    panel.app.guard.return_value = False
    panel.read()
    assert not FakeController.calls


def test_lower_read_apply_default_and_preserve_pending(panel):
    assert not enabled("integral_lower_apply")
    panel.read()
    assert dpg.get_value("integral_lower_input") == "75"
    display = dpg.get_value("integral_readback")
    assert "default lower: 70%" in display
    assert "Past samples: 43 (default 42)" in display
    assert "Next samples: 2 (default 3)" in display
    dpg.set_value("integral_lower_input", "0")
    panel.read()
    assert dpg.get_value("integral_lower_input") == "0"
    panel.apply_lower()
    assert ("set_lower", 7, 0, {"slot": "A"}) in FakeController.calls
    assert dpg.get_value("integral_lower_input") == "0"
    panel.use_lower_default()
    assert ("set_lower", 7, 70, {"slot": "A"}) in FakeController.calls


def test_lower_failure_disables_both_actions_until_read(panel):
    panel.read()
    dpg.set_value("integral_lower_input", "104")
    panel.apply_lower()
    assert not enabled("integral_lower_apply") and not enabled("integral_apply")
    assert "stale" in dpg.get_value("integral_status")
    panel.read()
    assert enabled("integral_lower_apply") and enabled("integral_apply")
    panel.app.gpu.slot = "B"
    panel.use_lower_default()
    assert len([call for call in FakeController.calls if call[0] == "set_lower"]) == 1
