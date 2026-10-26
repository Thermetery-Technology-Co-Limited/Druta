"""Rated clock activation, rollback and durable recovery contracts."""

import copy
import struct

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_rated_clock_runtime as rated
from tests.test_registry_overrides import FakeRegistry, transaction


def configured(monkeypatch):
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    bios = rated.inspection()
    changes = {"factory.3.rated": 2601}
    obs = rated.observation(slot=receipt["target"]["slot"])
    saved = rated.contract(monkeypatch, obs=obs, bios=bios, changes=changes)
    receipt.update(changes=changes, rated_clock_contract_version=1, rated_clock_contracts=saved)
    before, after = rated.observed(saved["before"]), rated.observed(saved["after"])
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    snapshot["rated_clock"] = copy.deepcopy(before)
    kwargs["gpu_factory"] = lambda slot: snapshot
    image_restart = kwargs["restart"]

    def restart(target):
        result = image_restart(target)
        snapshot["rated_clock"] = copy.deepcopy(after if registry.values else before)
        return result

    kwargs["restart"] = restart
    return receipt, registry, snapshot, restarts, kwargs, before, after


def test_apply_and_exact_undo_without_gpu_before_cleanup(monkeypatch):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert set(receipt["runtime_verified_fields"]) == set(receipt["changes"])
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("GPU GET before cleanup")
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert set(receipt["runtime_verified_fields"]) == set(receipt["changes"])
    assert snap["rated_clock"] == before and not reg.values and len(restarts) == 2


@pytest.mark.parametrize("failure", ["ignored", "opaque", "boost_request", "missing", "target", "native"])
def test_unexpected_driver_state_rolls_back(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def corrupt(target):
        result = restart(target)
        if reg.values:
            obs = snap["rated_clock"]
            if failure == "ignored":
                snap["rated_clock"] = copy.deepcopy(before)
            elif failure == "missing":
                snap["rated_clock"] = None
            elif failure == "opaque":
                raw = bytearray.fromhex(obs["state"]["info_hex"])
                raw[-1] ^= 1
                obs["state"]["info_hex"] = raw.hex()
            elif failure == "target":
                obs["state"]["factory"]["clock"]["identity"]["device_id"] += 1
            elif failure == "native":
                raw = bytearray.fromhex(obs["state"]["native"]["boost"])
                struct.pack_into("<I", raw, 28, 123000)
                obs["state"]["native"]["boost"] = raw.hex()
            else:
                boost = obs["state"]["factory"]["boost"]
                boost["payload_hex"] = boost["payload_hex"][:-2] + "00"
        return result

    kwargs["restart"] = corrupt
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not reg.values and snap["rated_clock"] == before and len(restarts) == 2


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("mutation", ["missing", "version", "missing_version", "empty", "request", "target", "peer"])
def test_receipt_corruption_refuses_before_write(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    saved = receipt["rated_clock_contracts"]
    if mutation == "missing":
        del receipt["rated_clock_contracts"]
    elif mutation == "version":
        receipt["rated_clock_contract_version"] = True
    elif mutation == "missing_version":
        del receipt["rated_clock_contract_version"]
    elif mutation == "empty":
        receipt["rated_clock_contracts"] = {}
    elif mutation == "request":
        receipt["changes"]["factory.3.rated"] += 1
    elif mutation == "peer":
        saved["selected"]["factory.3.rated"]["indices"] = []
    elif mutation == "target":
        for state in ("before", "after"):
            saved[state]["factory"]["clock"]["identity"]["slot"] = "0000:44:00.0"
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
def test_legacy_image_only_receipt_is_restore_only(monkeypatch, restore):
    receipt, reg, snap, _, kwargs, _, _ = configured(monkeypatch)
    receipt.pop("rated_clock_contracts")
    receipt.pop("rated_clock_contract_version")
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
        kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Premature GET")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["ok"] is restore, result["message"]
    if restore:
        assert result["state"] == "restored" and not reg.values
    else:
        assert not reg.operations


def test_actual_bios_binding_must_match_preview(monkeypatch):
    receipt, reg, _, _, kwargs, _, _ = configured(monkeypatch)
    bios = rated.inspection()
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    for field in bios["fields"]:
        field["factory_binding"]["clock_binding"]["bios_hal_selector"] += 1
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and "binding changed" in result["message"]


def test_changed_request_since_preview_refuses(monkeypatch):
    receipt, reg, snap, _, kwargs, _, _ = configured(monkeypatch)
    boost = snap["rated_clock"]["state"]["factory"]["boost"]
    boost["payload_hex"] = boost["payload_hex"][:-2] + "00"
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and "retained clock requests" in result["message"]


def test_prepare_saves_without_writing_or_unrelated_model_dependency(monkeypatch, tmp_path):
    receipt, _, _, _, _, _, _ = configured(monkeypatch)
    target = ro.Target(**receipt["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "Unrelated capability unavailable"))
    reg = FakeRegistry()
    result = ro.prepare(object(), receipt["changes"], registry=reg)
    assert result["rated_clock_contract_version"] == 1
    assert result["rated_clock_contracts"] == receipt["rated_clock_contracts"]
    assert not reg.operations and not reg.values


@pytest.mark.parametrize("name", ["named_changes", "flag_changes", "pstate_changes", "clock_vf_changes"])
def test_coupled_registry_changes_need_separate_transaction(monkeypatch, name):
    receipt, reg, _, _, kwargs, _, _ = configured(monkeypatch)
    receipt[name] = {"x": True}
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and "separately" in result["message"]
