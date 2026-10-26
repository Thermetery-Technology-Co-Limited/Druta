"""Offline producer-layout, routing, activation and restoration tests."""

import ctypes
import json
import struct

import pytest

from druta import fan_policy_runtime as policy
from druta import fan_runtime as fan
from tests.test_fan_runtime import FakeGpu as FanGpu


class Gpu(FanGpu):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # This fixture's policy mode selects duty (INFO policy byte13=1),
        # so each routed cooler must report the traced duty selector1.
        for index in (2, 11):
            self.payloads[fan.INFO_COMMAND][fan.INFO_OFFSET + index * fan.INFO_STRIDE + 14] = 1
        info, control, routing = bytearray(336), bytearray(1556), bytearray(332)
        info[0] = control[0] = 3
        mask = (1 << 3) | (1 << 9)
        struct.pack_into("<II", info, 4, mask, mask)
        struct.pack_into("<I", control, 4, mask)
        info[12] = 2
        routing[0] = 1
        struct.pack_into("<I", routing, 4, (1 << 1) | (1 << 7))
        for index in (3, 9):
            i, c = 16 + index * 20, 20 + index * 96
            info[i] = control[c] = 2
            info[i + 1] = 7
            info[i + 13 : i + 16] = bytes((1, 1, 1))
            struct.pack_into("<H", info, i + 16, 900)
            info[i + 18] = 4
            control[c + 0x48] = 1
            for j, temp in enumerate((30, 60, 90)):
                struct.pack_into(
                    "<iII",
                    control,
                    c + 0x24 + 12 * j,
                    temp * 256,
                    fan._value("level_minimum", (25, 50, 100)[j]),
                    (800, 2000, 4000)[j],
                )
            struct.pack_into("<ii", control, c + 0x4C, 40 * 256, 45 * 256)
        for index, cooler, a, b in ((1, 11, mask, 1 << 3), (7, 2, 1 << 9, 1 << 3)):
            off = 12 + index * 20
            routing[off : off + 3] = bytes((1, 1, cooler))
            struct.pack_into("<H", routing, off + 4, 123)
            struct.pack_into("<II", routing, off + 8, a, b)
        self.payloads.update(
            {policy.INFO_COMMAND: info, policy.CONTROL_COMMAND: control, policy.ROUTING_COMMAND: routing}
        )

    def _current_limit_transport_layouts(self):
        super()._current_limit_transport_layouts()
        header, _ = self._current_limit_transport
        for i, value in policy.ENVELOPE.items():
            header[i] = value

    def _legacy_clk_escape(self, packet, fields):
        command = packet[14]
        if command in (fan.INFO_COMMAND, fan.CONTROL_COMMAND):
            return super()._legacy_clk_escape(packet, fields)
        assert (command, packet[15]) in policy.GETS
        assert packet[2] == ctypes.sizeof(packet) == 68 + packet[15]
        if command == policy.CONTROL_COMMAND:
            assert packet[18] == int.from_bytes(self.payloads[policy.INFO_COMMAND][4:8], "little")
            assert bytes(packet)[68 + 16] == 0
        self.calls.append(command)
        outcomes = self.outcomes.get(command, [])
        outcome = outcomes.pop(0) if outcomes else (0, 0)
        if isinstance(outcome, BaseException):
            raise outcome
        status, rm = outcome
        packet[16], packet[11] = rm, 1
        if not status and not rm:
            payload = self.payloads[command]
            ctypes.memmove(ctypes.addressof(packet) + 68, bytes(payload), len(payload))
        if self.after_call:
            self.after_call(packet)
        return status


def inspection():
    return {
        "fan_policy_binding": {
            "version": 32,
            "policies": [
                {
                    "index": i,
                    "selector_raw": 7,
                    "flags": 6,
                    "thermal_offset_q8": 0,
                    "stop_word_44": 900,
                    "stop_selector_46": 4,
                }
                for i in (3, 9)
            ],
            "routes": [
                {"index": index, "kind": 1, "cooler_index": cooler, "word": 123, "policy_mask_a": a, "policy_mask_b": b}
                for index, cooler, a, b in ((1, 11, (1 << 3) | (1 << 9), 1 << 3), (7, 2, 1 << 9, 1 << 3))
            ],
            "client_id_to_index": {"1": 2, "2": 11, "3": None, "4": None},
        },
        "fields": [
            {"key": f"fan_stop.{i}.{role}", "units": "C", "current": value}
            for i in (3, 9)
            for role, value in (("low", 40), ("high", 45))
        ]
        + [
            {"key": f"fan_curve.{i}.{point}.{role}", "units": units, "current": value}
            for i in (3, 9)
            for point in range(3)
            for role, units, value in (
                ("temperature", "C", (30, 60, 90)[point]),
                ("level", "%", (25, 50, 100)[point]),
                ("rpm", "RPM", (800, 2000, 4000)[point]),
            )
        ],
    }


