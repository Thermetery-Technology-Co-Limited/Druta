"""Offline global stop ownership, capability, packet and recovery contracts."""

import ctypes
import json
import struct

import pytest

from druta import fan_policy_runtime as policy
from druta import fan_runtime as fan
from druta import fan_stop_runtime as stop
from tests.test_fan_policy_runtime import Gpu as PolicyGpu
from tests.test_fan_policy_runtime import inspection as policy_inspection


class Gpu(PolicyGpu):
    def __init__(self, *, enabled=True, **kwargs):
        super().__init__(**kwargs)
        for index in (3, 9):
            struct.pack_into("<III", self.payloads[policy.CONTROL_COMMAND], 20 + index * 96 + 0x54, 10, 20, 1)
        apply(self, enabled)

    def _legacy_clk_escape(self, packet, fields):
        result = super()._legacy_clk_escape(packet, fields)
        if packet[14] in (fan.INFO_COMMAND, fan.CONTROL_COMMAND):
            packet[11] = 1
        return result


def inspection(enabled=True):
    result = policy_inspection()
    for row in result["fan_policy_binding"]["policies"]:
        row["flags"] = (row["flags"] & ~6) | (6 if enabled else 0)
    result["fields"].append(
        {"key": stop.KEY, "current": bool(enabled), "units": "bool", "capability": "editable", "policy_indices": [3, 9]}
    )
    return result


def apply(gpu, enabled):
    for index in (3, 9):
        info, control = 16 + index * 20, 20 + index * 96
        gpu.payloads[policy.INFO_COMMAND][info + 14 : info + 16] = bytes([int(enabled)]) * 2
        gpu.payloads[policy.CONTROL_COMMAND][control + 0x48] = int(enabled)


def observe(gpu):
    result, error = stop.read(gpu)
    assert error is None, error
    return result


def change_raw(state, name, offset, value):
    raw = bytearray.fromhex(state["raw"][name])
    raw[offset] = value
    state["raw"][name] = raw.hex()


def test_independent_capability_and_separate_fan_transactions():
    assert stop.contracts(None, None, {"power.3.maximum": 1}) == {}
    assert stop.validate_contracts({}, {}) == [] and stop.verify(None, {}) == []
    for key in ("fan.2.pwm_hz", "fan_stop.3.low", "fan_curve.9.1.level"):
        with pytest.raises(stop.FanStopRuntimeError, match="separately"):
            stop.contracts(None, None, {stop.KEY: False, key: 1})


@pytest.mark.parametrize("architecture", range(4, 11))
@pytest.mark.parametrize("enabled", [False, True])
def test_global_enable_disable_and_exact_undo_on_alternate_identity(architecture, enabled):
    gpu = Gpu(enabled=enabled, architecture=architecture, slot="0000:fe:1f.7")
    before = observe(gpu)
    changes = {stop.KEY: not enabled}
    saved = stop.contracts(gpu, inspection(enabled), changes)
    r = saved[stop.KEY]
    assert r["affected_indices"] == [3, 9]
    assert r["before"] is enabled and r["after"] is not enabled
    assert r["before_state"]["identity"]["slot"] == "0000:fe:1f.7"
    assert r["platform_support_before"] == ("validated_by_loaded_support" if enabled else "not_observed")
    assert stop.verify(before, saved, restore=True) == [stop.KEY]
    with pytest.raises(stop.FanStopRuntimeError):
        stop.verify(before, saved)
    apply(gpu, not enabled)
    assert stop.verify({"fan_stop": observe(gpu)}, json.loads(json.dumps(saved))) == [stop.KEY]
    apply(gpu, enabled)
    assert stop.verify(observe(gpu), saved, restore=True) == [stop.KEY]


@pytest.mark.parametrize("value", [None, 2, -1, 1.0, "true", [], 10**1000])
def test_invalid_boolean_rejected_before_probe(value):
    with pytest.raises(stop.FanStopRuntimeError):
        stop.contracts(None, None, {stop.KEY: value})


