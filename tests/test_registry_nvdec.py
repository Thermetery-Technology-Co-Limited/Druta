"""NVDEC transaction activation, exact peer failures and legacy image-only Undo."""

import copy

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_nvdec_runtime as fixtures
from tests.test_registry_overrides import digest, transaction


def configured(monkeypatch, shutdown=False):
    inspect, plan = vbios_fields.inspect, vbios_fields.plan
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    monkeypatch.setattr(vbios_fields, "inspect", inspect)
    monkeypatch.setattr(vbios_fields, "plan", plan)
    target = ro.Target(**receipt["target"])
    image = fixtures.bios()
    changes = {f"lpwr.nvdec.{i}.enabled": False for i in (range(3) if shutdown else (0,))}
    before = fixtures.observation(slot=target.slot, device=0xD123)
    saved = fixtures.make_contract(monkeypatch, image=image, obs=before, changes=changes)
    after = fixtures.observed(saved)
    patch = plan(image, changes)
    changed = bytearray(image)
    for item in patch["patches"]:
        start, raw = item["offset"], bytes.fromhex(item["after"])
        changed[start : start + len(raw)] = raw
    changed = bytes(changed)
    identity = inspect(image)["identity"]
    values = ro.compose_records(image, patch, {}, identity["vbios_version_raw"], identity["oem_revision"])
    receipt.update(
        changes=changes,
        plan=patch,
        before_image_sha256=digest(image),
        after_image_sha256=digest(changed),
        after=values,
        verify_bios=True,
        nvdec_contract_version=1,
        nvdec_contracts=saved,
    )
    snapshot.update(image=image, target=target, nvdec=copy.deepcopy(before))

    def restart(device):
        restarts.append(device)
        snapshot["image"] = changed if registry.values else image
        snapshot["nvdec"] = copy.deepcopy(after if registry.values else before)
        snapshot.pop("nvdec_error", None)
        return ro.devicereset.RestartResult(True, False, "restarted", 0)

    kwargs.update(gpu_factory=lambda slot: snapshot, bind=lambda gpu: target, restart=restart)
    return receipt, registry, snapshot, restarts, kwargs, before, after


@pytest.mark.parametrize("shutdown", [False, True])
def test_exact_nvdec_activation_and_undo(monkeypatch, shutdown):
    receipt, registry, snapshot, restarts, kwargs, before, after = configured(monkeypatch, shutdown)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == sorted(receipt["changes"])
    assert snapshot["nvdec"] == after
    kwargs["gpu_factory"] = lambda slot: snapshot if not registry.values else pytest.fail("Premature recovery GET")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert result["runtime_verified_fields"] == sorted(receipt["changes"])
    assert snapshot["nvdec"] == before and not registry.values and len(restarts) == 2


@pytest.mark.parametrize("failure", ["ignored", "peer", "unavailable", "error"])
def test_wrong_nvdec_readback_rolls_back_exactly(monkeypatch, failure):
    receipt, registry, snapshot, restarts, kwargs, before, _ = configured(monkeypatch)
    original = kwargs["restart"]

    def restart(device):
        result = original(device)
        if registry.values:
            if failure == "ignored":
                snapshot["nvdec"] = copy.deepcopy(before)
            elif failure == "peer":
                snapshot["nvdec"]["identity"]["device_id"] ^= 1
            elif failure == "unavailable":
                snapshot["nvdec"] = None
            else:
                snapshot["nvdec_error"] = "NVDEC readback unavailable"
        return result

    kwargs["restart"] = restart
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not registry.values and len(restarts) == 2
    assert snapshot["nvdec"] == before and digest(snapshot["image"]) == receipt["before_image_sha256"]


@pytest.mark.parametrize("corruption", ["version", "empty", "target", "prestate", "projection"])
def test_invalid_nvdec_preview_never_mutates(monkeypatch, corruption):
    receipt, registry, snapshot, restarts, kwargs, _, _ = configured(monkeypatch)
    if corruption == "version":
        receipt["nvdec_contract_version"] = True
    elif corruption == "empty":
        receipt["nvdec_contracts"] = {}
    elif corruption == "target":
        receipt["nvdec_contracts"]["before"]["identity"]["slot"] = "0000:00:00.0"
    elif corruption == "prestate":
        receipt["nvdec_contracts"]["before"]["identity"]["device_id"] ^= 1
    else:
        receipt["nvdec_contracts"]["after"]["controllers"]["12"]["configured_pstate_mask"] = 9
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not registry.operations and not restarts


def test_legacy_nvdec_apply_refused_but_image_undo_retained(monkeypatch):
    receipt, registry, _, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt.pop("nvdec_contract_version")
    receipt.pop("nvdec_contracts")
    assert ro.execute(receipt, **kwargs)["state"] == "failed"
    assert not registry.operations and not restarts
    registry.values = copy.deepcopy(receipt["after"])
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert not registry.values and len(restarts) == 1


def test_nvdec_preview_isolated_from_other_control_changes():
    with pytest.raises(ro.OverrideError, match="separately"):
        ro._nvdec_separate({"lpwr.nvdec.0.enabled": False, "power.0.maximum": 500000})
    with pytest.raises(ro.OverrideError, match="separately"):
        ro._nvdec_separate({"lpwr.nvdec.0.enabled": False}, pstate_changes={"all_pstates": True})
