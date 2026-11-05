"""Offline rendering and refresh of configured power relationships."""
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.registry_panel import RegistryPanel


def report():
    return {"available": True, "reason": None, "identity": {"slot": "GPU-A"}, "captured_at": 1,
            "relationships": [
                {"index": 2, "type": 0, "target": 5, "weight_raw": 6144,
                 "target_metadata": {"policy": 5, "type": 3, "channel": 1, "unit": "mW",
                                     "minimum": 10000, "default": 50000, "maximum": 70000},
                 "target_reason": None},
                {"index": 7, "type": 9, "target": 30, "weight_raw": None,
                 "target_metadata": None, "target_reason": "Target absent"}]}


@pytest.fixture
def view():
    gpu = SimpleNamespace(slot=lambda: "GPU-A", read_power_relationships=Mock(return_value=(report(), None)))
    panel = RegistryPanel(SimpleNamespace(gpu=gpu))
    dpg.create_context()
    with dpg.window():
        dpg.add_input_text(tag="reg_relationships", multiline=True)
    yield panel
    dpg.destroy_context()


def test_relationships_render_known_and_unknown_rows(view):
    view.refresh_relationships()
    text = dpg.get_value("reg_relationships")
    assert "Relationship 2 -> target policy 5 (type 0)" in text
    assert "Weight: 1.5 (raw 6144)" in text
    assert "minimum 10 W; nominal 50 W; maximum 70 W" in text
    assert "Relationship 7 -> target policy 30 (type 9)" in text
    assert "uninterpreted" in text and "Target absent" in text


def test_failure_marks_history_and_gpu_switch_clears_it(view):
    view.refresh_relationships()
    view.app.gpu.read_power_relationships.return_value = ({"available": False, "reason": "busy"}, None)
    view.refresh_relationships()
    text = dpg.get_value("reg_relationships")
    assert "busy" in text and "historical" in text and "target policy 5" in text
    view.app.gpu = SimpleNamespace(slot=lambda: "GPU-B", read_power_relationships=Mock(side_effect=RuntimeError("offline")))
    view.refresh_relationships()
    text = dpg.get_value("reg_relationships")
    assert "offline" in text and "target policy 5" not in text and "historical" not in text


def test_empty_report_and_current_units(view):
    data = report()
    data["relationships"][0]["target_metadata"].update(unit="mA", minimum=1000, default=2000, maximum=3000)
    view.app.gpu.read_power_relationships.return_value = (data, None)
    view.refresh_relationships()
    assert "minimum 1 A; nominal 2 A; maximum 3 A" in dpg.get_value("reg_relationships")
    data["relationships"] = []
    view.refresh_relationships()
    assert "No configured relationships" in dpg.get_value("reg_relationships")


def test_source_mapping_only_shows_valid_enabled_references(view):
    data = report()
    data["source_references"] = [
        {"policy": 1, "available": True, "enabled": True, "relationships": [2, 7]},
        {"policy": 3, "available": True, "enabled": False, "relationships": [2]},
        {"policy": 4, "available": False, "reason": "invalid range", "enabled": True, "relationships": [7]},
    ]
    data["source_uninterpreted_policies"] = [8, 9]
    view.app.gpu.read_power_relationships.return_value = (data, None)
    view.refresh_relationships()
    text = dpg.get_value("reg_relationships")
    assert "Source policy 1 -> configured relationships: 2, 7" in text
    assert "Source policy 3: configured relationship range disabled" in text
    assert "Source policy 4: unavailable (invalid range)" in text
    assert "Source mapping uninterpreted for policy types at indices: 8, 9" in text
    assert text.index("Source policy 1") < text.index("Relationship 2")


def test_linked_source_uses_relationship_index_and_joined_target(view):
    data = report()
    data["linked_references"] = [
        {"policy": 3, "relationship": 2, "target": 5, "available": True, "reason": None},
        {"policy": 4, "relationship": 19, "target": None, "available": False, "reason": "absent"},
    ]
    view.app.gpu.read_power_relationships.return_value = (data, None)
    view.refresh_relationships()
    text = dpg.get_value("reg_relationships")
    assert "Source policy 3 -> configured relationship 2 -> target policy 5" in text
    assert "Source policy 4 -> configured relationship 19: unavailable (absent)" in text
