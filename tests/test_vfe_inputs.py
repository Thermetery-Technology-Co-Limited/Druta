"""VFE decoding and GET-only recovery without a hardware dependency."""

import ctypes
import dataclasses
import json
import struct
from types import SimpleNamespace

import pytest

from druta import vfe_inputs as vfe


def packets(rows=None):
    if rows is None:
        rows = [{"index": 29, "fuse_id": 0, "version_id": 1, "active": 2300}]
    info = bytearray(vfe.INFO_SIZE)
    control = bytearray(vfe.CONTROL_SIZE)
    struct.pack_into("<I", info, 0, vfe.INFO_VERSION)
    struct.pack_into("<I", control, 0, vfe.CONTROL_VERSION)
    mask = sum(1 << row["index"] for row in rows)
    info[4:36] = control[4:36] = mask.to_bytes(32, "little")
    for row in rows:
        base = row["index"] * vfe.INFO_STRIDE
        ctl = row["index"] * vfe.CONTROL_STRIDE
        typ = row.get("type", 13)
        struct.pack_into("<I", info, base + 0x128, typ)
        struct.pack_into("<Iff", control, ctl + 0x68, typ, row.get("minimum", 1000), row.get("maximum", 30000))
        info[base + 0x77C] = row.get("fuse_id", 0)
        info[base + 0x77D] = row.get("version_id", 1)
        for offset, key in ((0x240, "active"), (0x248, "raw_hardware"), (0x250, "default")):
            info[base + offset] = row.get("signed", False)
            struct.pack_into("<I", info, base + offset + 4, row.get(key, 1800) & 0xFFFFFFFF)
        for offset, key in ((0x264, "active"), (0x268, "raw_hardware")):
            struct.pack_into("<I", info, base + offset, row.get(key, 1800) & 0xFFFFFFFF)
        info[base + 0x260] = row.get("cache_ready", 1)
        info[base + 0x25C] = row.get("override_present", 0)
        struct.pack_into("<I", info, base + 0x258, row.get("override", 0))
        struct.pack_into("<Ii", info, base + 0x374, row.get("scale", 4096), row.get("offset", 0))
    return bytes(info), bytes(control)


def test_fuse_selector_not_row_index_and_json_metadata():
    info, control = packets(
        [
            {
                "index": 254,
                "fuse_id": 0,
                "version_id": 1,
                "active": 4567,
                "raw_hardware": 4566,
                "default": 4000,
                "override_present": 1,
                "override": 4567,
                "offset": -4,
            }
        ]
    )
    snapshot = vfe.decode_packets(info, control, slot="0000:09:03.0", architecture=8)
    field = snapshot.input_for("RMHwSpeedoOverride")
    assert field.index == 254
    assert (field.active, field.raw_hardware, field.default, field.override) == (4567, 4566, 4000, 4567)
    assert field.override_present and field.readback_ready
    assert field.hardware_scale_q12 == 4096 and field.hardware_offset == -4
    assert snapshot.input_for("RMHwSpeedo1Override") is None
    assert json.loads(json.dumps(dataclasses.asdict(snapshot)))["inputs"][0]["active"] == 4567


def test_partial_inventory_and_version_selector():
    rows = [
        {"index": 0, "fuse_id": 0, "version_id": 3},  # Wrong version selector.
        {"index": 5, "fuse_id": 0x18, "version_id": 3, "active": 18000, "raw_hardware": 360, "scale": 204800},
        {"index": 71, "type": 99},  # Unknown variable type is retained.
    ]
    snapshot = vfe.decode_packets(*packets(rows))
    assert len(snapshot.variables) == 3
    assert snapshot.input_for("RMHwSpeedoOverride") is None
    field = snapshot.input_for("RmPmgrIddqNvvddOverride")
    assert (field.active, field.raw_hardware, field.hardware_scale_q12) == (18000, 360, 204800)
    assert len(snapshot.issues) == 1 and snapshot.issues[0].index == 0


