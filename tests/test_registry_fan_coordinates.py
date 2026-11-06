"""Registry transaction coverage for typed duty/RPM fan-curve contracts."""

import copy
import struct

import pytest

from druta import registry_overrides as ro
from druta import vbios_fields
from tests.test_fan_policy_runtime import Gpu, apply, configure_mode, inspection, observe
from tests.test_registry_overrides import FakeRegistry, transaction


def coordinate_transaction(monkeypatch, *, role="rpm", mixed=False, rpm_mode=True):
    receipt, registry, observed, restarts, kwargs = transaction(monkeypatch)
    gpu, bios = Gpu(slot=receipt["target"]["slot"]), inspection()
    configure_mode(gpu, bios, rpm=rpm_mode)
    changes = {f"fan_curve.3.1.{role}": 2001 if role == "rpm" else 51}
    if mixed:
        changes.update(
            {
                "fan_curve.9.1.temperature": 61,
                "fan_stop.9.low": 41,
                f"fan_curve.9.1.{'level' if role == 'rpm' else 'rpm'}": 51 if role == "rpm" else 2001,
            }
        )
    contract = ro.fan_policy_runtime.contracts(gpu, bios, changes)
    baseline = observe(gpu)
    apply(gpu, changes)
    active = observe(gpu)
    receipt.update(changes=changes, fan_policy_contracts=copy.deepcopy(contract), fan_policy_contract_version=2)
    observed["fan_policies"] = copy.deepcopy(baseline)
    kwargs["gpu_factory"] = lambda slot: observed
    restart_image = kwargs["restart"]

    def restart(target):
        result = restart_image(target)
        observed["fan_policies"] = copy.deepcopy(active if registry.values else baseline)
        return result

    kwargs["restart"] = restart
    return receipt, registry, observed, restarts, kwargs, baseline, active


def write_control(observation, index, offset, value):
    row = next(row for row in observation["entries"] if row["index"] == index)
    raw = bytearray.fromhex(row["control_hex"])
    struct.pack_into("<I", raw, offset, value)
    row["control_hex"] = raw.hex()


def write_info_byte(observation, index, offset, value):
    row = next(row for row in observation["entries"] if row["index"] == index)
    raw = bytearray.fromhex(row["info_hex"])
    raw[offset] = value
    row["info_hex"] = raw.hex()


@pytest.mark.parametrize("role", ["level", "rpm"])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("rpm_mode", [False, True])
def test_coordinate_transaction_apply_and_exact_undo(monkeypatch, role, mixed, rpm_mode):
    receipt, reg, observed, restarts, kwargs, baseline, _ = coordinate_transaction(
        monkeypatch, role=role, mixed=mixed, rpm_mode=rpm_mode
    )
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "active"
    assert result["runtime_verified_fields"] == sorted(receipt["changes"])
    assert reg.values == receipt["after"]
    assert len(reg.operations) == len(restarts) == 1
    # Recovery must restore the registry before opening a possibly broken GPU.
    kwargs["gpu_factory"] = lambda slot: observed if not reg.values else pytest.fail("Premature recovery GET")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored"
    assert result["runtime_verified_fields"] == sorted(receipt["changes"])
    assert reg.values == receipt["before"] == {}
    assert observed["fan_policies"] == baseline
    assert len(reg.operations) == len(restarts) == 2


@pytest.mark.parametrize("role", ["level", "rpm"])
@pytest.mark.parametrize("failure", ["ignored", "rounded", "mode", "peer", "route", "cooler"])
def test_coordinate_failed_activation_rolls_back_all_state(monkeypatch, role, failure):
    receipt, reg, observed, restarts, kwargs, baseline, _ = coordinate_transaction(monkeypatch, role=role)
    restart = kwargs["restart"]

    def corrupt_active(target):
        result = restart(target)
        if not reg.values:
            return result
        current = observed["fan_policies"]
        if failure == "ignored":
            observed["fan_policies"] = copy.deepcopy(baseline)
        elif failure == "rounded":
            raw = next(iter(receipt["fan_policy_contracts"].values()))["after"]
            write_control(current, 3, 0x34 if role == "level" else 0x38, raw + 1)
        elif failure == "mode":
            write_info_byte(current, 3, 13, 1)
        elif failure == "peer":
            write_control(current, 9, 0x38, 2001)
        elif failure == "route":
            entry = current["routing_entries"][0]
            raw = bytearray.fromhex(entry["raw_hex"])
            struct.pack_into("<I", raw, 12, 1 << 9)
            entry["raw_hex"] = raw.hex()
        else:
            current["cooler"]["entries"][0]["pwm_frequency_hz"] += 1
        return result

    kwargs["restart"] = corrupt_active
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "rolled-back", result["message"]
    assert not result["ok"]
    assert reg.values == {}
    assert observed["fan_policies"] == baseline
    assert len(reg.operations) == len(restarts) == 2