def observe(gpu):
    value, error = policy.read(gpu)
    assert error is None, error
    return value


def apply(gpu, changes):
    for key, value in changes.items():
        i, point, role = policy._key(key)
        offset = 20 + i * 96 + (0x4C if role == "low" else 0x50 if role == "high" else 0x24 + point * 12)
        if role == "level":
            struct.pack_into("<I", gpu.payloads[policy.CONTROL_COMMAND], offset + 4, fan._value("level_minimum", value))
        elif role == "rpm":
            struct.pack_into("<I", gpu.payloads[policy.CONTROL_COMMAND], offset + 8, int(value))
        else:
            struct.pack_into("<i", gpu.payloads[policy.CONTROL_COMMAND], offset, int(value * 256))


def test_unrelated_fields_do_not_probe():
    assert policy.contracts(None, None, {"power.3.maximum": 450000}) == {}
    assert policy.validate_contracts({}, {}) == []
    assert policy.verify(None, {}) == []
    assert policy.contracts(None, None, {"fan.zero_rpm": 0}) == {}


@pytest.mark.parametrize("architecture", range(4, 11))
def test_sparse_nonordinal_shared_routes_and_alternate_adapter(architecture):
    gpu = Gpu(architecture=architecture)
    before = observe(gpu)
    changes = {"fan_stop.3.low": 40.03125, "fan_curve.9.1.temperature": 61}
    saved = policy.contracts(gpu, inspection(), changes)
    assert policy.validate_contracts(changes, saved) == sorted(changes)
    assert policy.verify(before, saved, restore=True) == sorted(changes)
    assert saved["fan_stop.3.low"]["before"] == 10240
    assert saved["fan_stop.3.low"]["after"] == 10248
    apply(gpu, changes)
    assert policy.verify({"fan_policies": observe(gpu)}, json.loads(json.dumps(saved))) == sorted(changes)
    apply(gpu, {"fan_stop.3.low": 40, "fan_curve.9.1.temperature": 60})
    assert policy.verify(observe(gpu), saved, restore=True) == sorted(changes)


@pytest.mark.parametrize(
    "key",
    [
        "fan_stop.3.low",
        "fan_stop.3.high",
        "fan_curve.3.0.temperature",
        "fan_curve.3.1.temperature",
        "fan_curve.3.2.temperature",
    ],
)
def test_each_supported_field_changes_exactly_its_temperature(key):
    gpu = Gpu()
    old = next(f["current"] for f in inspection()["fields"] if f["key"] == key)
    changes = {key: old + 1 / 32}
    saved = policy.contracts(gpu, inspection(), changes)
    apply(gpu, changes)
    assert policy.verify(observe(gpu), saved) == [key]


def configure_stop(gpu, bios, index, *, flags, low, high, word=900, selector=4):
    """Build a BIOS/runtime pair, retaining even noneditable stop metadata."""
    info, control = gpu.payloads[policy.INFO_COMMAND], gpu.payloads[policy.CONTROL_COMMAND]
    i, c = 16 + index * 20, 20 + index * 96
    info[i + 14 : i + 16] = bytes(((flags >> 1) & 1, (flags >> 2) & 1))
    struct.pack_into("<H", info, i + 16, word)
    info[i + 18] = selector
    control[c + 0x48] = (flags >> 2) & 1
    struct.pack_into("<ii", control, c + 0x4C, int(low * 256), int(high * 256))
    row = next(row for row in bios["fan_policy_binding"]["policies"] if row["index"] == index)
    row.update(flags=flags, stop_word_44=word, stop_selector_46=selector)
    for field in bios["fields"]:
        if field["key"] == f"fan_stop.{index}.low":
            field["current"] = low
        elif field["key"] == f"fan_stop.{index}.high":
            field["current"] = high


