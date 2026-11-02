"""Offline presentation tests for ELCG session requests."""

from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.engine_clock_gating_panel import EngineClockGatingPanel


@pytest.fixture
def panel():
    dpg.create_context()
    app = SimpleNamespace(s=lambda value: value, guard=Mock(return_value=True), log=Mock())
    instance = EngineClockGatingPanel(app)
    with dpg.window():
        instance.build()
    try:
        yield instance
    finally:
        dpg.destroy_context()


def state(**changes):
    return {
        "support": True, "available": True, "supported_engines": 3, "eligible_engines": 3,
        "owned_request": False, "owned_mode": None, "arbitration_verified": False,
        "uncertain_outcome": False, "observed_engines": [],
    } | changes


def enabled(name):
    return dpg.get_item_configuration("engine_clock_gating_" + name)["enabled"]


def test_complete_inventory_enables_both_actions_and_explains_scope(panel):
    panel._show(state(), "Ready")
    assert enabled("hold") and enabled("hold_enabled") and not enabled("release")
    text = dpg.get_value("engine_clock_gating_explainer")
    assert "rejects conflicting profiler requests" in text
    assert "other driver mechanisms" in text and "does not guarantee physical gating" in text


def test_partial_eligible_mask_allows_disable_but_not_enable(panel):
    panel._show(state(supported_engines=7, eligible_engines=3), "Ready")
    assert enabled("hold") and not enabled("hold_enabled")
    assert "Other disable reasons are active" in dpg.get_value("engine_clock_gating_readback")


@pytest.mark.parametrize("updates", [
    {"available": False}, {"supported_engines": None}, {"eligible_engines": None},
    {"supported_engines": 0, "eligible_engines": 0},
    {"supported_engines": 1 << 64, "eligible_engines": 1 << 64},
    {"support": False},
])
def test_enable_requires_complete_understood_nonzero_inventory(panel, updates):
    panel._show(state(**updates), "Read")
    assert not enabled("hold_enabled")


def test_complete_high_bit_inventory_is_preserved(panel):
    mask = (1 << 55) | 1
    panel._show(state(supported_engines=mask, eligible_engines=mask), "Ready")
    assert enabled("hold_enabled")


@pytest.mark.parametrize("mode", ["enabled", "disabled", None])
def test_owned_request_requires_release_before_another_hold(panel, mode):
    snapshot = state(owned_request=True, owned_mode=mode, arbitration_verified=mode == "enabled")
    panel._show(snapshot, "Held")
    text = dpg.get_value("engine_clock_gating_readback")
    assert f"held {mode}" in text if mode else "held (mode unknown)" in text
    assert not enabled("hold") and not enabled("hold_enabled") and enabled("release")
    assert ("Conflicting request rejection verified" in text) is (mode == "enabled")
    assert "Observed changes on engine bits" not in text


def test_enable_callback_and_uncertain_state(panel, monkeypatch):
    controller = SimpleNamespace(hold_enabled=Mock(return_value=state(
        owned_request=True, owned_mode="enabled", arbitration_verified=True)))
    monkeypatch.setattr(panel, "_controller", lambda: controller)
    panel._show(state(), "Ready")
    panel.hold_enabled()
    controller.hold_enabled.assert_called_once_with()
    panel._show(state(uncertain_outcome=True), "Uncertain")
    assert not enabled("hold") and not enabled("hold_enabled") and enabled("release")


def test_disable_transition_remains_distinct_from_arbitration(panel):
    panel._show(state(owned_request=True, owned_mode="disabled", observed_engines=[1]), "Held")
    text = dpg.get_value("engine_clock_gating_readback")
    assert "Observed changes on engine bits: 1" in text
    assert "Conflicting request rejection verified" not in text
