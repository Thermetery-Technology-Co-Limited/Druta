"""Offline GRPG transactions: scoped capability, exact activation and recovery."""

import copy
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from druta import grpg_runtime as gr
from druta import lpwr_runtime, vbios_fields
from druta import registry_overrides as ro
from tests import test_grpg_runtime as fixtures
from tests.test_registry_overrides import digest, transaction


def configured(monkeypatch, *, multiple=False, slot=None, device=0xC321, auxiliary=False, bit1=False):
    inspect, plan = vbios_fields.inspect, vbios_fields.plan
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    monkeypatch.setattr(vbios_fields, "inspect", inspect)
    monkeypatch.setattr(vbios_fields, "plan", plan)
    if slot is not None:
        receipt["target"]["slot"] = slot
    target = ro.Target(**receipt["target"])
    flags = {"flags": (0x69, 0x6B, 0x61)} if bit1 else {"flags": (0x1B, 0x13, 0x13)} if auxiliary else {}
    image = fixtures.bios(device=device, **flags)
    changes = {f"lpwr.gr_pg.{1 if bit1 else 0}.enabled": False}
    if multiple:
        changes["lpwr.gr_pg.2.enabled"] = False
    if bit1:
        observe, make_contract = fixtures.bit1_observation, fixtures.make_bit1_contract
    elif auxiliary:
        observe, make_contract = fixtures.auxiliary_observation, fixtures.make_aux_contract
    else:
        observe, make_contract = fixtures.observation, fixtures.make_contract
    before = observe(slot=target.slot, device=device)
    saved = make_contract(monkeypatch, image=image, obs=before, changes=changes)
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
        grpg_contract_version=1,
        grpg_contracts=saved,
    )
    snapshot.update(image=image, target=target, grpg=copy.deepcopy(before))

    def restart(device):
        restarts.append(device)
        snapshot["image"] = changed if registry.values else image
        snapshot["grpg"] = copy.deepcopy(after if registry.values else before)
        snapshot.pop("grpg_error", None)
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
    assert snap["grpg"] == after and reg.values == receipt["after"]
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Read before registry cleanup")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["grpg"] == before and not reg.values and len(restarts) == 2
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("failure", ["ignored", "routing", "peer", "unavailable", "error"])
def test_failed_activation_restores_registry_image_and_loaded_configuration(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def incomplete(device):
        result = restart(device)
        if reg.values:
            if failure == "ignored":
                snap["grpg"] = copy.deepcopy(before)
            elif failure == "routing":
                raw = bytearray.fromhex(snap["grpg"]["pstate_info_hex"])
                raw[12 + 552 * 3 + 13] = 255
                snap["grpg"]["pstate_info_hex"] = raw.hex()
                snap["grpg"]["pstates"] = lpwr_runtime._pstates(raw)
            elif failure == "peer":
                controllers = copy.deepcopy(snap["grpg"]["controllers"])
                controllers["9"]["configured_pstate_mask"] = 0
                snap["grpg"] = fixtures.observation(slot=before["identity"]["slot"], controllers=controllers)
                snap["grpg"]["identity"] = copy.deepcopy(before["identity"])
            elif failure == "unavailable":
                snap["grpg"] = None
            else:
                snap["grpg_error"] = "GRPG readback unavailable"
        return result

    kwargs["restart"] = incomplete
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert snap["grpg"] == before and digest(snap["image"]) == receipt["before_image_sha256"]
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("failure", ["unsupported", "changed_routes", "changed_support", "identity"])
def test_preflight_loaded_configuration_failure_never_mutates(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, _, after = configured(monkeypatch)
    if failure == "unsupported":
        snap["grpg"] = None
    elif failure == "changed_support":
        snap["grpg"] = after
    elif failure == "identity":
        snap["grpg"]["identity"]["device_id"] += 1
    else:
        raw = bytearray.fromhex(snap["grpg"]["pstate_info_hex"])
        raw[12 + 552 * 12 + 12] += 1
        snap["grpg"]["pstate_info_hex"] = raw.hex()
        snap["grpg"]["pstates"] = lpwr_runtime._pstates(raw)
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
        receipt["grpg_contract_version"] = True
    elif mutation == "missing_version":
        receipt.pop("grpg_contract_version")
    elif mutation == "missing":
        receipt.pop("grpg_contracts")
    elif mutation == "empty":
        receipt["grpg_contracts"] = {}
    elif mutation == "selected":
        receipt["grpg_contracts"]["selected"]["lpwr.gr_pg.0.enabled"]["offset"] += 1
    elif mutation == "projection":
        receipt["grpg_contracts"]["after"]["9"]["configured_pstate_mask"] ^= 4
    elif mutation == "request":
        receipt["changes"]["lpwr.gr_pg.0.enabled"] = True
    elif mutation == "slot":
        receipt["target"]["slot"] = "0000:11:02.5"
    elif mutation == "image":
        receipt["grpg_contracts"]["before_image_sha256"] = "0" * 64
    else:
        receipt["verify_bios"] = False
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed receipt caused GPU access")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
def test_legacy_image_only_gr_pg_receipt_retains_cleanup_without_new_apply(monkeypatch, restore):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt.pop("grpg_contract_version")
    receipt.pop("grpg_contracts")
    monkeypatch.setattr(gr, "read", lambda gpu: pytest.fail("Legacy receipt must not require new capability"))
    snap.pop("grpg")
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
        kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("GET before cleanup")
    else:
        kwargs["gpu_factory"] = lambda slot: pytest.fail("Old GRPG apply must require a new preview")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["ok"] is restore, result["message"]
    assert not reg.values and len(restarts) == int(restore)


def test_unrelated_transaction_does_not_request_grpg_capability(monkeypatch):
    receipt, reg, _, restarts, kwargs = transaction(monkeypatch)
    receipt.update(grpg_contract_version=1, grpg_contracts={})
    monkeypatch.setattr(gr, "read", lambda gpu: pytest.fail("Unrelated transaction requested GRPG"))
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert not reg.values and len(restarts) == 2


def test_failed_runtime_recovery_is_reported_after_registry_cleanup(monkeypatch):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def ignored_then_missing(device):
        result = restart(device)
        snap["grpg"] = copy.deepcopy(before) if reg.values else None
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
        snap["grpg"] = None
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
    snap.pop("grpg")
    gpu, created, reads = SimpleNamespace(), [], []
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: (created.append(slot), gpu)[1])
    monkeypatch.setattr(ro, "bind_target", lambda value: snap["target"] if value is gpu else pytest.fail("Wrong GPU"))

    def read(value):
        assert value is gpu
        reads.append(bool(reg.values))
        return copy.deepcopy(after if reg.values else before), None

    monkeypatch.setattr(gr, "read", read)
    restart = kwargs["restart"]

    def without_injection(device):
        result = restart(device)
        snap.pop("grpg")
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
    snap.pop("grpg")
    wrong = ro.Target("0000:81:04.3", "different", "other-key")
    if changed == "snapshot":
        snap["target"] = wrong
        monkeypatch.setattr(nvbackend, "GPU", lambda slot: pytest.fail("Unbound snapshot constructed GPU"))
    else:
        monkeypatch.setattr(nvbackend, "GPU", lambda slot: SimpleNamespace())
        monkeypatch.setattr(ro, "bind_target", lambda gpu: wrong)
    monkeypatch.setattr(gr, "read", lambda gpu: pytest.fail("Unbound target requested GRPG"))
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not reg.operations and not restarts


def test_scoped_typed_read_error_is_reported_without_write(monkeypatch):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    kwargs["gpu_factory"] = lambda slot: SimpleNamespace()
    monkeypatch.setattr(gr, "read", lambda gpu: (None, "Selected GRPG GET is unavailable"))
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and "Selected GRPG GET" in result["message"]
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
        raw = bytearray.fromhex(snap["grpg"]["pstate_info_hex"])
        struct.pack_into("<I", raw, 12 + 552 * 3 + 24, 999123)
        snap["grpg"]["pstate_info_hex"] = raw.hex()
        return result

    kwargs["restart"] = drift
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"


@pytest.mark.parametrize(
    "other", ["bios", "difr", "named_changes", "flag_changes", "pstate_changes", "clock_vf_changes", "startup_changes"]
)
def test_prepare_rejects_combined_gr_pg_categories_before_reading_image(monkeypatch, other):
    monkeypatch.setattr(ro, "bind_target", lambda gpu: ro.Target("0000:90:01.2", "instance", "key"))
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: pytest.fail("Mixed transaction read image"))
    changes = {"lpwr.gr_pg.0.enabled": False}
    kwargs = {}
    if other == "bios":
        changes["power.4.maximum"] = 900000
    elif other == "difr":
        changes["lpwr.difr.0.enabled"] = False
    else:
        kwargs[other] = {"other": 1}
    with pytest.raises(ro.OverrideError, match="separately"):
        ro.prepare(None, changes, **kwargs)


