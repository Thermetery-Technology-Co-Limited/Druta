# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent short RM26 policy packets, without loading a GPU driver."""

import ctypes
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import nvbackend as n


class Rm26Wire:
    sizes = {0x20802618: 5008, 0x20802619: 14148,
             0x2080261A: 2108, 0x2080261B: 2108}

    def __init__(self, *, peer=31, device=0xFFFF, slot="0000:ab:1f.7"):
        self.peer = peer
        self.mask = (1 << 2) | (1 << 13) | (1 << peer)
        self.info = [0] * (5008 // 4)
        self.info[1] = self.mask
        self.control = [(i * 17 + 31) & 0xFFFFFFFF for i in range(2108 // 4)]
        self.control[0] = self.mask
        self.status = [0] * (14148 // 4)
        self.status[0] = self.mask
        for policy, kind, unit, bounds in (
            (2, 0, 0, (180000, 321000, 550000)),
            (13, 0x0B, 1, (1000, 350780, 390000)),
            (peer, 5, 0, (1000, 63000, 100000)),
        ):
            meta, record, dynamic = (52 + policy * 116) // 4, (4 + policy * 52) // 4, (12 + policy * 372) // 4
            self.info[meta] = 0x30
            self.info[meta + 1:meta + 5] = [kind | 27 << 8 | unit << 16, *bounds]
            self.control[record:record + 2] = [kind, bounds[1]]
            self.status[dynamic:dynamic + 3] = [kind, bounds[1], bounds[1] // 2]
        self.writes, self.calls = [], []
        self.store_only = self.failed_after_set = self.break_get_once = False
        self.refuse_restore = self.mutate_peer = False
        gpu = n.GPU.__new__(n.GPU)
        gpu._lock = threading.RLock()
        gpu.arch = Mock(return_value=n.GPU.ARCH_TURING)
        gpu.nvapi = SimpleNamespace(ok=True, selected={"devid": device, "subsys": 0x12345678, "slot": slot})
        gpu.static = {"driver": "a deliberately unrelated label", "vbios": "different",
                      "pl_min_mw": 180000, "pl_def_mw": 321000, "pl_max_mw": 550000}
        gpu.voltage_xoc_enabled = False
        gpu._current_limit_transport = gpu._current_limit_layout_choice = None
        header = [0] * 17
        header[2], header[14], header[15] = 2524, 0x20802612, 2456
        header[12:14] = [1234, 5678]
        self.fields = {"hAdapter": 3, "hDevice": 0, "Type": 0, "Flags": 8, "hContext": 0}
        gpu._capture_current_limit_transport = Mock(return_value=(header, self.fields))
        gpu._legacy_clk_escape = Mock(side_effect=self.escape)
        gpu.read_power_limit_mw = Mock(return_value=321000)
        self.gpu = gpu

    def escape(self, packet, fields):
        command, size = packet[14], packet[15]
        assert command in self.sizes and size == self.sizes[command]
        assert packet[2] == ctypes.sizeof(packet) == 68 + size
        assert list(packet[12:14]) == [1234, 5678] and fields == self.fields
        params = list(packet[17:])
        self.calls.append((command, params))
        if command == 0x2080261B:
            self.writes.append(params)
            assert type(params[0]) is int and params[0].bit_count() == 1
            assert params[0] & self.mask
            if self.refuse_restore and len(self.writes) > 1:
                packet[16] = 0x1F
                return 0
            policy = params[0].bit_length() - 1
            record, dynamic = (4 + policy * 52) // 4, (12 + policy * 372) // 4
            self.control[record:record + 13] = params[record:record + 13]
            if not self.store_only:
                self.status[dynamic + 1] = self.control[record + 1]
            if self.mutate_peer and len(self.writes) == 1:
                self.control[(4 + self.peer * 52) // 4 + 3] ^= 1
            if self.failed_after_set and len(self.writes) == 1:
                raise OSError("transport failed after accepted SET")
        else:
            if self.break_get_once and self.writes:
                self.break_get_once = False
                raise OSError("readback temporarily unavailable")
            if command == 0x20802618:
                assert not any(params)
                packet[17:] = self.info
            else:
                assert params == [self.mask] + [0] * (len(params) - 1)
                packet[17:] = self.control if command == 0x2080261A else self.status
        return 0


@pytest.mark.parametrize("peer,device,slot", [(31, 0xFFFF, "0000:ab:1f.7"), (0, 0xABCD, "0000:07:03.1")])
def test_short_rm26_reads_named_current_and_all_occupied_indices(peer, device, slot):
    wire = Rm26Wire(peer=peer, device=device, slot=slot)
    gpu = wire.gpu
    row, = gpu.get_current_limits()
    assert (row["policy"], row["channel"], row["limit_ma"], row["maximum_ma"]) == (13, 27, 350780, 390000)
    assert "channel 27" in row["label"]
    rows, error = gpu.read_power_policies()
    assert error is None and {row["policy"] for row in rows} == {2, 13, peer}
    assert gpu._current_limit_capacity(gpu._current_limit_abi()) == 32
    assert gpu._current_limit_abi()["control_mask_word"] == 0
    assert all(command in (0x20802618, 0x20802619, 0x2080261A) for command, _ in wire.calls)
    assert not wire.writes


def test_short_rm26_current_write_preserves_every_opaque_word_and_inventory():
    wire = Rm26Wire(peer=0)
    original = list(wire.control)
    ok, message = wire.gpu.set_current_limit_ma(13, 349780)
    assert ok, message
    expected = list(original)
    expected[(4 + 13 * 52) // 4 + 1] = 349780
    assert wire.control == expected
    expected[0] = 1 << 13
    assert wire.writes == [expected]
    assert len(wire.writes[0]) * 4 == 2108
    assert wire.gpu.get_current_limits()[0]["limit_ma"] == 349780
    assert wire.gpu._power_policy_pins == {13: 349780}


@pytest.mark.parametrize("peer", [0, 31])
def test_short_rm26_full_policy_writer_uses_word_zero_and_replays_pins(peer):
    wire = Rm26Wire(peer=peer)
    original = list(wire.control)
    ok, message = wire.gpu.set_power_policy(peer, 64000)
    assert ok, message
    word = (4 + peer * 52) // 4 + 1
    expected = list(original)
    expected[word] = 64000
    assert wire.control == expected
    expected[0] = 1 << peer
    assert wire.writes == [expected]
    wire.control[word] = wire.status[(12 + peer * 372) // 4 + 1] = 63000
    ok, message = wire.gpu._reapply_power_policy_pins()
    assert ok, message
    assert wire.control[word] == 64000
    assert wire.writes[-1][0] == 1 << peer


@pytest.mark.parametrize("failure", ["store_only", "failed_after_set", "break_get_once"])
def test_short_rm26_uncertain_current_write_restores_exact_getter_state(failure):
    wire = Rm26Wire()
    original = list(wire.control)
    original_status = list(wire.status)
    setattr(wire, failure, True)
    ok, message = wire.gpu.set_current_limit_ma(13, 349780)
    assert not ok and "restored and verified" in message
    assert wire.control == original and wire.status == original_status
    restore = list(original)
    restore[0] = 1 << 13
    assert wire.writes[-1] == restore and len(wire.writes) == 2


def test_short_rm26_failed_restore_is_reported():
    wire = Rm26Wire()
    wire.store_only = wire.refuse_restore = True
    ok, message = wire.gpu.set_current_limit_ma(13, 349780)
    assert not ok and "restoration could not be verified" in message
    assert "RM 0x1F" in message and len(wire.writes) == 2


@pytest.mark.parametrize("failure", ["failed_after_set", "break_get_once"])
def test_short_rm26_full_policy_write_compensation_uses_short_header(failure):
    wire = Rm26Wire(peer=31)
    original = list(wire.control)
    setattr(wire, failure, True)
    ok, message = wire.gpu.set_power_policy(31, 64000)
    assert not ok and "restored and verified" in message
    assert wire.control == original
    restore = list(original)
    restore[0] = 1 << 31
    assert wire.writes[-1] == restore and len(wire.writes) == 2


def test_short_rm26_unrelated_opaque_mutation_prevents_claiming_full_current_recovery():
    wire = Rm26Wire()
    original = list(wire.control)
    wire.mutate_peer = True
    ok, message = wire.gpu.set_current_limit_ma(13, 349780)
    assert not ok and "restoration could not be verified" in message
    current_word = (4 + 13 * 52) // 4 + 1
    assert wire.control[current_word] == original[current_word]
    peer_opaque = (4 + wire.peer * 52) // 4 + 3
    assert wire.control[peer_opaque] != original[peer_opaque]
    assert len(wire.writes) == 2 and all(request[0] == 1 << 13 for request in wire.writes)


def test_short_rm26_current_effective_disagreement_prevents_a_new_write():
    wire = Rm26Wire()
    wire.status[(12 + 13 * 372) // 4 + 1] = 340000
    ok, message = wire.gpu.set_current_limit_ma(13, 349780)
    assert not ok and "disagree" in message and not wire.writes


@pytest.mark.parametrize("xoc", [False, True])
def test_short_rm26_current_request_remains_within_its_own_reported_maximum(xoc):
    wire = Rm26Wire()
    wire.gpu.voltage_xoc_enabled = xoc
    ok, message = wire.gpu.set_current_limit_ma(13, 390001)
    assert not ok and "390" in message and not wire.writes
    assert wire.gpu.set_current_limit_ma(13, 390000)[0]


@pytest.mark.parametrize("command", [0x2080A619, 0x2080A61A])
@pytest.mark.parametrize("mask", [None, True, 1.0, -1, 1 << 32])
def test_short_rm26_get_masks_require_uint32_integers(command, mask):
    wire = Rm26Wire()
    with pytest.raises(ValueError, match="validated mask"):
        wire.gpu._current_limit_rm(command, policy_mask=mask)
    assert not wire.calls


@pytest.mark.parametrize("selector", [0, True, (1 << 13) | (1 << 31), 1 << 31])
def test_short_rm26_named_current_set_refuses_broad_or_unlisted_masks(selector):
    wire = Rm26Wire()
    params = list(wire.control)
    params[0] = selector
    with pytest.raises(ValueError, match="select"):
        wire.gpu._current_limit_rm(0x2080E61B, params)
    assert not wire.calls
    wire.gpu._capture_current_limit_transport.assert_not_called()


def test_short_rm26_mask_in_modern_header_position_does_not_select_a_policy():
    wire = Rm26Wire()
    params = list(wire.control)
    params[0], params[4] = 0, 1 << 13
    with pytest.raises(ValueError, match="select"):
        wire.gpu._current_limit_rm(0x2080E61B, params)
    assert not wire.calls


@pytest.mark.parametrize("size", [2104, 2112])
def test_short_rm26_wrong_set_size_never_captures_transport(size):
    wire = Rm26Wire()
    params = [0] * (size // 4)
    params[0] = 1 << 13
    with pytest.raises(ValueError, match="buffer size"):
        wire.gpu._current_limit_rm(0x2080E61B, params)
    assert not wire.calls
    wire.gpu._capture_current_limit_transport.assert_not_called()


@pytest.mark.parametrize("corruption", ["control_mask", "status_mask", "unit", "type"])
def test_short_rm26_masks_and_selected_type_are_validated_before_writing(corruption):
    wire = Rm26Wire()
    if corruption == "control_mask":
        wire.control[0] = 1 << 13
    elif corruption == "status_mask":
        wire.status[0], wire.status[1] = 0, wire.mask
    elif corruption == "unit":
        wire.info[(52 + 13 * 116) // 4 + 1] ^= 1 << 16
    elif corruption == "type":
        wire.control[(4 + 13 * 52) // 4] = 0xFE
    assert wire.gpu.get_current_limits() == []
    assert not wire.gpu.set_current_limit_ma(13, 349780)[0]
    assert not wire.writes


@pytest.mark.parametrize("packet_bytes,param_bytes,command", [
    (2528, 2460, 0x20802612), (2524, 2456, 0x2080A612), (2524, 3480, 0x20802612),
])
def test_short_rm26_transport_geometry_and_command_must_match(packet_bytes, param_bytes, command):
    wire = Rm26Wire()
    header, _ = wire.gpu._capture_current_limit_transport.return_value
    header[2], header[14], header[15] = packet_bytes, command, param_bytes
    assert wire.gpu.get_current_limits() == []
    assert not wire.gpu.set_current_limit_ma(13, 349780)[0]
    assert not wire.calls


@pytest.mark.parametrize("block", ["info", "control", "status"])
def test_short_rm26_truncated_getter_packets_are_refused(block):
    wire = Rm26Wire()
    packets = [wire.info, wire.control, wire.status]
    index = {"info": 0, "control": 1, "status": 2}[block]
    packets[index] = packets[index][:-1]
    wire.gpu._current_limit_rm = Mock(side_effect=packets)
    assert wire.gpu.get_current_limits() == []
    assert not wire.writes