@pytest.mark.parametrize(
    "stop",
    [
        {"flags": 0, "low": 0, "high": 0, "word": 0, "selector": 255},
        {"flags": 0, "low": 50, "high": 45},
        {"flags": 2, "low": 0, "high": 0},
        {"flags": 6, "low": 40, "high": 40},
        {"flags": 6, "low": 0, "high": 45},
        {"flags": 0, "low": 65535 / 32, "high": 110.03125},
    ],
)
@pytest.mark.parametrize("with_other_stop", [False, True])
def test_curve_edit_preserves_independently_uneditable_stop(stop, with_other_stop):
    gpu, bios = Gpu(), inspection()
    configure_stop(gpu, bios, 3, **stop)
    changes = {"fan_curve.3.1.temperature": 60.03125}
    if with_other_stop:
        changes["fan_stop.9.low"] = 40.03125
    baseline = observe(gpu)
    saved = policy.contracts(gpu, bios, changes)
    assert policy.validate_contracts(changes, saved) == sorted(changes)
    assert policy.verify(baseline, saved, restore=True) == sorted(changes)
    peers = saved["fan_curve.3.1.temperature"]["unchanged"]["values"]
    assert peers["fan_stop.3.low"] == int(stop["low"] * 256)
    assert peers["fan_stop.3.high"] == int(stop["high"] * 256)
    apply(gpu, changes)
    assert policy.verify(observe(gpu), json.loads(json.dumps(saved))) == sorted(changes)
    # An uneditable stop value is still an exact peer, never normalized away.
    apply(gpu, {"fan_stop.3.low": stop["low"] + 1 / 32})
    with pytest.raises(policy.FanPolicyRuntimeError, match="peer"):
        policy.verify(observe(gpu), saved)
    apply(gpu, {"fan_stop.3.low": stop["low"], "fan_curve.3.1.temperature": 60, "fan_stop.9.low": 40})
    assert policy.verify(observe(gpu), saved, restore=True) == sorted(changes)
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(gpu, bios, {"fan_stop.3.low": 39})


def test_selected_stop_preserves_unrelated_unsupported_stop_peer():
    gpu, bios = Gpu(), inspection()
    configure_stop(gpu, bios, 9, flags=0, low=0, high=0, word=0, selector=255)
    changes = {"fan_stop.3.low": 40.03125}
    saved = policy.contracts(gpu, bios, changes)
    apply(gpu, changes)
    assert policy.verify(observe(gpu), saved) == list(changes)
    apply(gpu, {"fan_stop.3.low": 40})
    assert policy.verify(observe(gpu), saved, restore=True) == list(changes)


@pytest.mark.parametrize(
    "key,value",
    [
        ("fan_stop.3.low", True),
        ("fan_stop.3.low", float("nan")),
        ("fan_stop.3.low", 40.01),
        ("fan_stop.3.low", 0),
        ("fan_stop.3.low", 45),
        ("fan_stop.3.high", 40),
        ("fan_curve.3.1.temperature", 90),
        ("fan_curve.3.0.temperature", -1),
        ("fan_curve.3.2.temperature", 111),
    ],
)
def test_invalid_requests_fail_before_activation(key, value):
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(Gpu(), inspection(), {key: value})


@pytest.mark.parametrize(
    "mutation",
    [
        "identity",
        "mode",
        "route_mask",
        "route_target",
        "role_map",
        "metadata",
        "disabled",
        "unknown_class",
        "stop_support",
    ],
)
def test_operation_binding_refuses_mismatch(mutation):
    gpu, bios = Gpu(), inspection()
    if mutation == "identity":
        gpu.nvapi.selected["slot"] = "0000:01:00.0"
    elif mutation == "mode":
        gpu.payloads[policy.INFO_COMMAND][16 + 3 * 20 + 1] = 8
    elif mutation == "route_mask":
        bios["fan_policy_binding"]["routes"][0]["policy_mask_b"] = 1 << 9
    elif mutation == "route_target":
        bios["fan_policy_binding"]["routes"][0]["cooler_index"] = 2
    elif mutation == "role_map":
        bios["fan_policy_binding"]["client_id_to_index"]["1"] = 11
    elif mutation == "metadata":
        next(f for f in bios["fields"] if f["key"] == "fan_curve.3.1.rpm")["current"] = 2001
    elif mutation == "disabled":
        gpu.payloads[policy.CONTROL_COMMAND][20 + 3 * 96 + 0x48] = 0
    elif mutation == "unknown_class":
        gpu.payloads[policy.INFO_COMMAND][16 + 3 * 20] = 77
    else:
        gpu.payloads[policy.INFO_COMMAND][16 + 3 * 20 + 14] = 0
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(gpu, bios, {"fan_stop.3.low": 40.03125})


