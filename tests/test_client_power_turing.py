# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic Turing client/native ABI combinations and exact recovery."""

import ctypes
import struct
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import client_power_runtime as cp
from tests.test_client_power_runtime import FakeGpu, Lock, put


class TuringGpu:
    """Independent wire model: a 32-row client plus 124-byte native controls."""

    sizes = {
        cp.INFO: 1160, cp.CONTROL: 392, cp.SET: 392, cp.ROLE_INFO: 232,
        cp.NATIVE_INFO: 8620, cp.NATIVE_CONTROL: 4432, cp.NATIVE_STATUS: 172080,
    }

    def __init__(self, client_index=5, native_index=17, *, architecture=6,
                 slot="0000:52:1d.3", device=0xABCD, subsystem=0x87654321):
        self._lock = Lock()
        self.client_index, self.native_index = client_index, native_index
        self.peer = 7 if native_index != 7 else 29
        self.architecture, self.pci_slot = architecture, slot
        self.nvapi = SimpleNamespace(
            ok=True, gpu=929,
            selected={"slot": slot, "handle": 929, "devid": device, "subsys": subsystem},
        )
        self.nvml = SimpleNamespace(selected={"slot": slot, "devid": device, "subsys": subsystem})
        self.pairing_error = ""
        self._current_limit_transport = None
        self._current_limit_layout_choice = None
        self.calls, self.set_payloads = [], []
        self.counts, self.faults = defaultdict(int), {}
        self.client_mask = 1 << client_index
        self.native_mask = (1 << native_index) | (1 << self.peer)
        self.info = bytearray(1160)
        self.info[:4] = b"\xff\x17\x28\x39"
        put(self.info, 4, self.client_mask)
        at = 8 + client_index * 36
        self.info[at:at + 4] = b"\x57\x57\x01\x00"
        struct.pack_into("<III", self.info, at + 4, 180000, 321000, 550000)
        self.native_info = bytearray(8620)
        put(self.native_info, 4, self.native_mask)
        for index, kind, channel, bounds in (
            (native_index, 0x0A, 9, (180000, 321000, 550000)),
            (self.peer, 3, 12, (1000, 50000, 80000)),
        ):
            at = 88 + index * 228
            self.native_info[at:at + 7] = bytes([0x30, 0x12, 0x23, 0x34, kind, channel, 0])
            struct.pack_into("<III", self.native_info, at + 8, *bounds)
        self.native_info[-17:] = b"native-info-tail!"
        self.native_control = bytearray(4432)
        struct.pack_into("<5I", self.native_control, 0, 0, 0, 0, 255, self.native_mask)
        put(self.native_control, 20 + native_index * 124, 0x0A)
        put(self.native_control, 20 + self.peer * 124, 3)
        self.native_control[-17:] = b"native-ctrl-tail!"
        self.original = 321000
        self._update(self.original)
        self.restore_peer = Mock(side_effect=self._restore_peer)
        self._write_policy_limit = self.restore_peer

    def slot(self):
        return self.pci_slot

    def arch(self):
        return self.architecture

    def _update(self, value):
        self.requested = value
        put(self.native_control, 20 + self.native_index * 124 + 4, value)
        put(self.native_control, 20 + self.peer * 124 + 4, 50000 if value == self.original else 60000)

    def _restore_peer(self, index, value):
        assert self._lock.held and index == self.peer
        put(self.native_control, 20 + index * 124 + 4, value)
        return True, "restored"

    def _current_limit_transport_layouts(self):
        assert self._lock.held
        if self._current_limit_transport is None:
            header = [0] * 17
            header[0], header[1], header[3], header[4] = 0x4E564441, 0x10002, 0x4E562A2A, 0x0500002B
            header[2], header[14], header[15] = 11748, 0x2080A612, 11680
            self._current_limit_transport = (
                header, {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0},
            )
        return ({},)

    def _legacy_clk_escape(self, packet, fields):
        assert self._lock.held
        command, size = int(packet[14]), int(packet[15])
        assert packet[2] == 68 + size and len(packet) * 4 == 68 + size
        assert packet[5] == packet[11] == 0
        assert fields == {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0}
        request = bytes(packet)[68:]
        self.calls.append((command, request))
        self.counts[command, size] += 1
        event = self.faults.get((command, size, self.counts[command, size]), {})
        rejected = size != self.sizes[command]
        result = bytearray(request)
        if not rejected:
            if command == cp.INFO:
                assert not any(request)
                result[:] = self.info
            elif command == cp.ROLE_INFO:
                assert request == b"\x01" + bytes(231)
                at = 88 + self.native_index * 228
                result[4:] = self.native_info[at:at + 228]
            elif command == cp.NATIVE_INFO:
                assert not any(request)
                result[:] = self.native_info
            elif command == cp.NATIVE_CONTROL:
                assert cp._u32(request, 16) == self.native_mask
                result[:] = self.native_control
            elif command in (cp.CONTROL, cp.SET):
                at = 8 + self.client_index * 12
                assert cp._u32(request, 4) == self.client_mask
                assert request[at:at + 2] == b"\x57\x57" and request[at + 8] == 0xFE
                if command == cp.CONTROL:
                    result[at + 2:at + 4] = b"xy"
                    result[at + 9:at + 12] = b"abc"
                    put(result, at + 4, self.requested)
                else:
                    self.set_payloads.append(request)
                    if not event.get("ignore"):
                        self._update(cp._u32(request, at + 4))
            else:
                raise AssertionError("Optional native STATUS must not gate this client request")
        if event.get("mutate"):
            event["mutate"](self)
        if event.get("payload"):
            event["payload"](result)
        ctypes.memmove(ctypes.addressof(packet) + 68, bytes(result), size)
        packet[5], packet[11], packet[16] = 1, 1, event.get("rm", 0x1F if rejected else 0)
        if event.get("header"):
            index, value = event["header"]
            packet[index] = value
        return event.get("nt", 0)


