# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent extended RM26 wire model: synthetic buffers only, no installed driver."""

import ctypes
import struct
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from druta import client_power_runtime as cp
from tests.test_client_power_runtime import Lock


def put(raw, at, value):
    struct.pack_into("<I", raw, at, value)


def word(raw, at):
    return struct.unpack_from("<I", raw, at)[0]


class ExtendedRm26Gpu:
    """Model the extended wire format with literal sizes and row geometry.

    FE has an existing request. Public-wrapper F8 is a different caller and
    must never be substituted or seeded by the client controller.
    """

    sizes = {0x20802630: 900, 0x20802632: 388, 0x20802633: 388,
             0x2080261C: 136, 0x20802618: 5520, 0x2080261A: 2124}

    def __init__(self, *, client=11, native=31, slot="0000:52:1d.3",
                 device=0xABCD, subsystem=0x87654321, pinned=False):
        self._lock = Lock()
        self.client, self.native = client, native
        self.peers = (6, 18) if native not in (6, 18) else (7, 19)
        self.unknown = 24 if native != 24 else 25
        self.unrelated_client = 7 if client != 7 else 23
        self.peer_requests = {self.peers[0]: (50000, 60000), self.peers[1]: (80000, 90000)}
        self.pci_slot, self.architecture, self.pairing_error = slot, 6, ""
        self.nvapi = SimpleNamespace(ok=True, gpu=929, selected={
            "slot": slot, "handle": 929, "devid": device, "subsys": subsystem,
        })
        self.nvml = SimpleNamespace(selected={"slot": slot, "devid": device, "subsys": subsystem})
        self._current_limit_transport = self._current_limit_layout_choice = None
        self.calls, self.set_payloads = [], []
        self.counts, self.faults = defaultdict(int), {}
        self.transport_reads, self.captures, self.transport_faults = 0, 0, {}
        self.original = self.requested = 321000
        self.client_mask = (1 << client) | (1 << self.unrelated_client)
        self.native_mask = sum(1 << index for index in (native, *self.peers, self.unknown))

        self.info = bytearray((i * 17 + 37) % 256 for i in range(900))
        put(self.info, 0, self.client_mask)
        at = 4 + self.unrelated_client * 28
        self.info[at:at + 4] = b"\x99\x55\x77\x09"
        at = 4 + client * 28
        self.info[at:at + 4] = b"\x57\x57\x01\x00"
        struct.pack_into("<III", self.info, at + 4, 123000, 321000, 550000)

        self.native_info = bytearray((i * 13 + 43) % 256 for i in range(5520))
        put(self.native_info, 4, self.native_mask)
        for index, kind, channel, units, bounds in (
            (native, 10, 9, 0, (123000, 321000, 550000)),
            (self.peers[0], 11, 12, 1, (1000, 50000, 55000)),
            (self.peers[1], 4, 17, 0, (1000, 80000, 85000)),
            (self.unknown, 213, 22, 9, (0, 0, 0)),
        ):
            at = 60 + index * 132
            self.native_info[at:at + 7] = bytes([0x81 if index == self.unknown else 0x30,
                                               0x17, 0x28, 0x39, kind, channel, units])
            struct.pack_into("<III", self.native_info, at + 8, *bounds)

        self.native_control = bytearray((i * 19 + 31) % 256 for i in range(2124))
        struct.pack_into("<5I", self.native_control, 0, 0, 0, 0, 255, self.native_mask)
        for index in (native, *self.peers, self.unknown):
            put(self.native_control, 20 + index * 52, self.native_info[60 + index * 132 + 4])
        put(self.native_control, 20 + self.unknown * 52 + 4, 76543)
        self.client_template = bytearray((i * 23 + 51) % 256 for i in range(388))
        put(self.client_template, 0, 1 << client)
        at = 4 + client * 12
        self.client_template[at:at + 2] = b"\x57\x57"
        self.client_template[at + 8] = 0xFE
        self._update(self.original)
        if pinned:
            for index, (baseline, _) in self.peer_requests.items():
                put(self.native_control, 20 + index * 52 + 4, baseline + 1000)
        self.restore_peer = Mock(side_effect=self._restore_peer)
        self._write_policy_limit = self.restore_peer

    def slot(self):
        return self.pci_slot

    def arch(self):
        return self.architecture

    def _update(self, value):
        self.requested = value
        put(self.native_control, 20 + self.native * 52 + 4, value)
        for index, (baseline, raised) in self.peer_requests.items():
            put(self.native_control, 20 + index * 52 + 4, baseline if value == self.original else raised)

    def _restore_peer(self, index, value):
        assert self._lock.held and index in self.peer_requests
        put(self.native_control, 20 + index * 52 + 4, value)
        return True, "peer restored"

    def _current_limit_transport_layouts(self):
        assert self._lock.held
        self.transport_reads += 1
        if self._current_limit_transport is None:
            self.captures += 1
            header = list(range(17))
            header[0:5] = [0x4E564441, 0x10002, 3548, 0x4E562A2A, 0x0500002B]
            header[5] = header[11] = header[16] = 0
            header[14], header[15] = 0x20802612, 3480
            self._current_limit_transport = (
                header, {"hAdapter": 83, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0},
            )
        fault = self.transport_faults.get(self.transport_reads)
        if fault:
            fault(self)
        return ({},)

    def _legacy_clk_escape(self, packet, fields):
        assert self._lock.held
        command, size = int(packet[14]), int(packet[15])
        assert command in self.sizes and size == self.sizes[command]
        assert len(packet) * 4 == packet[2] == 68 + size
        assert packet[5] == packet[11] == 0
        assert fields == {"hAdapter": 83, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0}
        request = bytes(packet)[68:]
        self.calls.append((command, request))
        self.counts[command] += 1
        fault = self.faults.get((command, self.counts[command]), {})
        if callable(fault):
            fault = fault(self, request) or {}
        response = bytearray(request)
        if command == 0x20802630:
            assert request == bytes(900)
            response[:] = self.info
        elif command == 0x2080261C:
            assert request == b"\x01" + bytes(135)
            at = 60 + self.native * 132
            response[4:] = self.native_info[at:at + 132]
        elif command == 0x20802618:
            assert request == bytes(5520)
            response[:] = self.native_info
        elif command == 0x2080261A:
            assert word(request, 16) == word(self.native_info, 4)
            assert request[:16] == bytes(16) and request[20:] == bytes(2104)
            response[:] = self.native_control
        elif command in (0x20802632, 0x20802633):
            at = 4 + self.client * 12
            assert word(request, 0) == 1 << self.client
            assert request[at:at + 2] == b"\x57\x57" and request[at + 8] == 0xFE
            if command == 0x20802632:
                response[:] = self.client_template
                put(response, at + 4, self.requested)
            else:
                expected = bytearray(self.client_template)
                put(expected, at + 4, word(request, at + 4))
                assert request == expected, "SET must retain all getter-produced opaque bytes"
                self.set_payloads.append(request)
                if not fault.get("ignore"):
                    self._update(word(request, at + 4))
        if fault.get("mutate"):
            fault["mutate"](self)
        if fault.get("payload"):
            fault["payload"](response)
        if fault.get("raise_after"):
            raise fault["raise_after"]
        ctypes.memmove(ctypes.addressof(packet) + 68, bytes(response), size)
        packet[5], packet[11], packet[16] = 1, fault.get("completion", 1), fault.get("rm", 0)
        if fault.get("header"):
            index, value = fault["header"]
            packet[index] = value
        return fault.get("nt", 0)


