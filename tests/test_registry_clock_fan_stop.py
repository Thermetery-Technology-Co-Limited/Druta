"""Transaction coverage for auxiliary clock and global fan-stop readback."""

import copy

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_clock_bios_runtime as clock
from tests import test_fan_stop_runtime as stop
from tests.test_registry_overrides import FakeRegistry, transaction


def configured_transaction(monkeypatch, kind, enabled=True):
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    slot = receipt["target"]["slot"]
    if kind == "clock":
        bios = clock.inspection()
        changes = {"clock.1.minimum": -199}
        saved = clock.contract(monkeypatch, obs=clock.observation(slot=slot), fields=bios, changes=changes)
        before, after = clock.observed(saved["before"]), clock.observed(saved["after"])
        receipt.update(
            clock_bios_contract_version=1,
            clock_bios_table_version=80,
            clock_bios_changes=copy.deepcopy(changes),
            clock_bios_contracts=saved,
        )
        name = "clock_bios"
    else:
        gpu, bios = stop.Gpu(enabled=enabled, slot=slot), stop.inspection(enabled)
        changes = {"fan.zero_rpm": not enabled}
        saved = ro.fan_stop_runtime.contracts(gpu, bios, changes)
        before = stop.observe(gpu)
        stop.apply(gpu, not enabled)
        after = stop.observe(gpu)
        receipt.update(fan_stop_contract_version=1, fan_stop_contracts=saved)
        name = "fan_stop"
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    receipt["changes"] = changes
    snapshot[name] = copy.deepcopy(before)
    kwargs["gpu_factory"] = lambda slot: snapshot
    image_restart = kwargs["restart"]

    def restart(target):
        result = image_restart(target)
        snapshot[name] = copy.deepcopy(after if registry.values else before)
        return result

    kwargs["restart"] = restart
    return receipt, registry, snapshot, restarts, kwargs, name, before, after


@pytest.mark.parametrize("kind", ["clock", "stop"])
@pytest.mark.parametrize("enabled", [False, True])
def test_apply_and_cleanup_before_recovery_get(monkeypatch, kind, enabled):
    receipt, reg, snap, restarts, kwargs, name, before, _ = configured_transaction(monkeypatch, kind, enabled)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == list(receipt["changes"])
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Recovery GET before cleanup")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert result["runtime_verified_fields"] == list(receipt["changes"])
    assert snap[name] == before and reg.values == {}
    assert len(reg.operations) == len(restarts) == 2


@pytest.mark.parametrize("kind", ["clock", "stop"])
@pytest.mark.parametrize("failure", ["ignored", "peer", "identity", "missing"])
def test_unexpected_active_state_rolls_back(monkeypatch, kind, failure):
    receipt, reg, snap, restarts, kwargs, name, before, _ = configured_transaction(monkeypatch, kind)
    restart = kwargs["restart"]

    def corrupt(target):
        result = restart(target)
        if reg.values:
            if failure == "ignored":
                snap[name] = copy.deepcopy(before)
            elif failure == "missing":
                snap[name] = None
            elif kind == "clock":
                if failure == "identity":
                    snap[name]["identity"]["device_id"] += 1
                else:
                    clock.mutate_info(snap[name], 17, 1)
            elif failure == "identity":
                snap[name]["state"]["identity"]["device_id"] += 1
            else:
                stop.change_raw(snap[name]["state"], "policy_info", 10, 1)
        return result

    kwargs["restart"] = corrupt
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert reg.values == {} and snap[name] == before and len(restarts) == 2


@pytest.mark.parametrize("kind", ["clock", "stop"])
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("mutation", ["missing", "empty", "version_bool", "target", "request"])
def test_malformed_new_contract_refuses_before_any_write(monkeypatch, kind, restore, mutation):
    receipt, reg, _, restarts, kwargs, name, _, _ = configured_transaction(monkeypatch, kind)
    contract_key = name + "_contracts"
    version_key = name + "_contract_version"
    if mutation == "missing":
        del receipt[contract_key]
    elif mutation == "empty":
        receipt[contract_key] = {}
    elif mutation == "version_bool":
        receipt[version_key] = True
    elif mutation == "request":
        receipt["changes"][next(iter(receipt["changes"]))] = -198 if kind == "clock" else True
    elif kind == "clock":
        receipt[contract_key]["before"]["identity"]["slot"] = "0000:02:00.0"
        receipt[contract_key]["after"]["identity"]["slot"] = "0000:02:00.0"
    else:
        for key in ("before_state", "after_state"):
            receipt[contract_key]["fan.zero_rpm"][key]["identity"]["slot"] = "0000:02:00.0"
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("kind", ["clock", "stop"])
@pytest.mark.parametrize("restore", [False, True])
def test_legacy_image_only_contract_is_restore_only(monkeypatch, kind, restore):
    receipt, reg, snap, _, kwargs, name, _, _ = configured_transaction(monkeypatch, kind)
    for key in list(receipt):
        if key.startswith(name + "_"):
            receipt.pop(key)
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
        kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Premature GET")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["ok"] is restore, result["message"]
    if restore:
        assert result["state"] == "restored" and reg.values == {}
    else:
        assert not reg.operations


@pytest.mark.parametrize("mutation", ["omitted_selection", "wrong_version", "missing_scope"])
def test_clock_scope_cannot_bypass_v50_readback(monkeypatch, mutation):
    receipt, reg, _, _, kwargs, _, _, _ = configured_transaction(monkeypatch, "clock")
    if mutation == "omitted_selection":
        receipt["clock_bios_changes"] = {}
    elif mutation == "wrong_version":
        receipt.update(clock_bios_table_version=64, clock_bios_changes={}, clock_bios_contracts={})
    else:
        receipt.pop("clock_bios_table_version")
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations


def test_global_stop_preflight_checks_actual_bios_coverage(monkeypatch):
    receipt, reg, _, _, kwargs, _, _, _ = configured_transaction(monkeypatch, "stop")
    actual = stop.inspection()
    actual["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    actual["fields"][-1]["policy_indices"].append(12)
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: actual)
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations
    assert "omits an affected BIOS policy" in result["message"]


@pytest.mark.parametrize("kind", ["clock", "stop"])
def test_prepare_records_contract_without_unrelated_vfe(monkeypatch, tmp_path, kind):
    template, _, _, _, _, _, _, _ = configured_transaction(monkeypatch, kind)
    target = ro.Target(**template["target"])
    gpu = object() if kind == "clock" else stop.Gpu(slot=target.slot)
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "Unavailable unrelated model"))
    registry = FakeRegistry()
    receipt = ro.prepare(gpu, template["changes"], registry=registry)
    key = "clock_bios" if kind == "clock" else "fan_stop"
    assert receipt[key + "_contract_version"] == 1 and receipt[key + "_contracts"]
    assert not registry.values and not registry.operations