@pytest.mark.parametrize("flag", [0x240, 0x248, 0x250, 0x25C, 0x260])
def test_malformed_fuse_does_not_remove_supported_peer(flag):
    info, control = packets(
        [
            {"index": 2, "fuse_id": 0, "version_id": 1},
            {"index": 8, "fuse_id": 0x19, "version_id": 3},
        ]
    )
    damaged = bytearray(info)
    damaged[2 * vfe.INFO_STRIDE + flag] = 2
    snapshot = vfe.decode_packets(bytes(damaged), control)
    assert snapshot.input_for("RMHwSpeedoOverride") is None
    assert snapshot.input_for("RmPmgrIddqMsvddOverride") is not None
    assert snapshot.issues[0].registry_name == "RMHwSpeedoOverride"


def test_type_mismatch_isolated_to_row():
    info, control = packets(
        [
            {"index": 2, "fuse_id": 0, "version_id": 1},
            {"index": 8, "fuse_id": 0x19, "version_id": 3},
        ]
    )
    damaged = bytearray(control)
    struct.pack_into("<I", damaged, 2 * vfe.CONTROL_STRIDE + 0x68, 12)
    snapshot = vfe.decode_packets(info, bytes(damaged))
    assert snapshot.input_for("RMHwSpeedoOverride") is None
    assert snapshot.input_for("RmPmgrIddqMsvddOverride") is not None
    assert snapshot.issues[0].index == 2


@pytest.mark.parametrize("minimum,maximum", [(float("nan"), 5), (1, float("inf")), (10, 9)])
def test_invalid_known_range_is_reported(minimum, maximum):
    snapshot = vfe.decode_packets(*packets([{"index": 0, "minimum": minimum, "maximum": maximum}]))
    assert not snapshot.inputs
    assert "range" in snapshot.issues[0].reason
    json.dumps(dataclasses.asdict(snapshot), allow_nan=False)


def test_unknown_variable_nonfinite_metadata_cannot_suppress_valid_peer():
    snapshot = vfe.decode_packets(
        *packets(
            [
                {"index": 0, "type": 77, "minimum": float("nan"), "maximum": float("inf")},
                {"index": 3, "fuse_id": 0, "version_id": 1},
            ]
        )
    )
    assert snapshot.variables[0].minimum is None and snapshot.variables[0].maximum is None
    assert snapshot.input_for("RMHwSpeedoOverride") is not None
    json.dumps(dataclasses.asdict(snapshot), allow_nan=False)


def test_signed_input_readable_and_uncached_input_not_ready():
    snapshot = vfe.decode_packets(
        *packets(
            [
                {
                    "index": 0,
                    "signed": True,
                    "active": -7,
                    "raw_hardware": -9,
                    "default": -5,
                    "minimum": -100,
                    "maximum": 100,
                    "cache_ready": 0,
                }
            ]
        )
    )
    field = snapshot.input_for("RMHwSpeedoOverride")
    assert (field.active, field.raw_hardware, field.default) == (-7, -9, -5)
    assert field.signed and field.raw_hardware_signed and field.default_signed
    assert not field.readback_ready


def test_duplicate_selector_is_explicit_and_not_chosen():
    snapshot = vfe.decode_packets(*packets([{"index": 1}, {"index": 19}]))
    assert len(snapshot.inputs) == 2 and len(snapshot.issues) == 2
    with pytest.raises(vfe.VfeInputError, match="Multiple"):
        snapshot.input_for("RMHwSpeedoOverride")


@pytest.mark.parametrize(
    "damage", ["info_length", "control_length", "info_version", "control_version", "mask", "capacity"]
)
def test_global_geometry_rejected(damage):
    info, control = map(bytearray, packets())
    if damage == "info_length":
        info.pop()
    if damage == "control_length":
        control.pop()
    if damage == "info_version":
        info[0] ^= 1
    if damage == "control_version":
        control[0] ^= 1
    if damage == "mask":
        control[4] ^= 1
    if damage == "capacity":
        info[35] |= 0x80
    with pytest.raises(vfe.VfeInputError) as exc:
        vfe.decode_packets(bytes(info), bytes(control))
    assert exc.value.kind == "malformed"