@pytest.mark.parametrize("restore", [False, True])
def test_saved_gr_pg_receipt_cannot_mix_a_named_control(monkeypatch, restore):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt["flag_changes"] = {"perf_limits.cache": False}
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Mixed receipt read GPU")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and "separately" in result["message"]
    assert not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
def test_saved_grpg_receipt_cannot_mix_difr(monkeypatch, restore):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt["changes"]["lpwr.difr.0.enabled"] = False
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Mixed receipt read GPU")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("scope", ["ordinary", "auxiliary", "bit1"])
def test_prepare_durable_reload_apply_and_undo_keep_exact_runtime_binding(monkeypatch, tmp_path, scope):
    original, reg, snap, restarts, kwargs, before, _ = configured(
        monkeypatch, multiple=scope != "bit1", auxiliary=scope == "auxiliary", bit1=scope == "bit1"
    )
    target = ro.Target(**original["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: snap["image"])
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "Unrelated model getter unavailable"))
    prepared = ro.prepare(object(), original["changes"], registry=reg)
    path = Path(prepared["path"])
    assert not reg.operations and prepared["grpg_contract_version"] == 1
    assert prepared["grpg_contracts"]["before"] == before
    assert prepared["grpg_contracts"]["before_image_sha256"] == digest(snap["image"])
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
    assert not reg.values and snap["grpg"] == before and len(restarts) == 2


