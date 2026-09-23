# Druta - GPU monitor and tuner for NVIDIA cards
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure recognition and editing of understood voltage-rail packets."""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


_DWORD_SIZE = 4
_COMMAND_WORD = 14
_PARAMS_SIZE_WORD = 15
_STATUS_WORD = 16
_SUPPORTED_RAIL_MASK = 0b11
_RECORD_VALUE_COUNT = 4
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1


@dataclass(frozen=True)
class RailPacketLayout:
    packet_size: int
    params_size: int
    get_command: int
    set_command: int
    mask_word: int
    boost_word: int
    record0: int
    record_stride: int
    record_type: int
    valid_word: int | None


KNOWN_LAYOUTS = (
    RailPacketLayout(1104, 1036, 0x2080B213, 0x2080F214, 18, 19, 20, 8, 5, 7),
    # Driver 566.36 constructs seven-dword records in nvapi64.dll at RVAs
    # 0x22DFC6..0x22E029; its type converter maps control type 0 to wire type 2.
    # See experiments/566-schema-static/README.md for the static evidence.
    RailPacketLayout(976, 908, 0x2080B213, 0x2080F214, 18, 19, 20, 7, 2, None),
    RailPacketLayout(716, 648, 0x20803213, 0x20803214, 17, 18, 19, 5, 1, None),
)


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_requested_mask(requested_mask: object) -> bool:
    if not isinstance(requested_mask, int) or isinstance(requested_mask, bool):
        return False
    return requested_mask != 0 and requested_mask & ~_SUPPORTED_RAIL_MASK == 0


def _u32(payload: bytes, word: int) -> int:
    return struct.unpack_from("<I", payload, word * _DWORD_SIZE)[0]


def _word_in_bounds(payload: bytes, word: int) -> bool:
    return 0 <= word and (word + 1) * _DWORD_SIZE <= len(payload)


def _record_in_bounds(payload: bytes, layout: RailPacketLayout, rail: int) -> bool:
    base = layout.record0 + rail * layout.record_stride
    owned_words = [base, *(base + 1 + offset for offset in range(_RECORD_VALUE_COUNT))]
    if layout.valid_word is not None:
        owned_words.append(base + layout.valid_word)
    return all(_word_in_bounds(payload, word) for word in owned_words)


def recognize_packet(payload: bytes, requested_mask: int) -> RailPacketLayout | None:
    """Return the exact understood GET layout represented by *payload*.

    Recognition deliberately depends only on packet contents. It does not infer
    a protocol from a driver, GPU identity, or an NVAPI getter version.
    """
    if not isinstance(payload, bytes) or not _valid_requested_mask(requested_mask):
        return None

    for layout in KNOWN_LAYOUTS:
        if len(payload) != layout.packet_size:
            continue
        required_words = (
            _COMMAND_WORD,
            _PARAMS_SIZE_WORD,
            _STATUS_WORD,
            layout.mask_word,
            layout.boost_word,
        )
        if not all(_word_in_bounds(payload, word) for word in required_words):
            continue
        if _u32(payload, _COMMAND_WORD) != layout.get_command:
            continue
        if _u32(payload, _PARAMS_SIZE_WORD) != layout.params_size:
            continue
        if _u32(payload, layout.mask_word) != requested_mask:
            continue
        if not all(
            _record_in_bounds(payload, layout, rail)
            for rail in range(2)
            if requested_mask & (1 << rail)
        ):
            continue
        return layout
    return None


def _validated_records(
    requested_mask: int, records: Mapping[int, Sequence[int]]
) -> dict[int, tuple[int, int, int, int]]:
    if not _valid_requested_mask(requested_mask):
        raise ValueError("requested_mask must select rail 0, rail 1, or both")
    if not isinstance(records, Mapping):
        raise ValueError("records must be a mapping")

    expected_rails = {rail for rail in range(2) if requested_mask & (1 << rail)}
    if any(not _is_plain_int(rail) for rail in records) or set(records) != expected_rails:
        raise ValueError("requested_mask must exactly match the record rails")

    validated: dict[int, tuple[int, int, int, int]] = {}
    for rail, values in records.items():
        try:
            record = tuple(values)
        except TypeError as exc:
            raise ValueError(f"rail {rail} record must contain four integers") from exc
        if len(record) != _RECORD_VALUE_COUNT:
            raise ValueError(f"rail {rail} record must contain four integers")
        if any(
            not _is_plain_int(value) or not _INT32_MIN <= value <= _INT32_MAX
            for value in record
        ):
            raise ValueError(f"rail {rail} record values must be signed int32 integers")
        validated[rail] = (record[0], record[1], record[2], record[3])
    return validated


def prepare_write(
    payload: bytes,
    requested_mask: int,
    records: Mapping[int, Sequence[int]],
    boost: int,
) -> bytes:
    """Create a SET packet by changing only the understood owned words."""
    validated = _validated_records(requested_mask, records)
    if not _is_plain_int(boost) or not 0 <= boost <= 100:
        raise ValueError("boost must be an integer from 0 through 100")

    layout = recognize_packet(payload, requested_mask)
    if layout is None:
        raise ValueError("payload is not an exact known rail GET packet")

    result = bytearray(payload)
    struct.pack_into("<I", result, _COMMAND_WORD * _DWORD_SIZE, layout.set_command)
    struct.pack_into("<I", result, layout.mask_word * _DWORD_SIZE, requested_mask)
    struct.pack_into("<I", result, layout.boost_word * _DWORD_SIZE, boost)
    for rail, values in validated.items():
        base = layout.record0 + rail * layout.record_stride
        struct.pack_into("<i", result, base * _DWORD_SIZE, layout.record_type)
        for offset, value in enumerate(values, start=1):
            struct.pack_into("<i", result, (base + offset) * _DWORD_SIZE, value)
        if layout.valid_word is not None:
            struct.pack_into("<i", result, (base + layout.valid_word) * _DWORD_SIZE, 1)
    return bytes(result)


def packet_summary(payload: bytes) -> str:
    """Return a compact, non-guessing description for diagnostics."""
    if not isinstance(payload, bytes):
        return "rail packet invalid: payload is not bytes"

    fields = [f"size={len(payload)}"]
    if not all(
        _word_in_bounds(payload, word)
        for word in (_COMMAND_WORD, _PARAMS_SIZE_WORD, _STATUS_WORD)
    ):
        return "rail packet " + " ".join((*fields, "layout=unknown"))

    command = _u32(payload, _COMMAND_WORD)
    params_size = _u32(payload, _PARAMS_SIZE_WORD)
    status = _u32(payload, _STATUS_WORD)
    fields.extend(
        (
            f"command=0x{command:08X}",
            f"params={params_size}",
            f"status=0x{status:08X}",
        )
    )
    layout = next(
        (
            candidate
            for candidate in KNOWN_LAYOUTS
            if len(payload) == candidate.packet_size
            and params_size == candidate.params_size
            and command in (candidate.get_command, candidate.set_command)
        ),
        None,
    )
    if layout is None or not _word_in_bounds(payload, layout.mask_word):
        fields.append("layout=unknown")
    else:
        operation = "get" if command == layout.get_command else "set"
        fields.extend((f"layout={layout.packet_size}/{layout.params_size}", f"operation={operation}"))
        fields.append(f"mask=0x{_u32(payload, layout.mask_word):X}")
    return "rail packet " + " ".join(fields)
