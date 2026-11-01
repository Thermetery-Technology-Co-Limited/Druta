"""Voltage constant transactions, exact activation and cleanup before GPU GET."""

import copy

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_voltage_ceiling_runtime as ceiling
from tests.test_registry_overrides import FakeRegistry, transaction


def configured(monkeypatch, role="OV"):
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    bios = ceiling.inspection(role)
    key = bios["fields"][0]["key"]
    changes = {key: bios["fields"][0]["current"] + 5000}
    obs = ceiling.observation(role, slot=receipt["target"]["slot"])
    saved = ceiling.contract(monkeypatch, role, obs=obs, bios=bios, changes=changes)
    receipt.update(changes=changes, voltage_ceiling_contract_version=1, voltage_ceiling_contracts=saved)
    before, after = saved["before"], saved["after"]
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    snapshot["voltage_ceiling"] = copy.deepcopy(before)
    kwargs["gpu_factory"] = lambda slot: snapshot
    image_restart = kwargs["restart"]

    def restart(target):
        result = image_restart(target)
        snapshot["voltage_ceiling"] = copy.deepcopy(after if registry.values else before)
        return result

    kwargs["restart"] = restart
    return receipt, registry, snapshot, restarts, kwargs, before, after


@pytest.mark.parametrize("role", ["OV", "ALT"])
def test_activation_and_cleanup_before_gpu_get(monkeypatch, role):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, role)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == list(receipt["changes"])
    assert result["runtime_voltage_policy"] == snap["voltage_ceiling"]["policy"]
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Premature recovery GET")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["voltage_ceiling"] == before and not reg.values and len(restarts) == 2


@pytest.mark.parametrize("role", ["OV", "ALT"])
@pytest.mark.parametrize("failure", ["ignored", "quantized", "boost", "peer", "opaque", "missing"])
def test_unexpected_activation_rolls_back(monkeypatch, role, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, role)
    restart = kwargs["restart"]

    def corrupt(target):
        result = restart(target)
        if reg.values:
            obs = snap["voltage_ceiling"]
            if failure == "ignored":
                snap["voltage_ceiling"] = copy.deepcopy(before)
            elif failure == "quantized":
                obs["policy"]["rails"]["0"]["policy_uv"]["overvoltage" if role == "OV" else "alt_reliability"] += 2500
            elif failure == "boost":
                obs["boost_percent"] += 1
            elif failure == "peer":
                obs["policy"]["rails"]["1"]["offsets_uv"]["vmin"] += 1
            elif failure == "opaque":
                obs["equations"]["info_hex"] = obs["equations"]["info_hex"][:-2] + "00"
            else:
                snap["voltage_ceiling"] = None
        return result

    kwargs["restart"] = corrupt
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not reg.values and snap["voltage_ceiling"] == before and len(restarts) == 2


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "mutation", ["version", "missing", "missing_version", "empty", "request", "target", "selected", "peer"]
)
def test_invalid_receipt_refuses_before_write(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    saved = receipt["voltage_ceiling_contracts"]
    if mutation == "version":
        receipt["voltage_ceiling_contract_version"] = True
    elif mutation == "missing":
        receipt.pop("voltage_ceiling_contracts")
    elif mutation == "missing_version":
        receipt.pop("voltage_ceiling_contract_version")
    elif mutation == "empty":
        receipt["voltage_ceiling_contracts"] = {}
    elif mutation == "request":
        receipt["changes"]["voltage.0.OV.constant"] += 1
    elif mutation == "target":
        receipt["target"]["slot"] = "0000:44:00.0"
    elif mutation == "selected":
        saved["selected"]["voltage.0.OV.constant"]["shared"] = True
    else:
        saved["after"]["policy"]["rails"]["1"]["boost_uv"] += 1
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
def test_legacy_image_only_receipt_remains_restore_only(monkeypatch, restore):
    receipt, reg, snap, _, kwargs, _, _ = configured(monkeypatch)
    receipt.pop("voltage_ceiling_contract_version")
    receipt.pop("voltage_ceiling_contracts")
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
        kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Premature recovery GET")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["ok"] is restore, result["message"]
    assert not reg.values


def test_changed_request_refuses_before_registry(monkeypatch):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    snap["voltage_ceiling"]["boost_percent"] = 12
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


def test_changed_bios_binding_refuses_before_registry(monkeypatch):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    bios = ceiling.inspection()
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    bios["fields"][0]["offsets"] = [2003, 2007, 2011]
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts and "binding changed" in result["message"]


@pytest.mark.parametrize("name", ["named_changes", "flag_changes", "pstate_changes", "clock_vf_changes"])
def test_other_registry_changes_cannot_mask_dependencies(monkeypatch, name):
    receipt, reg, _, _, kwargs, _, _ = configured(monkeypatch)
    receipt[name] = {"unrelated": True}
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and "separately" in result["message"]


def test_prepare_is_read_only_and_saves_the_contract(monkeypatch, tmp_path):
    receipt, _, _, _, _, _, _ = configured(monkeypatch)
    target = ro.Target(**receipt["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "unrelated unavailable"))
    reg = FakeRegistry()
    result = ro.prepare(object(), receipt["changes"], registry=reg)
    assert result["voltage_ceiling_contract_version"] == 1
    assert result["voltage_ceiling_contracts"] == receipt["voltage_ceiling_contracts"]
    assert not reg.operations and not reg.values


@pytest.mark.parametrize("boost", [1, 37, 100])
def test_nonzero_boost_refuses_apply_before_any_write_but_allows_undo(monkeypatch, boost):
    receipt, reg, snap, restarts, kwargs, before, after = configured(monkeypatch, "ALT")
    before["boost_percent"] = after["boost_percent"] = boost
    snap["voltage_ceiling"]["boost_percent"] = boost
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts
    assert "resets the nonzero voltage boost request" in result["message"]
    reg.values = copy.deepcopy(receipt["after"])
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["ok"] and result["state"] == "restored"
    assert not reg.values and snap["voltage_ceiling"]["boost_percent"] == boost