@pytest.mark.parametrize("slot,device", [("0000:17:04.2", 0xDA31), ("0000:82:1e.1", 0x9B42)])
def test_auxiliary_transition_apply_and_undo_preserve_exact_contract(monkeypatch, slot, device):
    receipt, reg, snap, restarts, kwargs, before, after = configured(
        monkeypatch, auxiliary=True, slot=slot, device=device
    )
    assert receipt["grpg_contract_version"] == 1 and receipt["grpg_contracts"]["schema"] == 2
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == list(receipt["changes"])
    assert snap["grpg"] == after
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("GET before Undo cleanup")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["grpg"] == before and not reg.values and len(restarts) == 2


@pytest.mark.parametrize("failure", ["ignored_auxiliary", "missing_auxiliary", "feature_peer", "error"])
def test_auxiliary_activation_mismatch_rolls_back_full_original(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, auxiliary=True)
    restart = kwargs["restart"]

    def activate(device):
        outcome = restart(device)
        if reg.values:
            if failure == "ignored_auxiliary":
                snap["grpg"]["auxiliary"] = copy.deepcopy(before["auxiliary"])
            elif failure == "missing_auxiliary":
                snap["grpg"].pop("auxiliary")
            elif failure == "feature_peer":
                aux = snap["grpg"]["auxiliary"]
                aux["feature"]["value"] ^= 1
                raw = bytearray.fromhex(aux["feature_hex"])
                struct.pack_into("<I", raw, 12, aux["feature"]["value"])
                aux["feature_hex"] = raw.hex()
            else:
                snap["grpg_error"] = "Auxiliary getter unavailable after restart"
        return outcome

    kwargs["restart"] = activate
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert snap["grpg"] == before
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("mutation", ["missing_auxiliary", "missing_projection", "projection", "schema"])
def test_auxiliary_receipt_binding_precedes_any_read_or_cleanup(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch, auxiliary=True)
    saved = receipt["grpg_contracts"]
    if mutation == "missing_auxiliary":
        saved["before"].pop("auxiliary")
    elif mutation == "missing_projection":
        saved.pop("after_auxiliary")
    elif mutation == "projection":
        saved["after_auxiliary"]["feature"]["value"] ^= 1
    else:
        saved["schema"] = 1
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Invalid auxiliary receipt caused GPU access")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("scope", ["ordinary", "auxiliary", "bit1"])
def test_scoped_reader_requests_only_contract_selected_capability(monkeypatch, scope):
    from druta import nvbackend

    receipt, reg, snap, _, kwargs, before, after = configured(
        monkeypatch, auxiliary=scope == "auxiliary", bit1=scope == "bit1"
    )
    snap.pop("grpg")
    gpu, calls = SimpleNamespace(), []
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: gpu)
    monkeypatch.setattr(ro, "bind_target", lambda value: snap["target"] if value is gpu else pytest.fail("Wrong GPU"))

    def read(value, **options):
        assert value is gpu
        assert options == ({scope: True} if scope != "ordinary" else {})
        calls.append(bool(reg.values))
        return copy.deepcopy(after if reg.values else before), None

    monkeypatch.setattr(gr, "read", read)
    restart = kwargs["restart"]

    def without_injection(device):
        outcome = restart(device)
        snap.pop("grpg")
        return outcome

    kwargs["restart"] = without_injection
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert calls == [False, True, False]


