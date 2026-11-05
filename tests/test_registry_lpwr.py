"""Offline DIFR transactions: scoped capability, exact activation and recovery."""

import copy
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from druta import lpwr_runtime as lp
from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_lpwr_runtime as fixtures
from tests.test_registry_overrides import digest, transaction


def configured(monkeypatch, *, multiple=False, slot=None, device=0xC321):
    inspect, plan = vbios_fields.inspect, vbios_fields.plan
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    monkeypatch.setattr(vbios_fields, "inspect", inspect)
    monkeypatch.setattr(vbios_fields, "plan", plan)
    if slot is not None:
        receipt["target"]["slot"] = slot
    target = ro.Target(**receipt["target"])
    image = fixtures.bios(device=device)
    changes = {"lpwr.difr.0.enabled": False}
    if multiple:
        changes["lpwr.difr.2.enabled"] = False
    before = fixtures.observation(slot=target.slot, device=device)
    saved = fixtures.make_contract(monkeypatch, image=image, obs=before, changes=changes)
    after = fixtures.observed(saved)
    patch = plan(image, changes)
    changed = bytearray(image)
    for item in patch["patches"]:
        start = item["offset"]
        changed[start : start + len(bytes.fromhex(item["before"]))] = bytes.fromhex(item["after"])
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
        lpwr_contract_version=1,
        lpwr_contracts=saved,
    )
    snapshot.update(image=image, target=target, lpwr=copy.deepcopy(before))

    def restart(device):
        restarts.append(device)
        snapshot["image"] = changed if registry.values else image
        snapshot["lpwr"] = copy.deepcopy(after if registry.values else before)
        snapshot.pop("lpwr_error", None)
        return ro.devicereset.RestartResult(True, False, "restarted", 0)

    kwargs.update(gpu_factory=lambda slot: snapshot, bind=lambda gpu: target, restart=restart)
    return receipt, registry, snapshot, restarts, kwargs, before, after


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("slot,device", [("0000:08:05.1", 0xC321), ("0000:91:10.4", 0xB987)])
def test_exact_disable_apply_and_undo_with_alternate_identity(monkeypatch, multiple, slot, device):
    receipt, reg, snap, restarts, kwargs, before, after = configured(
        monkeypatch, multiple=multiple, slot=slot, device=device
    )
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == sorted(receipt["changes"])
    assert snap["lpwr"] == after and reg.values == receipt["after"]
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Read before registry cleanup")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["lpwr"] == before and not reg.values and len(restarts) == 2
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("failure", ["ignored", "routing", "peer", "unavailable", "error"])
def test_failed_activation_restores_registry_image_and_loaded_configuration(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def incomplete(device):
        result = restart(device)
        if reg.values:
            if failure == "ignored":
                snap["lpwr"] = copy.deepcopy(before)
            elif failure == "routing":
                raw = bytearray.fromhex(snap["lpwr"]["pstate_info_hex"])
                raw[12 + 552 * 3 + 13] = 255
                snap["lpwr"]["pstate_info_hex"] = raw.hex()
                snap["lpwr"]["pstates"] = lp._pstates(raw)
            elif failure == "peer":
                controllers = copy.deepcopy(snap["lpwr"]["controllers"])
                controllers["9"]["configured_pstate_mask"] = 0
                snap["lpwr"] = fixtures.observation(slot=before["identity"]["slot"], controllers=controllers)
                snap["lpwr"]["identity"] = copy.deepcopy(before["identity"])
            elif failure == "unavailable":
                snap["lpwr"] = None
            else:
                snap["lpwr_error"] = "DIFR readback unavailable"
        return result

    kwargs["restart"] = incomplete
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert snap["lpwr"] == before and digest(snap["image"]) == receipt["before_image_sha256"]
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("failure", ["unsupported", "changed_routes", "changed_support", "identity"])
def test_preflight_loaded_configuration_failure_never_mutates(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, _, after = configured(monkeypatch)
    if failure == "unsupported":
        snap["lpwr"] = None
    elif failure == "changed_support":
        snap["lpwr"] = after
    elif failure == "identity":
        snap["lpwr"]["identity"]["device_id"] += 1
    else:
        raw = bytearray.fromhex(snap["lpwr"]["pstate_info_hex"])
        raw[12 + 552 * 12 + 12] += 1
        snap["lpwr"]["pstate_info_hex"] = raw.hex()
        snap["lpwr"]["pstates"] = lp._pstates(raw)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not result["ok"]
    assert not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "missing_version",
        "missing",
        "empty",
        "selected",
        "projection",
        "request",
        "slot",
        "image",
        "no_image",
    ],
)
def test_malformed_contract_refused_before_any_read_or_registry_cleanup(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    if mutation == "version":
        receipt["lpwr_contract_version"] = True
    elif mutation == "missing_version":
        receipt.pop("lpwr_contract_version")
    elif mutation == "missing":
        receipt.pop("lpwr_contracts")
    elif mutation == "empty":
        receipt["lpwr_contracts"] = {}
    elif mutation == "selected":
        receipt["lpwr_contracts"]["selected"]["lpwr.difr.0.enabled"]["offset"] += 1
    elif mutation == "projection":
        receipt["lpwr_contracts"]["after"]["9"]["configured_pstate_mask"] ^= 4
    elif mutation == "request":
        receipt["changes"]["lpwr.difr.0.enabled"] = True
    elif mutation == "slot":
        receipt["target"]["slot"] = "0000:11:02.5"
    elif mutation == "image":
        receipt["lpwr_contracts"]["before_image_sha256"] = "0" * 64
    else:
        receipt["verify_bios"] = False
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed receipt caused GPU access")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
def test_legacy_image_only_difr_receipt_retains_cleanup_without_new_apply(monkeypatch, restore):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt.pop("lpwr_contract_version")
    receipt.pop("lpwr_contracts")
    monkeypatch.setattr(lp, "read", lambda gpu: pytest.fail("Legacy receipt must not require new capability"))
    snap.pop("lpwr")
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
        kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("GET before cleanup")
    else:
        kwargs["gpu_factory"] = lambda slot: pytest.fail("Old DIFR apply must require a new preview")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["ok"] is restore, result["message"]
    assert not reg.values and len(restarts) == int(restore)


def test_unrelated_transaction_does_not_request_lpwr_capability(monkeypatch):
    receipt, reg, _, restarts, kwargs = transaction(monkeypatch)
    receipt.update(lpwr_contract_version=1, lpwr_contracts={})
    monkeypatch.setattr(lp, "read", lambda gpu: pytest.fail("Unrelated transaction requested LPWR"))
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert not reg.values and len(restarts) == 2


def test_failed_runtime_recovery_is_reported_after_registry_cleanup(monkeypatch):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def ignored_then_missing(device):
        result = restart(device)
        snap["lpwr"] = copy.deepcopy(before) if reg.values else None
        return result

    kwargs["restart"] = ignored_then_missing
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert any(event["event"] == "rollback-failure" for event in result["events"])


def test_undo_unavailable_readback_keeps_original_registry_restored(monkeypatch):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    restart = kwargs["restart"]

    def unavailable_after_cleanup(device):
        result = restart(device)
        snap["lpwr"] = None
        return result

    kwargs["restart"] = unavailable_after_cleanup
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert reg.operations[0] == receipt["after"]
    assert all(values == receipt["before"] for values in reg.operations[1:])
    assert digest(snap["image"]) == receipt["before_image_sha256"]


def test_scoped_read_fallback_uses_fresh_bound_gpu_without_unrelated_getters(monkeypatch):
    from druta import nvbackend

    receipt, reg, snap, _, kwargs, before, after = configured(monkeypatch)
    snap.pop("lpwr")
    gpu, created, reads = SimpleNamespace(), [], []
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: (created.append(slot), gpu)[1])
    monkeypatch.setattr(ro, "bind_target", lambda value: snap["target"] if value is gpu else pytest.fail("Wrong GPU"))

    def read(value):
        assert value is gpu
        reads.append(bool(reg.values))
        return copy.deepcopy(after if reg.values else before), None

    monkeypatch.setattr(lp, "read", read)
    restart = kwargs["restart"]

    def without_injection(device):
        result = restart(device)
        snap.pop("lpwr")
        return result

    kwargs["restart"] = without_injection
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert reads == [False, True, False]
    assert created == [receipt["target"]["slot"]] * 3


