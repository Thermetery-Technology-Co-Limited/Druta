"""Pure packet tests: no NVAPI, GPU, registry, or driver loading."""

import copy
import struct

import pytest

from druta.power_arbitration import decode

INFO, STATUS, CONTROL = 0x2080A618, 0x2080A619, 0x2080A61A


def packets(indices=(9,)):
    layout = {
        "info": (0xCC, 0x104),
        "status": (0x9C, 0x1730),
        "control": (0x14, 0xC4),
        "sizes": {INFO: 20512, STATUS: 397048, CONTROL: 13876},
    }
    info = [0] * (layout["sizes"][INFO] // 4)
    status = [0] * (layout["sizes"][STATUS] // 4)
    control = [0] * (layout["sizes"][CONTROL] // 4)
    mask = sum(1 << index for index in indices)
    info[1], status[1] = mask, mask
    control[:5] = [0, 0, 0, 255, mask]
    for index in indices:
        i = (0xCC + index * 0x104) // 4
        s = (0x9C + index * 0x1730) // 4
        c = (0x14 + index * 0xC4) // 4
        info[i], info[i + 1] = 0x40, 30 | (9 << 8) | (1 << 16)
        control[c : c + 2] = [30, 45000]
        status[s : s + 4] = [30, 36002, 889, 0]
        vector(status, index, 0x40, [(254, 45000)], output=45000)
        vector(status, index, 0xE0, [(254, 45000), (2, 36002), (7, 45000)], output=36002)
    return layout, info, control, status


def vector(status, policy, offset, inputs, *, method=0, output=0, initial=45000):
    data = bytearray(0x50)
    data[0], data[1] = method, len(inputs)
    struct.pack_into("<II", data, 4, output, initial)
    for index, (client, value) in enumerate(inputs):
        data[12 + 8 * index] = client
        struct.pack_into("<I", data, 16 + 8 * index, value)
    start = (0x9C + policy * 0x1730 + offset) // 4
    status[start : start + 20] = struct.unpack("<20I", data)


def row(args, index=0):
    result = decode(*args)
    assert result["available"], result
    return result["policies"][index]


def test_saved_input_current_layers_are_distinct_and_client_ids_are_numeric():
    # Compact values from finer-current-1792720275788369300.json, before phase.
    result = row(packets())
    assert result["available"]
    assert result["requested"] == result["host"]["output"] == 45000
    assert result["effective"] == result["pmu"]["output"] == 36002
    assert result["value"] == 889
    assert result["delta"] == 0
    assert result["unit"] == "mA"
    assert result["pmu"]["inputs"] == [
        {"client": 254, "value": 45000},
        {"client": 2, "value": 36002},
        {"client": 7, "value": 45000},
    ]
    assert result["host"]["consistent"] and result["pmu"]["consistent"]
    assert len(result["observations"]) == 1


def test_effective_may_differ_from_pmu_arbitration_without_invalidating_it():
    args = packets((2,))
    s = (0x9C + 2 * 0x1730) // 4
    args[3][s + 1] = 370811
    vector(args[3], 2, 0xE0, [(254, 450000), (8, 360000)], output=360000, initial=450000)
    result = row(args)
    assert result["pmu"]["available"] and result["pmu"]["consistent"]
    assert result["effective"] != result["pmu"]["output"]
    assert any("effective limit" in item for item in result["observations"])


@pytest.mark.parametrize("delta", [-2147483648, -125, 0, 125, 2147483647])
def test_delta_is_signed_and_not_added_to_telemetry(delta):
    args = packets()
    args[3][(0x9C + 9 * 0x1730 + 12) // 4] = delta & 0xFFFFFFFF
    result = row(args)
    assert result["delta"] == delta
    assert result["effective"] == 36002
    assert result["requested"] == 45000
    assert any("signed adjustment" in item for item in result["observations"]) == bool(delta)


@pytest.mark.parametrize("index,kind,channel,unit", [(0, 17, 200, 0), (31, 18, 44, 1), (8, 253, 255, 4)])
def test_alternate_indices_classes_channels_and_unknown_units(index, kind, channel, unit):
    args = packets((index,))
    i, s, c = (0xCC + index * 0x104) // 4, (0x9C + index * 0x1730) // 4, (0x14 + index * 0xC4) // 4
    args[1][i + 1] = kind | (channel << 8) | (unit << 16) | (0xA5 << 24)
    args[2][c] = kind
    args[3][s] = kind | (0xBB << 24)
    result = row(args)
    assert result["available"]
    assert (result["policy"], result["type"], result["channel"], result["unit_code"]) == (index, kind, channel, unit)
    assert result["unit"] == {0: "mW", 1: "mA"}.get(unit)


@pytest.mark.parametrize(
    "method,values,output", [(0, [9, 3, 8], 3), (1, [9, 3, 8], 9), (2, [9, 3, 8], 20), (2, [0xFFFFFFFF, 2], 1)]
)
def test_each_traced_method_and_uint32_sum(method, values, output):
    args = packets()
    vector(args[3], 9, 0xE0, list(enumerate(values)), method=method, output=output)
    result = row(args)["pmu"]
    assert result["computed_output"] == output
    assert result["consistent"] is True


def test_zero_inputs_do_not_claim_a_computed_output():
    args = packets()
    vector(args[3], 9, 0x40, [], output=456, initial=789)
    result = row(args)["host"]
    assert result["available"]
    assert result["count"] == 0 and result["inputs"] == []
    assert result["output"] == 456 and result["initial_value"] == 789
    assert result["computed_output"] is None and result["consistent"] is None


def test_eight_inputs_are_bounded_and_unused_padding_is_ignored():
    args = packets()
    vector(args[3], 9, 0xE0, [(index, 80 - index) for index in range(8)], output=73)
    word = (0x9C + 9 * 0x1730 + 0xE0) // 4
    args[3][word] |= 0xFEEF0000  # uninterpreted header padding
    args[3][word + 19] = 0xDEADBEEF  # opaque trailing bytes
    result = row(args)["pmu"]
    assert result["available"] and result["consistent"]
    assert len(result["inputs"]) == 8


@pytest.mark.parametrize("offset,name,peer", [(0x40, "host", "pmu"), (0xE0, "pmu", "host")])
@pytest.mark.parametrize("bad", ["method", "count", "duplicate"])
def test_bad_vector_preserves_independent_peer(offset, name, peer, bad):
    args = packets()
    word = (0x9C + 9 * 0x1730 + offset) // 4
    if bad == "method":
        args[3][word] = (args[3][word] & ~255) | 3
    elif bad == "count":
        args[3][word] = (args[3][word] & ~0xFF00) | 9 << 8
    else:
        vector(args[3], 9, offset, [(254, 1), (254, 2)], output=1)
    result = row(args)
    assert result["available"] and result[peer]["available"]
    assert not result[name]["available"] and result[name]["reason"]


def test_inconsistent_output_is_reported_without_silently_replacing_it():
    args = packets()
    vector(args[3], 9, 0x40, [(254, 1), (12, 2)], output=999)
    result = row(args)
    assert result["host"]["output"] == 999
    assert result["host"]["computed_output"] == 1
    assert result["host"]["consistent"] is False
    assert any("does not match" in item for item in result["observations"])


@pytest.mark.parametrize("block", ["info_version", "control_type", "status_type"])
def test_wrong_row_identity_does_not_hide_other_rows(block):
    args = packets((4, 29))
    if block == "info_version":
        args[1][(0xCC + 4 * 0x104) // 4] = 0x30
    elif block == "control_type":
        args[2][(0x14 + 4 * 0xC4) // 4] = 17
    else:
        args[3][(0x9C + 4 * 0x1730) // 4] = 18
    result = decode(*args)
    assert result["available"]
    assert not result["policies"][0]["available"]
    assert result["policies"][1]["available"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda layout: layout.update(info=(0xCC, 0xFC)),
        lambda layout: layout.update(status=(0x9C, 0x1720)),
        lambda layout: layout.update(control=(0x14, 0xB8)),
        lambda layout: layout["sizes"].update({INFO: 20000}),
        lambda layout: layout.update(status_mask_word=0),
        lambda layout: layout.update(status_mask_word=True),
        lambda layout: layout.update(commands={INFO: 0x20802618}),
    ],
)
def test_untraced_geometries_are_independently_unavailable(mutate):
    args = packets()
    mutate(args[0])
    result = decode(*args)
    assert not result["available"] and result["reason"] and result["policies"] == []


@pytest.mark.parametrize("which", [1, 2, 3])
@pytest.mark.parametrize("bad", [-1, 1 << 32, 1.0, True, "1"])
def test_words_are_strict_unsigned_ints(which, bad):
    args = packets()
    args[which][-1] = bad
    assert not decode(*args)["available"]


@pytest.mark.parametrize("which", [1, 2, 3])
def test_truncated_packets_are_rejected_before_indexing(which):
    args = list(packets())
    args[which] = args[which][:-1]
    assert not decode(*args)["available"]


@pytest.mark.parametrize("which,offset", [(3, 1), (2, 4), (2, 3)])
def test_masks_and_control_header_must_agree(which, offset):
    args = packets()
    args[which][offset] ^= 1
    assert not decode(*args)["available"]


def test_empty_policy_mask_accepts_backend_absent_control_and_status():
    args = list(packets(()))
    args[2:] = [None, None]
    result = decode(*args)
    assert result["available"] and result["mask"] == 0 and result["policies"] == []


def test_nonempty_mask_cannot_skip_packets():
    args = list(packets())
    args[2:] = [None, None]
    assert not decode(*args)["available"]


def test_json_layout_keys_are_supported_and_input_is_unchanged():
    args = packets((0, 31))
    args[0]["sizes"] = {str(key): value for key, value in args[0]["sizes"].items()}
    args[0]["info"] = list(args[0]["info"])
    original = copy.deepcopy(args)
    assert decode(*args)["available"]
    assert args == original


@pytest.mark.parametrize("layout", [None, [], {"sizes": []}, {"sizes": {}}])
def test_missing_layout_is_unavailable(layout):
    result = decode(layout, None, None, None)
    assert not result["available"] and result["reason"]