def test_thermal_relative_point2_is_preserved_but_not_writable():
    gpu, bios = Gpu(), inspection()
    gpu.payloads[policy.INFO_COMMAND][16 + 3 * 20 + 5] = 1
    bios["fan_policy_binding"]["policies"][0]["flags"] |= 8
    struct.pack_into("<i", gpu.payloads[policy.CONTROL_COMMAND], 20 + 3 * 96 + 0x24 + 24, 93 * 256)
    with pytest.raises(policy.FanPolicyRuntimeError, match="thermal-table"):
        policy.contracts(gpu, bios, {"fan_curve.3.2.temperature": 94})
    saved = policy.contracts(gpu, bios, {"fan_curve.3.1.temperature": 61})
    apply(gpu, {"fan_curve.3.1.temperature": 61})
    assert policy.verify(observe(gpu), saved) == ["fan_curve.3.1.temperature"]


@pytest.mark.parametrize(
    "offset,format,value",
    [(20 + 9 * 96 + 0x4C, "i", 9999), (20 + 9 * 96 + 0x28, "I", 17000), (20 + 3 * 96 + 0x2C, "I", 801)],
)
def test_peer_or_unselected_representation_change_fails(offset, format, value):
    gpu = Gpu()
    saved = policy.contracts(gpu, inspection(), {"fan_stop.3.low": 40.03125})
    apply(gpu, {"fan_stop.3.low": 40.03125})
    struct.pack_into("<" + format, gpu.payloads[policy.CONTROL_COMMAND], offset, value)
    with pytest.raises(policy.FanPolicyRuntimeError, match="peer"):
        policy.verify(observe(gpu), saved)


def test_cooler_metadata_preserved_but_unrelated_bad_bounds_do_not_remove_policies():
    gpu = Gpu()
    struct.pack_into(
        "<II", gpu.payloads[fan.CONTROL_COMMAND], fan.CONTROL_OFFSET + 2 * fan.CONTROL_STRIDE + 24, 500, 100
    )
    saved = policy.contracts(gpu, inspection(), {"fan_stop.3.low": 40.03125})
    apply(gpu, {"fan_stop.3.low": 40.03125})
    assert policy.verify(observe(gpu), saved)
    struct.pack_into("<I", gpu.payloads[fan.INFO_COMMAND], fan.INFO_OFFSET + 11 * fan.INFO_STRIDE + 24, 26099)
    with pytest.raises(policy.FanPolicyRuntimeError, match="cooler"):
        policy.verify(observe(gpu), saved)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_peer",
        "before_bool",
        "after_bool",
        "missing_route",
        "missing_identity",
        "selected_fields",
        "point",
        "missing_cooler",
        "changed_request",
    ],
)
def test_receipt_tampering_is_refused(mutation):
    gpu = Gpu()
    changes = {"fan_stop.3.low": 40.03125}
    saved = policy.contracts(gpu, inspection(), changes)
    r = saved["fan_stop.3.low"]
    if mutation == "missing_peer":
        r["unchanged"]["values"].pop("fan_stop.9.low")
    elif mutation == "before_bool":
        r["before"] = True
    elif mutation == "after_bool":
        r["after"] = True
    elif mutation == "missing_route":
        r["unchanged"]["state"]["routing_rows"].clear()
    elif mutation == "missing_identity":
        r["unchanged"]["state"]["identity"].pop("device_id")
    elif mutation == "selected_fields":
        r["selected_fields"] = []
    elif mutation == "point":
        r["point"] = 0
    elif mutation == "missing_cooler":
        r["unchanged"].pop("cooler")
    else:
        changes["fan_stop.3.low"] = 41
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.validate_contracts(changes, saved)