@pytest.mark.parametrize("changed", ["snapshot", "gpu"])
def test_scoped_fallback_requires_both_snapshot_and_gpu_target(monkeypatch, changed):
    from druta import nvbackend

    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    snap.pop("lpwr")
    wrong = ro.Target("0000:81:04.3", "different", "other-key")
    if changed == "snapshot":
        snap["target"] = wrong
        monkeypatch.setattr(nvbackend, "GPU", lambda slot: pytest.fail("Unbound snapshot constructed GPU"))
    else:
        monkeypatch.setattr(nvbackend, "GPU", lambda slot: SimpleNamespace())
        monkeypatch.setattr(ro, "bind_target", lambda gpu: wrong)
    monkeypatch.setattr(lp, "read", lambda gpu: pytest.fail("Unbound target requested LPWR"))
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not reg.operations and not restarts


def test_scoped_typed_read_error_is_reported_without_write(monkeypatch):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    kwargs["gpu_factory"] = lambda slot: SimpleNamespace()
    monkeypatch.setattr(lp, "read", lambda gpu: (None, "Selected DIFR GET is unavailable"))
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and "Selected DIFR GET" in result["message"]
    assert not reg.operations and not restarts


def test_transient_fresh_snapshot_read_recovery_does_not_repeat_restart_or_write(monkeypatch):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    calls, sleeps = [], []

    def fresh(slot):
        if restarts:
            calls.append(slot)
            if len(calls) <= 2:
                raise OSError("Driver initialization incomplete")
        return snap

    kwargs["gpu_factory"] = fresh
    monkeypatch.setattr(ro.time, "sleep", sleeps.append)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert len(calls) == 3 and sleeps == [0.5, 0.5]
    assert len(restarts) == 1 and reg.operations == [receipt["after"]]