@pytest.mark.parametrize("architecture,client,native,slot,device,subsystem", [
    (6, 0, 2, "0000:52:1d.3", 0xABCD, 0x87654321),
    (6, 31, 31, "0000:ab:1f.7", 0xFFFF, 0x10203040),
    (7, 5, 17, "0000:07:02.1", 0x1234, 0xABCDEF01),
])
def test_small_client_and_policy124_bind_current_adapter_and_restore(
    architecture, client, native, slot, device, subsystem,
):
    gpu = TuringGpu(client, native, architecture=architecture, slot=slot, device=device, subsystem=subsystem)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    assert before["identity"] == {
        "slot": slot, "architecture": architecture, "device_id": device, "subsystem_id": subsystem,
    }
    assert before["binding"]["client_index"] == client and before["binding"]["native_index"] == native
    assert before["binding"]["layout"] == "legacy32"
    assert before["binding"]["native_layout"] == "policy124"
    assert before["arbitration"] is None and before["arbitration_error"]
    assert [(command, len(raw)) for command, raw in gpu.calls if command == cp.NATIVE_INFO] == [
        (cp.NATIVE_INFO, 8632), (cp.NATIVE_INFO, 8620), (cp.NATIVE_INFO, 8620),
    ]
    for requested in (550001, 1999000):
        result = controller.set_request(requested)
        assert result["after"]["requested_mw"] == requested
        assert cp._u32(bytes.fromhex(result["after"]["native_control_hex"]), 20 + native * 124 + 4) == requested
        assert cp._u32(gpu.native_control, 20 + gpu.peer * 124 + 4) == 60000
        expected = bytearray.fromhex(before["control_hex"])
        put(expected, 8 + client * 12 + 4, requested)
        assert gpu.set_payloads[-1] == expected
        controller.set_request(gpu.original)
        assert cp._state(controller.read()) == cp._state(before)
    assert len(gpu.set_payloads) == 4
    gpu.restore_peer.assert_not_called()


def test_turing_modern_geometry_does_not_probe_smaller_client_or_native_layout():
    gpu = FakeGpu(architecture=6)
    observation = cp.ClientPowerController(gpu).read()
    assert observation["requested_mw"] == 321000
    assert "layout" not in observation["binding"]
    assert all(len(raw) == cp.SIZES[command] for command, raw in gpu.calls)
    assert not gpu.set_payloads


@pytest.mark.parametrize("event", [{"rm": 0x55}, {"nt": -1}, {"ignore": True}])
def test_failed_write_restores_complete_baseline_and_preexisting_peer_request(event):
    gpu = TuringGpu(native_index=31)
    put(gpu.native_control, 20 + gpu.peer * 124 + 4, 51000)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    gpu.faults[cp.SET, 392, 1] = event
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    assert caught.value.rollback["attempted"] and caught.value.rollback["verified"]
    assert cp._state(caught.value.rollback["readback"]) == cp._state(before)
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2
    gpu.restore_peer.assert_called_once_with(gpu.peer, 51000)