@pytest.mark.parametrize("word,value", [(0, 0), (2, 0), (5, 2), (6, 42), (11, 0), (14, 0), (15, 0)])
def test_strict_nvda_envelope(word, value):
    gpu = Gpu()
    gpu.after_call = lambda packet: (
        packet.__setitem__(word, value) if packet[14] in {c for c, _ in policy.GETS} else None
    )
    observation, error = policy.read(gpu)
    assert observation is None and error


@pytest.mark.parametrize("side", ["before", "after"])
def test_guard_overwrite_refused(side):
    gpu = Gpu()

    def corrupt(packet):
        if packet[14] == policy.INFO_COMMAND:
            address = ctypes.addressof(packet) + (-1 if side == "before" else ctypes.sizeof(packet))
            ctypes.c_ubyte.from_address(address).value = 0

    gpu.after_call = corrupt
    observation, error = policy.read(gpu)
    assert observation is None and "bounds" in error


@pytest.mark.parametrize("kind", ["unsupported", "transient", "identity", "malformed"])
def test_optional_routing_failure_is_limited_to_capability_and_transport(kind):
    gpu = Gpu()
    if kind in ("unsupported", "transient"):
        gpu.outcomes[policy.ROUTING_COMMAND] = [(0, 0x56 if kind == "unsupported" else 0x40)] * 4
    elif kind == "identity":
        gpu.after_call = lambda p: gpu.nvapi.selected.update(handle=908) if p[14] == policy.ROUTING_COMMAND else None
    else:
        gpu.after_call = lambda p: p.__setitem__(6, 99) if p[14] == policy.ROUTING_COMMAND else None
    observation, error = policy.read(gpu)
    if kind in ("unsupported", "transient"):
        assert error is None and not observation["routing_available"]
    else:
        assert observation is None and error


def test_transient_policy_get_recovers_and_unknown_peer_stays_read_only():
    gpu = Gpu()
    gpu.outcomes[policy.INFO_COMMAND] = [(1, 0)]
    assert observe(gpu)["stable_reads"] == 2
    i, c = 16 + 9 * 20, 20 + 9 * 96
    gpu.payloads[policy.INFO_COMMAND][i] = gpu.payloads[policy.CONTROL_COMMAND][c] = 99
    bios = inspection()
    saved = policy.contracts(gpu, bios, {"fan_stop.3.low": 40.03125})
    apply(gpu, {"fan_stop.3.low": 40.03125})
    assert policy.verify(observe(gpu), saved)


def test_derived_display_value_cannot_replace_raw_getter_evidence():
    gpu = Gpu()
    saved = policy.contracts(gpu, inspection(), {"fan_stop.3.low": 40.03125})
    observation = observe(gpu)
    observation["entries"][0]["stop_low_q8"] = 10248
    with pytest.raises(policy.FanPolicyRuntimeError, match="temperature"):
        policy.verify(observation, saved)


def test_fresh_two_read_metadata_change_is_refused():
    gpu = Gpu()
    count = 0

    def mutate(packet):
        nonlocal count
        if packet[14] == policy.INFO_COMMAND:
            count += 1
            if count == 1:
                gpu.payloads[policy.INFO_COMMAND][16 + 3 * 20 + 1] = 8

    gpu.after_call = mutate
    observation, error = policy.read(gpu)
    assert observation is None and "changed between reads" in error


@pytest.mark.parametrize("other", ["fan.zero_rpm", "fan.2.pwm_hz", "fan.2.rpm_minimum"])
def test_temperature_transactions_cannot_implicitly_change_preserved_peers(other):
    with pytest.raises(policy.FanPolicyRuntimeError, match="separately"):
        policy.contracts(None, None, {"fan_stop.3.low": 41, other: 1})