@pytest.mark.parametrize("client,native,slot,device,subsystem", [
    (0, 2, "0000:72:1d.3", 0xABCD, 0x87654321),
    (31, 31, "0000:ab:1f.7", 0xFFFF, 0x10203040),
    (11, 0, "0000:09:03.1", 0x1234, 0xABCDEF01),
])
@pytest.mark.parametrize("target", [123001, 550001, 1999000])
def test_extended_exact_request_projection_and_restoration(
    client, native, slot, device, subsystem, target,
):
    gpu = ExtendedRm26Gpu(client=client, native=native, slot=slot,
                          device=device, subsystem=subsystem)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    assert before["identity"] == {"slot": slot, "architecture": 6,
                                   "device_id": device, "subsystem_id": subsystem}
    assert before["binding"] == {
        "client_index": client, "native_index": native, "semantic_role": 1,
        "client": 0xFE, "units": "mW", "minimum_mw": 123000,
        "rated_mw": 321000, "maximum_mw": 550000, "layout": "rm26",
        "native_layout": "rm26_extended",
    }
    assert before["arbitration"] is None and before["arbitration_error"]
    expected_client = bytearray.fromhex(before["control_hex"])
    expected_native = bytearray.fromhex(before["native_control_hex"])
    put(expected_client, 4 + client * 12 + 4, target)
    put(expected_native, 20 + native * 52 + 4, target)
    for index, (_baseline, raised) in gpu.peer_requests.items():
        put(expected_native, 20 + index * 52 + 4, raised)
    after = controller.set_request(target)["after"]
    assert after["requested_mw"] == target
    assert bytes.fromhex(after["control_hex"]) == expected_client
    assert bytes.fromhex(after["native_control_hex"]) == expected_native
    assert gpu.set_payloads == [bytes(expected_client)]
    for field in ("binding", "info_hex", "role_info_hex", "native_info_hex"):
        assert after[field] == before[field]
    controller.set_request(gpu.original)
    assert cp._state(controller.read()) == cp._state(before)
    assert all(len(raw) == gpu.sizes[command] for command, raw in gpu.calls)
    assert 0x20802619 not in gpu.counts  # Optional STATUS has no understood decoder.


