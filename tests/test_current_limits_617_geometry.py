# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The 20512-byte info ABI is selected by GET evidence, without hardware."""
import ctypes
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import nvbackend as n


INFO, STATUS, CONTROL, SET = 0x2080A618, 0x2080A619, 0x2080A61A, 0x2080E61B
INFO_RECORD = (0xCC + 13 * 0x104) // 4
STATUS_RECORD = (0x9C + 13 * 0x1730) // 4
CONTROL_RECORD = (0x14 + 13 * 0xC4) // 4


def packet_gpu(architecture=n.GPU.ARCH_AMPERE):
    gpu = n.GPU.__new__(n.GPU)
    gpu._lock = threading.RLock()
    # Packet selection must remain independent of generation and identity.
    gpu.arch = Mock(return_value=architecture)
    gpu.static = {"driver": "arbitrary", "vbios": "different", "name": "Other PCB"}
    gpu.nvapi = SimpleNamespace(ok=True, selected={"devid": 0xFFFF, "subsys": 0})
    gpu.voltage_xoc_enabled = False
    mask = (1 << 13) | (1 << 18)
    info = [0] * (20512 // 4)
    info[1] = mask
    control = [(i * 19 + 7) & 0xFFFFFFFF for i in range(13876 // 4)]
    control[:5] = [0, 0, 0, 255, mask]
    status = [0] * (397048 // 4)
    status[1] = mask
    for policy, type_id, channel, minimum, default, maximum in (
            (13, 0x0F, 19, 1000, 310000, 420000),
            (18, 0x0F, 22, 1, 5001000, 5001000)):
        meta = (0xCC + policy * 0x104) // 4
        live = (0x9C + policy * 0x1730) // 4
        record = (0x14 + policy * 0xC4) // 4
        info[meta + 1:meta + 5] = [type_id | channel << 8 | 1 << 16,
                                  minimum, default, maximum]
        control[record:record + 2] = [type_id, default]
        status[live:live + 3] = [type_id, default, 17000]
    state = SimpleNamespace(info=info, control=control, status=status,
                            calls=[], writes=[], store_only=False,
                            fail_info_once=False)
    header = [0] * 17
    header[2], header[14], header[15] = 54420, 0x2080A612, 54352
    header[12:14] = [1234, 5678]
    fields = dict(hAdapter=91, hDevice=0, Type=0, Flags=8, hContext=0)
    gpu._capture_current_limit_transport = Mock(return_value=(header, fields))

    def escape(packet, observed_fields):
        assert observed_fields == fields
        assert list(packet[12:14]) == [1234, 5678]
        command, size = packet[14:16]
        assert packet[2] == ctypes.sizeof(packet) == size + 68
        state.calls.append((command, size))
        if command == INFO and size != 20512:
            packet[16] = 0x1F
            return 0
        assert size == {INFO: 20512, STATUS: 397048, CONTROL: 13876, SET: 13876}[command]
        if command == INFO and state.fail_info_once:
            state.fail_info_once = False
            return -1
        if command == SET:
            params = list(packet[17:])
            assert params[4] == 1 << 13
            state.writes.append(params)
            end = CONTROL_RECORD + 0xC4 // 4
            state.control[CONTROL_RECORD:end] = params[CONTROL_RECORD:end]
            if not state.store_only:
                state.status[STATUS_RECORD + 1] = params[CONTROL_RECORD + 1]
        else:
            if command == INFO:
                assert not any(packet[17:])
            elif command == STATUS:
                assert list(packet[17:19]) == [0, state.info[1]]
                assert not any(packet[19:])
            else:
                assert list(packet[17:22]) == [0, 0, 0, 0, state.info[1]]
                assert not any(packet[22:])
            packet[17:] = {INFO: state.info, STATUS: state.status, CONTROL: state.control}[command]
        return 0

    gpu._legacy_clk_escape = Mock(side_effect=escape)
    return gpu, state


@pytest.mark.parametrize("identity", [
    {"driver": "472.12", "vbios": "unrelated"},
    {"driver": "future branch", "name": "Different adapter"},
])
@pytest.mark.parametrize("architecture", [n.GPU.ARCH_AMPERE, n.GPU.ARCH_ADA])
def test_info_acceptance_selects_geometry_independently_of_identity(identity, architecture):
    gpu, state = packet_gpu(architecture)
    gpu.static.update(identity)
    rows = gpu.get_current_limits()
    assert len(rows) == 1
    assert rows[0]["policy"] == 13
    assert (rows[0]["minimum_ma"], rows[0]["default_ma"], rows[0]["maximum_ma"]) == (1000, 310000, 420000)
    assert rows[0]["limit_ma"] == rows[0]["requested_ma"] == 310000
    assert rows[0]["value_ma"] == 17000
    assert state.calls[:3] == [(INFO, 20000), (INFO, 8632), (INFO, 20512)]
    assert (CONTROL, 13876) in state.calls
    assert (STATUS, 397048) in state.calls
    assert gpu.current_limit_diagnostics()["layout_info_bytes"] == 20512
    before = len(state.calls)
    gpu.get_current_limits()
    assert all(size == 20512 for command, size in state.calls[before:] if command == INFO)
    gpu._capture_current_limit_transport.assert_called_once()
    assert state.writes == []


@pytest.mark.parametrize("architecture", [n.GPU.ARCH_AMPERE, n.GPU.ARCH_ADA])
def test_selected_current_word_changes_and_full_buffer_restores(architecture):
    gpu, state = packet_gpu(architecture)
    original = state.control.copy()
    assert gpu.set_current_limit_ma(13, 309000)[0]
    assert [i for i, (a, b) in enumerate(zip(original, state.control)) if a != b] == [CONTROL_RECORD + 1]
    expected = original.copy()
    expected[4] = 1 << 13
    expected[CONTROL_RECORD + 1] = 309000
    assert state.writes[0] == expected
    assert state.status[STATUS_RECORD + 1] == 309000
    assert gpu.set_current_limit_ma(13, 310000)[0]
    assert state.control == original


def test_stored_only_success_rolls_back_the_full_selected_record():
    gpu, state = packet_gpu()
    original = state.control.copy()
    state.store_only = True
    ok, message = gpu.set_current_limit_ma(13, 309000)
    assert not ok
    assert "effective" in message
    assert len(state.writes) == 2
    assert state.control == original
    assert state.status[STATUS_RECORD + 1] == 310000


@pytest.mark.parametrize("architecture", [n.GPU.ARCH_AMPERE, n.GPU.ARCH_ADA])
def test_transient_info_failure_does_not_cache_unsupported_geometry(architecture):
    gpu, state = packet_gpu(architecture)
    state.fail_info_once = True
    assert gpu.get_current_limits() == []
    assert "NTSTATUS -1" in gpu.current_limit_diagnostics()["read_error"]
    assert gpu.get_current_limits()[0]["limit_ma"] == 310000
    assert gpu._capture_current_limit_transport.call_count == 2
    assert state.writes == []


@pytest.mark.parametrize("broken", ["status_mask", "control_header", "unit", "status_type"])
@pytest.mark.parametrize("architecture", [n.GPU.ARCH_AMPERE, n.GPU.ARCH_ADA])
def test_malformed_new_geometry_never_authorizes_a_write(broken, architecture):
    gpu, state = packet_gpu(architecture)
    if broken == "status_mask":
        state.status[1] = 1 << 13
    elif broken == "control_header":
        state.control[3] = 0
    elif broken == "unit":
        state.info[INFO_RECORD + 1] &= ~(255 << 16)
    else:
        state.status[STATUS_RECORD] = 0x12
    assert gpu.get_current_limits() == []
    assert not gpu.set_current_limit_ma(13, 309000)[0]
    assert state.writes == []


def test_different_live_channel_keeps_current_without_borrowing_rail_identity():
    gpu, state = packet_gpu()
    state.info[INFO_RECORD + 1] = 0x0F | 23 << 8 | 1 << 16
    row = gpu.get_current_limits()[0]
    assert row["channel"] == 23
    assert row["label"] == "Policy 13 current (channel 23)"
    assert gpu.set_current_limit_ma(13, 309000)[0]