@pytest.mark.parametrize(
    "mutation",
    ["omitted_row", "unknown_affected", "missing_inventory", "flag", "selector", "route", "role_map", "curve", "units"],
)
def test_full_bios_ownership_and_loaded_identity_required(mutation):
    gpu, bios = Gpu(), inspection()
    if mutation == "omitted_row":
        bios["fields"][-1]["policy_indices"] = [3]
    elif mutation == "unknown_affected":
        gpu.payloads[policy.INFO_COMMAND][16 + 9 * 20] = 77
        gpu.payloads[policy.CONTROL_COMMAND][20 + 9 * 96] = 77
    elif mutation == "missing_inventory":
        bios["fields"][-1].pop("policy_indices")
    elif mutation == "flag":
        gpu.payloads[policy.INFO_COMMAND][16 + 9 * 20 + 15] = 0
    elif mutation == "selector":
        bios["fan_policy_binding"]["policies"][1]["selector_raw"] += 1
    elif mutation == "route":
        bios["fan_policy_binding"]["routes"][0]["policy_mask_a"] = 1 << 3
    elif mutation == "role_map":
        bios["fan_policy_binding"]["client_id_to_index"]["1"] = 11
    else:
        field = next(f for f in bios["fields"] if f["key"] == "fan_curve.9.1.rpm")
        field["current" if mutation == "curve" else "units"] = 2001 if mutation == "curve" else "mA"
    with pytest.raises(stop.FanStopRuntimeError):
        stop.contracts(gpu, bios, {stop.KEY: False})


@pytest.mark.parametrize("mutation", ["zero_word", "inverted", "zero", "thresholds"])
def test_enable_validates_all_ancillary_values_without_assuming_platform_capability(mutation):
    gpu, bios = Gpu(enabled=False), inspection(False)
    if mutation == "zero_word":
        struct.pack_into("<H", gpu.payloads[policy.INFO_COMMAND], 16 + 9 * 20 + 16, 0)
        bios["fan_policy_binding"]["policies"][1]["stop_word_44"] = 0
    elif mutation == "thresholds":
        struct.pack_into("<ii", gpu.payloads[policy.CONTROL_COMMAND], 20 + 9 * 96 + 0x4C, 1, 0)
    else:
        struct.pack_into(
            "<III",
            gpu.payloads[policy.CONTROL_COMMAND],
            20 + 9 * 96 + 0x54,
            30 if mutation == "inverted" else 10,
            20,
            int(mutation != "zero"),
        )
    with pytest.raises(stop.FanStopRuntimeError, match="ancillary"):
        stop.contracts(gpu, bios, {stop.KEY: True})


def test_unreferenced_unknown_policy_is_preserved_as_opaque_peer():
    gpu = Gpu()
    for command in (policy.INFO_COMMAND, policy.CONTROL_COMMAND):
        mask = struct.unpack_from("<I", gpu.payloads[command], 4)[0] | (1 << 15)
        struct.pack_into("<I", gpu.payloads[command], 4, mask)
    gpu.payloads[policy.INFO_COMMAND][16 + 15 * 20] = 88
    gpu.payloads[policy.CONTROL_COMMAND][20 + 15 * 96] = 88
    saved = stop.contracts(gpu, inspection(), {stop.KEY: False})
    apply(gpu, False)
    assert stop.verify(observe(gpu), saved) == [stop.KEY]
    gpu.payloads[policy.CONTROL_COMMAND][20 + 15 * 96 + 70] = 201
    with pytest.raises(stop.FanStopRuntimeError, match="opaque"):
        stop.verify(observe(gpu), saved)


@pytest.mark.parametrize("low,high", [(0, 0), (120 * 256, 125 * 256)])
def test_global_flags_preserve_inherited_thresholds_accepted_by_loader(low, high):
    gpu = Gpu(enabled=False)
    bios = inspection(False)
    for index in (3, 9):
        struct.pack_into("<ii", gpu.payloads[policy.CONTROL_COMMAND], 20 + index * 96 + 0x4C, low, high)
    for field in bios["fields"]:
        if field["key"].startswith("fan_stop."):
            field["current"] = (low if field["key"].endswith(".low") else high) / 256
    saved = stop.contracts(gpu, bios, {stop.KEY: True})
    apply(gpu, True)
    assert stop.verify(observe(gpu), saved) == [stop.KEY]


