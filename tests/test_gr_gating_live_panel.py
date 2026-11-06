"""UI presentation of the aggregate ELPG request and partial witnesses."""

from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.gr_gating_live_panel import GrGatingLivePanel


@pytest.fixture
def panel():
    dpg.create_context()
    app = SimpleNamespace(s=lambda value: value, log=Mock(), open_save_profile=Mock(), guard=Mock(return_value=True))
    instance = GrGatingLivePanel(app)
    with dpg.window():
        instance.build()
    try:
        yield instance
    finally:
        dpg.destroy_context()


def test_explainer_describes_driver_selected_group_request(panel):
    text = dpg.get_value("gr_gating_live_explainer")
    assert "engine-level power gating (ELPG)" in text
    assert "driver selects associated controllers" in text and "graphics and media engines" in text
    assert "Individual engines cannot be selected" in text and "partial check" in text


def test_held_snapshot_distinguishes_transitions_from_latest_predicates(panel):
    snapshot = {
        "support": True,
        "owned_request": True,
        "observed_controllers": ["12"],
        "controllers": {
            "12": {"available": True, "supported": True, "enabled_predicate": False},
            "17": {"available": True, "supported": True, "enabled_predicate": True},
            "18": {"available": False, "supported": None, "enabled_predicate": None},
            "19": {"available": True, "supported": False, "enabled_predicate": None},
        },
    }
    panel._show(snapshot, "Held.")
    lines = dpg.get_value("gr_gating_live_readback").splitlines()
    assert "LPWR controller 12, latest enabled predicate: disabled; request transition observed" in lines
    assert "LPWR controller 17, latest enabled predicate: enabled; no new request transition observed" in lines
    assert "LPWR controller 18: support readback unavailable" in lines
    assert "LPWR controller 19: unsupported" in lines
    assert any("partial readback" in line for line in lines)
    assert all("unaffected" not in line and "all NVDEC" not in line for line in lines)


def test_idle_snapshot_does_not_claim_request_transitions(panel):
    snapshot = {
        "support": True,
        "owned_request": False,
        "controllers": {"12": {"available": True, "supported": True, "enabled_predicate": None}},
    }
    panel._show(snapshot, "Ready.")
    text = dpg.get_value("gr_gating_live_readback")
    assert "latest enabled predicate: unknown" in text
    assert "request transition observed" not in text


def test_save_request_profile_targets_instance(panel):
    tag = "gr_gating_live_save_profile"
    assert dpg.get_item_callback(tag) == panel.app.open_save_profile
    assert dpg.get_item_user_data(tag) == ("lowpower_requests", "elpg", None)
