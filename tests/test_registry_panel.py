"""Registry UI behavior with real DPG items and no hardware or helper process."""

import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta import registry_panel as panel_module

SPEEDO = "registry.RMHwSpeedoOverride"


def bios_field(key="power.4.default", **updates):
    field = {
        "key": key,
        "label": "Board default",
        "group": "Power policies",
        "units": "mW",
        "current": 325000,
        "minimum": 100000,
        "maximum": 600000,
        "mask": None,
        "capability": "editable",
        "reason": "",
    }
    field.update(updates)
    return field


def model_input(name="RMHwSpeedoOverride", **updates):
    field = {
        "registry_name": name,
        "label": "Speedo",
        "cache_ready": True,
        "signed": False,
        "active": 2400,
        "raw_hardware": 2400,
        "default": 2100,
        "minimum": 2250.5,
        "maximum": 2500.5,
    }
    field.update(updates)
    return field


def receipt(state="prepared"):
    return {"path": "fake-receipt.json", "state": state, "id": "test-transaction"}


@pytest.fixture
def harness(monkeypatch):
    dpg.create_context()
    app = SimpleNamespace(
        gpu=SimpleNamespace(slot=lambda: "0000:09:03.0"),
        s=lambda value: value,
        log=Mock(),
        _closing=False,
        _stop=threading.Event(),
    )
    app.guard = Mock(side_effect=lambda: not app._closing)
    mocks = SimpleNamespace(
        recover=Mock(return_value=None),
        bind=Mock(return_value=SimpleNamespace(slot="0000:09:03.0")),
        bios=Mock(return_value=b"fake-image"),
        inspect=Mock(return_value={"sha256": "fake-digest", "fields": [bios_field()], "diagnostics": []}),
        model=Mock(return_value=({"inputs": [model_input()]}, None)),
        flags=Mock(return_value=(None, "Driver flags unavailable")),
        flag_contracts=Mock(return_value={}),
        pstates=Mock(return_value=(None, "All-P-state getter unavailable")),
        pstate_contracts=Mock(return_value={}),
        clock_vf=Mock(return_value=(None, "VF policy getter unavailable")),
        clock_vf_contracts=Mock(return_value={}),
        prepare=Mock(return_value=receipt()),
        launch=Mock(),
        admin=Mock(return_value=True),
        stop=Mock(),
    )
    overrides = panel_module.overrides
    for name, replacement in (
        ("recovery_receipt", mocks.recover),
        ("bind_target", mocks.bind),
        ("read_vbios", mocks.bios),
        ("read_model_inputs", mocks.model),
        ("prepare", mocks.prepare),
        ("launch_helper", mocks.launch),
    ):
        monkeypatch.setattr(overrides, name, replacement)
    monkeypatch.setattr(panel_module.vbios_fields, "inspect", mocks.inspect)
    monkeypatch.setattr(overrides.registry_flags, "read", mocks.flags)
    monkeypatch.setattr(overrides.registry_flags, "contracts", mocks.flag_contracts)
    monkeypatch.setattr(overrides.all_pstates_registry, "read", mocks.pstates)
    monkeypatch.setattr(overrides.all_pstates_registry, "contracts", mocks.pstate_contracts)
    monkeypatch.setattr(overrides.clock_vf_registry, "read", mocks.clock_vf)
    monkeypatch.setattr(overrides.clock_vf_registry, "contracts", mocks.clock_vf_contracts)
    monkeypatch.setattr(overrides.devicereset, "is_admin", mocks.admin)
    monkeypatch.setattr(dpg, "stop_dearpygui", mocks.stop)
    panel = panel_module.RegistryPanel(app)
    with dpg.window():
        panel.build()
    try:
        yield SimpleNamespace(panel=panel, app=app, mocks=mocks)
    finally:
        dpg.destroy_context()


def enabled(tag):
    return dpg.get_item_configuration(tag)["enabled"]


def fixed_rel_field():
    return bios_field(
        "voltage.0.REL.value",
        label="NVVDD fixed REL equation bounds",
        group="Voltage",
        units="uV",
        current=1120000,
        minimum=1,
        maximum=2147483647,
        explicit_selection=True,
        upper_clamp_binding={"lower_uv": 900000},
        detail="The initial input is the stored upper bound; final reported REL can differ.",
    )


def test_fixed_rel_edit_without_selection_does_not_prepare(harness):
    h = harness
    h.mocks.inspect.return_value["fields"] = [fixed_rel_field()]
    h.panel.refresh()
    tag = "reg_field_voltage.0.REL.value"
    assert not dpg.get_value(tag + "_select")
    dpg.set_value(tag, 1045)
    h.panel.prepare()
    h.mocks.prepare.assert_not_called()


@pytest.mark.parametrize("wanted", [1045, 1120])
def test_fixed_rel_explicit_selection_stages_both_bounds_even_with_unchanged_upper(harness, wanted):
    h = harness
    h.mocks.inspect.return_value["fields"] = [fixed_rel_field()]
    h.mocks.prepare.return_value["voltage_rel_contracts"] = {"selected": {"voltage.0.REL.value": {}}}
    h.panel.refresh()
    tag = "reg_field_voltage.0.REL.value"
    dpg.set_value(tag + "_select", True)
    dpg.set_value(tag, wanted)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(
        h.app.gpu, {"voltage.0.REL.value": wanted * 1000}, named_changes={}, flag_changes={}
    )
    preview = dpg.get_value("reg_preview")
    assert f"bounds 900..1120 -> {wanted}..{wanted} mV" in preview
    assert "linked adjustments, offsets and voltage steps" in preview


def test_fixed_rel_can_be_deselected_after_preview(harness):
    h = harness
    h.mocks.inspect.return_value["fields"] = [fixed_rel_field()]
    h.panel.refresh()
    tag = "reg_field_voltage.0.REL.value"
    dpg.set_value(tag + "_select", True)
    h.panel.prepare()
    assert h.panel.preview is not None
    dpg.set_value(tag + "_select", False)
    h.panel.invalidate()
    h.mocks.prepare.reset_mock()
    h.panel.prepare()
    h.mocks.prepare.assert_not_called()
    assert h.panel.preview is None


