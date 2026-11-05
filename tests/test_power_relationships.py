"""Offline tests of the configured relationship INFO decoder."""

import copy
import struct

import pytest

from druta.power_relationships import decode

INFO = 0x2080A618


def packet():
    layout = {"info": (0xCC, 0x104), "sizes": {INFO: 20512}}
    raw = bytearray(20512)
    struct.pack_into("<I", raw, 4, 1 << 9)
    at = 0xCC + 9 * 0x104
    # Compact policy 9 metadata from the saved 5080 / 617.42 INFO capture.
    struct.pack_into("<5I", raw, at, 0x40, 30 | (9 << 8) | (1 << 16), 1, 45000, 45000)
    struct.pack_into("<I", raw, 0x41CC, 1 << 9)
    # Saved relation 9 begins 00 09 00 00 00 00 00 10.
    raw[0x41D0 + 9 * 0x24:0x41D0 + 9 * 0x24 + 8] = bytes.fromhex("0009000000000010")
    return layout, raw


def decode_raw(layout, raw):
    return decode(layout, list(struct.unpack(f"<{len(raw) // 4}I", raw)))


def test_saved_relation_target_and_unity_weight():
    layout, raw = packet()
    original = bytes(raw)
    result = decode_raw(layout, raw)
    assert result == {
        "available": True,
        "reason": None,
        "policy_mask": 1 << 9,
        "relationship_mask": 1 << 9,
        "source_references": [],
        "linked_references": [],
        "range_references": [],
        "source_uninterpreted_policies": [9],
        "relationships": [{
            "index": 9, "type": 0, "target": 9, "weight_raw": 4096,
            "target_reason": None,
            "target_metadata": {"policy": 9, "type": 30, "channel": 9, "unit": "mA",
                                "minimum": 1, "default": 45000, "maximum": 45000},
        }],
    }
    assert raw == original


def test_alternate_indices_nonunity_and_unknown_class_remain_visible():
    layout, raw = packet()
    struct.pack_into("<I", raw, 4, (1 << 2) | (1 << 31))
    for index, kind, channel, unit in ((2, 8, 3, 0), (31, 17, 200, 1)):
        struct.pack_into("<5I", raw, 0xCC + index * 0x104,
                         0x40, kind | (channel << 8) | (unit << 16), 5, 100, 300)
    struct.pack_into("<I", raw, 0x41CC, (1 << 0) | (1 << 31))
    at = 0x41D0
    raw[at:at + 8] = bytes.fromhex("001f000000000018")
    at += 31 * 0x24
    raw[at:at + 8] = bytes.fromhex("0702000000003412")
    result = decode_raw(layout, raw)
    assert result["available"]
    first, second = result["relationships"]
    assert (first["index"], first["target"], first["weight_raw"]) == (0, 31, 6144)
    assert first["target_metadata"]["channel"] == 200
    assert (second["index"], second["type"], second["target"], second["weight_raw"]) == (31, 7, 2, None)
    assert second["target_metadata"]["unit"] == "mW"


def test_missing_and_unknown_target_format_are_local_failures():
    layout, raw = packet()
    struct.pack_into("<I", raw, 0x41CC, (1 << 9) | (1 << 10) | (1 << 11))
    raw[0x41D0 + 10 * 0x24 + 1] = 8
    raw[0x41D0 + 11 * 0x24 + 1] = 31
    struct.pack_into("<I", raw, 4, (1 << 9) | (1 << 8))
    struct.pack_into("<I", raw, 0xCC + 8 * 0x104, 0x30)
    result = decode_raw(layout, raw)
    assert result["available"]
    assert result["relationships"][0]["target_metadata"] is not None
    assert "format" in result["relationships"][1]["target_reason"]
    assert "absent" in result["relationships"][2]["target_reason"]


def test_unknown_unit_does_not_invalidate_other_records():
    layout, raw = packet()
    struct.pack_into("<I", raw, 0xCC + 9 * 0x104 + 4, 30 | (9 << 8) | (8 << 16))
    result = decode_raw(layout, raw)
    assert result["available"]
    assert result["relationships"][0]["target_metadata"] is None
    assert "unit" in result["relationships"][0]["target_reason"]


def test_empty_mask_and_info_only_layout():
    layout, raw = packet()
    struct.pack_into("<I", raw, 0x41CC, 0)
    assert decode_raw(layout, raw)["relationships"] == []
    layout["status"] = "unrelated"
    assert decode_raw(layout, raw)["available"]


@pytest.mark.parametrize("change", ["base", "size", "command", "truncated", "word"])
def test_unrecognized_info_or_bad_packet_is_unavailable(change):
    layout, raw = packet()
    words = list(struct.unpack(f"<{len(raw) // 4}I", raw))
    if change == "base":
        layout["info"] = (0xD0, 0x104)
    elif change == "size":
        layout["sizes"][INFO] = 1
    elif change == "command":
        layout["commands"] = {INFO: INFO + 1}
    elif change == "truncated":
        words.pop()
    else:
        words[0] = -1
    snapshot = copy.deepcopy(words)
    result = decode(layout, words)
    assert not result["available"] and result["reason"]
    assert result["relationships"] == []
    assert words == snapshot



