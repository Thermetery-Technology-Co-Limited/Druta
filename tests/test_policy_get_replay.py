# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Decode recorded policy GETs at their original offsets through both GPU backends."""
import json
import struct
from pathlib import Path

import pytest

from druta import native
from tests.fake_driver import CL_FIELDS, CLRig, Driver
from tests.test_current_limits import fixture


CAPTURES = json.loads(Path(__file__).with_name("fixture_policy_gets_rm26.json").read_text())["cards"]


def replay_gets(capture):
    """Build response bytes from the recording, independently of Druta's layout tables."""
    responses = {}
    for command, block in capture["responses"].items():
        payload = bytearray(block["size"])
        previous_end = 0
        for offset, hex_bytes in block["spans"]:
            data = bytes.fromhex(hex_bytes)
            end = offset + len(data)
            assert previous_end <= offset < end <= len(payload)
            payload[offset:end] = data
            previous_end = end
        words = struct.unpack(f"<{len(payload) // 4}I", payload)
        responses[int(command, 16)] = (block["request_mask_word"], words)
    refused_command, refused_size, refusal = capture["refused_info"]

    def get(packet, fields):
        command, size = packet[14:16]
        assert fields == CL_FIELDS and packet[2] == 68 + size
        if command == int(refused_command, 16) and size == refused_size:
            assert not any(packet[17:])
            packet[16] = refusal
            return 0
        selector, words = responses[command]  # Only recorded GETs have a response.
        assert size == len(words) * 4
        expected = [0] * len(words)
        if selector is not None:
            expected[selector] = int(capture["mask"], 16)
        assert packet[17:] == expected
        packet[17:] = words
        return 0

    return Driver(get)


@pytest.mark.parametrize("capture", CAPTURES, ids=lambda c: c["model"])
@pytest.mark.parametrize("backend", ["python", "native"])
def test_full_policy_table_replays_hardware_capture(capture, backend):
    arch = capture["architecture"]
    driver = replay_gets(capture)
    command, packet_size, param_size = capture["power_get"]
    header = [0] * 17
    header[2], header[14], header[15] = packet_size, int(command, 16), param_size
    if backend == "native":
        if not native.available():
            pytest.skip("native core not built")
        rig = CLRig(arch, driver)
        gpu = rig.native_gpu()
    else:
        gpu, _ = fixture(arch)
        gpu._legacy_clk_escape.side_effect = driver
    try:
        gpu._current_limit_transport = (header, dict(CL_FIELDS))
        minimum, default, maximum, configured = capture["nvml_mw"]
        gpu.static.update(pl_min_mw=minimum, pl_def_mw=default, pl_max_mw=maximum)
        gpu.read_power_limit_mw = lambda: configured
        rows, error = gpu.read_power_policies()
        if backend == "native":
            assert not rig.errors
        assert error is None
        mask = int(capture["mask"], 16)
        assert len(rows) == capture["policy_count"]
        assert [r["policy"] for r in rows] == [idx for idx in range(32) if mask & (1 << idx)]
        assert [r["type"] for r in rows] == capture["policy_types"]
        assert all(r["consistent"] for r in rows)
        board, = [r for r in rows if r["board"]]
        assert board["unit"] == "mW"
        assert [board[k] for k in ("minimum", "default", "maximum", "requested")] == capture["nvml_mw"]
        core, = [r for r in rows if r["named_current"]]
        assert core["unit"] == "mA"
        assert [core[k] for k in ("policy", "type", "channel", "requested")] == capture["core_policy_type_channel_ma"]
    finally:
        if backend == "native":
            gpu.close()
            native.set_export_resolver(None)