@pytest.mark.parametrize("mutation", ["coordinate", "mode", "peer", "cooler"])
def test_stale_coordinate_preview_refuses_before_registry_write(monkeypatch, mutation):
    receipt, reg, observed, restarts, kwargs, _, _ = coordinate_transaction(monkeypatch)
    current = observed["fan_policies"]
    if mutation == "coordinate":
        write_control(current, 3, 0x38, 1999)
    elif mutation == "mode":
        write_info_byte(current, 3, 13, 1)
    elif mutation == "peer":
        write_control(current, 9, 0x50, 46 * 256)
    else:
        current["cooler"]["entries"][0]["pwm_frequency_hz"] += 1
    assert ro.execute(receipt, **kwargs)["state"] == "failed"
    assert not reg.values and not reg.operations and not restarts


@pytest.mark.parametrize("role", ["level", "rpm"])
def test_failed_coordinate_undo_keeps_registry_clean_and_reports_recovery(monkeypatch, role):
    receipt, reg, observed, restarts, kwargs, _, _ = coordinate_transaction(monkeypatch, role=role)
    assert ro.execute(receipt, **kwargs)["state"] == "active"
    restart = kwargs["restart"]

    def failed_undo(target):
        result = restart(target)
        raw = next(iter(receipt["fan_policy_contracts"].values()))["before"]
        write_control(observed["fan_policies"], 3, 0x34 if role == "level" else 0x38, raw + 1)
        return result

    kwargs["restart"] = failed_undo
    kwargs["gpu_factory"] = lambda slot: observed if not reg.values else pytest.fail("Premature recovery GET")
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "recovery-required"
    assert not result["ok"] and reg.values == {}
    assert len(restarts) == 2


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("mutation", ["empty", "missing", "version_bool", "version_old", "version_future"])
def test_versioned_coordinate_receipt_needs_exact_contracts_before_any_get_or_write(monkeypatch, restore, mutation):
    receipt, reg, _, restarts, kwargs, _, _ = coordinate_transaction(monkeypatch)
    if mutation == "empty":
        receipt["fan_policy_contracts"] = {}
    elif mutation == "missing":
        del receipt["fan_policy_contracts"]
    else:
        receipt["fan_policy_contract_version"] = {"version_bool": True, "version_old": 1, "version_future": 3}[mutation]
    if restore:
        receipt["state"] = "active"
        reg.values = copy.deepcopy(receipt["after"])
    expected = copy.deepcopy(reg.values)
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Invalid receipt queried GPU")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["state"] == "failed"
    assert reg.values == expected and not reg.operations and not restarts


@pytest.mark.parametrize("member", ["empty", "missing"])
@pytest.mark.parametrize("role", ["level", "rpm"])
def test_old_unverified_coordinate_apply_is_refused_before_gpu_or_write(monkeypatch, member, role):
    receipt, reg, _, restarts, kwargs, _, _ = coordinate_transaction(monkeypatch, role=role)
    receipt.pop("fan_policy_contract_version")
    if member == "empty":
        receipt["fan_policy_contracts"] = {}
    else:
        receipt.pop("fan_policy_contracts")
    kwargs["gpu_factory"] = lambda slot: pytest.fail("Unverified prepared receipt queried GPU")
    assert ro.execute(receipt, **kwargs)["state"] == "failed"
    assert not reg.values and not reg.operations and not restarts