def test_linked_current_preview_explains_firmware_readback_and_lower_votes(harness):
    h = harness
    key = "power.7.linked_current"
    h.mocks.inspect.return_value["fields"] = [bios_field(key, label="Linked current limit", units="mA", current=45000)]
    h.mocks.prepare.return_value["linked_current_contracts"] = {"schema": 1}
    h.panel.refresh()
    dpg.set_value("reg_field_" + key, 44)
    h.panel.prepare()
    assert h.mocks.prepare.call_args.args[1] == {key: 44000}
    text = dpg.get_value("reg_preview")
    assert "linked-current cache and firmware-reported value must match" in text
    assert "lower firmware vote" in text
    assert "Other loaded power definitions and requests must stay exact" in text


def gate_reader(h, monkeypatch, initial=True):
    snapshot = {
        "enabled": initial,
        "identity": {"slot": h.app.gpu.slot(), "architecture": 8, "device_id": 1234, "subsystem_id": 5678},
    }
    reader = SimpleNamespace(gpu=h.app.gpu, read=Mock(return_value=copy.deepcopy(snapshot)))

    def change(value, *, expected):
        assert expected is snapshot["enabled"]
        snapshot["enabled"] = value
        reader.read.return_value = copy.deepcopy(snapshot)
        return {"readback": copy.deepcopy(snapshot)}

    reader.set_enabled = Mock(side_effect=change)
    factory = Mock(return_value=reader)
    monkeypatch.setattr(panel_module, "VoltageRequestGate", factory)
    return reader, factory


def test_voltage_gate_read_apply_and_restore_initial(harness, monkeypatch):
    h = harness
    reader, factory = gate_reader(h, monkeypatch)
    assert not enabled("reg_voltage_gate_value") and not enabled("reg_voltage_gate_apply")
    reader.read.assert_not_called()
    h.panel.refresh_voltage_gate()
    assert enabled("reg_voltage_gate_apply") and not enabled("reg_voltage_gate_restore")
    reader.set_enabled.assert_not_called()
    dpg.set_value("reg_voltage_gate_value", False)
    h.panel.apply_voltage_gate()
    reader.set_enabled.assert_called_once_with(False, expected=True)
    assert enabled("reg_voltage_gate_restore")
    h.panel.refresh_voltage_gate()
    assert h.panel.voltage_gate_initial is True
    h.panel.restore_voltage_gate()
    assert reader.set_enabled.call_args.args == (True,)
    assert reader.set_enabled.call_args.kwargs == {"expected": False}
    assert not enabled("reg_voltage_gate_restore")
    assert "without a GPU restart" in dpg.get_value("reg_voltage_gate_status")
    factory.assert_called_once_with(h.app.gpu)


@pytest.mark.parametrize("stage", ["constructor", "read", "set"])
def test_voltage_gate_failure_disables_writes_without_losing_registry_preview(harness, monkeypatch, stage):
    h = harness
    prepare_speedo(h)
    preview = h.panel.preview
    reader, factory = gate_reader(h, monkeypatch)
    if stage == "constructor":
        factory.side_effect = RuntimeError("unavailable")
    elif stage == "read":
        reader.read.side_effect = RuntimeError("unavailable")
    h.panel.refresh_voltage_gate()
    if stage == "set":
        reader.set_enabled.side_effect = RuntimeError("unavailable; rollback verified")
        h.panel.apply_voltage_gate()
    assert h.panel.voltage_gate_snapshot is None
    assert all(
        not enabled(tag) for tag in ("reg_voltage_gate_value", "reg_voltage_gate_apply", "reg_voltage_gate_restore")
    )
    assert "unavailable" in dpg.get_value("reg_voltage_gate_status")
    assert h.panel.preview is preview and enabled("reg_apply_button")


@pytest.mark.parametrize("switch", ["object", "slot"])
def test_voltage_gate_target_switch_requires_fresh_read(harness, monkeypatch, switch):
    h = harness
    reader, factory = gate_reader(h, monkeypatch)
    h.panel.refresh_voltage_gate()
    if switch == "object":
        h.app.gpu = SimpleNamespace(slot=lambda: "0000:04:00.0")
    else:
        h.app.gpu.slot = lambda: "0000:04:00.0"
    h.panel.apply_voltage_gate()
    reader.set_enabled.assert_not_called()
    assert h.panel.voltage_gate_snapshot is None
    if switch == "object":
        reader2, factory2 = gate_reader(h, monkeypatch, initial=False)
        h.panel.refresh_voltage_gate()
        factory2.assert_called_once_with(h.app.gpu)
        assert h.panel.voltage_gate_initial is False
        reader2.set_enabled.assert_not_called()


def test_voltage_gate_locked_ui_does_not_dispatch(harness, monkeypatch):
    h = harness
    reader, _ = gate_reader(h, monkeypatch)
    h.panel.refresh_voltage_gate()
    h.app.guard.side_effect = None
    h.app.guard.return_value = False
    h.panel.apply_voltage_gate()
    h.panel.restore_voltage_gate()
    reader.set_enabled.assert_not_called()


def test_voltage_gate_initial_state_survives_panel_rebuild_and_adapter_switch(harness, monkeypatch):
    h = harness
    reader, _ = gate_reader(h, monkeypatch)
    h.panel.refresh_voltage_gate()
    dpg.set_value("reg_voltage_gate_value", False)
    h.panel.apply_voltage_gate()
    h.panel = panel_module.RegistryPanel(h.app)
    gate_reader(h, monkeypatch, initial=False)
    h.panel.refresh_voltage_gate()
    assert h.panel.voltage_gate_initial is True and enabled("reg_voltage_gate_restore")
    original_gpu = h.app.gpu
    h.app.gpu = SimpleNamespace(slot=lambda: "0000:04:00.0")
    gate_reader(h, monkeypatch, initial=False)
    h.panel.refresh_voltage_gate()
    assert h.panel.voltage_gate_initial is False and not enabled("reg_voltage_gate_restore")
    h.app.gpu = original_gpu
    gate_reader(h, monkeypatch, initial=False)
    h.panel.refresh_voltage_gate()
    assert h.panel.voltage_gate_initial is True and enabled("reg_voltage_gate_restore")