def test_auxiliary_reader_scope_also_applies_to_automatic_recovery(monkeypatch):
    from druta import nvbackend

    receipt, reg, snap, restarts, kwargs, before, after = configured(monkeypatch, auxiliary=True)
    snap.pop("grpg")
    gpu, calls = SimpleNamespace(), []
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: gpu)
    monkeypatch.setattr(ro, "bind_target", lambda value: snap["target"])
    ignored = copy.deepcopy(after)
    ignored["auxiliary"] = copy.deepcopy(before["auxiliary"])

    def read(value, *, auxiliary=False):
        assert value is gpu and auxiliary is True
        calls.append(bool(reg.values))
        return copy.deepcopy(ignored if reg.values else before), None

    monkeypatch.setattr(gr, "read", read)
    restart = kwargs["restart"]

    def without_injection(device):
        outcome = restart(device)
        snap.pop("grpg")
        return outcome

    kwargs["restart"] = without_injection
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert calls == [False, True, False] and not reg.values and len(restarts) == 2


def test_auxiliary_unavailable_undo_does_not_reintroduce_override(monkeypatch):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch, auxiliary=True)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    restart = kwargs["restart"]

    def incomplete(device):
        outcome = restart(device)
        snap["grpg"].pop("auxiliary")
        return outcome

    kwargs["restart"] = incomplete
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert all(values == receipt["before"] for values in reg.operations[1:])


def _set_bit1_role(observation, role, available):
    """Change both wire and decoded support availability in a synthetic reply."""
    item = observation["bit1"]
    item["roles"][str(role)] = {"available": available, "supported": True if available else None}
    raw = bytearray.fromhex(item["support_hex"])
    struct.pack_into("<HI", raw, 4 + role * 16 + 6, int(available), int(available))
    item["support_hex"] = raw.hex()


@pytest.mark.parametrize("slot,device", [("0000:19:08.3", 0xDA37), ("0000:a2:1d.5", 0x8342)])
def test_bit1_apply_and_undo_preserve_role_availability_and_peers(monkeypatch, slot, device):
    receipt, reg, snap, restarts, kwargs, before, after = configured(monkeypatch, bit1=True, slot=slot, device=device)
    assert receipt["grpg_contract_version"] == 1 and receipt["grpg_contracts"]["schema"] == 3
    assert after["bit1"]["roles"]["0"] == {"available": False, "supported": None}
    assert after["bit1"]["roles"] | {"0": before["bit1"]["roles"]["0"]} == before["bit1"]["roles"]
    assert "auxiliary" not in before and "auxiliary" not in after
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == ["lpwr.gr_pg.1.enabled"]
    assert snap["grpg"] == after
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("GET before Undo cleanup")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["grpg"] == before and not reg.values and len(restarts) == 2


