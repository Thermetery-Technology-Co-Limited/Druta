#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare the two supplied ROM files and the recorded P0 readbacks offline."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from struct import unpack_from

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from druta.vbios_fields import FormatError, _Image

EVIDENCE_PATH = Path(__file__).with_name("firmware-tfaw-20261119.json")
READER_SOURCE = "https://github.com/fmuniztriana/nvidia-bios-reader/blob/892723fd23537c639f89f1e8bf62ac6f92850236"
SOURCES = {
    "nvidia_bit": "https://download.nvidia.com/open-gpu-doc/BIOS-Information-Table/1/BIOS-Information-Table.html",
    "nvidia_clock_table": "https://download.nvidia.com/open-gpu-doc/MemoryClockTable/1/MemoryClockTable.html",
    "nvidia_tweak_table": "https://download.nvidia.com/open-gpu-doc/MemoryTweakTable/1/MemoryTweakTable.html",
    "envytools_training_layout": "https://github.com/envytools/envytools/blob/master/nvbios/mem.c",
    "nouveau_ramcfg_descriptor": "https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/bios/M0203.c",
    "reader_descriptor_and_timing_decode": f"{READER_SOURCE}/src/nvbios_reader.cpp",
    "reader_density_confidence": f"{READER_SOURCE}/docs/format-notes.md#memory-descriptor",
    "prior_p0_readbacks": EVIDENCE_PATH.name,
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def open_rom(raw):
    starts = []
    for start in range(0, len(raw) - 25, 512):
        if raw[start : start + 2] != b"\x55\xaa":
            continue
        pcir = start + unpack_from("<H", raw, start + 24)[0]
        if raw[pcir : pcir + 6] == b"PCIR\xde\x10" and pcir + 24 <= len(raw) and raw[pcir + 20] == 0:
            starts.append(start)
    if len(starts) != 1:
        raise FormatError("expected exactly one NVIDIA legacy PCI image in the ROM file")
    return starts[0], _Image(raw[starts[0] :])


def versioned_rows(image, wrapper, pointer, version, minimum_base):
    at = image.pointer("P", pointer)
    actual, header, base, sub_size, sub_count, count = image.span(at, 6)
    stride = base + sub_size * sub_count
    if actual != version or header < 6 or base < minimum_base or not count or (sub_count and not sub_size):
        raise FormatError(f"invalid BIT P+{pointer:#x} table geometry")
    extent = header + stride * count
    image.own(at, extent)
    rows = [image.span(at + header + i * stride, stride) for i in range(count)]
    return {
        "bit_p_pointer_offset": hex(pointer),
        "unadjusted_pointer": hex(image.u("I", image.tokens["P"]["offset"] + pointer)),
        "image_offset": hex(at),
        "file_offset": hex(at + wrapper),
        "version": hex(version),
        "header_bytes": header,
        "base_record_bytes": base,
        "subrecord_bytes": sub_size,
        "subrecords_per_row": sub_count,
        "stride_bytes": stride,
        "declared_rows": count,
        "extent_bytes": extent,
    }, rows


def positions(raw, pattern):
    hits = []
    offset = raw.find(pattern)
    while offset >= 0:
        hits.append(hex(offset))
        offset = raw.find(pattern, offset + 1)
    return hits


def memory_metadata(image):
    token = image.tokens["M"]
    at, size = token["offset"], token["size"]
    if token["version"] != 2 or size < 21:
        raise FormatError("expected BIT M v2 through Memory Script List")
    fields = (
        ("strap_translation", 1, "H"),
        ("memory_info", 3, "H"),
        ("candidate_training", 5, "I"),
        ("candidate_training_pattern", 9, "I"),
        ("memory_partition_info", 13, "I"),
        ("memory_script_list", 17, "I"),
    )
    pointers = {}
    for name, offset, kind in fields:
        ptr = image.u(kind, at + offset)
        record = {"token_offset": hex(offset), "raw": hex(ptr), "width_bits": 16 if kind == "H" else 32}
        if ptr:
            adjusted = image.pointer("M", offset) if kind == "I" else ptr
            record.update(image_offset=hex(adjusted), leading_bytes=image.span(adjusted, 12).hex())
        pointers[name] = record
    training = pointers["candidate_training"]
    pattern = pointers["candidate_training_pattern"]
    if "image_offset" in training and "image_offset" in pattern:
        location = int(training["image_offset"], 16)
        version, header, base, sub, columns, count, clock = unpack_from("<6BH", image.span(location, 8))
        if version == 0x10 and header >= 8 and base and sub and columns and count:
            extent = header + (base + sub * columns) * count
            image.own(location, extent)
            training["geometry"] = {
                "version": hex(version),
                "header_bytes": header,
                "base_record_bytes": base,
                "strap_cell_bytes": sub,
                "strap_cells_per_row": columns,
                "rows": count,
                "training_clock_header_value": clock,
                "extent_bytes": extent,
                "ends_at_pattern_pointer": location + extent == int(pattern["image_offset"], 16),
            }
    return {
        "version": token["version"],
        "length_bytes": size,
        "image_offset": hex(at),
        "strap_data_count": image.b[at],
        "pointers": pointers,
        "beyond_published_memory_script_pointer_hex": image.span(at + 21, size - 21).hex(),
        "training_pointer_provenance": "envytools labels M+5/M+9 TRAIN/TRAIN PATTERN using a historical 16-bit reader. Here the 32-bit pointers follow NVIDIA's BIT adjustment and the apparent v0x10 geometry follows envytools. Consumer and GDDR6X extension semantics are unresolved.",
    }


def p0_readback(evidence, board):
    trials = [
        (i, trial)
        for i, trial in enumerate(evidence["attempts"])
        if trial["board"] == board and trial["p0_evidence"] and trial["before_a"]["pstate"] == "P0"
    ]
    if not trials:
        raise ValueError(f"no prior P0 trial for {board}")
    return trials[-1]


def timing_fields(config1, config3):
    return {"tcl": config1 & 0x7F, "tfaw": (config3 >> 9) & 0xFF}


def ramcfg_profiles(image, wrapper, memory, clock, bands, ranges, tweak, rows, words, p0_band, candidates):
    pointer = memory["pointers"]["memory_info"]
    if "image_offset" not in pointer:
        raise FormatError("missing BIT M v2 Memory Information Table")
    at = int(pointer["image_offset"], 16)
    version, header, stride, count, lookup = image.span(at, 5)
    if version != 0x10 or header < 7 or stride < 4 or not count or lookup != 0:
        raise FormatError("unsupported Memory Information Table RAMCFG layout")
    extent = header + stride * count
    image.own(at, extent)
    used_bands = [i for i, limits in enumerate(ranges) if limits != (0, 0)]
    p0_position = used_bands.index(p0_band)
    columns = clock["subrecords_per_row"]
    base = clock["base_record_bytes"]
    sub = clock["subrecord_bytes"]
    first_row = int(tweak["file_offset"], 16) + tweak["header_bytes"]
    p0_rows = {}
    profiles = []
    straps = set()
    for i in range(count):
        record_at = at + header + i * stride
        descriptor = image.span(record_at, 4)
        strap, group = descriptor[0] >> 4, descriptor[1] & 0x0F
        memory_type, density = descriptor[0] & 0x0F, descriptor[2] >> 4
        if strap in straps:
            raise FormatError(f"duplicate RAMCFG descriptor for strap {strap}")
        straps.add(strap)
        profile = {
            "descriptor_file_offset": hex(wrapper + record_at),
            "descriptor_first_four_bytes": descriptor.hex(),
            "ramcfg_strap": strap,
            "clock_group": group,
            "memory_type_code": hex(memory_type),
            "density_code": hex(density),
        }
        type_name = {0x3: "GDDR5", 0xA: "GDDR6X", 0xF: "unused"}.get(memory_type)
        if type_name:
            profile["memory_type"] = type_name
        if memory_type != 0xF and density in (5, 6):
            profile["empirical_density_gbit_per_device"] = {5: 8, 6: 16}[density]
        if memory_type == 0xF or group >= columns:
            profile["timing_coverage"] = "unused_descriptor" if memory_type == 0xF else "group_out_of_range"
            profiles.append(profile)
            continue
        cell = base + group * sub
        ids = [bands[band][cell] for band in used_bands]
        populated_bands = [
            band
            for band, timing_id in zip(used_bands, ids, strict=True)
            if timing_id != 0xFF and timing_id < len(words) and any(words[timing_id])
        ]
        if len(populated_bands) == len(ids):
            coverage = "full"
        elif populated_bands:
            coverage = "partial"
        else:
            coverage = "unavailable"
        p0_id = ids[p0_position]
        if p0_id not in p0_rows:
            if p0_id == 0xFF or p0_id >= len(words):
                p0_rows[p0_id] = {"status": "no_reference" if p0_id == 0xFF else "out_of_range"}
            else:
                config = words[p0_id]
                populated = any(config)
                row = {
                    "status": "populated" if populated else "zero_config_words",
                    "whole_record_zero": not any(rows[p0_id]),
                    "file_offset": hex(first_row + p0_id * tweak["stride_bytes"]),
                    "six_config_words": [f"{word:#010x}" for word in config],
                }
                if populated:
                    row.update(timing_fields(config[1], config[3]))
                    row["trfc"] = (config[0] >> 8) & 0x1FF
                p0_rows[p0_id] = row
        profile.update(
            used_band_tweak_rows=ids,
            used_bands_with_populated_config=populated_bands,
            timing_coverage=coverage,
            p0_tweak_row=p0_id,
            matches_recorded_p0_fingerprint=group in candidates,
        )
        profiles.append(profile)
    return {
        "table_file_offset": hex(wrapper + at),
        "header_bytes": header,
        "record_bytes": stride,
        "declared_records": count,
        "extent_bytes": extent,
        "used_clock_bands": [{"band_index": i, "min_max_mhz": ranges[i]} for i in used_bands],
        "p0_rom_rows_by_tweak_id": p0_rows,
        "recorded_p0_fingerprint_candidate_ramcfg": [
            p["ramcfg_strap"] for p in profiles if p.get("matches_recorded_p0_fingerprint")
        ],
        "empirical_16_gbit_ramcfg": [
            p["ramcfg_strap"] for p in profiles if p.get("empirical_density_gbit_per_device") == 16
        ],
        "profiles": profiles,
        "density_note": "Descriptor byte 2 high nibble is reported raw. The 5/6 labels are empirical per-device density interpretations from NVIDIA BIOS Reader. Profile descriptors do not establish installed capacity or the active strap.",
        "timing_note": "Each used_band_tweak_rows array follows used_clock_bands. 255 indicates no reference. Coverage counts in-range rows with nonzero CONFIG0..5. P0 values come from ROM rows at the recorded P0 memory clock and are not alternate-strap hardware readbacks.",
        "timing_encoding": "tCL = CONFIG1[6:0], tFAW = CONFIG3[16:9], tRFC = CONFIG0[16:8], in controller cycles for populated rows.",
    }


def inspect_rom(path, prior):
    trial_index, trial = prior
    before = trial["before_a"]
    raw = path.read_bytes()
    wrapper, image = open_rom(raw)
    clock, bands = versioned_rows(image, wrapper, 4, 0x11, 4)
    tweak, rows = versioned_rows(image, wrapper, 8, 0x20, 24)
    base, sub, count = [clock[name] for name in ("base_record_bytes", "subrecord_bytes", "subrecords_per_row")]
    if not count:
        raise FormatError("the memory clock map has no strap columns")
    ranges = [tuple(value & 0x3FFF for value in unpack_from("<HH", row)) for row in bands]
    frequency = before["memory_clock_reported_mhz"]
    selected_bands = [i for i, (low, high) in enumerate(ranges) if (low or high) and low <= frequency <= high]
    if len(selected_bands) != 1:
        raise FormatError("prior P0 memory clock does not identify exactly one applicable band")
    band_index = selected_bands[0]
    strap_row = bands[band_index]
    strap_entries = [strap_row[base + i * sub : base + (i + 1) * sub] for i in range(count)]
    indexes = [entry[0] for entry in strap_entries]
    words = [unpack_from("<6I", row) for row in rows]
    c1, c3 = [int(before[key], 16) for key in ("config1_raw", "config3_raw")]
    mask = int(trial["row_fingerprint"].get("config3_mask", "0xffffffff"), 16)
    candidates = [
        i
        for i, index in enumerate(indexes)
        if index < len(rows) and words[index][1] == c1 and (words[index][3] & mask) == (c3 & mask)
    ]
    selected_rows = {indexes[i] for i in candidates}
    if selected_rows != {trial["tested_row"]}:
        raise FormatError(f"ROM row does not match the recorded P0 fingerprint: {selected_rows}")
    row_index = selected_rows.pop()
    row_offset = int(tweak["image_offset"], 16) + tweak["header_bytes"] + row_index * tweak["stride_bytes"]
    faw_offset = row_offset + 13
    if faw_offset != int(trial["runtime_image_byte_offset"], 16):
        raise FormatError("derived CONFIG3 field offset differs from the recorded P0 edit")
    populated = [w for w in words if any(w)]
    block_hits = positions(raw, rows[row_index][:24])
    hashes = {hex(record["end"]): digest(image.b[: record["end"]]) for record in image.images if record["complete"]}
    memory = memory_metadata(image)
    if timing_fields(c1, c3) != {"tcl": before["decoded_cl"], "tfaw": before["decoded_faw"]}:
        raise FormatError("decoded CONFIG fields differ from the recorded P0 readback")
    return {
        "raw_file": path.name,
        "raw_bytes": len(raw),
        "raw_sha256": digest(raw),
        "pci_device_id": hex(image.legacy["device"]),
        "pci_wrapper_bytes": wrapper,
        "normalized_pci_images": image.images,
        "prior_a_digest_matches_normalized_prefix_ending_at": [
            end for end, sha in hashes.items() if sha == before["effective_vbios_sha256"]
        ],
        "prior_a_identity_note": "A whole-image digest match is independent of the selected timing-row fingerprint. The GP107 P0 A capture included earlier clock edits.",
        "bit_m": memory,
        "ramcfg_profiles": ramcfg_profiles(
            image, wrapper, memory, clock, bands, ranges, tweak, rows, words, band_index, candidates
        ),
        "clock_table": dict(
            clock,
            zero_frequency_placeholder_rows=ranges.count((0, 0)),
            all_zero_rows=sum(not any(row) for row in bands),
            nominal_frequency_bounds_mhz=ranges,
            extension_note="Use the header-declared sizes. No semantics were assigned to bytes outside the published fields.",
        ),
        "tweak_table": dict(
            tweak,
            rows_with_nonzero_config_words=len(populated),
            rows_with_zero_config_words=len(rows) - len(populated),
            all_zero_rows=sum(not any(row) for row in rows),
            faw_histogram_nonzero_config_rows=Counter((w[3] >> 9) & 0xFF for w in populated),
            semantics_note="Histogram counts ROM rows with any nonzero CONFIG0..5 word. It does not describe permitted values or other row bytes.",
        ),
        "p0": {
            "prior_attempt_index": trial_index,
            "prior_a_sha256": before["effective_vbios_sha256"],
            "prior_a_memory_clock_reported_mhz": frequency,
            "prior_a_readback_config1": hex(c1),
            "prior_a_readback_config3": hex(c3),
            "prior_a_readback_timing_fields": timing_fields(c1, c3),
            "config3_mask_for_row_selection_only": f"{mask:#010x}",
            "rom_config3_exactly_matches_prior_a": words[row_index][3] == c3,
            "band_index": band_index,
            "band_min_max_mhz": ranges[band_index],
            "strap_cell_row_indexes": indexes,
            "matching_strap_columns_zero_based": candidates,
            "all_matching_strap_cells_byte_equal": all(
                strap_entries[i] == strap_entries[candidates[0]] for i in candidates
            ),
            "matched_tweak_row": row_index,
            "row_image_offset": hex(row_offset),
            "row_raw_file_offset": hex(wrapper + row_offset),
            "faw_image_byte_offset": hex(faw_offset),
            "faw_raw_file_byte_offset": hex(wrapper + faw_offset),
            "faw_offset_provenance": "Adjusted BIT P+8 pointer plus declared header and row stride, then CONFIG3 at byte 12 and FAW bit 9 in its second byte.",
            "six_config_words": [f"{word:#010x}" for word in words[row_index]],
            "six_word_exact_hits_in_full_raw_file": block_hits,
            "six_word_block_unique_in_full_raw_file": block_hits == [hex(wrapper + row_offset)],
            "strap_selection_note": "Candidates match the observed fingerprint within the P0 band. This does not identify the physical strap.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ga104", type=Path, required=True)
    parser.add_argument("--gp107", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve() in {args.ga104.resolve(), args.gp107.resolve(), EVIDENCE_PATH.resolve()}:
        parser.error("output must differ from the two input ROMs and evidence journal")
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    report = {
        "study": "Offline comparison of the supplied GA104 and GP107 ROMs",
        "sources": SOURCES,
        "scope": "Per-board ROM observations and prior P0 readbacks. Runtime consumption, training effects, and unpublished extension meanings were not inferred from ROM bytes.",
        "boards": {
            board: inspect_rom(path, p0_readback(evidence, board))
            for board, path in (("GA104", args.ga104), ("GP107", args.gp107))
        },
    }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