def test_unrelated_raw_pstate_frequency_drift_does_not_change_routing_contract(monkeypatch):
    receipt, _, snap, _, kwargs, _, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def drift(device):
        result = restart(device)
        raw = bytearray.fromhex(snap["lpwr"]["pstate_info_hex"])
        struct.pack_into("<I", raw, 12 + 552 * 3 + 24, 999123)
        snap["lpwr"]["pstate_info_hex"] = raw.hex()
        return result

    kwargs["restart"] = drift
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"


@pytest.mark.parametrize(
    "other", ["bios", "named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes"]
)
def test_prepare_rejects_combined_difr_categories_before_reading_image(monkeypatch, other):
    monkeypatch.setattr(ro, "bind_target", lambda gpu: ro.Target("0000:90:01.2", "instance", "key"))
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: pytest.fail("Mixed transaction read image"))
    changes = {"lpwr.difr.0.enabled": False}
    kwargs = {}
    if other == "bios":
        changes["power.4.maximum"] = 900000
    else:
        kwargs[other] = {"other": 1}
    with pytest.raises(ro.OverrideError, match="separately"):
        ro.prepare(None, changes, **kwargs)


@pytest.mark.parametrize("restore", [False, True])
def test_saved_difr_receipt_cannot_mix_a_named_control(monkeypatch, restore):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt["flag_changes"] = {"perf_limits.cache": False}
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Mixed receipt read GPU")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and "separately" in result["message"]
    assert not reg.operations and not restarts


def test_prepare_durable_reload_apply_and_undo_keep_exact_runtime_binding(monkeypatch, tmp_path):
    original, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, multiple=True)
    target = ro.Target(**original["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: snap["image"])
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "Unrelated model getter unavailable"))
    prepared = ro.prepare(object(), original["changes"], registry=reg)
    path = Path(prepared["path"])
    assert not reg.operations and prepared["lpwr_contract_version"] == 1
    assert prepared["lpwr_contracts"]["before"] == before
    assert prepared["lpwr_contracts"]["before_image_sha256"] == digest(snap["image"])
    assert path.with_suffix(".rom").read_bytes() == snap["image"]
    assert path.stat().st_size < 256 * 1024
    loaded = ro.load_receipt(path)
    assert loaded == prepared
    kwargs["save"] = ro.startup.atomic_json
    result = ro.execute(loaded, **kwargs)
    assert result["state"] == "active", result["message"]
    loaded = ro.load_receipt(path)
    assert ro.active_receipt(target) == loaded
    result = ro.execute(loaded, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert ro.load_receipt(path) == result and ro.active_receipt(target) is None
    assert not reg.values and snap["lpwr"] == before and len(restarts) == 2