def arbitration_report():
    vector = {
        "available": True,
        "method": "minimum",
        "output": 360000,
        "inputs": [{"client": 254, "value": 450000}, {"client": 8, "value": 360000}],
    }
    return {
        "available": True,
        "identity": {"slot": "0000:09:03.0"},
        "policies": [
            {
                "available": True,
                "policy": 7,
                "type": 5,
                "channel": 7,
                "unit": "mW",
                "requested": 450000,
                "effective": 360000,
                "delta": 0,
                "host": copy.deepcopy(vector),
                "pmu": copy.deepcopy(vector),
                "observations": [],
            }
        ],
    }


def test_arbitration_read_does_not_touch_preview_and_labels_votes(harness):
    h = harness
    prepare_speedo(h)
    preview = h.panel.preview
    h.app.gpu.read_power_arbitration = Mock(return_value=(arbitration_report(), None))
    h.panel.refresh_arbitration()
    text = dpg.get_value("reg_arbitration")
    assert "Requested 450 W; effective 360 W" in text
    assert "client 8: 360 W" in text and "policy 8" not in text
    assert "Read sequentially" in text and "may still be updating" in text
    assert h.panel.preview is preview and enabled("reg_apply_button")


def test_arbitration_failure_retains_explicit_history_but_adapter_switch_clears_it(harness):
    h = harness
    h.app.gpu.read_power_arbitration = Mock(return_value=(arbitration_report(), None))
    h.panel.refresh_arbitration()
    h.app.gpu.read_power_arbitration.return_value = (None, "transient GET failure")
    h.panel.refresh_arbitration()
    text = dpg.get_value("reg_arbitration")
    assert "historical" in text and "Requested 450 W" in text
    h.app.gpu = SimpleNamespace(
        slot=lambda: "0000:04:00.0", read_power_arbitration=Mock(return_value=(None, "unavailable"))
    )
    h.panel.refresh_arbitration()
    text = dpg.get_value("reg_arbitration")
    assert "historical" not in text and "450 W" not in text


def test_arbitration_partial_vectors_and_sentinel_remain_explicit():
    report = arbitration_report()
    row = report["policies"][0]
    row["host"] = {"available": False, "reason": "unknown method"}
    row["pmu"]["output"] = 0xFFFFFFFF
    row["unit"] = None
    text = "\n".join(panel_module.RegistryPanel.arbitration_lines(report))
    assert "Host: unavailable (unknown method)" in text
    assert "Firmware: minimum -> 0xFFFFFFFF (sentinel)" in text
    assert "450000 (raw units)" in text and "450 W" not in text


def test_skip_flag_is_independent_and_has_semantic_limit(harness):
    h = harness
    h.mocks.flags.return_value = ({"cache_enabled": True, "skip_updates": False}, None)
    h.panel.refresh()
    key = "flag.perf_limits.skip_updates"
    assert key in h.panel.inputs
    assert "Firmware enforcement effects remain unverified" in h.panel.fields[key]["detail"]
    edit(h.panel, key, True)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(
        h.app.gpu, {}, named_changes={}, flag_changes={"perf_limits.skip_updates": True}
    )


def edit(panel, key, value):
    tag, _factor = panel.inputs[key]
    dpg.set_value(tag, value)
    callback = dpg.get_item_callback(tag)
    assert callable(callback)
    callback(tag, value, None)


def prepare_speedo(h):
    h.panel.refresh()
    edit(h.panel, SPEEDO, 2401)
    h.panel.prepare()
    assert h.panel.preview is h.mocks.prepare.return_value
    assert enabled("reg_apply_button")


@pytest.mark.parametrize(
    ("contract_key", "required_text"),
    [
        ("voltage_contracts", ("VMIN must read back exactly", "voltage-step rounding triggers undo")),
        ("fan_policy_contracts", ("Fan-policy values must read back exactly", "not measured fan behavior")),
    ],
)
def test_preview_explains_effective_readback_contract(harness, contract_key, required_text):
    h = harness
    h.mocks.prepare.return_value[contract_key] = {"selected": {}}
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert all(part in text for part in required_text)


def test_build_discovers_recovery_without_bios_or_model_getters(harness):
    h = harness
    h.mocks.recover.assert_called_once_with("0000:09:03.0")
    h.mocks.bios.assert_not_called()
    h.mocks.model.assert_not_called()
    assert not enabled("reg_preview_button") and not enabled("reg_apply_button")


def test_recovery_remains_available_when_both_getters_fail(harness):
    h = harness
    saved = receipt("recovery-required")
    h.mocks.recover.return_value = saved
    h.mocks.bios.side_effect = RuntimeError("BIOS temporarily unavailable")
    h.mocks.model.return_value = (None, "VFE temporarily unavailable")
    h.panel.refresh()
    assert h.panel.active is saved and enabled("reg_restore_button")
    assert not enabled("reg_preview_button")
    assert "BIOS temporarily unavailable" in dpg.get_value("reg_status")
    assert "VFE temporarily unavailable" in dpg.get_value("reg_status")
    h.panel.restore()
    h.mocks.launch.assert_called_once_with(saved, restore=True)
    assert h.app._closing and h.app._stop.is_set()


def test_recovery_survives_failed_live_binding(harness):
    h = harness
    saved = receipt("recovery-required")
    h.mocks.recover.return_value = saved
    h.mocks.bind.side_effect = RuntimeError("GPU is not started")
    h.panel.refresh()
    assert h.panel.active is saved and enabled("reg_restore_button")
    h.mocks.bios.assert_not_called()
    h.mocks.model.assert_not_called()
    h.panel.restore()
    h.mocks.launch.assert_called_once_with(saved, restore=True)


def test_failed_recovery_discovery_clears_previously_selected_receipt(harness):
    h = harness
    h.mocks.recover.return_value = receipt("active")
    h.panel.recover()
    assert h.panel.active and enabled("reg_restore_button")
    h.mocks.recover.side_effect = RuntimeError("ambiguous saved targets")
    h.panel.recover()
    assert h.panel.active is None and not enabled("reg_restore_button")
    h.panel.restore()
    h.mocks.launch.assert_not_called()


