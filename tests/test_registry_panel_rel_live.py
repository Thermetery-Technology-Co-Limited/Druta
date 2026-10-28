"""Live REL panel behavior without a GPU or native calls."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta.registry_panel import RegistryPanel


def snapshot():
    return {
        "identity": {"slot": "A", "device_id": 1},
        "image_sha256": "image",
        "rows": [
            {
                "rail": 0,
                "label": "NVVDD",
                "equation": 3,
                "status": "ready",
                "issues": [],
                "lower_uv": 900000,
                "upper_uv": 1120000,
                "stored_upper_uv": 1120000,
                "policy_uv": {"reliability": 1040000, "alt_reliability": 1060000, "effective": 1040000},
            },
            {"rail": 1, "label": "MSVDD", "equation": None, "status": "unsupported", "issues": ["unavailable"]},
        ],
    }


@pytest.fixture
def view(monkeypatch):
    dpg.create_context()
    app = SimpleNamespace(gpu=SimpleNamespace(slot=lambda: "A"), guard=Mock(return_value=True), log=Mock())
    panel = RegistryPanel(app)
    controller = SimpleNamespace(gpu=app.gpu, read=Mock(return_value=snapshot()), set=Mock())
    monkeypatch.setitem(
        sys.modules, "druta.voltage_rel_live", SimpleNamespace(VoltageRelController=lambda gpu: controller)
    )
    with dpg.window():
        dpg.add_combo([], tag="reg_rel_live_select")
        dpg.add_input_text(tag="reg_rel_live_value")
        dpg.add_button(tag="reg_rel_live_apply")
        dpg.add_button(tag="reg_rel_live_restore")
        dpg.add_text(tag="reg_rel_live_status")
        dpg.add_input_text(tag="reg_rel_live_inventory")
    yield panel, app, controller
    dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration(tag)["enabled"]


def test_transient_read_keeps_ready_peer_and_marks_stale(view):
    panel, _, controller = view
    panel.refresh_rel_live()

    class TransientError(Exception):
        kind = "transient"

    controller.read.side_effect = TransientError("temporary")
    panel.refresh_rel_live()
    assert enabled("reg_rel_live_apply")
    assert "stale" in dpg.get_value("reg_rel_live_status")
    assert "MSVDD" in dpg.get_value("reg_rel_live_inventory")


def test_absent_optional_readbacks_are_reported_without_disabling_ready_rail(view):
    panel, _, controller = view
    observed = snapshot()
    observed["unavailable_peers"] = ["boost", "curve"]
    controller.read.return_value = observed
    panel.refresh_rel_live()
    assert "Optional readbacks unavailable: boost, curve" in dpg.get_value("reg_rel_live_inventory")
    assert enabled("reg_rel_live_apply")


def test_target_change_invalidates_old_actions(view):
    panel, app, controller = view
    panel.refresh_rel_live()
    app.gpu = SimpleNamespace(slot=lambda: "B")
    controller.read.side_effect = RuntimeError("unavailable")
    panel.refresh_rel_live()
    assert not enabled("reg_rel_live_apply")
    assert panel.rel_live_snapshot is None
    assert not dpg.get_value("reg_rel_live_inventory")


def test_new_image_has_its_own_initial_bound(view):
    panel, _, controller = view
    panel.refresh_rel_live()
    newer = snapshot()
    newer["image_sha256"] = "other-image"
    newer["rows"][0]["upper_uv"] = 1100000
    controller.read.return_value = newer
    panel.refresh_rel_live()
    assert not enabled("reg_rel_live_restore")
    changed = snapshot()
    changed["image_sha256"] = "other-image"
    changed["rows"][0]["upper_uv"] = 1050000
    controller.set.return_value = changed
    panel.apply_rel_live()
    panel.restore_rel_live()
    assert controller.set.call_args.args == (0, 1100000)


def test_fractional_input_apply_and_restore_initial(view):
    panel, _, controller = view
    panel.refresh_rel_live()
    updated = snapshot()
    updated["rows"][0]["upper_uv"] = 1035123
    controller.set.return_value = updated
    dpg.set_value("reg_rel_live_value", "1035.123")
    panel.apply_rel_live()
    controller.set.assert_called_once_with(0, 1035123)
    assert enabled("reg_rel_live_restore")
    controller.set.return_value = snapshot()
    panel.restore_rel_live()
    assert controller.set.call_args.args == (0, 1120000)
    dpg.set_value("reg_rel_live_value", "1035.1234")
    panel.apply_rel_live()
    assert controller.set.call_count == 2


def test_verified_and_failed_rollback(view):
    panel, _, controller = view
    panel.refresh_rel_live()

    class FailureError(Exception):
        attempted = True
        rollback = {"verified": True, "readback": snapshot()}

    controller.set.side_effect = FailureError("failed")
    panel.apply_rel_live()
    assert enabled("reg_rel_live_apply")
    assert "restored" in dpg.get_value("reg_rel_live_status")
    failure = FailureError("failed again")
    failure.rollback = {"verified": False}
    controller.set.side_effect = failure
    panel.apply_rel_live()
    assert not enabled("reg_rel_live_apply")
    assert "Recovery required" in dpg.get_value("reg_rel_live_status")
    assert controller.set.call_count == 2


def test_rejected_before_dispatch_keeps_input_and_controls(view):
    panel, _, controller = view
    panel.refresh_rel_live()

    class InputError(Exception):
        attempted = False
        kind = "validation"

    controller.set.side_effect = InputError("outside range")
    dpg.set_value("reg_rel_live_value", "123.456")
    panel.apply_rel_live()
    assert dpg.get_value("reg_rel_live_value") == "123.456"
    assert enabled("reg_rel_live_apply")
    assert "outside range" in dpg.get_value("reg_rel_live_status")
    assert "Recovery required" not in dpg.get_value("reg_rel_live_status")
    assert controller.set.call_count == 1


def test_identity_error_before_dispatch_clears_controls(view):
    panel, _, controller = view
    panel.refresh_rel_live()

    class IdentityError(Exception):
        attempted = False
        kind = "identity"

    controller.set.side_effect = IdentityError("adapter changed")
    panel.apply_rel_live()
    assert not enabled("reg_rel_live_apply")
    assert panel.rel_live_snapshot is None
