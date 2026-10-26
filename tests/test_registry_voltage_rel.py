"""Offline REL transactions, independent readbacks, and durable cleanup paths."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields
from tests import test_voltage_rel_runtime as rel
from tests.test_registry_overrides import FakeRegistry, transaction


def configured(monkeypatch, *, rail=0, boost=0, roots=None, partial=False, fixed=False):
    receipt, registry, snapshot, restarts, kwargs = transaction(monkeypatch)
    obs = rel.observation(rail, boost=boost, partial=partial, slot=receipt["target"]["slot"])
    bios = rel.inspection(obs, rail, roots=roots)
    if fixed:
        field = bios["fields"][0]
        field["key"] = field["key"].removesuffix("maximum") + "value"
        field["offsets"] = [field["equation_offset"] + 3, field["equation_offset"] + 7]
    key = bios["fields"][0]["key"]
    changes = {key: bios["fields"][0]["current"] - 5000}
    saved = rel.contract(monkeypatch, rail, obs=obs, bios=bios, changes=changes)
    receipt.update(changes=changes, voltage_rel_contract_version=1, voltage_rel_contracts=saved)
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


@pytest.mark.parametrize("rail", [0, 1])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("fixed", [False, True])
def test_exact_activation_and_cleanup_before_gpu_read(monkeypatch, rail, partial, fixed):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, rail=rail, partial=partial, fixed=fixed)
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_verified_fields"] == list(receipt["changes"])
    assert result["runtime_voltage_policy"] == snap["voltage_ceiling"]["policy"]
    assert reg.values == receipt["after"]
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Premature recovery GPU read")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored", result["message"]
    assert snap["voltage_ceiling"] == before and not reg.values and len(restarts) == 2


def test_derived_root_readbacks_are_saved_without_predicting_a_voltage(monkeypatch):
    roots = [rel.root(), rel.root(0, "ALT", 401), rel.root(1, "REL", 7999)]
    receipt, reg, snap, _, kwargs, before, _ = configured(monkeypatch, roots=roots)
    restart = kwargs["restart"]

    def recompute(target):
        result = restart(target)
        if reg.values:
            rails = snap["voltage_ceiling"]["policy"]["rails"]
            rails["0"]["policy_uv"]["reliability"] -= 1234
            rails["0"]["policy_uv"]["alt_reliability"] -= 2345
            rails["0"]["policy_uv"]["effective"] -= 3456
            rails["1"]["policy_uv"]["reliability"] -= 4567
            rails["1"]["policy_uv"]["effective"] -= 5678
        return result

    kwargs["restart"] = recompute
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active", result["message"]
    assert result["runtime_voltage_policy"] == snap["voltage_ceiling"]["policy"]
    assert result["runtime_voltage_policy"] != before["policy"]
    assert ro.execute(receipt, restore=True, **kwargs)["state"] == "restored"
    assert snap["voltage_ceiling"] == before


@pytest.mark.parametrize(
    "failure",
    [
        "ignored",
        "quantized",
        "boost",
        "peer_alt",
        "peer_rel",
        "peer_boost",
        "overvoltage",
        "vmin",
        "offset",
        "native_opaque",
        "control_opaque",
        "identity",
        "missing",
    ],
)
def test_unexpected_activation_rolls_back_every_state(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def corrupt(target):
        result = restart(target)
        if reg.values:
            obs = snap["voltage_ceiling"]
            rails = obs["policy"]["rails"]
            if failure == "ignored":
                snap["voltage_ceiling"] = copy.deepcopy(before)
            elif failure == "quantized":
                rel.native.mutate(
                    obs["equations"],
                    "control",
                    rel.equations.HEADER + 37 * rel.equations.CONTROL_STRIDE + 8,
                    1110000,
                    "f",
                )
            elif failure == "boost":
                obs["boost_percent"] += 1
            elif failure in ("peer_alt", "peer_rel"):
                rails["1"]["policy_uv"]["alt_reliability" if failure == "peer_alt" else "reliability"] += 1
            elif failure == "peer_boost":
                rails["1"]["boost_uv"] += 1
            elif failure in ("overvoltage", "vmin"):
                rails["0"]["policy_uv"][failure] += 1
            elif failure == "offset":
                rails["0"]["offsets_uv"]["reliability"] += 1
            elif failure == "native_opaque":
                rel.native.mutate(obs["equations"], "info", 3, 1)
            elif failure == "control_opaque":
                raw = bytearray.fromhex(obs["rail_control"]["rails"]["0"]["payload_hex"])
                raw[0x48 + 24] ^= 1
                obs["rail_control"]["rails"]["0"]["payload_hex"] = raw.hex()
            elif failure == "identity":
                for section in ("equations", "policy", "rail_control"):
                    obs[section]["identity"]["device_id"] = 42
            else:
                snap["voltage_ceiling"] = None
        return result

    kwargs["restart"] = corrupt
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"] and not reg.values
    assert snap["voltage_ceiling"] == before and len(restarts) == 2
    assert reg.operations == [receipt["after"], receipt["before"]]


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "mutation",
    ["version", "missing", "missing_version", "empty", "request", "target", "selected", "peer", "graph", "native_kind"],
)
def test_malformed_contracts_refuse_apply_and_undo_before_gpu_or_registry(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    saved = receipt["voltage_rel_contracts"]
    if mutation == "version":
        receipt["voltage_rel_contract_version"] = True
    elif mutation == "missing":
        receipt.pop("voltage_rel_contracts")
    elif mutation == "missing_version":
        receipt.pop("voltage_rel_contract_version")
    elif mutation == "empty":
        receipt["voltage_rel_contracts"] = {}
    elif mutation == "request":
        receipt["changes"]["voltage.0.REL.maximum"] -= 1
    elif mutation == "target":
        receipt["target"]["slot"] = "0000:44:00.0"
    elif mutation == "selected":
        saved["selected"]["voltage.0.REL.maximum"]["upper_clamp_binding"]["index"] = 401
    elif mutation == "peer":
        saved["after"]["policy"]["rails"]["1"]["boost_uv"] += 1
    elif mutation == "graph":
        saved["selected"]["voltage.0.REL.maximum"]["affected_rail_roots"] = []
    else:
        saved["native"]["kind"] = "constant"
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Malformed receipt triggered GPU read")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("restore", [False, True])
def test_old_image_only_receipt_is_restore_only(monkeypatch, restore):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt.pop("voltage_rel_contract_version")
    receipt.pop("voltage_rel_contracts")
    if restore:
        reg.values = copy.deepcopy(receipt["after"])
        snap.pop("voltage_ceiling")
        restart = kwargs["restart"]

        def without_voltage(target):
            result = restart(target)
            snap.pop("voltage_ceiling", None)
            return result

        kwargs["restart"] = without_voltage
        kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Premature recovery read")
    else:
        kwargs["gpu_factory"] = lambda slot: pytest.fail("Legacy apply triggered GPU read")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["ok"] is restore, result["message"]
    assert not reg.values and len(restarts) == int(restore)


@pytest.mark.parametrize("failure", ["missing", "boost", "native", "policy", "controls", "identity"])
def test_preflight_drift_refuses_before_write(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    obs = snap["voltage_ceiling"]
    if failure == "missing":
        snap["voltage_ceiling"] = None
    elif failure == "boost":
        obs["boost_percent"] = 1
    elif failure == "native":
        rel.native.mutate(obs["equations"], "control", 3, 1)
    elif failure == "policy":
        obs["policy"]["rails"]["0"]["policy_uv"]["reliability"] += 1
    elif failure == "controls":
        raw = bytearray.fromhex(obs["rail_control"]["rails"]["1"]["payload_hex"])
        raw[-1] ^= 1
        obs["rail_control"]["rails"]["1"]["payload_hex"] = raw.hex()
    else:
        for section in ("equations", "policy", "rail_control"):
            obs[section]["identity"]["device_id"] = 42
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("change", ["offset", "root", "sharing"])
def test_fresh_bios_graph_binding_must_equal_preview(monkeypatch, change):
    receipt, reg, _, restarts, kwargs, before, _ = configured(monkeypatch)
    bios = rel.inspection(before)
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    field = bios["fields"][0]
    if change == "offset":
        field["equation_offset"] += 25
        field["offsets"][0] += 25
    elif change == "root":
        field["affected_rail_roots"].append(rel.root(1, "REL", 7999))
    else:
        field["sharing_graph_complete"] = False
    monkeypatch.setattr(vbios_fields, "inspect", lambda image: bios)
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts


@pytest.mark.parametrize("name", ["named_changes", "flag_changes", "pstate_changes", "clock_vf_changes"])
def test_other_registry_changes_cannot_obscure_rel_dependencies(monkeypatch, name):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch)
    receipt[name] = {"unrelated": True}
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts
    assert "separately" in result["message"]


@pytest.mark.parametrize("boost", [1, 37, 100])
def test_nonzero_boost_apply_precondition_never_blocks_restore(monkeypatch, boost):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, boost=boost)
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts
    assert "resets the nonzero voltage boost request" in result["message"]
    reg.values = copy.deepcopy(receipt["after"])
    kwargs["gpu_factory"] = lambda slot: snap if not reg.values else pytest.fail("Premature recovery read")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["ok"] and result["state"] == "restored", result["message"]
    assert not reg.values and snap["voltage_ceiling"] == before


def prepare_fixture(monkeypatch, tmp_path, *, boost=0, fixed=False):
    receipt, _, _, _, _, _, _ = configured(monkeypatch, boost=boost, fixed=fixed)
    target = ro.Target(**receipt["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "unrelated model readback unavailable"))
    return receipt, FakeRegistry()


@pytest.mark.parametrize("fixed", [False, True])
def test_prepare_is_read_only_and_saves_exact_rel_contract(monkeypatch, tmp_path, fixed):
    receipt, reg = prepare_fixture(monkeypatch, tmp_path, fixed=fixed)
    result = ro.prepare(object(), receipt["changes"], registry=reg)
    assert result["voltage_rel_contract_version"] == 1
    assert result["voltage_rel_contracts"] == receipt["voltage_rel_contracts"]
    assert result["voltage_ceiling_contracts"] == {}
    assert not reg.operations and not reg.values


@pytest.mark.parametrize("boost", [1, 37, 100])
def test_prepare_rejects_nonzero_boost_before_saving_receipt(monkeypatch, tmp_path, boost):
    receipt, reg = prepare_fixture(monkeypatch, tmp_path, boost=boost)
    with pytest.raises(RuntimeError, match="resets the nonzero voltage boost request"):
        ro.prepare(object(), receipt["changes"], registry=reg)
    assert not reg.operations and not reg.values
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("failure", ["policy", "missing", "request"])
def test_rollback_readback_failure_requires_recovery_with_registry_already_restored(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    restart = kwargs["restart"]

    def fail_recovery(target):
        result = restart(target)
        if reg.values:
            snap["voltage_ceiling"] = copy.deepcopy(before)  # Driver ignored the selected clamp.
        elif failure == "missing":
            snap["voltage_ceiling"] = None
        elif failure == "request":
            snap["voltage_ceiling"]["boost_percent"] = 1
        else:
            snap["voltage_ceiling"]["policy"]["rails"]["0"]["policy_uv"]["reliability"] += 1
        return result

    kwargs["restart"] = fail_recovery
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not result["ok"] and not reg.values and len(restarts) == 2
    assert any(event["event"] == "rollback-failure" for event in result["events"])


def test_failed_restore_never_reintroduces_the_override(monkeypatch):
    receipt, reg, snap, restarts, kwargs, _, _ = configured(monkeypatch)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    restart = kwargs["restart"]

    def lost_readback(target):
        result = restart(target)
        snap["voltage_ceiling"] = None
        return result

    kwargs["restart"] = lost_readback
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "recovery-required", result["message"]
    assert not reg.values and len(restarts) == 2
    assert reg.operations[0] == receipt["after"]
    assert all(not operation for operation in reg.operations[1:])


@pytest.mark.parametrize("failure", [None, "activation", "preflight", "rollback"])
def test_non_dict_gpu_reuses_combined_getter_once_per_observation(monkeypatch, failure):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch)
    gpu = SimpleNamespace()
    kwargs["gpu_factory"] = lambda slot: gpu
    calls = []

    def read_combined(target):
        assert target is gpu
        calls.append(bool(reg.values))
        if failure == "preflight" and len(calls) == 1:
            return None, "preflight combined GET unavailable"
        if failure in ("activation", "rollback") and reg.values:
            return None, "post-restart combined GET unavailable"
        if failure == "rollback" and len(calls) > 2:
            return None, "rollback combined GET unavailable"
        return copy.deepcopy(snap["voltage_ceiling"]), None

    monkeypatch.setattr(ro.voltage_ceiling_runtime, "read", read_combined)
    monkeypatch.setattr(ro.voltage_rel_runtime, "read", lambda target: pytest.fail("Duplicate REL GET"))
    result = ro.execute(receipt, **kwargs)
    if failure == "preflight":
        assert result["state"] == "failed" and calls == [False]
        assert not reg.operations and not restarts
    elif failure in ("activation", "rollback"):
        assert result["state"] == ("rolled-back" if failure == "activation" else "recovery-required")
        assert calls == [False, True, False]
        assert not reg.values and len(restarts) == 2
    else:
        assert result["state"] == "active", result["message"]
        assert calls == [False, True]
        result = ro.execute(receipt, restore=True, **kwargs)
        assert result["state"] == "restored", result["message"]
        assert calls == [False, True, False]
        assert snap["voltage_ceiling"] == before and not reg.values


@pytest.mark.parametrize("fixed", [False, True])
def test_full_native_receipt_prepare_active_and_undo_roundtrip_under_existing_limit(monkeypatch, tmp_path, fixed):
    original, reg, snap, restarts, kwargs, before, after = configured(monkeypatch, fixed=fixed)
    target = ro.Target(**original["target"])
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "unrelated model readback unavailable"))
    prepared = ro.prepare(object(), original["changes"], registry=reg)
    path = Path(prepared["path"])

    def reload_exact(expected):
        assert path.stat().st_size <= 8 * 1024 * 1024
        loaded = ro.load_receipt(path)
        assert loaded == expected
        saved = loaded["voltage_rel_contracts"]
        assert set(saved["native"]) == {"schema", "kind", "selected"}
        for name, full in (("before", before), ("after", after)):
            assert len(bytes.fromhex(saved[name]["equations"]["info_hex"])) == rel.equations.INFO_SIZE
            assert len(bytes.fromhex(saved[name]["equations"]["control_hex"])) == rel.equations.CONTROL_SIZE
            assert saved[name] == full
        rel.rel.validate_contracts(loaded["changes"], saved)
        return loaded

    assert not reg.operations and path.with_suffix(".rom").read_bytes() == b"abcdef"
    loaded = reload_exact(prepared)
    kwargs["save"] = ro.startup.atomic_json
    active = ro.execute(loaded, **kwargs)
    assert active["state"] == "active", active["message"]
    loaded = reload_exact(active)
    assert ro.active_receipt(target) == loaded
    assert loaded["runtime_voltage_policy"] == snap["voltage_ceiling"]["policy"]
    restored = ro.execute(loaded, restore=True, **kwargs)
    assert restored["state"] == "restored", restored["message"]
    reload_exact(restored)
    assert ro.active_receipt(target) is None
    assert snap["voltage_ceiling"] == before and not reg.values and len(restarts) == 2


@pytest.mark.parametrize("offset", [4, 8, 0x2C])
def test_fixed_bounds_ignored_bound_or_coefficient_change_rolls_back(monkeypatch, offset):
    receipt, reg, snap, restarts, kwargs, before, _ = configured(monkeypatch, fixed=True)
    restart = kwargs["restart"]

    def incomplete_load(target):
        result = restart(target)
        if reg.values:
            observation = snap["voltage_ceiling"]["equations"]
            at = rel.equations.HEADER + 37 * rel.equations.CONTROL_STRIDE + offset
            original = bytes.fromhex(observation["control_hex"])[at]
            rel.native.mutate(observation, "control", at, original ^ 1)
        return result

    kwargs["restart"] = incomplete_load
    result = ro.execute(receipt, **kwargs)
    assert not result["ok"] and result["state"] == "rolled-back", result["message"]
    assert not reg.values and snap["voltage_ceiling"] == before and len(restarts) == 2


def test_fixed_bounds_receipt_projection_is_validated_before_restore_writes(monkeypatch):
    receipt, reg, _, restarts, kwargs, _, _ = configured(monkeypatch, fixed=True)
    reg.values = copy.deepcopy(receipt["after"])
    row = next(iter(receipt["voltage_rel_contracts"]["native"]["selected"].values()))
    row["after_lower_uv"] -= 5000
    result = ro.execute(receipt, restore=True, **kwargs)
    assert not result["ok"] and not reg.operations and not restarts