class FakeApi:
    def __init__(self):
        self.ok = True
        self.gpu = 987
        self.selected = {"slot": "0000:09:03.0", "handle": 987, "devid": 0xABCD, "subsys": 0x12345678}
        self.payloads = dict(zip((vfe.INFO_ID, vfe.CONTROL_ID), packets(), strict=True))
        self.statuses = []
        self.calls = []
        self.exports = {vfe.INFO_ID, vfe.CONTROL_ID}

    def _i(self, query_id, *argtypes):
        assert argtypes == (ctypes.c_void_p, ctypes.c_void_p)
        if query_id not in self.exports:
            return None

        def getter(handle, pointer):
            self.calls.append(query_id)
            assert handle == self.gpu
            payload = self.payloads[query_id]
            incoming = ctypes.string_at(pointer, len(payload))
            assert incoming[:4] == payload[:4]
            if query_id == vfe.CONTROL_ID:
                assert incoming[4:36] == self.payloads[vfe.INFO_ID][4:36]
            status = self.statuses.pop(0) if self.statuses else 0
            if status == 0:
                ctypes.memmove(pointer, payload, len(payload))
            return status

        return getter


def gpu(architecture=8):
    return SimpleNamespace(nvapi=FakeApi(), pairing_error="", arch=lambda: architecture, slot=lambda: "0000:09:03.0")


@pytest.mark.parametrize("architecture", sorted(vfe.PROBE_ARCHITECTURES))
def test_supported_architecture_probes_actual_getters_without_identity_or_version_gates(architecture):
    target = gpu(architecture)
    reader = vfe.VfeInputReader(target)
    snapshot = reader.read()
    assert snapshot.slot == "0000:09:03.0" and snapshot.architecture == architecture
    assert snapshot.input_for("RMHwSpeedoOverride").active == 2300
    assert target.nvapi.calls == [vfe.INFO_ID, vfe.CONTROL_ID]
    assert reader.last_valid is snapshot and reader.last_error is None


def test_transient_failure_retries_fresh_pair_and_keeps_valid_history():
    target = gpu()
    reader = vfe.VfeInputReader(target)
    first = reader.read()
    target.nvapi.statuses = [-1, 0, 0]
    second = reader.read()
    assert second is not first and reader.last_valid is second
    target.nvapi.statuses = [-1, -1]
    with pytest.raises(vfe.VfeInputError) as exc:
        reader.read()
    assert exc.value.kind == "transient" and reader.last_valid is second
    assert reader.last_error is exc.value
    assert reader.read() is not second  # Failure was not permanently cached.


def test_control_failure_restarts_whole_read_pair():
    target = gpu()
    target.nvapi.statuses = [0, -1, 0, 0]
    vfe.VfeInputReader(target).read()
    assert target.nvapi.calls == [vfe.INFO_ID, vfe.CONTROL_ID] * 2


@pytest.mark.parametrize("status", [-3, -9, -104])
def test_explicit_unsupported_status_is_not_retried(status):
    target = gpu()
    target.nvapi.statuses = [status]
    with pytest.raises(vfe.VfeInputError) as exc:
        vfe.VfeInputReader(target).read()
    assert exc.value.kind == "unsupported" and exc.value.status == status
    assert target.nvapi.calls == [vfe.INFO_ID]


def test_missing_export_and_pre_pascal_have_no_calls():
    for target in (gpu(3), gpu(8)):
        if target.arch() == 8:
            target.nvapi.exports.remove(vfe.CONTROL_ID)
        with pytest.raises(vfe.VfeInputError) as exc:
            vfe.VfeInputReader(target).read()
        assert exc.value.kind == "unsupported"
        assert not target.nvapi.calls


