# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline coverage of rail GET export with opaque/private data in every sample."""
import json

from experiments.capture_rail_get import summarize_get


def captured(size, command, mask):
    header = [0xDEADBEEF] * 17
    header[0], header[2], header[3] = 0x4E564441, size, 0x4E562A2A
    header[11], header[14:17] = 0, [command, size - 68, 0]
    params = [0xDEADBEEF] * (size // 4 - 17)
    params[0] = mask
    returned = header.copy()
    returned[11] = 1
    return {"escape_size": size, "escape_ntstatus": 0, "escape_fields": {"hAdapter": 0xDEADBEEF},
            "escape_header": header, "escape_input_params": params, "escape_header_after": returned,
            "escape_output_params": list(params), "output_params": list(params), "private": "sensitive"}


def test_known_get_exports_only_metadata_and_checks_each_half():
    call = captured(716, 0x20803213, 1)
    call["escape_output_params"][0] = 2
    call["escape_header_after"][16] = 31
    out = summarize_get({"nvapi_status": -1, "captures": [call]})
    assert out == {
        "getter_status": -1,
        "escapes": [{"packet_bytes": 716, "ntstatus": 0,
                     "input": {"command": "0x20803213", "parameter_bytes": 648, "completion": 0,
                               "rm_status": 0, "recognized": True, "mask": 1},
                     "returned": {"command": "0x20803213", "parameter_bytes": 648, "completion": 1,
                                  "rm_status": 31, "recognized": False, "mask": 2}}]}


def test_unrecognized_packets_do_not_export_opaque_selector_or_payload():
    unknown = captured(720, 0x20803213, 0xDEADBEEF)
    unrelated = captured(716, 0x20803213, 1)
    unrelated["escape_header_after"][0] = unrelated["escape_header"][0] = 17
    short = {**unknown, "escape_size": 8}  # Research probe can retain prior headers in its raw dict.
    out = summarize_get({"nvapi_status": 0, "captures": [unknown, unrelated, short]})
    first, other, tiny = out["escapes"]
    for phase in ("input", "returned"):
        assert first[phase]["recognized"] is False and first[phase]["mask"] is None
        assert other[phase] is None and tiny[phase] is None
    encoded = json.dumps(out)
    assert "3735928559" not in encoded and "sensitive" not in encoded
    assert summarize_get({"nvapi_status": 0, "captures": []}) == {"getter_status": 0, "escapes": []}