@pytest.mark.parametrize("event,kind", [
    ({"rm": 0x55}, "transient"), ({"nt": -1}, "transient"),
    ({"ignore": True}, "readback"), ({"completion": 0}, "transient"),
    ({"raise_after": RuntimeError("interrupted after SET")}, "transient"),
    ({"payload": lambda raw: raw.__setitem__(-1, raw[-1] ^ 1)}, "malformed"),
])
def test_extended_failed_write_compensates_board_and_captured_peer_pins(event, kind):
    gpu = ExtendedRm26Gpu(pinned=True)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    gpu.faults[0x20802633, 1] = event
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    assert caught.value.kind == kind
    assert caught.value.rollback["verified"]
    assert cp._state(caught.value.rollback["readback"]) == cp._state(before)
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2
    assert gpu.restore_peer.call_args_list == [
        call(index, baseline + 1000)
        for index, (baseline, _raised) in sorted(gpu.peer_requests.items())
    ]


def test_extended_failed_peer_restore_remains_unverified():
    gpu = ExtendedRm26Gpu(pinned=True)
    gpu.restore_peer.side_effect = None
    gpu.restore_peer.return_value = False, "peer refused"
    gpu.faults[0x20802633, 1] = {"rm": 0x55}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.rollback["attempted"]
    assert not caught.value.rollback["verified"]
    assert "peer refused" in caught.value.rollback["error"]


@pytest.mark.parametrize("fault", [{"rm": 0x55}, {"nt": -1}, {"completion": 0}])
def test_extended_transient_read_recaptures_once_and_then_requires_stability(fault):
    gpu = ExtendedRm26Gpu()
    gpu.faults[0x20802630, 1] = fault
    observed = cp.ClientPowerController(gpu).read()
    assert observed["requested_mw"] == gpu.original
    assert gpu.captures == 2 and gpu.counts[0x20802630] == 3
    assert not gpu.set_payloads


def test_extended_persistent_read_failure_is_bounded():
    gpu = ExtendedRm26Gpu()
    gpu.faults[0x20802630, 1] = gpu.faults[0x20802630, 2] = {"rm": 0x55}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "transient" and caught.value.rollback is None
    assert gpu.counts[0x20802630] == 2 and not gpu.set_payloads


@pytest.mark.parametrize("defect,kind", [
    ("units", "unsupported"), ("client_types", "malformed"),
    ("client_bounds", "malformed"), ("duplicate_role", "unsupported"),
    ("role_tail", "malformed"), ("native_units", "malformed"),
    ("native_duplicate", "malformed"), ("native_bounds", "malformed"),
    ("client_caller", "malformed"), ("missing", "unsupported"),
    ("zero", "unsupported"), ("native_header", "malformed"),
    ("native_mask", "malformed"), ("native_type", "malformed"),
    ("native_request", "unstable"),
])
def test_extended_invalid_binding_or_request_refuses_before_set(defect, kind):
    gpu = ExtendedRm26Gpu()
    client, native = 4 + gpu.client * 28, 60 + gpu.native * 132
    control, native_control = 4 + gpu.client * 12, 20 + gpu.native * 52
    if defect == "units":
        gpu.info[client + 3] = 1
    elif defect == "client_types":
        gpu.info[client + 1] ^= 1
    elif defect == "client_bounds":
        put(gpu.info, client + 4, 550001)
    elif defect == "duplicate_role":
        gpu.info[4 + gpu.unrelated_client * 28 + 2] = 1
    elif defect == "role_tail":
        # Exercise bytes outside the short ABI's 116-byte definition.
        gpu.faults[0x2080261C, 1] = {"payload": lambda raw: raw.__setitem__(135, raw[135] ^ 1)}
    elif defect == "native_units":
        gpu.native_info[native + 6] = 1
    elif defect == "native_duplicate":
        at = 60 + gpu.peers[0] * 132
        gpu.native_info[at:at + 132] = gpu.native_info[native:native + 132]
    elif defect == "native_bounds":
        put(gpu.native_info, native + 16, 550001)
    elif defect in ("client_caller", "missing", "zero"):
        offset, value = {"client_caller": (control + 8, 0xF8),
                         "missing": (control + 4, 0xFFFFFFFF),
                         "zero": (control + 4, 0)}[defect]
        gpu.faults[0x20802632, 1] = {"payload": lambda raw: put(raw, offset, value)}
    else:
        offset, value = {"native_header": (12, 254), "native_mask": (16, 0),
                         "native_type": (native_control, 213),
                         "native_request": (native_control + 4, gpu.original + 1)}[defect]
        put(gpu.native_control, offset, value)
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == kind and caught.value.rollback is None
    assert not gpu.set_payloads


