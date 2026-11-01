"""GUI request callbacks: explicit fields, stale target protection and truthful labels."""

from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta import voltage_requests_panel as panel


@pytest.fixture
def ui(monkeypatch):
    dpg.create_context()
    slot = "0000:01:00.0"
    app = SimpleNamespace(
        gpu=SimpleNamespace(slot=lambda: slot), s=lambda x: x, guard=Mock(return_value=True), log=Mock()
    )
    instance = panel.VoltageRequestsPanel(app)
    with dpg.window():
        instance.build()
    snapshot = {
        "slot": slot,
        "identity": {"slot": slot},
        "gate_enabled": True,
        "gate_error": None,
        "values": {
            "0": dict(zip(panel.requests.FIELDS, [-5000, 2000, 0, 1000, -1234, 2345], strict=True)),
            "1": dict(zip(panel.requests.FIELDS, [6000, 0, 0, 0, -3456, 4567], strict=True)),
        },
        "request": {
            "complete": True,
            "missing_indices": [],
            "boost_percent": 37,
            "policy": {
                "rails": {
                    "0": {
                        "policy_uv": {
                            "reliability": 1040000,
                            "alt_reliability": 1060000,
                            "overvoltage": 1200000,
                            "vmin": 800000,
                            "effective": 1040000,
                        }
                    }
                }
            },
            "rail_identity": {"0": {"name": "NVVDD"}, "1": {"name": "MSVDD"}},
        },
    }
    read = Mock(return_value=snapshot)
    write = Mock(return_value={"snapshot": snapshot, "verified": True, "changed": True})
    monkeypatch.setattr(panel.requests, "read", read)
    monkeypatch.setattr(panel.requests, "set_request", write)
    yield SimpleNamespace(instance=instance, app=app, snapshot=snapshot, read=read, write=write)
    dpg.destroy_context()


def test_build_is_collapsed_and_never_reads_or_writes(ui):
    assert dpg.get_value("voltage_requests_header") is False
    ui.read.assert_not_called()
    ui.write.assert_not_called()


def test_read_shows_six_inputs_truthful_vcrit_and_preserves_selected_rail(ui):
    ui.instance.refresh()
    ui.write.assert_not_called()
    assert dpg.get_value("voltage_requests_0_value") == "-5"
    assert dpg.get_value("voltage_requests_4_value") == "-1.234"
    assert "VCRIT low: -1.234 mV (stored request; effective threshold unverified)" in dpg.get_value(
        "voltage_requests_inventory"
    )
    assert "reported REL policy: 1040 mV" in dpg.get_value("voltage_requests_inventory")
    assert "reported ALT/OP policy: 1060 mV" in dpg.get_value("voltage_requests_inventory")
    assert "reported effective policy: 1040 mV" in dpg.get_value("voltage_requests_inventory")
    dpg.set_value("voltage_requests_select", "Rail 1 (MSVDD)")
    ui.instance.select()
    assert dpg.get_value("voltage_requests_0_value") == "6"
    ui.instance.refresh()
    assert dpg.get_value("voltage_requests_select") == "Rail 1 (MSVDD)"
    for position, field in enumerate(panel.requests.FIELDS):
        assert dpg.get_item_configuration(f"voltage_requests_{position}_value")["label"] == f"{field} offset (mV)"


def test_apply_only_dispatches_selected_field_with_exact_uv(ui):
    ui.instance.refresh()
    dpg.set_value("voltage_requests_select", "Rail 1 (MSVDD)")
    ui.instance.select()
    dpg.set_value("voltage_requests_5_value", "-5.001")
    ui.instance.apply(user_data="VCRIT high")
    ui.write.assert_called_once_with(
        ui.app.gpu, "1", "VCRIT high", -5001, expected=ui.snapshot, slot=ui.snapshot["slot"]
    )
    status = dpg.get_value("voltage_requests_status")
    assert "exact full request readback verified" in status and panel.requests.VCRIT_NOTE in status