def test_failed_peer_restoration_does_not_claim_full_recovery():
    gpu = TuringGpu()
    put(gpu.native_control, 20 + gpu.peer * 124 + 4, 51000)
    gpu.restore_peer.side_effect = None
    gpu.restore_peer.return_value = False, "peer restoration refused"
    gpu.faults[cp.SET, 392, 1] = {"rm": 0x55}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert "peer restoration refused" in caught.value.rollback["error"]
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2


@pytest.mark.parametrize("surface", ["tail", "selected_opaque", "peer_opaque"])
def test_unexpected_native_mutation_is_rejected_and_not_reported_restored(surface):
    gpu = TuringGpu()
    at = {"tail": -1, "selected_opaque": 20 + gpu.native_index * 124 + 13,
          "peer_opaque": 20 + gpu.peer * 124 + 12}[surface]
    gpu.faults[cp.SET, 392, 1] = {
        "mutate": lambda g: g.native_control.__setitem__(at, g.native_control[at] ^ 1),
    }
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "readback"
    assert caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2


@pytest.mark.parametrize("command,size,change,kind", [
    (cp.ROLE_INFO, 232, lambda raw: put(raw, 0, 2), "malformed"),
    (cp.ROLE_INFO, 232, lambda raw: raw.__setitem__(10, 1), "malformed"),
    (cp.INFO, 1160, lambda raw: raw.__setitem__(8 + 5 * 36 + 3, 1), "unsupported"),
    (cp.INFO, 1160, lambda raw: put(raw, 4, 0), "unsupported"),
    (cp.NATIVE_INFO, 8620, lambda raw: put(raw, 4, 0), "malformed"),
    (cp.NATIVE_CONTROL, 4432, lambda raw: put(raw, 16, 0), "malformed"),
    (cp.CONTROL, 392, lambda raw: put(raw, 4, 0), "malformed"),
])
def test_malformed_role_units_and_inventory_prevent_any_set(command, size, change, kind):
    gpu = TuringGpu()
    gpu.faults[command, size, 1] = {"payload": change}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == kind and caught.value.rollback is None
    assert not gpu.set_payloads


@pytest.mark.parametrize("command,size", [(cp.INFO, 2340), (cp.NATIVE_INFO, 8632)])
@pytest.mark.parametrize("event", [{"nt": -1}, {"rm": 0x55}])
def test_transient_rejections_do_not_guess_a_smaller_geometry(command, size, event):
    gpu = TuringGpu()
    gpu.faults[command, size, 1] = gpu.faults[command, size, 2] = event
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "transient" and caught.value.rollback is None
    assert [len(raw) for cmd, raw in gpu.calls if cmd == command] == [size, size]
    assert not gpu.set_payloads


@pytest.mark.parametrize("command,size", [(cp.INFO, 1160), (cp.NATIVE_INFO, 8620),
                                         (cp.ROLE_INFO, 232), (cp.NATIVE_CONTROL, 4432),
                                         (cp.CONTROL, 392)])
def test_transient_accepted_geometry_read_recovers_without_a_write(command, size):
    gpu = TuringGpu()
    gpu.faults[command, size, 1] = {"nt": -1}
    assert cp.ClientPowerController(gpu).read()["requested_mw"] == gpu.original
    assert gpu.counts[command, size] == 3
    assert not gpu.set_payloads


@pytest.mark.parametrize("command,size,event,kind", [
    (cp.INFO, 1160, {"rm": 0x1F}, "unsupported"),
    (cp.NATIVE_INFO, 8620, {"rm": 0x1F}, "unsupported"),
    (cp.NATIVE_CONTROL, 4432, {"rm": 0x1F}, "unsupported"),
    (cp.NATIVE_INFO, 8632, {"header": (15, 8620)}, "malformed"),
    (cp.NATIVE_INFO, 8620, {"header": (15, 8632)}, "malformed"),
])
def test_rejected_or_changed_packet_geometry_never_dispatches_set(command, size, event, kind):
    gpu = TuringGpu()
    gpu.faults[command, size, 1] = event
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == kind and caught.value.rollback is None
    assert not gpu.set_payloads