@pytest.mark.parametrize("failure", ["ignored", "missing", "witness_lost", "peer_gained", "identity", "error"])
def test_bit1_ignored_or_incomplete_activation_rolls_back(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, bit1=True)
    restart = kwargs["restart"]

    def activate(device):
        result = restart(device)
        if reg.values:
            if failure == "ignored":
                snap["grpg"]["bit1"] = copy.deepcopy(before["bit1"])
            elif failure == "missing":
                snap["grpg"].pop("bit1")
            elif failure == "witness_lost":
                for role, row in before["bit1"]["roles"].items():
                    if role != "0" and row["available"]:
                        _set_bit1_role(snap["grpg"], int(role), False)
            elif failure == "peer_gained":
                role = next(role for role, row in before["bit1"]["roles"].items() if not row["available"])
                _set_bit1_role(snap["grpg"], int(role), True)
            elif failure == "identity":
                snap["grpg"]["identity"]["device_id"] += 1
            else:
                snap["grpg_error"] = "Bit1 support getter failed"
        return result

    kwargs["restart"] = activate
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert snap["grpg"] == before and digest(snap["image"]) == receipt["before_image_sha256"]
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("mutation", ["missing_before", "missing_after", "supported_false", "lost_witness", "schema"])
def test_bit1_malformed_receipts_fail_before_read_or_cleanup(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch, bit1=True)
    saved = receipt["grpg_contracts"]
    if mutation == "missing_before":
        saved["before"].pop("bit1")
    elif mutation == "missing_after":
        saved.pop("after_bit1")
    elif mutation == "supported_false":
        saved["after_bit1"]["roles"]["0"]["supported"] = False
    elif mutation == "lost_witness":
        for role in range(1, 11):
            _set_bit1_role(saved["before"], role, False)
            saved["after_bit1"]["roles"][str(role)] = {"available": False, "supported": None}
    else:
        saved["schema"] = 2
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed bit1 receipt caused GPU access")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


def test_bit1_preflight_lost_peer_is_refused_without_mutation(monkeypatch):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, bit1=True)
    role = next(role for role, row in before["bit1"]["roles"].items() if role != "0" and row["available"])
    _set_bit1_role(snap["grpg"], int(role), False)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "failed" and not result["ok"]
    assert not reg.operations and not restarts


def test_bit1_scoped_reader_is_also_used_for_automatic_recovery(monkeypatch):
    from druta import nvbackend

    receipt, reg, snap, restarts, kwargs, before, after = configured(monkeypatch, bit1=True)
    snap.pop("grpg")
    gpu, calls = SimpleNamespace(), []
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: gpu)
    monkeypatch.setattr(ro, "bind_target", lambda value: snap["target"])
    ignored = copy.deepcopy(after)
    ignored["bit1"] = copy.deepcopy(before["bit1"])

    def read(value, **options):
        assert value is gpu and options == {"bit1": True}
        calls.append(bool(reg.values))
        return copy.deepcopy(ignored if reg.values else before), None

    monkeypatch.setattr(gr, "read", read)
    restart = kwargs["restart"]

    def without_injection(device):
        result = restart(device)
        snap.pop("grpg")
        return result

    kwargs["restart"] = without_injection
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert calls == [False, True, False] and not reg.values and len(restarts) == 2


@pytest.mark.parametrize("phase", ["undo", "automatic_recovery"])
def test_bit1_restore_readback_failure_keeps_original_registry(monkeypatch, phase):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, bit1=True)
    if phase == "undo":
        assert ro.execute(receipt, **kwargs)["state"] == "active"
    restart = kwargs["restart"]

    def incomplete(device):
        outcome = restart(device)
        if reg.values:
            snap["grpg"]["bit1"] = copy.deepcopy(before["bit1"])
        else:
            snap["grpg"].pop("bit1")
        return outcome

    kwargs["restart"] = incomplete
    result = ro.execute(receipt, restore=phase == "undo", **kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert all(values == receipt["before"] for values in reg.operations[1:])
