"""MS transaction activation, exact peer failures and legacy image-only Undo."""

import copy

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_ms_runtime as fixtures
from tests.test_registry_overrides import digest, transaction


def configured(monkeypatch, shutdown=False):
    inspect, plan = vbios_fields.inspect, vbios_fields.plan
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    monkeypatch.setattr(vbios_fields, "inspect", inspect)
    monkeypatch.setattr(vbios_fields, "plan", plan)
    target = ro.Target(**receipt["target"])
    image = fixtures.bios()
    changes = {f"lpwr.ms.{i}.enabled": False for i in (range(3) if shutdown else (0,))}
    before = fixtures.observation(slot=target.slot, device=0xD123)
    saved = fixtures.make_contract(monkeypatch, img=image, obs=before, changes=changes)
    after = fixtures.observed(saved)
    patch = plan(image, changes)
    changed = bytearray(image)
    for item in patch["patches"]:
        start, raw = item["offset"], bytes.fromhex(item["after"])
        changed[start:start + len(raw)] = raw
    changed = bytes(changed)
    identity = inspect(image)["identity"]
    values = ro.compose_records(image, patch, {}, identity["vbios_version_raw"], identity["oem_revision"])
    receipt.update(changes=changes, plan=patch, before_image_sha256=digest(image),
                   after_image_sha256=digest(changed), after=values, verify_bios=True,
                   ms_contract_version=1, ms_contracts=saved)
    snapshot.update(image=image, target=target, ms=copy.deepcopy(before))

    def restart(device):
        restarts.append(device)
        snapshot["image"] = changed if registry.values else image
        snapshot["ms"] = copy.deepcopy(after if registry.values else before)
        snapshot.pop("ms_error", None)
        return ro.devicereset.RestartResult(True, False, "restarted", 0)

    kwargs.update(gpu_factory=lambda slot: snapshot, bind=lambda gpu: target, restart=restart)
    return receipt, registry, snapshot, restarts, kwargs, before, after


@pytest.mark.parametrize("shutdown", [False, True])
def test_exact_ms_activation_and_undo(monkeypatch, shutdown):
    receipt, registry, snapshot, restarts, kwargs, before, after = configured(monkeypatch, shutdown)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == sorted(receipt["changes"])
    assert snapshot["ms"] == after
    kwargs["gpu_factory"] = lambda slot: snapshot if not registry.values else pytest.fail("Premature recovery GET")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert result["runtime_verified_fields"] == sorted(receipt["changes"])
    assert snapshot["ms"] == before and not registry.values and len(restarts) == 2


@pytest.mark.parametrize("failure", ["ignored", "peer", "unavailable", "error"])
def test_wrong_ms_readback_rolls_back_exactly(monkeypatch, failure):
    receipt, registry, snapshot, restarts, kwargs, before, _ = configured(monkeypatch)
    original = kwargs["restart"]

    def restart(device):
        result = original(device)
        if registry.values:
            if failure == "ignored":
                snapshot["ms"] = copy.deepcopy(before)
            elif failure == "peer":
                snapshot["ms"]["identity"]["device_id"] ^= 1
            elif failure == "unavailable":
                snapshot["ms"] = None
            else:
                snapshot["ms_error"] = "MS readback unavailable"
        return result

    kwargs["restart"] = restart
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not registry.values and len(restarts) == 2
    assert snapshot["ms"] == before and digest(snapshot["image"]) == receipt["before_image_sha256"]


@pytest.mark.parametrize("failure", ["version", "empty", "identity", "projection"])
def test_invalid_ms_preview_never_mutates(monkeypatch, failure):
    receipt, registry, snapshot, restarts, kwargs, _, _ = configured(monkeypatch)
    if failure == "version":
        receipt["ms_contract_version"] = True
    elif failure == "empty":
        receipt["ms_contracts"] = {}
    elif failure == "identity":
        snapshot["ms"]["identity"]["device_id"] ^= 1
    else:
        receipt["ms_contracts"]["after"]["controllers"]["1"]["configured_pstate_mask"] = 9
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not registry.operations and not restarts


def test_legacy_ms_apply_refused_but_image_undo_retained(monkeypatch):
    receipt, registry, _, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt.pop("ms_contract_version")
    receipt.pop("ms_contracts")
    assert ro.execute(receipt, **kwargs)["state"] == "failed"
    assert not registry.operations and not restarts
    registry.values = copy.deepcopy(receipt["after"])
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert not registry.values and len(restarts) == 1


def test_ms_preview_isolated_from_other_control_changes():
    with pytest.raises(ro.OverrideError, match="separately"):
        ro._ms_separate({"lpwr.ms.0.enabled": False, "power.0.maximum": 500000})
    with pytest.raises(ro.OverrideError, match="separately"):
        ro._ms_separate({"lpwr.ms.0.enabled": False}, pstate_changes={"all_pstates": True})