def test_bios_failure_does_not_hide_model_controls(harness):
    h = harness
    h.mocks.bios.side_effect = RuntimeError("unsupported image getter")
    h.panel.refresh()
    assert list(h.panel.inputs) == [SPEEDO]
    assert enabled("reg_preview_button")
    assert h.panel.inspection is None


def test_model_failure_does_not_hide_bios_controls(harness):
    h = harness
    h.mocks.model.return_value = (None, "unsupported VFE layout")
    h.panel.refresh()
    assert list(h.panel.inputs) == ["power.4.default"]
    assert enabled("reg_preview_button")


def test_preview_explains_coupled_current_maximum(harness):
    h = harness
    h.mocks.inspect.return_value["fields"] = [bios_field(units="mA")]
    h.mocks.prepare.return_value["policy_contracts"] = {
        "power.4.default": {
            "mode": "rated-maximum",
            "policy": 4,
            "before_bounds": {"maximum": 325000},
            "after_bounds": {"maximum": 326000},
        }
    }
    h.panel.refresh()
    edit(h.panel, "power.4.default", 326)
    h.panel.prepare()
    assert "active maximum: 325 -> 326 A (follows the default limit)" in dpg.get_value("reg_preview")


@pytest.mark.parametrize("failed", ["bios", "model"])
def test_transient_group_failure_preserves_prior_fields_and_updates_other_group(harness, failed):
    h = harness
    h.panel.refresh()
    if failed == "bios":
        h.mocks.bios.side_effect = RuntimeError("transient image read")
        h.mocks.model.return_value = ({"inputs": [model_input(active=2410)]}, None)
    else:
        h.mocks.model.return_value = (None, "transient VFE read")
        h.mocks.inspect.return_value = {"sha256": "new", "fields": [bios_field(current=335000)], "diagnostics": []}
    h.panel.refresh()
    assert set(h.panel.inputs) == {SPEEDO, "power.4.default"}
    assert h.panel.fields[SPEEDO]["current"] == (2410 if failed == "bios" else 2400)
    assert h.panel.fields["power.4.default"]["current"] == (325000 if failed == "bios" else 335000)
    assert "Previous values retained" in dpg.get_value("reg_status")


def test_successful_partial_inventory_removes_absent_group_members(harness):
    h = harness
    h.panel.refresh()
    h.mocks.model.return_value = ({"inputs": []}, None)
    h.panel.refresh()
    assert SPEEDO not in h.panel.fields and SPEEDO not in h.panel.inputs
    assert "power.4.default" in h.panel.inputs


@pytest.mark.parametrize("case", ["iddq", "signed", "unready", "duplicate"])
def test_unvalidated_individual_input_is_read_only_without_hiding_other_controls(harness, case):
    h = harness
    row = model_input("RMHwSpeedo1Override", label="Speedo 1")
    if case == "iddq":
        row = model_input("RmPmgrIddqNvvddOverride", label="NVVDD IDDQ")
    if case == "signed":
        row["signed"] = True
    if case == "unready":
        row["cache_ready"] = False
    rows = [model_input(), row]
    if case == "duplicate":
        rows.append(copy.deepcopy(row))
    h.mocks.model.return_value = ({"inputs": rows}, None)
    h.panel.refresh()
    key = "registry." + row["registry_name"]
    assert h.panel.fields[key]["capability"] == "read-only" and key not in h.panel.inputs
    assert SPEEDO in h.panel.inputs and "power.4.default" in h.panel.inputs


@pytest.mark.parametrize("partial", [False, True])
def test_valid_iddq_fields_are_editable_and_preview_effective_model_contract(harness, partial):
    from tests.test_model_registry import MSVDD, NVVDD, observation

    h = harness
    vfe = observation(names=(NVVDD,) if partial else (NVVDD, MSVDD), slot=h.app.gpu.slot())
    h.mocks.model.return_value = (vfe, None)
    h.panel.refresh()
    key = "registry." + NVVDD
    assert key in h.panel.inputs and "power.4.default" in h.panel.inputs
    assert "physical units are unresolved" in h.panel.fields[key]["detail"]
    value = h.panel.fields[key]["current"] + 1
    edit(h.panel, key, value)
    h.mocks.prepare.return_value["model_peer_contracts"] = {"selected": {NVVDD: value}}
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(h.app.gpu, {}, named_changes={NVVDD: value}, flag_changes={})
    assert "Evaluated model inputs and cached overrides must match" in dpg.get_value("reg_preview")
    assert "physical units remain unresolved" in dpg.get_value("reg_preview")


def test_fan_preview_identifies_stored_inactive_coordinate(harness):
    h = harness
    h.mocks.prepare.return_value["fan_policy_contracts"] = {
        "fan_curve.5.1.level": {
            "index": 5,
            "role": "level",
            "output_mode": {"coordinate": "rpm", "selected_active": False},
        }
    }
    prepare_speedo(h)
    assert "Policy 5: stored duty coordinate; currently controls RPM." in dpg.get_value("reg_preview")


@pytest.mark.parametrize(
    "key,expected",
    [
        ("fan_stop_contracts", "Zero-RPM support and enable flags change across every affected fan policy"),
        ("clock_bios_contracts", "Clock bounds must match the driver's loaded domain table"),
    ],
)
def test_preview_explains_loaded_clock_and_global_stop_checks(harness, key, expected):
    h = harness
    h.mocks.prepare.return_value[key] = {"present": True}
    prepare_speedo(h)
    assert expected in dpg.get_value("reg_preview")


def test_named_only_prepare_does_not_read_bios(harness):
    h = harness
    h.mocks.bios.side_effect = RuntimeError("unsupported image getter")
    h.panel.refresh()
    h.mocks.bios.reset_mock()
    h.mocks.inspect.reset_mock()
    edit(h.panel, SPEEDO, 2401)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(h.app.gpu, {}, named_changes={"RMHwSpeedoOverride": 2401}, flag_changes={})
    h.mocks.bios.assert_not_called()
    h.mocks.inspect.assert_not_called()
    assert enabled("reg_apply_button") and "2400 -> 2401" in dpg.get_value("reg_preview")


