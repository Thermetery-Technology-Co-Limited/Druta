"""Offline checks for relationship weight binding and exact INFO projection."""

import copy
import struct

import pytest

from druta import power_relationships, vbios_fields
from druta import power_weight_runtime as weight
from tests.test_linked_current_runtime import LAYOUT, inspection, observation
from tests.test_vbios_fields import apply, fixture, v30_power_fixture


def setup_case():
    bios = inspection()
    table = bios["tables"][0]
    offset = table["offset"] + table["size"] + 2 * 18
    bios["fields"].append({
        "key": "power_relationship.2.weight", "relationship_index": 2,
        "relationship_kind": 1, "relationship_stride": 18, "relationship_count": 4,
        "relationship_offset": offset, "target_policy": 9, "offsets": [offset + 2],
        "encoding": "H", "width": 2, "scale": 4096, "mask": None,
        "units": "multiplier", "capability": "editable", "current": 1, "raw": 4096,
    })
    raw = bytearray.fromhex(observation()["info_hex"])
    struct.pack_into("<I", raw, 0x41CC, 1 << 2)
    raw[0x41D0 + 2 * 0x24:0x41D0 + 2 * 0x24 + 2] = bytes((0, 9))
    struct.pack_into("<H", raw, 0x41D0 + 2 * 0x24 + 6, 4096)
    report = power_relationships.decode(LAYOUT, struct.unpack(f"<{len(raw)//4}I", raw))
    report["identity"] = observation()["identity"]
    report["info_hex"] = raw.hex()
    return bios, report


def test_contract_and_full_info_projection(monkeypatch):
    bios, before = setup_case()
    monkeypatch.setattr(weight, "read", lambda gpu: (copy.deepcopy(before), None))
    changes = {"power_relationship.2.weight": 4080 / 4096}
    saved = weight.contracts(None, bios, changes)
    weight.validate_inspection(bios, changes, saved)
    after = copy.deepcopy(before)
    raw = bytearray.fromhex(after["info_hex"])
    struct.pack_into("<H", raw, 0x41D0 + 2 * 0x24 + 6, 4080)
    after["info_hex"] = raw.hex()
    after["relationships"][0]["weight_raw"] = 4080
    assert weight.verify(after, saved) == list(changes)
    assert weight.verify(before, saved, restore=True) == list(changes)
    raw[30] ^= 1
    after["info_hex"] = raw.hex()
    with pytest.raises(weight.PowerWeightError):
        weight.verify(after, saved)
    with pytest.raises(weight.PowerWeightError):
        weight.verify(None, saved)


def test_stale_target_and_unrepresentable_values(monkeypatch):
    bios, before = setup_case()
    monkeypatch.setattr(weight, "read", lambda gpu: (copy.deepcopy(before), None))
    key = "power_relationship.2.weight"
    with pytest.raises(weight.PowerWeightError):
        weight.contracts(None, bios, {key: 1.00001})
    saved = weight.contracts(None, bios, {key: 1})
    bios["fields"][-1]["target_policy"] = 8
    with pytest.raises(weight.PowerWeightError):
        weight.validate_inspection(bios, {key: 1}, saved)
    before["relationships"][0]["type"] = 5
    with pytest.raises(weight.PowerWeightError):
        weight.verify(before, saved, restore=True)


@pytest.mark.parametrize("value", [0, 4097 / 4096, 65535 / 4096])
def test_synthetic_rom_low_halfword_only(value):
    blob = bytearray(fixture())
    table, index, target = 0x3800, 3, 7
    count, stride = 4, 18
    size = 0x38 + 20 * 0x66
    # Extend the existing synthetic power object's ownership over relationships.
    struct.pack_into("<I", blob, 0x2210 + 2 * 60 + 8, size + count * stride)
    blob[table + 4:table + 6] = bytes((stride, count))
    relation = table + size + index * stride
    blob[relation:relation + stride] = bytes((1, target, 0, 16, 0xAD, 0xDE)) + bytes(range(12))
    key = f"power_relationship.{index}.weight"
    result = vbios_fields.inspect(blob)
    field = next(f for f in result["fields"] if f["key"] == key)
    assert field["target_policy"] == target
    changed = apply(bytes(blob), vbios_fields.plan(blob, {key: value}))
    expected = bytearray(blob)
    struct.pack_into("<H", expected, relation + 2, int(value * 4096))
    assert changed == expected
    assert changed[relation + 4:relation + 6] == b"\xad\xde"


def test_unknown_stride_and_legacy_are_not_editable():
    blob = bytearray(fixture())
    blob[0x3804:0x3806] = bytes((19, 4))
    for image in (blob, v30_power_fixture()):
        assert not any(f["key"].startswith("power_relationship.") for f in vbios_fields.inspect(image)["fields"])


def test_short_v30_relationship_plan_and_compact_info_verification(monkeypatch):
    image = bytearray(v30_power_fixture(stride=0x42, count=2))
    table, target = 0x3800, 1
    relation = table + 0x2C + 2 * 0x42
    image[table + 8:table + 10] = bytes((14, 1))
    struct.pack_into("<BBH", image, relation, 1, target, 4096)
    bios = vbios_fields.inspect(image)
    limits = [f for f in bios["fields"] if f["key"].startswith("power.")]
    assert len(limits) == 6 and all(f["policy_type"] == 1 and f["capability"] == "read-only" for f in limits)
    assert [f["current"] for f in limits if f["key"].endswith(".maximum")] == [380000, 380000]
    key = "power_relationship.0.weight"
    changes = {key: 4097 / 4096}
    expected = bytearray(image)
    struct.pack_into("<H", expected, relation + 2, 4097)
    assert apply(bytes(image), vbios_fields.plan(image, changes)) == bytes(expected)

    raw = bytearray(20000)
    struct.pack_into("<I", raw, 4, 1 << target)
    struct.pack_into("<5I", raw, 0xCC + 0xFC * target, 0x30, 5 << 8, 100000, 310000, 380000)
    struct.pack_into("<I", raw, 0x3FCC, 1)
    struct.pack_into("<BB4xH", raw, 0x3FD0, 0, target, 4096)

    def read_packet():
        report = power_relationships.decode(
            {"info": (0xCC, 0xFC), "sizes": {0x2080A618: 20000}},
            struct.unpack("<5000I", raw),
        )
        return dict(report, identity=observation()["identity"], info_hex=raw.hex())

    before = read_packet()
    monkeypatch.setattr(weight, "read", lambda gpu: (before, None))
    saved = weight.contracts(None, bios, changes)
    assert weight.verify(before, saved, restore=True) == [key]
    struct.pack_into("<H", raw, 0x3FD6, 4097)
    assert weight.verify(read_packet(), saved) == [key]
    raw[-1] ^= 1
    with pytest.raises(weight.PowerWeightError, match="Unselected INFO bytes"):
        weight.verify(read_packet(), saved)


def test_missing_target_or_invalid_identity_rejected(monkeypatch):
    bios, before = setup_case()
    key = "power_relationship.2.weight"
    monkeypatch.setattr(weight, "read", lambda gpu: (copy.deepcopy(before), None))
    before["relationships"][0]["target_metadata"] = None
    with pytest.raises(weight.PowerWeightError):
        weight.contracts(None, bios, {key: 1})
    bios, before = setup_case()
    before["identity"]["slot"] = "not-a-pci-slot"
    with pytest.raises((weight.PowerWeightError, ValueError)):
        weight.contracts(None, bios, {key: 1})