@pytest.mark.parametrize(
    "mutation",
    ["cooler_state_list", "mode_bool", "route_mask_bool", "wrong_group_type", "fake_readable", "missing_route_mask"],
)
def test_structurally_invalid_receipts_fail_cleanly(mutation):
    changes = {"fan_stop.3.low": 41}
    saved = policy.contracts(Gpu(), inspection(), changes)
    peers = saved["fan_stop.3.low"]["unchanged"]
    state = peers["state"]
    if mutation == "cooler_state_list":
        peers["cooler"]["state"] = []
    elif mutation == "mode_bool":
        state["policy_rows"][0]["curve_unit_selector_raw"] = True
    elif mutation == "route_mask_bool":
        state["routing_rows"][0]["policy_mask_b"] = True
    elif mutation == "wrong_group_type":
        state["group_id"] = True
    elif mutation == "fake_readable":
        state["readable_indices"].append(1)
    else:
        state.pop("routing_mask")
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.validate_contracts(changes, saved)


def test_unknown_bios_numeric_type_has_the_module_error_class():
    bios = inspection()
    next(f for f in bios["fields"] if f["key"] == "fan_curve.3.1.rpm")["current"] = True
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(Gpu(), bios, {"fan_stop.3.low": 41})


def test_huge_integer_input_refuses_cleanly():
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(Gpu(), inspection(), {"fan_stop.3.low": 10**1000})


def configure_mode(gpu, bios, *, rpm):
    for index in (3, 9):
        gpu.payloads[policy.INFO_COMMAND][16 + index * 20 + 13] = 0 if rpm else 1
    for row in bios["fan_policy_binding"]["policies"]:
        row["flags"] = (row["flags"] & ~1) | int(rpm)
    for index in (2, 11):
        gpu.payloads[fan.INFO_COMMAND][fan.INFO_OFFSET + index * fan.INFO_STRIDE + 14] = 2 if rpm else 1


@pytest.mark.parametrize("rpm_mode", [False, True])
@pytest.mark.parametrize("index", [3, 9])
@pytest.mark.parametrize("point", [0, 1, 2])
@pytest.mark.parametrize("role", ["level", "rpm"])
def test_curve_coordinate_exact_apply_undo_and_mode(index, point, role, rpm_mode):
    gpu, bios = Gpu(), inspection()
    configure_mode(gpu, bios, rpm=rpm_mode)
    key = f"fan_curve.{index}.{point}.{role}"
    before = next(f["current"] for f in bios["fields"] if f["key"] == key)
    value = before + (1 if point != 2 else -1)
    changes = {key: value}
    baseline = observe(gpu)
    saved = policy.contracts(gpu, bios, changes)
    receipt = saved[key]
    assert receipt["schema"] == 2
    assert receipt["kind"] == "fan_policy_curve_value"
    assert receipt["unit"] == ("q16_fraction" if role == "level" else "RPM")
    assert receipt["before"] == (fan._value("level_minimum", before) if role == "level" else before)
    assert receipt["after"] == (fan._value("level_minimum", value) if role == "level" else value)
    assert receipt["output_mode"] == {
        "selector_raw": int(not rpm_mode),
        "coordinate": "rpm" if rpm_mode else "level",
        "selected_active": rpm_mode == (role == "rpm"),
    }
    assert policy.verify(baseline, saved, restore=True) == [key]
    with pytest.raises(policy.FanPolicyRuntimeError, match="curve coordinate"):
        policy.verify(baseline, saved)
    apply(gpu, changes)
    assert policy.verify(observe(gpu), json.loads(json.dumps(saved))) == [key]
    with pytest.raises(policy.FanPolicyRuntimeError, match="restored"):
        policy.verify(observe(gpu), saved, restore=True)
    apply(gpu, {key: before})
    assert policy.verify(observe(gpu), saved, restore=True) == [key]


def test_mixed_curve_and_stop_receipts_preserve_schema1_and_exact_peers():
    gpu, bios = Gpu(), inspection()
    changes = {
        "fan_curve.3.1.level": 51,
        "fan_curve.3.1.rpm": 2001,
        "fan_curve.9.1.temperature": 61,
        "fan_stop.9.low": 41,
    }
    saved = policy.contracts(gpu, bios, changes)
    for key in ("fan_curve.9.1.temperature", "fan_stop.9.low"):
        assert saved[key]["schema"] == 1
        assert saved[key]["kind"] == "fan_policy_temperature"
        assert "output_mode" not in saved[key]
    for receipt in saved.values():
        assert receipt["selected_fields"] == sorted(changes)
        assert receipt["unchanged"] == saved["fan_curve.3.1.level"]["unchanged"]
    apply(gpu, changes)
    assert policy.verify(observe(gpu), saved) == sorted(changes)
    baseline = {f["key"]: f["current"] for f in bios["fields"] if f["key"] in changes}
    apply(gpu, baseline)
    assert policy.verify(observe(gpu), saved, restore=True) == sorted(changes)