def test_combined_prepare_preserves_units_and_separates_changes(harness):
    h = harness
    h.panel.refresh()
    edit(h.panel, "power.4.default", 326)
    edit(h.panel, SPEEDO, 2401)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(
        h.app.gpu, {"power.4.default": 326000}, named_changes={"RMHwSpeedoOverride": 2401}, flag_changes={}
    )
    assert "325 -> 326 W" in dpg.get_value("reg_preview")


def test_bios_change_since_read_refuses_prepare(harness):
    h = harness
    h.panel.refresh()
    edit(h.panel, "power.4.default", 326)
    h.mocks.inspect.return_value = {"sha256": "changed", "fields": [], "diagnostics": []}
    h.panel.prepare()
    h.mocks.prepare.assert_not_called()
    assert h.panel.preview is None and not enabled("reg_apply_button")
    assert "running BIOS changed" in dpg.get_value("reg_status")


def test_checkbox_edit_invalidates_preview_and_keeps_boolean(harness):
    h = harness
    field = bios_field("power.4.enabled", current=1, units="bool", mask=1)
    h.mocks.inspect.return_value["fields"].append(field)
    prepare_speedo(h)
    edit(h.panel, field["key"], False)
    assert h.panel.preview is None and not enabled("reg_apply_button")
    h.panel.prepare()
    assert h.mocks.prepare.call_args.args[1][field["key"]] is False


def test_numeric_edit_invalidates_existing_preview(harness):
    h = harness
    prepare_speedo(h)
    edit(h.panel, SPEEDO, 2402)
    assert h.panel.preview is None and not enabled("reg_apply_button")
    h.panel.apply()
    h.mocks.launch.assert_not_called()
    assert "Preview changes before applying" in dpg.get_value("reg_preview")


def test_failed_refresh_invalidates_existing_preview(harness):
    h = harness
    prepare_speedo(h)
    h.mocks.bind.side_effect = RuntimeError("adapter no longer available")
    h.panel.refresh()
    assert h.panel.preview is None and not enabled("reg_apply_button")
    h.panel.apply()
    h.mocks.launch.assert_not_called()


def test_prepare_failure_leaves_apply_disabled(harness):
    h = harness
    prepare_speedo(h)
    edit(h.panel, SPEEDO, 9999)
    h.mocks.prepare.side_effect = RuntimeError("outside reported range")
    h.panel.prepare()
    assert h.panel.preview is None and not enabled("reg_apply_button")
    assert "outside reported range" in dpg.get_value("reg_status")
    h.mocks.launch.assert_not_called()


def test_no_changes_and_locked_prepare_never_create_transaction(harness):
    h = harness
    h.panel.refresh()
    h.panel.prepare()
    h.mocks.prepare.assert_not_called()
    assert "No fields changed" in dpg.get_value("reg_status")
    edit(h.panel, SPEEDO, 2401)
    h.app.guard.side_effect = None
    h.app.guard.return_value = False
    h.panel.prepare()
    h.mocks.prepare.assert_not_called()


@pytest.mark.parametrize("action", ["apply", "restore"])
def test_launch_guard_precedes_admin_and_helper(harness, action):
    h = harness
    saved = receipt("active")
    h.panel.preview = h.panel.active = saved
    h.app.guard.side_effect = None
    h.app.guard.return_value = False
    getattr(h.panel, action)()
    h.mocks.admin.assert_not_called()
    h.mocks.launch.assert_not_called()
    assert not h.app._closing and not h.app._stop.is_set()


def test_nonadmin_refusal_preserves_open_application(harness):
    h = harness
    prepare_speedo(h)
    h.mocks.admin.return_value = False
    h.panel.apply()
    h.mocks.launch.assert_not_called()
    h.mocks.stop.assert_not_called()
    assert not h.app._closing and not h.app._stop.is_set()
    assert "Administrator rights" in dpg.get_value("reg_status")


def test_helper_failure_preserves_preview_and_application_for_retry(harness):
    h = harness
    prepare_speedo(h)
    saved = h.panel.preview
    h.mocks.launch.side_effect = OSError("process creation failed")
    h.panel.apply()
    assert h.panel.preview is saved and enabled("reg_apply_button")
    assert not h.app._closing and not h.app._stop.is_set()
    h.mocks.stop.assert_not_called()
    assert "process creation failed" in dpg.get_value("reg_status")


@pytest.mark.parametrize("action,restore", [("apply", False), ("restore", True)])
def test_successful_launch_stops_app_only_after_helper_start_and_blocks_repeat(harness, action, restore):
    h = harness
    saved = receipt("active" if restore else "prepared")
    h.panel.preview = h.panel.active = saved

    def launch(record, *, restore):
        assert record is saved
        assert not h.app._closing and not h.app._stop.is_set()
        h.mocks.stop.assert_not_called()

    h.mocks.launch.side_effect = launch
    getattr(h.panel, action)()
    h.mocks.launch.assert_called_once_with(saved, restore=restore)
    assert h.app._closing and h.app._stop.is_set()
    h.mocks.stop.assert_called_once_with()
    getattr(h.panel, action)()
    assert h.mocks.launch.call_count == 1


def test_diagnostics_are_independent_and_explicitly_historical(harness, monkeypatch):
    h = harness
    prepare_speedo(h)
    preview = h.panel.preview
    snapshot = {
        "present_count": 1,
        "entries": [{"index": 19, "driver_label": "RATED_TDP_MAX", "issues": []}],
        "flags": {},
        "registry_targets": [],
        "issues": [],
    }
    report = {
        "groups": {
            "limits": {"status": "ready", "snapshot": snapshot, "error": None},
            "cf_policy": {
                "status": "transient",
                "snapshot": None,
                "last_valid": snapshot,
                "error": "temporary failure",
            },
        }
    }
    reader = SimpleNamespace(gpu=h.app.gpu, read=Mock(return_value=report))
    factory = Mock(return_value=reader)
    monkeypatch.setattr(panel_module, "PerfDiagnosticsReader", factory)
    h.panel.refresh_diagnostics()
    text = dpg.get_value("reg_diagnostics")
    assert "RATED_TDP_MAX" in text and "historical" in text and "temporary failure" in text
    assert h.panel.preview is preview and enabled("reg_apply_button")
    h.panel.refresh_diagnostics()
    factory.assert_called_once_with(h.app.gpu)
    h.mocks.launch.assert_not_called()


