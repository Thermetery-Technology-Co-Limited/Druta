"""Offline linked-current transaction activation and exact recovery contracts."""

import copy
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from druta import linked_current_runtime as linked
from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_linked_current_runtime as fixtures
from tests.test_registry_overrides import transaction


def configured(monkeypatch, *, source=7, target=9, relationship=2, partial=False):
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    obs = fixtures.observation(
        source=source, target=target, relationship=relationship, slot=receipt["target"]["slot"], partial=partial
    )
    bios = fixtures.inspection(source=source, target=target, relationship=relationship)
    key = f"power.{source}.linked_current"
    changes = {key: 44000}
    saved = fixtures.contract(monkeypatch, obs=obs, bios=bios, changes=changes)
    before, after = fixtures.observed(saved, restore=True), fixtures.observed(saved)
    receipt.update(changes=changes, linked_current_contract_version=1, linked_current_contracts=saved)
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    snapshot["linked_current"] = copy.deepcopy(before)
    kwargs["gpu_factory"] = lambda slot: snapshot
    image_restart = kwargs["restart"]

    def restart(device):
        result = image_restart(device)
        snapshot["linked_current"] = copy.deepcopy(after if registry.values else before)
        return result

    kwargs["restart"] = restart
    return receipt, registry, snapshot, restarts, kwargs, before, after, bios


def replace_cached(observation, source, value):
    raw = bytearray.fromhex(observation["control_hex"])
    struct.pack_into("<I", raw, linked.CONTROL_BASE + source * linked.CONTROL_STRIDE + 0x28, value)
    observation["control_hex"] = raw.hex()


def replace_status(observation, source, value):
    observation["status_rows"][str(source)]["linked_current_hex"] = struct.pack("<f", value).hex()


@pytest.mark.parametrize("source,target,relationship", [(7, 9, 2), (4, 12, 5)])
@pytest.mark.parametrize("partial", [False, True])
def test_exact_apply_and_undo_resolve_relationship_and_preserve_partial_inventory(
    monkeypatch, source, target, relationship, partial
):
    receipt, reg, snap, restarts, kwargs, before, after, _ = configured(
        monkeypatch, source=source, target=target, relationship=relationship, partial=partial
    )
    assert source != target != relationship
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == list(receipt["changes"])
    assert snap["linked_current"] == after and reg.values == receipt["after"]
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("GPU read before registry undo")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["linked_current"] == before and not reg.values and len(restarts) == 2
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("failure", ["cache", "status", "rounded_status", "unrelated_control", "unavailable"])
def test_incomplete_or_changed_activation_rolls_back_full_registry_and_runtime(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def incomplete(device):
        outcome = restart(device)
        if reg.values:
            obs = snap["linked_current"]
            if failure == "cache":
                replace_cached(obs, 7, 45000)
            elif failure == "status":
                replace_status(obs, 7, 45000)
            elif failure == "rounded_status":
                replace_status(obs, 7, 44001)
            elif failure == "unrelated_control":
                raw = bytearray.fromhex(obs["control_hex"])
                raw[-1] ^= 1
                obs["control_hex"] = raw.hex()
            else:
                snap["linked_current"] = None
        return outcome

    kwargs["restart"] = incomplete
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not reg.values
    assert snap["linked_current"] == before and len(restarts) == 2
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "mutation", ["version", "missing", "missing_version", "empty", "projection", "request", "slot"]
)
def test_malformed_receipt_is_rejected_before_gpu_or_registry_cleanup(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _, _ = configured(monkeypatch)
    if mutation == "version":
        receipt["linked_current_contract_version"] = True
    elif mutation == "missing":
        receipt.pop("linked_current_contracts")
    elif mutation == "missing_version":
        receipt.pop("linked_current_contract_version")
    elif mutation == "empty":
        receipt["linked_current_contracts"] = {}
    elif mutation == "projection":
        raw = bytearray.fromhex(receipt["linked_current_contracts"]["after_control_hex"])
        raw[-1] ^= 1
        receipt["linked_current_contracts"]["after_control_hex"] = raw.hex()
    elif mutation == "request":
        receipt["changes"]["power.7.linked_current"] += 1
    else:
        receipt["target"]["slot"] = "0000:44:00.0"
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed receipt caused GPU access")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


def test_changed_bios_relationship_target_is_rejected_before_mutation(monkeypatch):
    receipt, reg, _, restarts, kwargs, _, _, bios = configured(monkeypatch)
    changed = fixtures.inspection(source=7, target=12, relationship=2)
    changed["identity"] = bios["identity"]
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: changed)
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
def test_old_image_only_receipt_allows_cleanup_but_not_new_apply(monkeypatch, restore):
    receipt, reg, snap, restarts, kwargs, _, _, _ = configured(monkeypatch)
    receipt.pop("linked_current_contract_version")
    receipt.pop("linked_current_contracts")
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
        restart = kwargs["restart"]

        def without_runtime(device):
            result = restart(device)
            snap.pop("linked_current", None)
            return result

        kwargs["restart"] = without_runtime
        kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Recovery read before cleanup")
    else:
        kwargs["gpu_factory"] = lambda slot: pytest.fail("Legacy apply attempted a GPU read")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["ok"] is restore, result["message"]
    assert not reg.values and len(restarts) == int(restore)