@pytest.mark.parametrize("role", ["level", "rpm"])
def test_coordinate_edits_preserve_unsupported_stop_and_replaced_temperature(role):
    gpu, bios = Gpu(), inspection()
    configure_stop(gpu, bios, 3, flags=8, low=0, high=0, word=0, selector=255)
    gpu.payloads[policy.INFO_COMMAND][16 + 3 * 20 + 5] = 1
    apply(gpu, {"fan_curve.3.2.temperature": 93})
    key = f"fan_curve.3.2.{role}"
    value = 99 if role == "level" else 3999
    saved = policy.contracts(gpu, bios, {key: value})
    peers = saved[key]["unchanged"]["values"]
    assert peers["fan_stop.3.low"] == peers["fan_stop.3.high"] == 0
    assert peers["fan_curve.3.2.temperature"] == 93 * 256
    apply(gpu, {key: value})
    assert policy.verify(observe(gpu), saved) == [key]


@pytest.mark.parametrize("role", ["level", "rpm"])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, 0.5, 10**1000])
def test_coordinate_numeric_encoding_refused_cleanly(role, value):
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(Gpu(), inspection(), {f"fan_curve.3.1.{role}": value})


@pytest.mark.parametrize(
    "key,value",
    [
        ("fan_curve.3.1.level", 101),
        ("fan_curve.3.1.rpm", 32768),
        ("fan_curve.3.1.level", 24),
        ("fan_curve.3.1.rpm", 799),
        ("fan_curve.3.0.level", 24),
        ("fan_curve.3.2.rpm", 5000),
    ],
)
def test_coordinate_encoding_ordering_and_actual_cooler_bounds(key, value):
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(Gpu(), inspection(), {key: value})


@pytest.mark.parametrize("mutation", ["mode", "shared_mode", "unknown_shared_class", "mask_b_only", "bounds"])
def test_output_route_binding_uses_actual_producers(mutation):
    gpu, bios = Gpu(), inspection()
    if mutation == "mode":
        gpu.payloads[fan.INFO_COMMAND][fan.INFO_OFFSET + 11 * fan.INFO_STRIDE + 14] = 2
    elif mutation == "shared_mode":
        gpu.payloads[policy.INFO_COMMAND][16 + 9 * 20 + 13] = 0
    elif mutation == "unknown_shared_class":
        gpu.payloads[policy.INFO_COMMAND][16 + 9 * 20] = 77
        gpu.payloads[policy.CONTROL_COMMAND][20 + 9 * 96] = 77
    elif mutation == "mask_b_only":
        struct.pack_into("<I", gpu.payloads[policy.ROUTING_COMMAND], 12 + 1 * 20 + 8, 1 << 9)
        bios["fan_policy_binding"]["routes"][0]["policy_mask_a"] = 1 << 9
    else:
        struct.pack_into(
            "<II", gpu.payloads[fan.CONTROL_COMMAND], fan.CONTROL_OFFSET + 11 * fan.CONTROL_STRIDE + 24, 2, 1
        )
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.contracts(gpu, bios, {"fan_curve.3.1.level": 51})


def test_second_output_route_also_enforces_its_bounds():
    gpu = Gpu()
    # Policy9 uses both outputs. The first cooler allows51%; the second stops50%.
    struct.pack_into("<I", gpu.payloads[fan.CONTROL_COMMAND], fan.CONTROL_OFFSET + 2 * fan.CONTROL_STRIDE + 28, 32768)
    with pytest.raises(policy.FanPolicyRuntimeError, match="bounds"):
        policy.contracts(gpu, inspection(), {"fan_curve.9.1.level": 51})


def test_mask_b_does_not_invent_output_binding_or_require_unrelated_bounds():
    gpu = Gpu()
    # Policy3 reaches cooler2 only through maskB. Its output is cooler11.
    struct.pack_into("<II", gpu.payloads[fan.CONTROL_COMMAND], fan.CONTROL_OFFSET + 2 * fan.CONTROL_STRIDE + 24, 2, 1)
    changes = {"fan_curve.3.1.level": 51}
    saved = policy.contracts(gpu, inspection(), changes)
    apply(gpu, changes)
    assert policy.verify(observe(gpu), saved) == list(changes)


