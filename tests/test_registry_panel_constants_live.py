"""Live OV and ALT panel behavior without hardware."""

import copy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.registry_panel import RegistryPanel


def snapshot():
    return {"identity": {"slot": "A", "device_id": 1}, "image_sha256": "image", "rows": [
        {"rail": 0, "label": "NVVDD", "role": "ALT", "equation": 3, "status": "ready", "issues": [],
         "value_uv": 20000, "stored_value_uv": 20000, "reported_uv": 1060000, "policy_uv": {}},
        {"rail": 0, "label": "NVVDD", "role": "OV", "equation": 4, "status": "ready", "issues": [],
         "value_uv": 1200000, "stored_value_uv": 1200000, "reported_uv": 1200000, "policy_uv": {}},
        {"rail": 1, "label": "MSVDD", "role": "ALT", "status": "unsupported", "issues": ["unavailable"]},
    ], "unavailable_peers": ["curve"]}


@pytest.fixture
def view(monkeypatch):
    dpg.create_context()
    app = SimpleNamespace(gpu=SimpleNamespace(slot=lambda: "A"), guard=Mock(return_value=True), log=Mock())
    panel = RegistryPanel(app)
    control = SimpleNamespace(gpu=app.gpu, read=Mock(return_value=snapshot()), set=Mock())
    monkeypatch.setitem(sys.modules, "druta.voltage_constants_live",
                        SimpleNamespace(VoltageConstantsController=lambda gpu: control))
    with dpg.window():
        dpg.add_combo([], tag="reg_constants_live_select")
        dpg.add_input_text(tag="reg_constants_live_value")
        dpg.add_button(tag="reg_constants_live_apply")
        dpg.add_button(tag="reg_constants_live_restore")
        dpg.add_text(tag="reg_constants_live_status")
        dpg.add_input_text(tag="reg_constants_live_inventory")
    yield panel, app, control
    dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration(tag)["enabled"]


def test_inventory_distinguishes_addend_and_reported_value(view):
    panel, _, _ = view
    panel.refresh_constants_live()
    text = dpg.get_value("reg_constants_live_inventory")
    assert "ALT addend: current 20 mV" in text
    assert "reported ALT 1060 mV" in text
    assert "OV constant: current 1200 mV" in text
    assert "MSVDD" in text and "Optional readbacks unavailable: curve" in text
    assert enabled("reg_constants_live_apply")


def test_apply_and_restore_are_per_role_and_preserve_fractional_mv(view):
    panel, _, control = view
    panel.refresh_constants_live()
    changed = snapshot()
    changed["rows"][0]["value_uv"] = 20123
    control.set.return_value = changed
    dpg.set_value("reg_constants_live_value", "20.123")
    panel.apply_constants_live()
    control.set.assert_called_once_with(0, "ALT", 20123)
    assert enabled("reg_constants_live_restore")
    control.set.return_value = snapshot()
    panel.restore_constants_live()
    assert control.set.call_args.args == (0, "ALT", 20000)
    dpg.set_value("reg_constants_live_select", "NVVDD (rail 0) OV constant")
    panel.select_constants_live()
    panel.restore_constants_live()
    assert control.set.call_args.args == (0, "OV", 1200000)
    dpg.set_value("reg_constants_live_value", "20.1234")
    panel.apply_constants_live()
    assert control.set.call_count == 3


def test_transient_and_target_change(view):
    panel, app, control = view
    panel.refresh_constants_live()
    class TransientError(Exception):
        kind = "transient"
    control.read.side_effect = TransientError("temporary")
    panel.refresh_constants_live()
    assert enabled("reg_constants_live_apply") and "stale" in dpg.get_value("reg_constants_live_status")
    app.gpu = SimpleNamespace(slot=lambda: "B")
    panel.refresh_constants_live()
    assert not enabled("reg_constants_live_apply")
    assert not dpg.get_value("reg_constants_live_inventory")