def test_failed_runtime_recovery_is_explicit_after_registry_is_restored(monkeypatch):
    receipt, reg, snap, restarts, kwargs, before, _, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def ignored_then_unavailable(device):
        result = restart(device)
        snap["linked_current"] = copy.deepcopy(before) if reg.values else None
        return result

    kwargs["restart"] = ignored_then_unavailable
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert any(item["event"] == "rollback-failure" for item in result["events"])


def test_transient_fresh_process_reads_do_not_repeat_restart_or_registry_write(monkeypatch):
    receipt, reg, snap, restarts, kwargs, _, _, _ = configured(monkeypatch)
    calls, sleeps = [], []

    def fresh(slot):
        if restarts:
            calls.append(slot)
            if len(calls) <= 2:
                raise OSError("Driver is still initializing")
        return snap

    monkeypatch.setattr(ro.time, "sleep", sleeps.append)
    kwargs["gpu_factory"] = fresh
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert len(calls) == 3 and sleeps == [0.5, 0.5]
    assert len(restarts) == 1 and reg.operations == [receipt["after"]]


def test_typed_getter_is_independent_and_used_once_per_transaction_observation(monkeypatch):
    receipt, reg, snap, _, kwargs, _, _, _ = configured(monkeypatch)
    gpu, calls = SimpleNamespace(), []
    kwargs["gpu_factory"] = lambda slot: gpu

    def read(target):
        assert target is gpu
        calls.append(bool(reg.values))
        return copy.deepcopy(snap["linked_current"]), None

    monkeypatch.setattr(linked, "read", read)
    for module in (ro.voltage_ceiling_runtime, ro.clock_bios_runtime, ro.fan_stop_runtime):
        monkeypatch.setattr(module, "read", lambda gpu: pytest.fail("Unrelated typed getter was required"))
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert calls == [False, True]
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert calls == [False, True, False]


def test_full_packet_receipt_prepare_active_and_restore_reload_under_existing_cap(monkeypatch, tmp_path):
    original, reg, snap, restarts, kwargs, before, _, _ = configured(monkeypatch, partial=True)
    target = ro.Target(**original["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "Unrelated getter unavailable"))
    prepared = ro.prepare(object(), original["changes"], registry=reg)
    path = Path(prepared["path"])

    def reload_exact(expected):
        assert path.stat().st_size < 8 * 1024 * 1024
        loaded = ro.load_receipt(path)
        assert loaded == expected
        saved = loaded["linked_current_contracts"]
        assert len(bytes.fromhex(saved["before"]["info_hex"])) == linked.INFO_SIZE
        assert len(bytes.fromhex(saved["before"]["control_hex"])) == linked.CONTROL_SIZE
        assert len(bytes.fromhex(saved["after_control_hex"])) == linked.CONTROL_SIZE
        linked.validate_contracts(loaded["changes"], saved)
        return loaded

    assert not reg.operations and path.with_suffix(".rom").read_bytes() == b"abcdef"
    loaded = reload_exact(prepared)
    kwargs["save"] = ro.startup.atomic_json
    active = ro.execute(loaded, **kwargs)
    assert active["state"] == "active", active["message"]
    loaded = reload_exact(active)
    assert ro.active_receipt(target) == loaded
    restored = ro.execute(loaded, restore=True, **kwargs)
    assert restored["state"] == "restored", restored["message"]
    reload_exact(restored)
    assert ro.active_receipt(target) is None
    assert not reg.values and snap["linked_current"] == before and len(restarts) == 2