@pytest.mark.parametrize("damage", ["pairing", "slot", "handle", "segment"])
def test_binding_failure_precedes_native_getter(damage):
    target = gpu()
    if damage == "pairing":
        target.pairing_error = "Identity mismatch"
    if damage == "slot":
        target.nvapi.selected["slot"] = "0000:0a:03.0"
    if damage == "handle":
        target.nvapi.selected["handle"] = 123
    if damage == "segment":
        target.slot = lambda: "0001:09:03.0"
    with pytest.raises(vfe.VfeInputError) as exc:
        vfe.VfeInputReader(target).read()
    assert exc.value.kind == "identity" and not target.nvapi.calls


def test_unknown_architecture_can_recover_on_next_attempt():
    target = gpu()
    architectures = iter((None, 8))
    target.arch = lambda: next(architectures)
    assert vfe.VfeInputReader(target).read().architecture == 8


@pytest.mark.parametrize("offset", [0x264, 0x268])
def test_mirror_mismatch_removes_only_affected_known_input(offset):
    info, control = packets(
        [
            {"index": 2, "fuse_id": 0x18, "version_id": 3},
            {"index": 8, "fuse_id": 0x19, "version_id": 3},
        ]
    )
    damaged = bytearray(info)
    damaged[2 * vfe.INFO_STRIDE + offset] ^= 1
    snapshot = vfe.decode_packets(bytes(damaged), control)
    assert snapshot.input_for("RmPmgrIddqNvvddOverride") is None
    assert snapshot.input_for("RmPmgrIddqMsvddOverride") is not None
    assert "mirrors" in snapshot.issues[0].reason
    assert snapshot.model_static_sha256 != vfe.decode_packets(info, control).model_static_sha256


def test_static_hash_masks_exact_known_active_override_fields_and_no_peers():
    rows = [
        {"index": 2, "fuse_id": 0x18, "version_id": 3, "active": 16000},
        {"index": 8, "fuse_id": 0x19, "version_id": 3, "active": 11000},
        {"index": 99, "fuse_id": 0xFE, "version_id": 3},
    ]
    info, control = packets(rows)
    before = vfe.decode_packets(info, control)
    changed = bytearray(info)
    for index in (2, 8):
        base = index * vfe.INFO_STRIDE
        for offset in (0x244, 0x258, 0x264):
            struct.pack_into("<I", changed, base + offset, 15001)
        changed[base + 0x25C] = 1
    after = vfe.decode_packets(bytes(changed), control)
    assert before.info_sha256 != after.info_sha256
    assert before.model_static_sha256 == after.model_static_sha256
    assert before.control_sha256 == after.control_sha256
    # Even a field at an otherwise masked offset stays covered for unknown
    # selectors, unknown row types and unoccupied rows.
    for index, offset in ((99, 0x244), (50, 0x258), (2, 0x25D), (2, 0x260), (2, 0x270), (2, 0x374)):
        extra = bytearray(changed)
        extra[index * vfe.INFO_STRIDE + offset] ^= 1
        assert vfe.decode_packets(bytes(extra), control).model_static_sha256 != after.model_static_sha256


def test_static_hash_preserves_raw_hardware_even_with_consistent_mirrors():
    info, control = packets()
    changed = bytearray(info)
    for offset in (0x24C, 0x268):
        struct.pack_into("<I", changed, 29 * vfe.INFO_STRIDE + offset, 1801)
    original, after = vfe.decode_packets(info, control), vfe.decode_packets(bytes(changed), control)
    assert after.inputs
    assert original.model_static_sha256 != after.model_static_sha256


def test_snapshot_optional_static_hash_preserves_old_constructor_shape():
    value = vfe.VfeSnapshot("0000:01:00.0", 8, 0, (), (), (), "a" * 64, "b" * 64)
    assert value.model_static_sha256 == ""


def test_unrecognized_type_is_not_masked_even_with_known_selector_bytes():
    info, control = packets([{"index": 2, "type": 99}])
    changed = bytearray(info)
    changed[2 * vfe.INFO_STRIDE + 0x258] ^= 1
    assert (
        vfe.decode_packets(info, control).model_static_sha256
        != vfe.decode_packets(bytes(changed), control).model_static_sha256
    )