def source_policy(raw, index=2, *, flag_a=0, flag_b=1, first=9, last=9, version=0x40):
    struct.pack_into("<I", raw, 4, struct.unpack_from("<I", raw, 4)[0] | (1 << index))
    at = 0xCC + index * 0x104
    struct.pack_into("<5I", raw, at, version, 0, 1, 450000, 450000)
    raw[at + 0xB9] = flag_a
    raw[at + 0xC0] = flag_b
    struct.pack_into("<HH", raw, at + 0xBC, first, last)


def test_saved_type0_source_range_and_uninterpreted_policies():
    layout, raw = packet()
    source_policy(raw)
    result = decode_raw(layout, raw)
    assert result["source_references"] == [{
        "policy": 2, "available": True, "reason": None,
        "enabled": True, "first": 9, "last": 9, "relationships": [9],
    }]
    assert result["source_uninterpreted_policies"] == [9]


@pytest.mark.parametrize("flag_a,flag_b", [(1, 0), (0, 1), (1, 1)])
def test_either_valid_enable_flag_exposes_range(flag_a, flag_b):
    layout, raw = packet()
    source_policy(raw, flag_a=flag_a, flag_b=flag_b)
    assert decode_raw(layout, raw)["source_references"][0]["relationships"] == [9]


def test_disabled_range_does_not_interpret_unused_indices():
    layout, raw = packet()
    source_policy(raw, flag_a=0, flag_b=0, first=0xFFFF, last=0)
    row = decode_raw(layout, raw)["source_references"][0]
    assert row["available"] and row["enabled"] is False and row["relationships"] == []


@pytest.mark.parametrize("problem", ["flags", "order", "capacity", "missing_relation", "unknown_type", "missing_target", "version"])
def test_invalid_source_is_local_and_remains_visible(problem):
    layout, raw = packet()
    source_policy(raw)
    at = 0xCC + 2 * 0x104
    if problem == "flags":
        raw[at + 0xB9] = 2
    elif problem == "order":
        struct.pack_into("<HH", raw, at + 0xBC, 10, 9)
    elif problem == "capacity":
        struct.pack_into("<HH", raw, at + 0xBC, 9, 32)
    elif problem == "missing_relation":
        struct.pack_into("<I", raw, 0x41CC, 0)
    elif problem == "unknown_type":
        raw[0x41D0 + 9 * 0x24] = 3
    elif problem == "missing_target":
        raw[0x41D0 + 9 * 0x24 + 1] = 8
    else:
        raw[at] = 0x30
    result = decode_raw(layout, raw)
    assert result["available"]
    assert len(result["source_references"]) == 1
    assert not result["source_references"][0]["available"]
    assert result["source_references"][0]["reason"]
    assert result["source_references"][0]["relationships"] == []
    assert result["source_uninterpreted_policies"] == [9]


def test_inclusive_range_requires_every_relationship_and_target():
    layout, raw = packet()
    source_policy(raw, first=8, last=9)
    struct.pack_into("<I", raw, 0x41CC, (1 << 8) | (1 << 9))
    raw[0x41D0 + 8 * 0x24 + 1] = 9
    result = decode_raw(layout, raw)
    assert result["source_references"][0]["relationships"] == [8, 9]
    raw[0x41D0 + 8 * 0x24] = 5
    result = decode_raw(layout, raw)
    assert not result["source_references"][0]["available"]
    assert result["source_references"][0]["relationships"] == []



def linked_policy(raw, *, index=7, relation=3, version=0x40):
    struct.pack_into("<I", raw, 4, struct.unpack_from("<I", raw, 4)[0] | (1 << index))
    at = 0xCC + index * 0x104
    struct.pack_into("<5I", raw, at, version, 32, 1, 450000, 450000)
    struct.pack_into("<HH", raw, at + 0x34, relation, 0xBBAA)
    return at


def test_linked_source_joins_relation_to_distinct_target_and_preserves_opaque_bytes():
    layout, raw = packet()
    at = linked_policy(raw)
    struct.pack_into("<I", raw, 0x41CC, (1 << 3) | (1 << 9))
    rel = 0x41D0 + 3 * 0x24
    raw[rel:rel + 8] = bytes.fromhex("0009000000000010")
    before = bytes(raw)
    result = decode_raw(layout, raw)
    assert result["linked_references"] == [{
        "policy": 7, "relationship": 3, "target": 9, "available": True, "reason": None,
    }]
    assert result["source_uninterpreted_policies"] == [9]
    assert raw[at + 0x36:at + 0x38] == b"\xaa\xbb"
    assert raw == before


