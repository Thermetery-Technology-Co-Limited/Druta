"""LPWR media-entry field masks and exact byte-preserving plans."""

import struct

import pytest

from druta import vbios_fields as vf
from tests.test_vbios_fields import apply, extend, fixture

PROFILES = {
    "nvenc": (0x120, 0xB5, 1),
    "ofa": (0x124, 0xB6, 1),
    "nvjpg": (0x128, 0xB7, 2),
}


def image(family, flags):
    pointer, owner, _mask = PROFILES[family]
    table = bytearray(10 + 2 * len(flags))
    table[:4] = bytes((0x10, 10, 2, len(flags)))
    for index, value in enumerate(flags):
        struct.pack_into("<H", table, 10 + 2 * index, value)
    return extend(fixture(), [(pointer, owner, 0x4A00, table)])


@pytest.mark.parametrize("family", ["nvenc", "ofa"])
def test_nvenc_and_ofa_keep_bit0_enable_mask(family):
    blob = image(family, (0xFFF7, 0xFFF7))
    key = f"lpwr.{family}.0.enabled"
    fields = {field["key"]: field for field in vf.inspect(blob)["fields"]}
    assert fields[key]["mask"] == 1
    assert fields[key]["current"] is True
    changed = apply(blob, vf.plan(blob, {key: False}))
    assert struct.unpack_from("<H", changed, 0x4A0A)[0] == 0xFFF6
    assert struct.unpack_from("<H", changed, 0x4A0C)[0] == 0xFFF7


def test_nvjpg_enabled_uses_bit1_and_preserves_bit0_and_opaque_bits():
    blob = image("nvjpg", (0xFFF7, 0xFFFE))
    key = "lpwr.nvjpg.0.enabled"
    fields = {field["key"]: field for field in vf.inspect(blob)["fields"]}
    assert fields[key]["mask"] == 2
    assert fields[key]["current"] is True
    changed = apply(blob, vf.plan(blob, {key: False}))
    before = struct.unpack_from("<H", blob, 0x4A0A)[0]
    after = struct.unpack_from("<H", changed, 0x4A0A)[0]
    assert before == 0xFFF7 and after == 0xFFF5
    assert before ^ after == 2
    assert after & 1 == before & 1
    assert struct.unpack_from("<H", changed, 0x4A0C)[0] == 0xFFFE


def test_nvjpg_bit1_not_bit0_controls_enabled_and_disable_only():
    blob = image("nvjpg", (0xFFF6, 0xFFF5))
    fields = {field["key"]: field for field in vf.inspect(blob)["fields"]}
    enabled = fields["lpwr.nvjpg.0.enabled"]
    disabled = fields["lpwr.nvjpg.1.enabled"]
    assert enabled["current"] is True and enabled["capability"] == "editable"
    assert enabled["mask"] == 2
    assert disabled["current"] is False and disabled["capability"] == "read-only"
    with pytest.raises(ValueError, match="read-only"):
        vf.plan(blob, {"lpwr.nvjpg.1.enabled": True})