@pytest.mark.parametrize("member", ["empty", "missing"])
@pytest.mark.parametrize("role", ["level", "rpm"])
def test_legacy_image_only_coordinate_undo_preserves_cleanup_first(monkeypatch, member, role):
    receipt, reg, observed, restarts, kwargs, _, _ = coordinate_transaction(monkeypatch, role=role)
    receipt.pop("fan_policy_contract_version")
    if member == "empty":
        receipt["fan_policy_contracts"] = {}
    else:
        receipt.pop("fan_policy_contracts")
    receipt["state"] = "active"
    reg.values = copy.deepcopy(receipt["after"])
    observed["image"] = b"abXYef"
    calls = []

    def gpu(slot):
        assert reg.values == {}, "Restoration read a broken GPU before registry cleanup"
        calls.append(slot)
        # No policy observation: this receipt never promised a runtime check.
        return {"image": observed["image"]}

    kwargs["gpu_factory"] = gpu
    result = ro.execute(receipt, restore=True, **kwargs)
    assert result["state"] == "restored"
    assert result["runtime_verified_fields"] == []
    assert reg.values == {} and len(reg.operations) == len(restarts) == len(calls) == 1


def test_prepare_records_new_coordinate_contract_version_and_units(monkeypatch, tmp_path):
    template, _, _, _, _ = transaction(monkeypatch)
    target = ro.Target(**template["target"])
    gpu, bios = Gpu(slot=target.slot), inspection()
    bios["identity"] = {"vbios_version_raw": 1, "oem_revision": 0}
    monkeypatch.setattr(ro, "bind_target", lambda gpu: target)
    monkeypatch.setattr(ro, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(ro, "active_receipt", lambda target: None)
    monkeypatch.setattr(ro, "read_vbios", lambda gpu: b"abcdef")
    monkeypatch.setattr(ro, "read_model_inputs", lambda gpu: (None, "Unrelated VFE unavailable"))
    monkeypatch.setattr(vbios_fields, "inspect", lambda blob: bios)
    registry = FakeRegistry()
    receipt = ro.prepare(gpu, {"fan_curve.3.1.level": 51}, registry=registry)
    assert receipt["fan_policy_contract_version"] == 2
    assert receipt["fan_policy_contracts"]["fan_curve.3.1.level"]["unit"] == "q16_fraction"
    assert not registry.values and not registry.operations


@pytest.mark.parametrize("restore", [False, True])
def test_legacy_mixed_receipt_retains_temperature_contract_during_undo_only(monkeypatch, restore):
    receipt, reg, observed, restarts, kwargs, baseline, _ = coordinate_transaction(monkeypatch, mixed=True)
    receipt.pop("fan_policy_contract_version")
    temperature_changes = {k: v for k, v in receipt["changes"].items() if k.endswith((".temperature", ".low"))}
    gpu, bios = Gpu(slot=receipt["target"]["slot"]), inspection()
    configure_mode(gpu, bios, rpm=True)
    receipt["fan_policy_contracts"] = ro.fan_policy_runtime.contracts(gpu, bios, temperature_changes)
    if restore:
        receipt["state"] = "active"
        reg.values = copy.deepcopy(receipt["after"])
        observed["image"] = b"abXYef"
        kwargs["gpu_factory"] = lambda slot: observed if not reg.values else pytest.fail("Premature recovery GET")
    else:
        kwargs["gpu_factory"] = lambda slot: pytest.fail("Legacy mixed apply queried GPU")
    result = ro.execute(receipt, restore=restore, **kwargs)
    assert result["state"] == ("restored" if restore else "failed")
    assert not reg.values
    assert len(reg.operations) == len(restarts) == int(restore)
    if restore:
        assert result["runtime_verified_fields"] == sorted(temperature_changes)
        assert observed["fan_policies"] == baseline


def test_failed_apply_and_failed_coordinate_rollback_retains_recovery_state(monkeypatch):
    receipt, reg, observed, restarts, kwargs, _, _ = coordinate_transaction(monkeypatch)
    restart = kwargs["restart"]

    def bad_readback(target):
        result = restart(target)
        # Neither the requested value2001 nor the captured baseline2000 loads.
        write_control(observed["fan_policies"], 3, 0x38, 1999)
        return result

    kwargs["restart"] = bad_readback
    result = ro.execute(receipt, **kwargs)
    assert result["state"] == "recovery-required"
    assert not result["ok"] and reg.values == {}
    assert len(reg.operations) == len(restarts) == 2
    assert "recovery required" in result["message"]