@pytest.mark.parametrize(
    "packet,offset",
    [
        ("policy_info", 16 + 9 * 20 + 15),
        ("policy_control", 20 + 9 * 96 + 0x48),
        ("policy_control", 20 + 9 * 96 + 0x54),
        ("policy_control", 20 + 3 * 96 + 1),
        ("policy_info", 1),
        ("policy_control", 19),
        ("routes", 12 + 1 * 20 + 19),
        ("cooler_info", fan.INFO_OFFSET + 11 * fan.INFO_STRIDE + 1),
        ("cooler_control", fan.CONTROL_OFFSET + 2 * fan.CONTROL_STRIDE + 20),
    ],
)
def test_every_peer_byte_and_global_row_is_verified(packet, offset):
    gpu = Gpu()
    saved = stop.contracts(gpu, inspection(), {stop.KEY: False})
    apply(gpu, False)
    current = observe(gpu)
    change_raw(current["state"], packet, offset, 1)
    with pytest.raises(stop.FanStopRuntimeError):
        stop.verify(current, saved)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "before_bool",
        "after_int",
        "indices",
        "omitted_row",
        "identity",
        "projection",
        "capability",
        "raw_size",
        "raw_type",
        "opaque",
    ],
)
def test_durable_receipt_cannot_drop_coverage_or_invent_preserved_bytes(mutation):
    changes = {stop.KEY: False}
    saved = stop.contracts(Gpu(), inspection(), changes)
    r = saved[stop.KEY]
    if mutation == "missing":
        saved.clear()
    elif mutation == "before_bool":
        r["before"] = 1
    elif mutation == "after_int":
        r["after"] = 0
    elif mutation == "indices":
        r["affected_indices"] = [True, 9]
    elif mutation == "omitted_row":
        r["affected_indices"] = [3]
    elif mutation == "identity":
        r["after_state"]["identity"]["subsystem_id"] += 1
    elif mutation == "projection":
        change_raw(r["after_state"], "policy_info", 16 + 9 * 20 + 14, 1)
    elif mutation == "capability":
        r["platform_support_before"] = "universal"
    elif mutation == "raw_size":
        r["before_state"]["raw"]["routes"] = "00"
    elif mutation == "raw_type":
        r["before_state"]["raw"] = []
    else:
        change_raw(r["after_state"], "policy_control", 1, 201)
    with pytest.raises(stop.FanStopRuntimeError):
        stop.validate_contracts(changes, saved)


@pytest.mark.parametrize("word,value", [(0, 0), (2, 0), (5, 2), (6, 42), (11, 0), (14, 0), (15, 0)])
def test_strict_envelope(word, value):
    gpu = Gpu()
    gpu.after_call = lambda p: p.__setitem__(word, value) if p[14] == policy.INFO_COMMAND else None
    value, error = stop.read(gpu)
    assert value is None and error


@pytest.mark.parametrize("side", [-1, 0])
def test_packet_guards_are_enforced(side):
    gpu = Gpu()

    def damage(packet):
        if packet[14] == policy.INFO_COMMAND:
            address = ctypes.addressof(packet) + (-1 if side == -1 else ctypes.sizeof(packet))
            ctypes.c_ubyte.from_address(address).value = 0

    gpu.after_call = damage
    result, error = stop.read(gpu)
    assert result is None and "bounds" in error


def test_transient_read_recovers_and_broken_pairing_does_not():
    gpu = Gpu()
    gpu.outcomes[policy.INFO_COMMAND] = [(1, 0)]
    assert observe(gpu)["stable_reads"] == 2
    gpu.after_call = lambda p: gpu.nvapi.selected.update(handle=999) if p[14] == policy.ROUTING_COMMAND else None
    result, error = stop.read(gpu)
    assert result is None and error


def test_opaque_byte_instability_cannot_pass_semantic_stability():
    gpu = Gpu()

    def alternate(packet):
        if packet[14] == policy.INFO_COMMAND:
            gpu.payloads[policy.INFO_COMMAND][1] ^= 1

    gpu.after_call = alternate
    result, error = stop.read(gpu)
    assert result is None and "changed between reads" in error


def test_receipt_is_json_serializable_and_full_packet_scope_is_explicit():
    saved = stop.contracts(Gpu(), inspection(), {stop.KEY: False})
    assert stop.validate_contracts({stop.KEY: False}, json.loads(json.dumps(saved))) == [stop.KEY]
    r = saved[stop.KEY]
    for name, (_, size) in stop.GETS.items():
        assert len(r["before_state"]["raw"][name]) == len(r["after_state"]["raw"][name]) == size * 2