@pytest.mark.parametrize("problem", ["missing_relation", "capacity", "missing_target", "unknown_relation", "version"])
def test_bad_link_is_local_unavailable(problem):
    layout, raw = packet()
    at = linked_policy(raw)
    struct.pack_into("<I", raw, 0x41CC, (1 << 3) | (1 << 9))
    rel = 0x41D0 + 3 * 0x24
    raw[rel:rel + 8] = bytes.fromhex("0009000000000010")
    if problem == "missing_relation":
        struct.pack_into("<I", raw, 0x41CC, 1 << 9)
    elif problem == "capacity":
        struct.pack_into("<H", raw, at + 0x34, 32)
    elif problem == "missing_target":
        raw[rel + 1] = 6
    elif problem == "unknown_relation":
        raw[rel] = 8
    else:
        raw[at] = 0x30
    result = decode_raw(layout, raw)
    assert result["available"]
    assert len(result["linked_references"]) == 1
    assert not result["linked_references"][0]["available"]
    assert result["linked_references"][0]["reason"]
    assert 7 not in result["source_uninterpreted_policies"]
    assert any(item["index"] == 9 and item["target_metadata"] for item in result["relationships"])


def range_policy(raw, first=2, last=4, *, index=7, kind=33, version=0x40):
    struct.pack_into("<I", raw, 4, struct.unpack_from("<I", raw, 4)[0] | (1 << index))
    at = 0xCC + index * 0x104
    struct.pack_into("<5I", raw, at, version, kind, 1, 100, 200)
    struct.pack_into("<HH", raw, at + 0x34, first, last)
    return at


def range_packet():
    layout, raw = packet()
    range_policy(raw)
    targets = (9, 11, 12)
    struct.pack_into("<I", raw, 4, struct.unpack_from("<I", raw, 4)[0] | (1 << 11) | (1 << 12))
    for index in (11, 12):
        struct.pack_into("<5I", raw, 0xCC + index * 0x104, 0x40, 30 | (1 << 16), 1, 100, 200)
    struct.pack_into("<I", raw, 0x41CC, sum(1 << index for index in (2, 3, 4, 9)))
    for index, target in zip((2, 3, 4), targets, strict=True):
        raw[0x41D0 + index * 0x24:0x41D0 + index * 0x24 + 2] = bytes((0, target))
    return layout, raw


def test_type33_range_joins_distinct_targets_and_displays():
    from druta.registry_panel import RegistryPanel

    layout, raw = range_packet()
    before = bytes(raw)
    result = decode_raw(layout, raw)
    assert result["range_references"] == [{
        "policy": 7, "first": 2, "last": 4, "relationships": [2, 3, 4],
        "available": True, "reason": None,
    }]
    assert [r["target"] for r in result["relationships"] if r["index"] in (2, 3, 4)] == [9, 11, 12]
    assert "Source policy 7 -> configured relationship range 2..4: 2, 3, 4" in RegistryPanel.relationship_lines(result)
    assert raw == before


@pytest.mark.parametrize("first,last", [(4, 2), (0xFFFF, 0), (32, 31)])
def test_type33_reversed_range_is_empty_without_lookup(first, last):
    from druta.registry_panel import RegistryPanel

    layout, raw = packet()
    range_policy(raw, first, last)
    result = decode_raw(layout, raw)
    row = result["range_references"][0]
    assert row["available"] and row["relationships"] == []
    assert f"Source policy 7 -> configured relationship range {first}..{last}: empty" in RegistryPanel.relationship_lines(result)


@pytest.mark.parametrize("problem", ["gap", "unknown_relation", "missing_target", "version", "capacity", "sentinel"])
def test_type33_invalid_or_unavailable_range_is_visible(problem):
    from druta.registry_panel import RegistryPanel

    layout, raw = range_packet()
    at = 0xCC + 7 * 0x104
    if problem == "gap":
        struct.pack_into("<I", raw, 0x41CC, (1 << 2) | (1 << 4) | (1 << 9))
    elif problem == "unknown_relation":
        raw[0x41D0 + 3 * 0x24] = 9
    elif problem == "missing_target":
        raw[0x41D0 + 3 * 0x24 + 1] = 8
    elif problem == "version":
        raw[at] = 0x30
    else:
        struct.pack_into("<HH", raw, at + 0x34, 0, 0xFFFF if problem == "sentinel" else 32)
    result = decode_raw(layout, raw)
    row = result["range_references"][0]
    assert not row["available"] and row["reason"] and row["relationships"] == []
    assert any("Source policy 7 -> configured relationship range" in line and "unavailable" in line
               for line in RegistryPanel.relationship_lines(result))
    assert result["available"] and 7 not in result["source_uninterpreted_policies"]


def test_type3_is_not_type33_range():
    layout, raw = packet()
    range_policy(raw, kind=3)
    result = decode_raw(layout, raw)
    assert result["range_references"] == [] and 7 in result["source_uninterpreted_policies"]