@pytest.mark.parametrize("area", ["native_tail", "peer_opaque", "unknown_vote"])
def test_extended_unexplained_changes_do_not_get_accepted_as_peer_recalculation(area):
    gpu = ExtendedRm26Gpu(pinned=True)
    at = {"native_tail": -1, "peer_opaque": 20 + gpu.peers[0] * 52 + 9,
          "unknown_vote": 20 + gpu.unknown * 52 + 4}[area]
    gpu.faults[0x20802633, 1] = {
        "mutate": lambda target: target.native_control.__setitem__(at, target.native_control[at] ^ 1)}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "readback"
    assert not caught.value.rollback["verified"]
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2


@pytest.mark.parametrize("new_key", [
    (0x20802612, 2524, 2456), (0x2080A612, 54420, 54352),
    (0x20802612, 3552, 3484),
])
def test_extended_sticky_transport_rejects_changed_layout_before_another_exchange(new_key):
    gpu = ExtendedRm26Gpu()
    controller = cp.ClientPowerController(gpu)
    controller.read()
    calls = len(gpu.calls)
    header, _fields = gpu._current_limit_transport
    header[14], header[2], header[15] = new_key
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    assert caught.value.kind == "identity" and caught.value.rollback is None
    assert len(gpu.calls) == calls and not gpu.set_payloads


def test_short_rm26_controller_cannot_change_to_extended_geometry():
    from tests.test_client_power_rm26 import Rm26Gpu

    gpu = Rm26Gpu()
    controller = cp.ClientPowerController(gpu)
    controller.read()
    calls = len(gpu.calls)
    header, _fields = gpu._current_limit_transport
    header[2], header[15] = 3548, 3480
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.read()
    assert caught.value.kind == "identity"
    assert len(gpu.calls) == calls and not gpu.set_payloads


def test_modern_controller_cannot_change_to_extended_geometry():
    from tests.test_client_power_runtime import FakeGpu

    gpu = FakeGpu(architecture=6)
    controller = cp.ClientPowerController(gpu)
    controller.read()
    calls = len(gpu.calls)
    header, _fields = gpu._current_limit_transport
    header[14], header[2], header[15] = 0x20802612, 3548, 3480
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.read()
    assert caught.value.kind == "identity"
    assert len(gpu.calls) == calls and not gpu.set_payloads


@pytest.mark.parametrize("architecture", [5, 7, 8, 10])
def test_extended_geometry_is_turing_capability_not_a_driver_allowlist(architecture):
    gpu = ExtendedRm26Gpu()
    gpu.architecture = architecture
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).read()
    assert caught.value.kind == "unsupported" and not gpu.calls


def test_extended_transport_change_after_set_refuses_restore_into_new_geometry():
    gpu = ExtendedRm26Gpu()

    def change_transport(target):
        header, _fields = target._current_limit_transport
        header[2], header[15] = 2524, 2456

    gpu.faults[0x20802633, 1] = {"mutate": change_transport}
    # Persist the changed capture even when the recovery path clears its cache.
    getter = gpu._current_limit_transport_layouts

    def capture():
        result = getter()
        if gpu.set_payloads:
            change_transport(gpu)
        return result

    gpu._current_limit_transport_layouts = capture
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "identity"
    assert not caught.value.rollback["attempted"]
    assert not caught.value.rollback["verified"]
    assert len(gpu.set_payloads) == 1


