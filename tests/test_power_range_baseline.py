"""Original power-range styling uses synthetic ROMs and mocked read services."""

import copy
import hashlib
import json
import struct
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from druta import client_power_runtime as client
from druta import power_range_baseline as baseline
from druta import registry_overrides as overrides
from druta import vbios_fields
from tests.test_vbios_fields import apply, fixture


def observation(image, *, index=5, client_index=2, slot="0000:02:00.0", subsystem=0xA1B2C3D4):
    parsed = vbios_fields.inspect(image)
    fields = {field["key"]: field for field in parsed["fields"]}
    selected = fields[f"power.{index}.minimum"]
    bounds = tuple(fields[f"power.{index}.{key}"]["current"] for key in ("minimum", "default", "maximum"))
    native = bytearray(client.SIZES[client.NATIVE_INFO])
    struct.pack_into("<I", native, 4, 1 << index)
    at = client.NATIVE_INFO_BASE + index * client.NATIVE_INFO_STRIDE
    native[at], native[at + 4], native[at + 5] = 0x40, selected["runtime_binding"]["runtime_type"], selected["channel"]
    struct.pack_into("<III", native, at + 8, *bounds)
    role = struct.pack("<I", 1) + native[at : at + client.NATIVE_INFO_STRIDE]
    public = bytearray(client.SIZES[client.INFO])
    public[4 : client.INFO_BASE] = (1 << client_index).to_bytes(32, "little")
    at = client.INFO_BASE + client_index * client.INFO_STRIDE
    public[at : at + 4] = bytes((1, 1, 1, 0))
    struct.pack_into("<III", public, at + 4, *bounds)
    return {
        "schema": 1,
        "stable_reads": 2,
        "identity": {
            "slot": slot,
            "architecture": 10,
            "device_id": parsed["identity"]["pci_device"],
            "subsystem_id": subsystem,
        },
        "binding": {
            "client_index": client_index,
            "native_index": index,
            "semantic_role": 1,
            "client": 254,
            "units": "mW",
            "minimum_mw": bounds[0],
            "rated_mw": bounds[1],
            "maximum_mw": bounds[2],
        },
        "native_info_hex": native.hex(),
        "role_info_hex": role.hex(),
        "info_hex": public.hex(),
        "arbitration": None,
        "arbitration_error": "unrelated optional telemetry unavailable",
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    target = overrides.Target("0000:02:00.0", r"PCI\VEN_10DE&DEV_1234&SUBSYS_A1B2C3D4\example", r"driver\0003")
    state = SimpleNamespace(target=target, image=fixture(), registry={}, gpu=object(), calls=[], ordinal=5)
    monkeypatch.setattr(overrides, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(overrides, "bind_target", lambda gpu: state.target)
    monkeypatch.setattr(overrides, "read_vbios", lambda gpu: state.image)
    monkeypatch.setattr(overrides, "WindowsRegistry", lambda target: SimpleNamespace(snapshot=lambda: state.registry))

    def read_client():
        state.calls.append("client")
        return observation(state.image, index=state.ordinal, slot=state.target.slot)

    monkeypatch.setattr(client, "ClientPowerController", lambda gpu: SimpleNamespace(read_metadata=read_client))

    def receipt(before, after, *, before_registry=None, parent=None, image=True):
        ident = f"{len(list(tmp_path.glob('[0-9a-f]' * 32 + '.json'))) + 1:032x}"
        path = tmp_path / (ident + ".json")
        old = copy.deepcopy(before_registry if before_registry is not None else state.registry)
        new = copy.deepcopy(old)
        if before != after:
            new["romOverride00"] = {"type": 3, "value": hashlib.sha256(after).hexdigest()}
        else:
            new["RMHwSpeedoOverride"] = {"type": 4, "value": len(list(tmp_path.glob("*.json"))) + 1}
        item = {
            "schema": 1,
            "id": ident,
            "path": str(path),
            "state": "active",
            "target": asdict(target),
            "before": old,
            "after": new,
            "parent": parent["path"] if parent else None,
            "changes": {f"power.{state.ordinal}.maximum": 999000} if before != after else {},
            "before_image_sha256": hashlib.sha256(before).hexdigest() if image else None,
            "after_image_sha256": hashlib.sha256(after).hexdigest() if image else None,
        }
        path.write_text(json.dumps(item), encoding="utf-8")
        if image:
            path.with_suffix(".rom").write_bytes(before)
        overrides._active_path(target).write_text(json.dumps({"receipt": str(path)}), encoding="utf-8")
        state.registry, state.image = new, after
        return item

    state.receipt = receipt
    state.tmp = tmp_path
    return state


def changed(image, index=5, **bounds):
    return apply(image, vbios_fields.plan(image, {f"power.{index}.{key}": value for key, value in bounds.items()}))


def save(item):
    from pathlib import Path

    Path(item["path"]).write_text(json.dumps(item), encoding="utf-8")


def unavailable(result):
    assert result["minimum_mw"] is None and result["maximum_mw"] is None
    assert result["source"] == "unavailable" and result["reason"]


@pytest.mark.parametrize("index,device,slot", [(5, 0x1234, "0000:02:00.0"), (17, 0xBEEF, "0001:09:00.0")])
def test_original_live_bios_uses_dynamic_policy_and_maximum_not_rated(env, index, device, slot):
    env.target = overrides.Target(slot, rf"PCI\VEN_10DE&DEV_{device:04X}&SUBSYS_A1B2C3D4\alternate", r"driver\0034")
    env.image = fixture(device=device)
    live = observation(env.image, index=index, client_index=37, slot=slot)
    assert baseline.read(env.gpu, live) == {
        "minimum_mw": 100000,
        "maximum_mw": 450000,
        "source": "live-bios",
        "reason": None,
    }
    assert env.calls == []


def test_missing_supplied_binding_uses_existing_bounded_metadata_read(env):
    assert baseline.read(env.gpu)["maximum_mw"] == 450000
    assert env.calls == ["client"]


def test_nested_receipts_resolve_earliest_image_not_latest_changed_backup(env):
    original = env.image
    first = env.receipt(original, changed(original, maximum=700000))
    second = env.receipt(env.image, changed(env.image, minimum=90000, maximum=999000), parent=first)
    env.receipt(env.image, env.image, parent=second)
    result = baseline.read(env.gpu)
    assert result == {"minimum_mw": 100000, "maximum_mw": 450000, "source": "receipt-original", "reason": None}


def test_registry_only_first_receipt_can_precede_first_bios_backup(env):
    original = env.image
    first = env.receipt(original, original, image=False)
    env.receipt(original, changed(original, maximum=999000), parent=first)
    assert baseline.read(env.gpu)["maximum_mw"] == 450000


def test_only_registry_flags_do_not_invent_an_image_backup_requirement(env):
    env.receipt(env.image, env.image, image=False)
    assert baseline.read(env.gpu)["source"] == "live-bios"


@pytest.mark.parametrize("name", ["vbios", "VBIOS", "romOverride00", "ROMoverrideaF"])
def test_external_bios_override_without_original_is_unknown(env, name):
    env.registry[name] = {"type": 3, "value": "00"}
    result = baseline.read(env.gpu)
    unavailable(result)
    assert "no original Druta receipt" in result["reason"]


def test_receipt_chain_beginning_with_external_override_is_not_original(env):
    env.registry["vbios"] = {"type": 3, "value": env.image.hex()}
    env.receipt(env.image, changed(env.image, maximum=999000))
    result = baseline.read(env.gpu)
    unavailable(result)
    assert "began with an external" in result["reason"]


@pytest.mark.parametrize(
    "fault",
    [
        "backup_hash",
        "current_hash",
        "before_hash",
        "missing_backup",
        "target",
        "state",
        "registry",
        "cycle",
        "parent_missing",
        "image_link",
        "registry_link",
    ],
)
def test_incoherent_receipt_chain_never_falls_back_to_changed_current_range(env, fault):
    original = env.image
    first = env.receipt(original, changed(original, maximum=700000))
    head = env.receipt(env.image, changed(env.image, maximum=999000), parent=first)
    if fault == "backup_hash":
        (env.tmp / (first["id"] + ".rom")).write_bytes(b"bad" * 100)
    elif fault == "current_hash":
        env.image = changed(env.image, maximum=998000)
    elif fault == "before_hash":
        first["before_image_sha256"] = True
    elif fault == "missing_backup":
        (env.tmp / (first["id"] + ".rom")).unlink()
    elif fault == "target":
        first["target"]["instance_id"] += "-other"
    elif fault == "state":
        head["state"] = "prepared"
    elif fault == "registry":
        env.registry = {}
    elif fault == "cycle":
        first["parent"] = head["path"]
    elif fault == "parent_missing":
        head["parent"] = str(env.tmp / ("f" * 32 + ".json"))
    elif fault == "image_link":
        first["after_image_sha256"] = "a" * 64
    elif fault == "registry_link":
        first["after"] = {}
    save(first)
    save(head)
    unavailable(baseline.read(env.gpu))


@pytest.mark.parametrize(
    "fault",
    [
        "device",
        "subsystem",
        "slot",
        "native_index_bool",
        "class",
        "channel",
        "unit",
        "role",
        "limits",
        "unstable",
        "packet_size",
        "duplicate_native",
    ],
)
def test_live_typed_binding_must_match_selected_bios(env, fault):
    live = observation(env.image)
    if fault in ("device", "subsystem", "slot"):
        field = {"device": "device_id", "subsystem": "subsystem_id", "slot": "slot"}[fault]
        live["identity"][field] = "0000:03:00.0" if fault == "slot" else 0
    elif fault == "native_index_bool":
        live["binding"]["native_index"] = True
    elif fault == "role":
        live["binding"]["semantic_role"] = 2
    elif fault == "limits":
        live["binding"]["maximum_mw"] = 999000
    elif fault == "unstable":
        live["stable_reads"] = 1
    elif fault == "packet_size":
        live["native_info_hex"] += "00"
    else:
        raw = bytearray.fromhex(live["native_info_hex"])
        at = client.NATIVE_INFO_BASE + 5 * client.NATIVE_INFO_STRIDE
        if fault == "duplicate_native":
            other = client.NATIVE_INFO_BASE + 6 * client.NATIVE_INFO_STRIDE
            raw[other : other + client.NATIVE_INFO_STRIDE] = raw[at : at + client.NATIVE_INFO_STRIDE]
            struct.pack_into("<I", raw, 4, (1 << 5) | (1 << 6))
        else:
            raw[at + {"class": 4, "channel": 5, "unit": 6}[fault]] += 1
            live["role_info_hex"] = (struct.pack("<I", 1) + raw[at : at + client.NATIVE_INFO_STRIDE]).hex()
        live["native_info_hex"] = raw.hex()
    unavailable(baseline.read(env.gpu, live))


def test_original_policy_binding_cannot_change_across_receipts(env):
    original = env.image
    after = bytearray(changed(original, maximum=999000))
    after[0x3800 + 0x38 + 5 * 0x66 + 5] = 31
    env.receipt(original, bytes(after))
    unavailable(baseline.read(env.gpu))


def test_other_table_failure_and_unrelated_telemetry_do_not_hide_range(env, monkeypatch):
    inspect = vbios_fields.inspect

    def partial(image):
        result = inspect(image)
        result["fields"] = [f for f in result["fields"] if f["key"].startswith("power.5.")]
        result["diagnostics"].append({"group": "voltage", "message": "unavailable"})
        return result

    monkeypatch.setattr(vbios_fields, "inspect", partial)
    assert baseline.read(env.gpu)["maximum_mw"] == 450000


def test_unavailable_controller_is_only_an_unknown_baseline(env, monkeypatch):
    def failed():
        raise client.ClientPowerError("unsupported", "Client role unavailable")

    monkeypatch.setattr(client, "ClientPowerController", lambda gpu: SimpleNamespace(read_metadata=failed))
    result = baseline.read(env.gpu)
    unavailable(result)
    assert "Client role unavailable" in result["reason"]


@pytest.mark.parametrize("bounds", [(0, 360000, 450000), (0, 0, 0), (0x80000000, 0x80000001, 0xFFFFFFFF)])
def test_read_only_uint32_bios_bounds_are_not_restricted_by_editing_ranges(env, bounds):
    raw = bytearray(env.image)
    struct.pack_into("<III", raw, 0x3800 + 0x38 + 5 * 0x66 + 13, *bounds)
    env.image = bytes(raw)
    assert baseline.read(env.gpu) == {
        "minimum_mw": bounds[0],
        "maximum_mw": bounds[2],
        "source": "live-bios",
        "reason": None,
    }


def test_metadata_only_succeeds_when_request_control_and_status_are_unavailable(env, monkeypatch):
    metadata = observation(env.image)
    metadata.pop("arbitration")
    metadata.pop("arbitration_error")
    calls = []

    def read_metadata():
        calls.append("metadata")
        return metadata

    def full_read():
        pytest.fail("Original range must not require an FE request, CONTROL or STATUS")

    monkeypatch.setattr(
        client, "ClientPowerController", lambda gpu: SimpleNamespace(read_metadata=read_metadata, read=full_read)
    )
    assert baseline.read(env.gpu)["maximum_mw"] == 450000
    assert calls == ["metadata"]


@pytest.mark.parametrize("fe_request", [0, 0xFFFFFFFF, None])
def test_supplied_metadata_remains_usable_with_missing_or_zero_fe_request(env, fe_request):
    live = observation(env.image)
    live.update(
        requested_mw=fe_request,
        control_hex=None,
        native_control_hex=None,
        arbitration=None,
        arbitration_error="STATUS unavailable",
    )
    assert baseline.read(env.gpu, live)["maximum_mw"] == 450000
    assert env.calls == []


@pytest.mark.parametrize("value", [-1, 0x100000000, True, 123.0])
def test_ordinary_metadata_still_requires_exact_uint32_values(env, value):
    live = observation(env.image)
    live["binding"]["minimum_mw"] = value
    unavailable(baseline.read(env.gpu, live))
