# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

import struct

import pytest

from druta.rail_transport import KNOWN_LAYOUTS, packet_summary, prepare_write, recognize_packet


def packet(layout, mask, *, fill=0xA5):
    payload = bytearray([fill] * layout.packet_size)
    struct.pack_into("<I", payload, 14 * 4, layout.get_command)
    struct.pack_into("<I", payload, 15 * 4, layout.params_size)
    struct.pack_into("<I", payload, 16 * 4, 0x12345678)
    struct.pack_into("<I", payload, layout.mask_word * 4, mask)
    return bytes(payload)


def i32(payload, word):
    return struct.unpack_from("<i", payload, word * 4)[0]


def test_known_layouts_include_statically_recovered_566_schema():
    layout = next(item for item in KNOWN_LAYOUTS if item.packet_size == 976)
    assert layout == type(layout)(
        976, 908, 0x2080B213, 0x2080F214, 18, 19, 20, 7, 2, None
    )


@pytest.mark.parametrize("layout", KNOWN_LAYOUTS)
def test_recognizes_and_prepares_each_known_layout(layout):
    source = packet(layout, 3)
    records = {0: (-1, 2, -3, 4), 1: (5, -6, 7, -(1 << 31))}

    assert recognize_packet(source, 3) is layout
    result = prepare_write(source, 3, records, 42)

    assert struct.unpack_from("<I", result, 14 * 4)[0] == layout.set_command
    assert struct.unpack_from("<I", result, 15 * 4)[0] == layout.params_size
    assert struct.unpack_from("<I", result, 16 * 4)[0] == 0x12345678
    assert struct.unpack_from("<I", result, layout.mask_word * 4)[0] == 3
    assert struct.unpack_from("<I", result, layout.boost_word * 4)[0] == 42
    for rail, values in records.items():
        base = layout.record0 + rail * layout.record_stride
        assert i32(result, base) == layout.record_type
        assert tuple(i32(result, base + offset) for offset in range(1, 5)) == values
        if layout.valid_word is not None:
            assert i32(result, base + layout.valid_word) == 1


@pytest.mark.parametrize("layout", KNOWN_LAYOUTS)
def test_prepare_write_preserves_every_unowned_byte(layout):
    source = packet(layout, 1, fill=0x6C)
    result = prepare_write(source, 1, {0: (10, 20, 30, 40)}, 75)
    owned_words = {14, layout.mask_word, layout.boost_word, layout.record0}
    owned_words.update(layout.record0 + offset for offset in range(1, 5))
    if layout.valid_word is not None:
        owned_words.add(layout.record0 + layout.valid_word)

    assert len(result) == len(source)  # zip(strict=True) is Python 3.10+
    for index, (before, after) in enumerate(zip(source, result)):
        if index // 4 not in owned_words:
            assert after == before


def test_566_full_record_tail_is_preserved():
    layout = next(item for item in KNOWN_LAYOUTS if item.packet_size == 976)
    source = bytearray(packet(layout, 1, fill=0))
    base = layout.record0
    struct.pack_into("<II", source, (base + 5) * 4, 0x11223344, 0xAABBCCDD)

    result = prepare_write(bytes(source), 1, {0: (-1, -2, -3, -4)}, 9)

    assert struct.unpack_from("<II", result, (base + 5) * 4) == (0x11223344, 0xAABBCCDD)


@pytest.mark.parametrize("layout", KNOWN_LAYOUTS)
def test_malformed_packets_are_not_recognized_or_mutated(layout):
    source = packet(layout, 1)
    malformed = [
        source[:-1],
        source + b"\0",
        source[: 14 * 4] + struct.pack("<I", layout.set_command) + source[15 * 4 :],
        source[: 15 * 4] + struct.pack("<I", layout.params_size - 4) + source[16 * 4 :],
        source[: layout.mask_word * 4] + struct.pack("<I", 2) + source[(layout.mask_word + 1) * 4 :],
    ]
    for candidate in malformed:
        assert recognize_packet(candidate, 1) is None
        with pytest.raises(ValueError):
            prepare_write(candidate, 1, {0: (1, 2, 3, 4)}, 20)

    assert recognize_packet(bytearray(source), 1) is None


@pytest.mark.parametrize("layout", KNOWN_LAYOUTS)
def test_masks_records_values_and_boost_are_strictly_validated(layout):
    source = packet(layout, 1)
    bad_calls = [
        (0, {}, 20),
        (4, {}, 20),
        (True, {0: (1, 2, 3, 4)}, 20),
        (1, {1: (1, 2, 3, 4)}, 20),
        (1, {0: (1, 2, 3)}, 20),
        (1, {0: (1, 2, 3, 1 << 31)}, 20),
        (1, {0: (1, 2, 3, -(1 << 31) - 1)}, 20),
        (1, {0: (1, 2, 3, True)}, 20),
        (1, {0: (1, 2, 3, 4)}, -1),
        (1, {0: (1, 2, 3, 4)}, 101),
        (1, {0: (1, 2, 3, 4)}, True),
    ]
    for mask, records, boost in bad_calls:
        with pytest.raises(ValueError):
            prepare_write(source, mask, records, boost)


def test_packet_summary_reports_only_understood_layouts():
    layout = KNOWN_LAYOUTS[0]
    source = packet(layout, 1)
    summary = packet_summary(source)
    assert "size=1104" in summary
    assert "command=0x2080B213" in summary
    assert "params=1036" in summary
    assert "status=0x12345678" in summary
    assert "operation=get" in summary
    assert "mask=0x1" in summary

    assert packet_summary(b"short").endswith("layout=unknown")
    assert packet_summary(bytearray(source)) == "rail packet invalid: payload is not bytes"