def test_preflight_error_and_rollback_outcomes(view):
    panel, _, control = view
    panel.refresh_constants_live()
    class InputError(Exception):
        attempted = False
        kind = "validation"
    dpg.set_value("reg_constants_live_value", "21.001")
    control.set.side_effect = InputError("invalid")
    panel.apply_constants_live()
    assert enabled("reg_constants_live_apply")
    assert dpg.get_value("reg_constants_live_value") == "21.001"
    class WriteError(Exception):
        attempted = True
        rollback = {"verified": True, "readback": snapshot()}
    control.set.side_effect = WriteError("failed")
    panel.apply_constants_live()
    assert enabled("reg_constants_live_apply") and "restored" in dpg.get_value("reg_constants_live_status")
    failed = WriteError("failed again")
    failed.rollback = {"verified": False}
    control.set.side_effect = failed
    panel.apply_constants_live()
    assert not enabled("reg_constants_live_apply")
    assert "Recovery required" in dpg.get_value("reg_constants_live_status")


def test_initial_value_tracks_image_and_role(view):
    panel, _, control = view
    panel.refresh_constants_live()
    newer = copy.deepcopy(snapshot())
    newer["image_sha256"] = "other"
    newer["rows"][0]["value_uv"] = 25000
    control.read.return_value = newer
    panel.refresh_constants_live()
    changed = copy.deepcopy(newer)
    changed["rows"][0]["value_uv"] = 27000
    control.set.return_value = changed
    panel.apply_constants_live()
    panel.restore_constants_live()
    assert control.set.call_args.args == (0, "ALT", 25000)


def test_vmin_display_apply_and_restore_are_independent_per_rail(view):
    panel, _, control = view
    observed = snapshot()
    observed["rows"].extend([
        {"rail": 0, "label": "NVVDD", "role": "VMIN", "equation": 9, "status": "ready", "issues": [],
         "value_uv": 800000, "stored_value_uv": 810000, "reported_uv": 805000, "maximum_uv": 1040000},
        {"rail": 1, "label": "MSVDD", "role": "VMIN", "equation": 271, "status": "ready", "issues": [],
         "value_uv": 790000, "stored_value_uv": 790000, "reported_uv": 790000},
    ])
    control.read.return_value = observed
    panel.refresh_constants_live()
    inventory = dpg.get_value("reg_constants_live_inventory")
    assert "VMIN constant: current 800 mV; stored 810 mV; reported VMIN 805 mV" in inventory
    assert "current maximum VMIN input 1040 mV" in inventory
    dpg.set_value("reg_constants_live_select", "NVVDD (rail 0) VMIN constant")
    panel.select_constants_live()
    changed = copy.deepcopy(observed)
    changed["rows"][-2]["value_uv"] = 801125
    control.set.return_value = changed
    dpg.set_value("reg_constants_live_value", "801.125")
    panel.apply_constants_live()
    assert control.set.call_args.args == (0, "VMIN", 801125)
    assert enabled("reg_constants_live_restore")
    control.set.return_value = observed
    panel.restore_constants_live()
    assert control.set.call_args.args == (0, "VMIN", 800000)
    dpg.set_value("reg_constants_live_select", "MSVDD (rail 1) VMIN constant")
    panel.select_constants_live()
    panel.restore_constants_live()
    assert control.set.call_args.args == (1, "VMIN", 790000)
    dpg.set_value("reg_constants_live_value", "790.0001")
    panel.apply_constants_live()
    assert control.set.call_count == 3


def test_vmin_unsupported_row_does_not_disable_other_roles(view):
    panel, _, control = view
    observed = snapshot()
    observed["rows"].append({"rail": 0, "label": "NVVDD", "role": "VMIN", "status": "unsupported",
                             "issues": ["unavailable"]})
    control.read.return_value = observed
    panel.refresh_constants_live()
    assert "VMIN constant: unsupported" in dpg.get_value("reg_constants_live_inventory")
    assert enabled("reg_constants_live_apply")
