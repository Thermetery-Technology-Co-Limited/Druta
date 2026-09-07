"""Read a GK104/GM107 VBIOS performance table, without GPU access or ROM writes.

The v0x40 layout/frequency mask follows Nouveau's bios/perf.c; source roles
follow clk/gk104.c. The short clock labels and P-state display names were
cross-checked against the supplied GTX 690 and GM107 ROMs and BIOS Tweaker
clock-state displays. Nouveau source names below describe the GK104 reference
implementation; they do not establish Maxwell register mappings.
These are BIOS table indices, NOT private NVAPI GetAllClocks domain IDs.
This deliberately decodes only the nine-clock schema verified on those ROMs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

SOURCES = {
    "performance_layout": "https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/bios/perf.c",
    "gk104_clock_sources": "https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/clk/gk104.c",
}
CLOCKS = (
    ("GPC", "gpc"), ("XBAR", "hubk07"), ("L2C", "rop"),
    ("DDR", "mem"), ("SYS", "hubk06"), ("HUB", "hubk01"),
    ("MSD", "vdec"), ("PWR", "pmu"), ("DISP", None),
)


class DecodeError(ValueError):
    pass


def decode(data: bytes) -> dict:
    """Return auditable file offsets, raw fields, and stored MHz; never infer NVAPI IDs."""
    def span(start, size, limit=None):
        end = len(data) if limit is None else limit
        if start < 0 or size < 0 or start + size > end:
            raise DecodeError(f"out-of-bounds ROM span: {start:#x}+{size:#x}")
        return data[start:start + size]

    def u16(start, limit=None):
        return struct.unpack("<H", span(start, 2, limit))[0]

    # The supplied NVGI dumps identify their PCI-image offset at 0x14:
    # GTX 690 uses 0x400, GM107 uses 0x600. Never search arbitrary payload
    # bytes for 55 AA: later EFI images and instruction data can match too.
    if data[:2] == b"\x55\xaa":
        base = 0
    elif data[:4] == b"NVGI":
        base = struct.unpack("<I", span(0x14, 4))[0]
        if base < 0x18 or base % 512 or span(base, 2) != b"\x55\xaa":
            raise DecodeError("invalid NVGI PCI-image offset/signature")
    else:
        raise DecodeError("expected plain PCI ROM or NVGI container")
    image_size = span(base + 2, 1)[0] * 512
    if image_size < 32:
        raise DecodeError("invalid PCI ROM image length")
    image = span(base, image_size)
    end = base + image_size
    if sum(image) & 255:
        raise DecodeError("PCI ROM checksum is not zero")
    pcir = base + u16(base + 0x18, end)
    if span(pcir, 4, end) != b"PCIR":
        raise DecodeError("missing PCIR structure")
    vendor, device = u16(pcir + 4, end), u16(pcir + 6, end)
    if vendor != 0x10DE:
        raise DecodeError("not an NVIDIA PCI ROM")
    positions = []
    pos = base
    while (pos := data.find(b"\xff\xb8BIT\x00", pos, end)) >= 0:
        positions.append(pos)
        pos += 1
    if len(positions) != 1:
        raise DecodeError("expected exactly one BIT directory")
    bit = positions[0]
    header = span(bit, 12, end)
    hlen, stride, count = header[8:11]
    if hlen < 12 or stride < 6 or sum(span(bit, hlen, end)) & 255:
        raise DecodeError("invalid BIT directory header/checksum")
    span(bit + hlen, stride * count, end)
    entries = [bit + hlen + i * stride for i in range(count)]
    p_entries = [offset for offset in entries if data[offset] == ord("P")]
    if len(p_entries) != 1:
        raise DecodeError("expected exactly one BIT P entry")
    p_entry = p_entries[0]
    if data[p_entry + 1] not in (1, 2) or u16(p_entry + 2, end) < 4:
        raise DecodeError("unsupported BIT P version/length")
    p_payload = base + u16(p_entry + 4, end)
    span(p_payload, u16(p_entry + 2, end), end)
    pointer = struct.unpack("<I", span(p_payload, 4, end))[0]
    if pointer == 0:
        raise DecodeError("missing performance table pointer")
    table = base + pointer
    version, hlen, rlen, clen, clocks, states = span(table, 6, end)
    if version != 0x40 or hlen < 6 or rlen < 1 or clen < 2 or clocks != 9:
        raise DecodeError("unsupported performance table; expected v0x40 nine-clock GK104/GM107 layout")
    if states == 0:
        raise DecodeError("empty performance table")
    record_size = rlen + clocks * clen
    span(table, hlen + states * record_size, end)
    rows = []
    for i in range(states):
        offset = table + hlen + i * record_size
        raw_state = data[offset]
        row = {"file_offset": offset, "raw_state": raw_state,
               "disabled": raw_state == 0xFF,
               "pstate": 15 - raw_state if raw_state <= 15 else None,
               "header_hex": span(offset, rlen, end).hex(), "clocks": []}
        for index, (label, source) in enumerate(CLOCKS):
            at = offset + rlen + index * clen
            raw = u16(at, end)
            row["clocks"].append({"bios_clock_index": index, "label": label,
                                  "nouveau_source": source, "file_offset": at,
                                  "raw_word": raw, "stored_mhz": raw & 0x3FFF,
                                  "flags": raw & 0xC000,
                                  "record_hex": span(at, clen, end).hex()})
        rows.append(row)
    return {"sha256": hashlib.sha256(data).hexdigest(), "file_size": len(data),
            "rom_base": base, "rom_size": image_size, "rom_checksum": 0,
            "pci_vendor": vendor, "pci_device": device, "bit_offset": bit,
            "bit_p_payload_offset": p_payload, "performance_table_offset": table,
            "table_version": version, "table_header_length": hlen,
            "table_header_hex": span(table, hlen, end).hex(),
            "state_header_length": rlen, "clock_record_length": clen,
            "clock_count": clocks, "states": rows, "sources": SOURCES,
            "scope": "Nine-clock GK104/GM107 BIOS clock-state table only; labels cross-checked with GTX 690 and the supplied edited GM107 ROM. Nouveau source names refer to GK104. Stored frequencies do not establish live clocks, writable controls, NVAPI domain IDs, or factory settings."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom", type=Path)
    args = parser.parse_args()
    try:
        result = decode(args.rom.read_bytes())
    except (OSError, DecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