@pytest.mark.parametrize("change", ["slot", "gpu"])
def test_selection_change_rejects_apply(ui, change):
    ui.instance.refresh()
    if change == "slot":
        ui.app.gpu.slot = lambda: "0000:02:00.0"
    else:
        ui.app.gpu = SimpleNamespace(slot=lambda: ui.snapshot["slot"])
    ui.instance.apply(user_data="REL")
    ui.write.assert_not_called()
    assert "selected adapter" in dpg.get_value("voltage_requests_status")


def test_selection_change_during_read_discards_snapshot(ui):
    def read(gpu, **kwargs):
        ui.app.gpu = SimpleNamespace(slot=lambda: "0000:02:00.0")
        return ui.snapshot

    ui.read.side_effect = read
    ui.instance.refresh()
    assert ui.instance.snapshot is None
    assert not dpg.get_item_configuration("voltage_requests_0_apply")["enabled"]


def test_transient_read_retains_valid_display_and_apply_dispatches_only_api(ui):
    ui.instance.refresh()
    ui.read.side_effect = panel.requests.replay.ReplayError("transient", "temporary GET failure")
    ui.instance.refresh()
    assert ui.instance.snapshot is ui.snapshot
    assert dpg.get_value("voltage_requests_0_value") == "-5"
    assert "Previous valid display retained" in dpg.get_value("voltage_requests_status")
    ui.write.assert_not_called()
    ui.write.side_effect = panel.requests.replay.ReplayError("transient", "fresh GET failed")
    dpg.set_value("voltage_requests_0_value", "5")
    ui.instance.apply(user_data="REL")
    assert "Could not apply request: fresh GET failed" in dpg.get_value("voltage_requests_status")
    assert ui.instance.snapshot is ui.snapshot


@pytest.mark.parametrize("mode", ["disabled", "unknown", "partial"])
def test_gate_or_incomplete_inventory_disables_apply_keeps_readable_inputs(ui, mode):
    if mode == "disabled":
        ui.snapshot["gate_enabled"] = False
    elif mode == "unknown":
        ui.snapshot["gate_enabled"] = None
        ui.snapshot["gate_error"] = "unavailable"
    else:
        ui.snapshot["request"].update(complete=False, missing_indices=[8])
    ui.instance.refresh()
    assert dpg.get_item_configuration("voltage_requests_0_value")["enabled"]
    assert not dpg.get_item_configuration("voltage_requests_0_apply")["enabled"]
    if mode == "disabled":
        assert "suppressed" in dpg.get_value("voltage_requests_status")
    ui.write.assert_not_called()


def test_suppression_and_readback_failure_never_show_success(ui):
    ui.instance.refresh()
    ui.write.side_effect = panel.requests.replay.ReplayError(
        "partial", "SET returned 0; unchanged from before", attempted=True
    )
    dpg.set_value("voltage_requests_0_value", "5")
    ui.instance.apply(user_data="REL")
    assert "Could not apply request" in dpg.get_value("voltage_requests_status")
    assert "unchanged from before" in dpg.get_value("voltage_requests_status")
    assert ui.instance.snapshot["values"]["0"]["REL"] == -5000


def test_noop_message_does_not_claim_applied(ui):
    ui.instance.refresh()
    ui.write.return_value.update(changed=False, verified=False)
    ui.instance.apply(user_data="REL")
    assert dpg.get_value("voltage_requests_status").startswith("No change requested")
    assert "No SET sent" in dpg.get_value("voltage_requests_status")


@pytest.mark.parametrize("value", ["nan", "inf", "0.0001", "2147483.648"])
def test_invalid_input_never_dispatches_write(ui, value):
    ui.instance.refresh()
    dpg.set_value("voltage_requests_0_value", value)
    ui.instance.apply(user_data="REL")
    ui.write.assert_not_called()


def test_guard_blocks_read_and_apply(ui):
    ui.app.guard.return_value = False
    ui.instance.refresh()
    ui.instance.apply(user_data="REL")
    ui.read.assert_not_called()
    ui.write.assert_not_called()