def test_diagnostic_exception_preserves_override_controls(harness, monkeypatch):
    h = harness
    prepare_speedo(h)
    monkeypatch.setattr(panel_module, "PerfDiagnosticsReader", Mock(side_effect=RuntimeError("reader failed")))
    h.panel.refresh_diagnostics()
    assert "reader failed" in dpg.get_value("reg_diagnostics")
    assert enabled("reg_apply_button") and h.panel.preview is not None


def test_flag_only_preview_works_without_bios_or_model_inputs(harness):
    h = harness
    h.mocks.bios.side_effect = RuntimeError("No BIOS getter")
    h.mocks.model.return_value = (None, "No VFE getter")
    h.mocks.flags.return_value = ({"cache_enabled": True}, None)
    h.panel.refresh()
    assert list(h.panel.inputs) == ["flag.perf_limits.cache"]
    edit(h.panel, "flag.perf_limits.cache", False)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(h.app.gpu, {}, named_changes={}, flag_changes={"perf_limits.cache": False})
    assert enabled("reg_apply_button")
    h.mocks.bios.assert_called_once()


@pytest.mark.parametrize(
    "label,value", [("Force -50 mV startup adjustment", True), ("Suppress startup adjustment", False)]
)
def test_startup_selection_routes_explicit_value_without_editing_bios(harness, monkeypatch, label, value):
    h = harness
    key = "startup.rail_rel_adjustment"
    field = bios_field(
        key,
        label="Startup REL adjustment",
        units="selection",
        current=None,
        mask=1,
        choices={
            "Leave unchanged": None,
            "Force -50 mV startup adjustment": True,
            "Suppress startup adjustment": False,
        },
    )
    monkeypatch.setattr(h.panel, "_startup_field", lambda target: field)
    h.panel.refresh()
    assert dpg.get_value(h.panel.inputs[key][0]) == "Leave unchanged"
    h.panel.prepare()
    h.mocks.prepare.assert_not_called()
    dpg.set_value(h.panel.inputs[key][0], label)
    h.mocks.prepare.return_value = {
        **receipt(),
        "voltage_startup_contracts": {"selected": {"rail_rel_adjustment": value}},
    }
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(
        h.app.gpu, {}, named_changes={}, flag_changes={}, startup_changes={"rail_rel_adjustment": value}
    )
    preview = dpg.get_value("reg_preview")
    assert label in preview and "recovery required" in preview
    h.mocks.launch.assert_not_called()


def test_failed_startup_refresh_keeps_other_controls_and_previous_selection(harness, monkeypatch):
    h = harness
    field = bios_field(
        "startup.rail_rel_adjustment",
        units="selection",
        current=False,
        mask=1,
        choices={
            "Leave unchanged": None,
            "Force -50 mV startup adjustment": True,
            "Suppress startup adjustment": False,
        },
    )
    reader = Mock(return_value=field)
    monkeypatch.setattr(h.panel, "_startup_field", reader)
    h.panel.refresh()
    reader.side_effect = RuntimeError("temporary startup read failure")
    h.panel.refresh()
    assert "startup.rail_rel_adjustment" in h.panel.inputs and SPEEDO in h.panel.inputs
    assert "temporary startup read failure" in dpg.get_value("reg_status")


def test_transient_flag_read_keeps_established_control_and_marks_stale(harness):
    h = harness
    h.mocks.flags.return_value = ({"cache_enabled": True}, None)
    h.panel.refresh()
    assert "flag.perf_limits.cache" in h.panel.inputs
    h.mocks.flags.return_value = (None, "Temporary flag read failure")
    h.panel.refresh()
    assert "flag.perf_limits.cache" in h.panel.inputs
    assert "Temporary flag read failure" in dpg.get_value("reg_status")


def test_unvalidated_flag_is_visible_without_writable_checkbox(harness):
    h = harness
    h.mocks.flags.return_value = ({"cache_enabled": True}, None)
    h.mocks.flag_contracts.side_effect = RuntimeError("Unknown peer flag")
    h.panel.refresh()
    assert "flag.perf_limits.cache" in h.panel.fields
    assert "flag.perf_limits.cache" not in h.panel.inputs
    assert SPEEDO in h.panel.inputs


def test_all_pstate_preview_is_independent_of_bios_vfe_and_other_flags(harness):
    h = harness
    h.mocks.bios.side_effect = RuntimeError("No BIOS getter")
    h.mocks.model.return_value = (None, "No VFE getter")
    h.mocks.pstates.return_value = ({"enabled": False}, None)
    h.panel.refresh()
    key = "pstate.pstates.all_editable"
    assert list(h.panel.inputs) == [key]
    edit(h.panel, key, True)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(
        h.app.gpu, {}, named_changes={}, flag_changes={}, pstate_changes={"pstates.all_editable": True}
    )
    assert enabled("reg_apply_button")


def test_all_pstate_transient_failure_retains_established_field(harness):
    h = harness
    h.mocks.pstates.return_value = ({"enabled": False}, None)
    h.panel.refresh()
    h.mocks.pstates.return_value = (None, "Temporary Pstate read failure")
    h.panel.refresh()
    assert "pstate.pstates.all_editable" in h.panel.inputs
    assert "Temporary Pstate read failure" in dpg.get_value("reg_status")


def test_all_pstate_mixed_capability_stays_read_only(harness):
    h = harness
    h.mocks.pstates.return_value = ({"enabled": None}, None)
    h.mocks.pstate_contracts.side_effect = RuntimeError("Mixed state editability")
    h.panel.refresh()
    assert "pstate.pstates.all_editable" not in h.panel.inputs
    assert "Mixed state editability" in h.panel.fields["pstate.pstates.all_editable"]["reason"]
    assert SPEEDO in h.panel.inputs