@pytest.mark.parametrize("role", ["level", "rpm"])
@pytest.mark.parametrize(
    "mutation",
    ["mode", "unit", "before_raw", "after_raw", "before_huge", "after_huge", "schema", "cooler_bounds", "peer_mode"],
)
def test_coordinate_receipt_cannot_change_unit_or_output_binding(role, mutation):
    key = f"fan_curve.3.1.{role}"
    changes = {key: 51 if role == "level" else 2001}
    saved = policy.contracts(Gpu(), inspection(), changes)
    r = saved[key]
    if mutation == "mode":
        r["output_mode"]["selected_active"] = not r["output_mode"]["selected_active"]
    elif mutation == "unit":
        r["unit"] = "q8_c"
    elif mutation == "before_raw":
        r["before"] = 32769 if role == "level" else -1
    elif mutation == "after_raw":
        r["after"] += 1
    elif mutation in ("before_huge", "after_huge"):
        r[mutation.split("_")[0]] = 10**1000
    elif mutation == "schema":
        r["schema"] = 1
    elif mutation == "cooler_bounds":
        r["unchanged"]["cooler"]["bounds"].pop(f"fan.11.{role}_minimum")
    else:
        r["unchanged"]["state"]["policy_rows"][1]["curve_unit_selector_raw"] = 0
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.validate_contracts(changes, saved)
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.verify(None, saved)


@pytest.mark.parametrize(
    "mutation", ["policy_mode", "route", "cooler_mode", "cooler_bound", "alternate_coordinate", "other_policy"]
)
def test_coordinate_apply_requires_exact_loaded_state_and_untouched_peers(mutation):
    gpu = Gpu()
    changes = {"fan_curve.3.1.rpm": 2001}
    saved = policy.contracts(gpu, inspection(), changes)
    apply(gpu, changes)
    if mutation == "policy_mode":
        gpu.payloads[policy.INFO_COMMAND][16 + 3 * 20 + 13] = 0
    elif mutation == "route":
        struct.pack_into("<I", gpu.payloads[policy.ROUTING_COMMAND], 12 + 7 * 20 + 12, 1 << 9)
    elif mutation == "cooler_mode":
        gpu.payloads[fan.INFO_COMMAND][fan.INFO_OFFSET + 11 * fan.INFO_STRIDE + 14] = 2
    elif mutation == "cooler_bound":
        struct.pack_into(
            "<I", gpu.payloads[fan.CONTROL_COMMAND], fan.CONTROL_OFFSET + 11 * fan.CONTROL_STRIDE + 12, 4212
        )
    elif mutation == "alternate_coordinate":
        apply(gpu, {"fan_curve.3.1.level": 51})
    else:
        apply(gpu, {"fan_curve.9.1.rpm": 2001})
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.verify(observe(gpu), saved)


def test_legacy_temperature_receipt_does_not_gain_coordinate_mode_requirements():
    gpu = Gpu()
    # A legacy temperature contract captured independent cooler metadata without
    # binding the two mode enum domains. Retain that exact recovery shape.
    gpu.payloads[fan.INFO_COMMAND][fan.INFO_OFFSET + 11 * fan.INFO_STRIDE + 14] = 2
    changes = {"fan_curve.3.1.temperature": 61}
    saved = policy.contracts(gpu, inspection(), changes)
    assert set(saved[next(iter(changes))]) == {
        "schema",
        "kind",
        "index",
        "point",
        "role",
        "unit",
        "before",
        "after",
        "selected_fields",
        "unchanged",
    }
    apply(gpu, changes)
    assert policy.verify(observe(gpu), saved) == list(changes)


@pytest.mark.parametrize("where", ["before", "after"])
def test_huge_raw_temperature_receipt_refuses_without_overflow(where):
    saved = policy.contracts(Gpu(), inspection(), {"fan_curve.3.1.temperature": 61})
    saved["fan_curve.3.1.temperature"][where] = 10**1000
    with pytest.raises(policy.FanPolicyRuntimeError):
        policy.verify(None, saved)
