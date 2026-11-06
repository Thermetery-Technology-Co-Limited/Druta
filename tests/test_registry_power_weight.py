"""Offline UI and registry integration for relationship-weight overrides."""
import copy
from unittest.mock import Mock

import dearpygui.dearpygui as dpg

from druta import power_weight_runtime as weight
from druta import registry_overrides as ro
from tests.test_registry_overrides import transaction
from tests.test_registry_panel import bios_field, harness  # noqa: F401


def test_relationship_readback_displays_full_q12_precision():
    from druta.registry_panel import RegistryPanel
    from tests.test_power_weight_runtime import setup_case

    _, observation = setup_case()
    observation['relationships'][0]['weight_raw'] = 4097
    assert '  Weight: 1.000244140625 (raw 4097)' in RegistryPanel.relationship_lines(observation)


def test_weight_precision_and_unchanged_input(harness, monkeypatch):  # noqa: F811
    h = harness
    field = bios_field("power_relationship.9.weight", label="Relationship 9 weight", units="multiplier",
                       current=1, raw=4096, minimum=0, maximum=65535 / 4096)
    h.mocks.inspect.return_value["fields"] = [field]
    monkeypatch.setattr(weight, "annotate_fields", lambda gpu, inspection: (inspection, None))
    h.panel.refresh()
    tag = "reg_field_power_relationship.9.weight"
    assert dpg.get_item_configuration(tag)["format"] == "%.12f"
    assert h.panel.format_field_value(field, 4097 / 4096) == "1.000244140625"
    h.panel.prepare()
    h.mocks.prepare.assert_not_called()
    dpg.set_value(tag, 4097 / 4096)
    h.panel.prepare()
    assert h.mocks.prepare.call_args.args[1] == {"power_relationship.9.weight": 4097 / 4096}
    assert "1 -> 1.000244140625 multiplier" in dpg.get_value("reg_preview")


def configured(monkeypatch):
    receipt, reg, snap, restarts, kwargs = transaction(monkeypatch)
    key = "power_relationship.9.weight"
    old, new = {"identity": {"slot": receipt["target"]["slot"]}, "value": 4096}, {"identity": {"slot": receipt["target"]["slot"]}, "value": 4097}
    saved = {"before": old, "selected": {key: {"after_raw": 4097}}}
    receipt.update(changes={key: 4097 / 4096}, power_weight_contract_version=1, power_weight_contracts=saved)
    snap["power_weight"] = copy.deepcopy(old)
    monkeypatch.setattr(weight, "validate_contracts", lambda changes, contract: None if contract else (_ for _ in ()).throw(ValueError("missing contract")))
    monkeypatch.setattr(weight, "validate_inspection", lambda *args: None)
    calls = []
    def verify(observation, contract, *, restore=False):
        calls.append((copy.deepcopy(observation), restore))
        if observation != (old if restore else new):
            raise ValueError("wrong INFO readback")
        return [key]
    monkeypatch.setattr(weight, "verify", verify)
    kwargs["gpu_factory"] = lambda slot: snap
    restart = kwargs["restart"]
    def refresh(device):
        result = restart(device)
        snap["power_weight"] = copy.deepcopy(new if reg.values else old)
        return result
    kwargs["restart"] = refresh
    return receipt, reg, snap, restarts, kwargs, calls


def test_preflight_apply_and_restore_use_weight_observations(monkeypatch):
    receipt, reg, snap, _, kwargs, calls = configured(monkeypatch)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert [call[1] for call in calls] == [True, False]
    assert "power_relationship.9.weight" in result["runtime_verified_fields"]
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert calls[-1][1] is True and not reg.values


def test_failed_post_refresh_check_rolls_back_and_verifies_old(monkeypatch):
    receipt, reg, snap, restarts, kwargs, calls = configured(monkeypatch)
    refresh = kwargs["restart"]
    def wrong_after(device):
        result = refresh(device)
        if reg.values:
            snap["power_weight"]["value"] = 777
        return result
    kwargs["restart"] = wrong_after
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not reg.values and len(restarts) == 2
    assert [call[1] for call in calls] == [True, False, True]


def test_missing_contract_blocks_mutation(monkeypatch):
    receipt, reg, _, restarts, kwargs, _ = configured(monkeypatch)
    receipt.pop("power_weight_contracts")
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


def test_real_weight_contract_applies_and_restores_offline(monkeypatch):
    import struct

    from druta import vbios_fields
    from tests.test_power_weight_runtime import setup_case

    receipt, reg, snap, restarts, kwargs = transaction(monkeypatch)
    bios, before = setup_case()
    before["identity"]["slot"] = receipt["target"]["slot"]
    key = "power_relationship.2.weight"
    changes = {key: 4097 / 4096}
    monkeypatch.setattr(weight, "read", lambda gpu: (copy.deepcopy(before), None))
    saved = weight.contracts(None, bios, changes)
    receipt.update(changes=changes, power_weight_contract_version=1, power_weight_contracts=saved)
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    after = copy.deepcopy(before)
    raw = bytearray.fromhex(after["info_hex"])
    struct.pack_into("<H", raw, 0x41D0 + 2 * 0x24 + 6, 4097)
    after["info_hex"] = raw.hex()
    after["relationships"][0]["weight_raw"] = 4097
    snap["power_weight"] = copy.deepcopy(before)
    kwargs["gpu_factory"] = lambda slot: snap
    restart = kwargs["restart"]
    def refresh(device):
        result = restart(device)
        snap["power_weight"] = copy.deepcopy(after if reg.values else before)
        return result
    kwargs["restart"] = refresh
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == [key]
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["power_weight"] == before and not reg.values and len(restarts) == 2


def test_fresh_snapshot_without_weight_reopens_paired_gpu(monkeypatch):
    from druta import nvbackend

    receipt, reg, snap, _, kwargs, calls = configured(monkeypatch)
    target = ro.Target(**receipt["target"])
    snap["target"] = target
    snap.pop("power_weight")
    gpu = object()
    opened = []
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: opened.append(slot) or gpu)
    monkeypatch.setattr(ro, "bind_target", lambda item: target if item is gpu else None)
    before = copy.deepcopy(receipt["power_weight_contracts"]["before"])
    after = copy.deepcopy(before)
    after["value"] = 4097
    monkeypatch.setattr(weight, "read", lambda item: (copy.deepcopy(after if reg.values else before), None))
    # Keep restart from adding a synthetic observation; the production snapshot has no weight field.
    restart = kwargs["restart"]
    def refresh(device):
        result = restart(device)
        snap.pop("power_weight", None)
        return result
    kwargs["restart"] = refresh
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert opened == [target.slot] * 3
    assert [call[1] for call in calls] == [True, False, True]


def test_fallback_rejects_changed_adapter_before_weight_read(monkeypatch):
    from druta import nvbackend

    receipt, reg, snap, restarts, kwargs, _ = configured(monkeypatch)
    target = ro.Target(**receipt["target"])
    snap["target"] = target
    snap.pop("power_weight")
    gpu = object()
    read = Mock()
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: gpu)
    monkeypatch.setattr(ro, "bind_target", lambda item: ro.Target(target.slot, "changed", target.driver_key))
    monkeypatch.setattr(weight, "read", read)
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts
    assert "driver mapping changed" in result["message"]
    read.assert_not_called()