@pytest.mark.parametrize("selected", [("monotonicity",), ("smoothening",), ("monotonicity", "smoothening")])
def test_clock_vf_preview_routes_flags_independently_of_other_getters(harness, selected):
    h = harness
    h.mocks.bios.side_effect = RuntimeError("No BIOS getter")
    h.mocks.model.return_value = (None, "No VFE getter")
    h.mocks.clock_vf.return_value = ({"vf_monotonicity_enforced": True, "vf_smoothening_enforced": True}, None)
    h.panel.refresh()
    assert set(h.panel.inputs) == {"clockvf.clock_vf.monotonicity", "clockvf.clock_vf.smoothening"}
    for name in selected:
        edit(h.panel, "clockvf.clock_vf." + name, False)
    h.panel.prepare()
    h.mocks.prepare.assert_called_once_with(
        h.app.gpu,
        {},
        named_changes={},
        flag_changes={},
        clock_vf_changes={"clock_vf." + name: False for name in selected},
    )
    assert enabled("reg_apply_button")
    assert "GPU restart required" in dpg.get_value("reg_preview")
    assert "derived VF processing mode" in dpg.get_value("reg_preview")


def test_clock_vf_transient_read_retains_fields_without_removing_other_controls(harness):
    h = harness
    h.mocks.clock_vf.return_value = ({"vf_monotonicity_enforced": True, "vf_smoothening_enforced": True}, None)
    h.panel.refresh()
    h.mocks.clock_vf.return_value = (None, "Temporary VF policy read failure")
    h.panel.refresh()
    assert "clockvf.clock_vf.monotonicity" in h.panel.inputs
    assert "clockvf.clock_vf.smoothening" in h.panel.inputs
    assert SPEEDO in h.panel.inputs
    assert "Temporary VF policy read failure" in dpg.get_value("reg_status")


def test_clock_vf_unknown_header_remains_read_only_with_other_controls_available(harness):
    h = harness
    h.mocks.clock_vf.return_value = ({"vf_monotonicity_enforced": True, "vf_smoothening_enforced": True}, None)
    h.mocks.clock_vf_contracts.side_effect = RuntimeError("Unknown VF header")
    h.panel.refresh()
    assert "clockvf.clock_vf.monotonicity" not in h.panel.inputs
    assert "clockvf.clock_vf.smoothening" not in h.panel.inputs
    assert "Unknown VF header" in h.panel.fields["clockvf.clock_vf.monotonicity"]["reason"]
    assert SPEEDO in h.panel.inputs


def test_inactive_rated_clock_preview_distinguishes_metadata_from_operating_clocks(harness):
    h = harness
    h.mocks.inspect.return_value["fields"] = [
        bios_field(
            "factory.2.rated",
            label="Base clock override",
            group="Factory clocks",
            units="MHz",
            current=0,
            minimum=1,
            maximum=65535,
            detail="0 means inactive. Restore removes your saved changes.",
        )
    ]
    h.mocks.prepare.return_value.update(rated_clock_contracts={"schema": 1})
    h.panel.refresh()
    assert dpg.get_value(h.panel.inputs["factory.2.rated"][0]) == 0
    edit(h.panel, "factory.2.rated", 2296)
    h.panel.prepare()
    assert h.mocks.prepare.call_args.args[1] == {"factory.2.rated": 2296}
    assert enabled("reg_apply_button")
    text = dpg.get_value("reg_preview")
    assert "linked by its original frequency" in text
    assert "Rated clocks are metadata" in text
    assert "GPU restart required" in text


@pytest.mark.parametrize("suffix,current,wanted", [("ALT.addend", 20000, 15000), ("OV.constant", 1200000, 1195000)])
def test_constant_voltage_preview_explains_addend_and_derived_outputs(harness, suffix, current, wanted):
    h = harness
    key = "voltage.1." + suffix
    h.mocks.inspect.return_value["fields"] = [
        bios_field(
            key, label="MSVDD " + suffix, group="Voltage", units="uV", current=current, minimum=0, maximum=2147483647
        )
    ]
    h.mocks.prepare.return_value.update(voltage_ceiling_contracts={"schema": 1})
    h.panel.refresh()
    assert dpg.get_value(h.panel.inputs[key][0]) == current / 1000
    edit(h.panel, key, wanted / 1000)
    h.panel.prepare()
    assert h.mocks.prepare.call_args.args[1] == {key: wanted}
    assert enabled("reg_apply_button")
    text = dpg.get_value("reg_preview")
    assert "ALT is an addend to REL" in text
    assert "shared voltage boost on both rails" in text
    assert "computed by firmware" in text
    assert "GPU restart required" in text


def test_rel_clamp_preview_separates_definition_and_reported_voltage(harness):
    h = harness
    key = "voltage.0.REL.maximum"
    h.mocks.inspect.return_value["fields"] = [
        bios_field(
            key,
            label="NVVDD REL upper clamp",
            group="Voltage",
            units="uV",
            current=1120000,
            minimum=900000,
            maximum=2147483647,
        )
    ]
    h.mocks.prepare.return_value.update(voltage_rel_contracts={"schema": 1})
    h.panel.refresh()
    edit(h.panel, key, 1036)
    h.panel.prepare()
    assert h.mocks.prepare.call_args.args[1] == {key: 1036000}
    assert enabled("reg_apply_button")
    text = dpg.get_value("reg_preview")
    assert "loaded REL upper clamp must match exactly" in text
    assert "Reported REL can remain below the clamp" in text
    assert "rounded to a voltage step" in text
    assert "GPU restart required" in text


def test_bit4_preview_describes_shared_capability_and_main_support(harness):
    h = harness
    h.mocks.prepare.return_value["grpg_contracts"] = {
        "schema": 6,
        "before": {"controllers": {"0": {"configured_pstate_mask": 0x120}}},
        "after": {
            "controllers": {"0": {"configured_pstate_mask": 0x100}},
            "main": {"bit4_group": {"1:0": {"available": True, "value": 1}}},
        },
    }
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert "last contributor to a shared auxiliary capability" in text
    assert "main controller remains supported" in text
    assert "P5/P8 -> P8" in text


def test_mixed_bit4_preview_describes_multiple_shared_capabilities(harness):
    h = harness
    h.mocks.prepare.return_value["grpg_contracts"] = {
        "schema": 7,
        "before": {"controllers": {"0": {"configured_pstate_mask": 0x120}}},
        "after": {
            "controllers": {"0": {"configured_pstate_mask": 0}},
            "bit1": {"roles": {str(i): {"available": False, "supported": None} for i in range(11)}},
            "main": {"bit4_group": {"1:0": {"available": True, "value": 1}}},
        },
    }
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert "multiple shared auxiliary capabilities" in text
    assert "main controller remains supported" in text
    assert "P5/P8 -> none" in text


@pytest.mark.parametrize("schema,mask,eligibility", [(6, 0x100, "P5/P8 -> P8"), (7, 0, "P5/P8 -> none")])
def test_bit4_preview_distinguishes_empty_group_from_main_support(harness, schema, mask, eligibility):
    h = harness
    h.mocks.prepare.return_value["grpg_contracts"] = {
        "schema": schema,
        "before": {"controllers": {"0": {"configured_pstate_mask": 0x120}}},
        "after": {
            "controllers": {"0": {"configured_pstate_mask": mask, "supported": True}},
            "bit1": {"roles": {str(i): {"available": False, "supported": None} for i in range(11)}},
            "main": {"bit4_group": {"1:0": {"available": False, "value": None}}},
        },
    }
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert "All readbacks for that group become unavailable" in text
    assert "main controller remains supported" in text
    assert "separate configuration readback" in text
    assert "another auxiliary role remains available" not in text
    assert eligibility in text


@pytest.mark.parametrize("family,controller", [("nvenc", "13"), ("ofa", "14"), ("nvjpg", "15")])
@pytest.mark.parametrize(
    ("supported", "mask", "expected", "other_state"),
    [
        (True, 0x100, "P5/P8 -> P8; support retained", "P5/P8 -> unavailable"),
        (True, 0, "P5/P8 -> none; support retained", "P5/P8 -> unavailable"),
        (False, None, "P5/P8 -> unavailable; support removed", "P5/P8 -> none"),
    ],
)
def test_media_lpwr_preview_distinguishes_mask_and_support_states(
    harness, family, controller, supported, mask, expected, other_state
):
    h = harness
    h.mocks.prepare.return_value["media_contracts"] = {
        "family": family,
        "before": {"controllers": {controller: {"configured_pstate_mask": 0x120}}},
        "after": {
            "controllers": {
                controller: {"supported": supported, "configured_pstate_mask": mask},
            },
        },
    }
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert f"{family.upper()} low power:" in text
    assert expected in text
    assert other_state not in text
    assert "Undo verifies the original configuration" in text


@pytest.mark.parametrize("schema", [8, 9])
@pytest.mark.parametrize("all_unavailable", [False, True])
def test_full_main_grpg_preview_describes_peer_readability(harness, schema, all_unavailable):
    h = harness
    before_roles = {str(i): {"available": False, "supported": None} for i in range(11)}
    before_roles["0"] = {"available": True, "supported": True}
    after_roles = {str(i): {"available": False, "supported": None} for i in range(11)}
    if not all_unavailable:
        after_roles["1"] = {"available": True, "supported": True}
    h.mocks.prepare.return_value["grpg_contracts"] = {
        "schema": schema,
        "before": {
            "controllers": {"0": {"configured_pstate_mask": 0x8542}},
            "bit1": {"roles": before_roles},
        },
        "after": {
            "controllers": {"0": {"supported": True, "configured_pstate_mask": 0x8540}},
            "bit1": {"roles": after_roles},
        },
    }
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert "Restore verifies the original configuration" in text
    if all_unavailable:
        assert "main controller remains supported and provides a separate configured-mask and feature readback" in text
        assert "selected auxiliary support queries all become unavailable" in text
        assert "another supported role remains readable" not in text
    else:
        assert "selected auxiliary role's support query becomes unavailable while another supported role remains readable" in text
        assert "selected auxiliary support queries all become unavailable" not in text


def test_schema7_no_peer_preview_does_not_promise_a_readable_type4_peer(harness):
    h = harness
    roles = {str(i): {"available": False, "supported": None} for i in range(11)}
    roles["0"] = {"available": True, "supported": True}
    h.mocks.prepare.return_value["grpg_contracts"] = {
        "schema": 7,
        "before": {
            "controllers": {"0": {"configured_pstate_mask": 0x8542}},
            "bit1": {"roles": roles},
        },
        "after": {
            "controllers": {"0": {"supported": True, "configured_pstate_mask": 0x8540}},
            "bit1": {"roles": {str(i): {"available": False, "supported": None} for i in range(11)}},
            "main": {"bit4_group": {"1:0": {"available": True, "value": 1}}},
        },
    }
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert "All support queries for the selected auxiliary group also become unavailable" in text
    assert "another supported role remains readable" not in text
    assert "another auxiliary role remains available" not in text
    assert "Restore verifies the original configuration" in text


@pytest.mark.parametrize(
    ("supported", "mask", "expected", "other_state"),
    [
        (True, 0x100, "P5/P8 -> P8; support retained", "P5/P8 -> unavailable"),
        (True, 0, "P5/P8 -> none; support retained", "P5/P8 -> unavailable"),
        (False, None, "P5/P8 -> unavailable; support removed", "P5/P8 -> none"),
    ],
)
def test_nvdec_preview_distinguishes_mask_and_support_states(harness, supported, mask, expected, other_state):
    h = harness
    h.mocks.prepare.return_value["nvdec_contracts"] = {
        "before": {"controllers": {"12": {"configured_pstate_mask": 0x120}}},
        "after": {
            "controllers": {
                "12": {"supported": supported, "configured_pstate_mask": mask},
            },
        },
    }
    prepare_speedo(h)
    text = dpg.get_value("reg_preview")
    assert expected in text
    assert other_state not in text
    assert "Undo verifies the original configuration" in text
