# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent RM26 wire model: synthetic buffers only, no installed driver."""

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


class Rm26Gpu:
    """Model the observed old wire format without production geometry helpers.

    FE has an existing request. Public-wrapper F8 is a different caller and
    must never be substituted or seeded by the client controller.
    """

    sizes = {0x20802630: 644, 0x20802632: 388, 0x20802633: 388,
             0x2080261C: 120, 0x20802618: 5008, 0x2080261A: 2108}

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

        self.info = bytearray((i * 17 + 37) % 256 for i in range(644))
        put(self.info, 0, self.client_mask)
        at = 4 + self.unrelated_client * 20
        self.info[at:at + 4] = b"\x99\x55\x77\x09"
        at = 4 + client * 20
        self.info[at:at + 4] = b"\x57\x57\x01\x00"
        struct.pack_into("<III", self.info, at + 4, 123000, 321000, 550000)

        self.native_info = bytearray((i * 13 + 43) % 256 for i in range(5008))
        put(self.native_info, 4, self.native_mask)
        for index, kind, channel, units, bounds in (
            (native, 10, 9, 0, (123000, 321000, 550000)),
            (self.peers[0], 11, 12, 1, (1000, 50000, 55000)),
            (self.peers[1], 4, 17, 0, (1000, 80000, 85000)),
            (self.unknown, 213, 22, 9, (0, 0, 0)),
        ):
            at = 52 + index * 116
            self.native_info[at:at + 7] = bytes([0x81 if index == self.unknown else 0x30,
                                               0x17, 0x28, 0x39, kind, channel, units])
            struct.pack_into("<III", self.native_info, at + 8, *bounds)

        self.native_control = bytearray((i * 19 + 31) % 256 for i in range(2108))
        put(self.native_control, 0, self.native_mask)
        for index in (native, *self.peers, self.unknown):
            put(self.native_control, 4 + index * 52, self.native_info[52 + index * 116 + 4])
        put(self.native_control, 4 + self.unknown * 52 + 4, 76543)
        self.client_template = bytearray((i * 23 + 51) % 256 for i in range(388))
        put(self.client_template, 0, 1 << client)
        at = 4 + client * 12
        self.client_template[at:at + 2] = b"\x57\x57"
        self.client_template[at + 8] = 0xFE
        self._update(self.original)
        if pinned:
            for index, (baseline, _) in self.peer_requests.items():
                put(self.native_control, 4 + index * 52 + 4, baseline + 1000)
        self.restore_peer = Mock(side_effect=self._restore_peer)
        self._write_policy_limit = self.restore_peer

    def slot(self):
        return self.pci_slot

    def arch(self):
        return self.architecture

    def _update(self, value):
        self.requested = value
        put(self.native_control, 4 + self.native * 52 + 4, value)
        for index, (baseline, raised) in self.peer_requests.items():
            put(self.native_control, 4 + index * 52 + 4, baseline if value == self.original else raised)

    def _restore_peer(self, index, value):
        assert self._lock.held and index in self.peer_requests
        put(self.native_control, 4 + index * 52 + 4, value)
        return True, "peer restored"

    def _current_limit_transport_layouts(self):
        assert self._lock.held
        self.transport_reads += 1
        if self._current_limit_transport is None:
            self.captures += 1
            header = list(range(17))
            header[0:5] = [0x4E564441, 0x10002, 2524, 0x4E562A2A, 0x0500002B]
            header[5] = header[11] = header[16] = 0
            header[14], header[15] = 0x20802612, 2456
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
            assert request == bytes(644)
            response[:] = self.info
        elif command == 0x2080261C:
            assert request == b"\x01" + bytes(119)
            at = 52 + self.native * 116
            response[4:] = self.native_info[at:at + 116]
        elif command == 0x20802618:
            assert request == bytes(5008)
            response[:] = self.native_info
        elif command == 0x2080261A:
            assert word(request, 0) == word(self.native_info, 4)
            assert request[4:] == bytes(2104)
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
def test_rm26_existing_fe_request_writes_and_restores_only_selected_fields(
    client, native, slot, device, subsystem, target,
):
    gpu = Rm26Gpu(client=client, native=native, slot=slot, device=device, subsystem=subsystem)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    assert before["identity"] == {
        "slot": slot, "architecture": 6, "device_id": device, "subsystem_id": subsystem,
    }
    assert before["binding"] == {
        "client_index": client, "native_index": native, "semantic_role": 1, "client": 0xFE,
        "units": "mW", "minimum_mw": 123000, "rated_mw": 321000, "maximum_mw": 550000, "layout": "rm26",
    }
    assert before["arbitration"] is None and before["arbitration_error"]
    assert not gpu.set_payloads
    expected_client = bytearray.fromhex(before["control_hex"])
    expected_native = bytearray.fromhex(before["native_control_hex"])
    put(expected_client, 4 + client * 12 + 4, target)
    put(expected_native, 4 + native * 52 + 4, target)
    for index, (_, raised) in gpu.peer_requests.items():
        put(expected_native, 4 + index * 52 + 4, raised)
        assert raised > word(gpu.native_info, 52 + index * 116 + 16)
    after = controller.set_request(target)["after"]
    assert after["requested_mw"] == target
    assert gpu.set_payloads == [bytes(expected_client)]
    assert bytes.fromhex(after["control_hex"]) == expected_client
    assert bytes.fromhex(after["native_control_hex"]) == expected_native
    for key in ("identity", "binding", "info_hex", "role_info_hex", "native_info_hex"):
        assert after[key] == before[key]
    controller.set_request(gpu.original)
    assert cp._state(controller.read()) == cp._state(before)
    assert len(gpu.set_payloads) == 2
    assert all(len(raw) == gpu.sizes[command] for command, raw in gpu.calls)
    gpu.restore_peer.assert_not_called()


@pytest.mark.parametrize("event,kind", [
    ({"rm": 0x55}, "transient"), ({"nt": -1}, "transient"),
    ({"ignore": True}, "readback"), ({"completion": 0}, "transient"),
    ({"raise_after": RuntimeError("transport interrupted after dispatch")}, "transient"),
])
def test_rm26_failed_write_restores_captured_board_and_two_peer_pins(event, kind):
    gpu = Rm26Gpu(pinned=True)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    gpu.faults[0x20802633, 1] = event
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    error = caught.value
    assert error.kind == kind
    assert error.rollback["attempted"] and error.rollback["verified"]
    assert error.rollback["error"] is None
    assert cp._state(error.rollback["readback"]) == cp._state(before)
    assert cp._state(controller.read()) == cp._state(before)
    assert len(gpu.set_payloads) == 2 and gpu.requested == gpu.original
    # Even an ignored attempted board SET is followed by the compensating
    # board SET, whose ordinary recomputation can replace pre-existing pins.
    assert gpu.restore_peer.call_args_list == [
        call(index, baseline + 1000) for index, (baseline, _) in sorted(gpu.peer_requests.items())
    ]


def test_rm26_request_acceptance_does_not_require_peer_recomputation_or_status():
    gpu = Rm26Gpu()
    gpu.peer_requests = {index: (baseline, baseline) for index, (baseline, _) in gpu.peer_requests.items()}
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    after = controller.set_request(551000)["after"]
    expected = bytearray.fromhex(before["native_control_hex"])
    put(expected, 4 + gpu.native * 52 + 4, 551000)
    assert after["requested_mw"] == 551000
    assert bytes.fromhex(after["native_control_hex"]) == expected
    assert after["arbitration"] is None and after["arbitration_error"]
    # The request readback proves neither a changed effective native limit nor
    # physical enforcement. STATUS is deliberately absent from this wire model.
    assert {command for command, _ in gpu.calls}.issubset(gpu.sizes)


def test_rm26_metadata_remains_readable_when_fe_has_no_existing_request():
    gpu = Rm26Gpu()
    gpu.requested = 0xFFFFFFFF
    result = cp.ClientPowerController(gpu).read_metadata()
    assert result["binding"]["layout"] == "rm26"
    assert result["binding"]["maximum_mw"] == 550000
    assert not gpu.set_payloads and gpu.counts[0x20802632] == 0


def test_rm26_changed_set_echo_is_not_treated_as_verified_acceptance():
    gpu = Rm26Gpu(pinned=True)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    gpu.faults[0x20802633, 1] = {"payload": lambda raw: raw.__setitem__(99, raw[99] ^ 1)}
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    assert caught.value.kind == "malformed"
    assert caught.value.rollback["verified"] and len(gpu.set_payloads) == 2
    assert cp._state(caught.value.rollback["readback"]) == cp._state(before)


def test_rm26_changed_target_after_dispatch_does_not_restore_into_new_handle():
    gpu = Rm26Gpu()

    def change_target(target):
        target.nvapi.gpu += 1
        target.nvapi.selected["handle"] = target.nvapi.gpu

    gpu.faults[0x20802633, 1] = {"mutate": change_target}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "identity"
    assert not caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert len(gpu.set_payloads) == 1


def test_rm26_explicit_rollback_restores_pins_and_full_opaque_state():
    gpu = Rm26Gpu(pinned=True)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    controller.set_request(551000)
    with gpu._lock:
        restored = controller._rollback(before)
    assert restored["attempted"] and restored["verified"] and restored["error"] is None
    assert cp._state(restored["readback"]) == cp._state(before)
    assert gpu.restore_peer.call_args_list == [
        call(index, baseline + 1000) for index, (baseline, _) in sorted(gpu.peer_requests.items())
    ]


def test_rm26_failed_peer_restore_reports_incomplete_recovery():
    gpu = Rm26Gpu(pinned=True)
    gpu.restore_peer.side_effect = None
    gpu.restore_peer.return_value = False, "peer request refused"
    gpu.faults[0x20802633, 1] = {"rm": 0x55}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert "peer request refused" in caught.value.rollback["error"]
    assert len(gpu.set_payloads) == 2 and gpu.requested == gpu.original


@pytest.mark.parametrize("corruption,kind", [
    ("client_units", "unsupported"), ("client_types", "malformed"),
    ("client_bounds", "malformed"), ("client_empty", "unsupported"),
    ("client_duplicate_role", "unsupported"), ("native_units", "malformed"),
    ("native_table_version", "malformed"), ("native_bounds", "malformed"),
    ("native_unoccupied", "malformed"), ("native_duplicate_role", "malformed"),
    ("role_selector", "malformed"), ("role_definition", "malformed"),
    ("control_mask", "malformed"), ("control_types", "malformed"),
    ("control_caller_f8", "malformed"), ("control_missing", "unsupported"),
    ("control_zero", "unsupported"), ("native_control_mask", "malformed"),
    ("native_control_type", "malformed"), ("native_control_request", "unstable"),
])
def test_rm26_invalid_metadata_or_request_prevents_any_set(corruption, kind):
    gpu = Rm26Gpu()
    ca, na, nc = 4 + gpu.client * 20, 52 + gpu.native * 116, 4 + gpu.native * 52
    selected = 4 + gpu.client * 12
    if corruption == "client_units":
        gpu.info[ca + 3] = 1
    elif corruption == "client_types":
        gpu.info[ca + 1] ^= 1
    elif corruption == "client_bounds":
        put(gpu.info, ca + 4, 400000)
    elif corruption == "client_empty":
        put(gpu.info, 0, 0)
    elif corruption == "client_duplicate_role":
        gpu.info[4 + gpu.unrelated_client * 20 + 2] = 1
    elif corruption == "native_units":
        gpu.native_info[na + 6] = 1
    elif corruption == "native_table_version":
        gpu.native_info[na] = 0x31
    elif corruption == "native_bounds":
        put(gpu.native_info, na + 16, 550001)
    elif corruption == "native_unoccupied":
        put(gpu.native_info, 4, gpu.native_mask & ~(1 << gpu.native))
    elif corruption == "native_duplicate_role":
        at = 52 + gpu.peers[0] * 116
        gpu.native_info[at:at + 116] = gpu.native_info[na:na + 116]
    elif corruption in ("role_selector", "role_definition"):
        at = 0 if corruption == "role_selector" else 100
        gpu.faults[0x2080261C, 1] = {"payload": lambda raw: raw.__setitem__(at, raw[at] ^ 1)}
    elif corruption.startswith("control_"):
        offset, value = {
            "control_mask": (0, 0), "control_types": (selected, 0),
            "control_caller_f8": (selected + 8, 0xF8),
            "control_missing": (selected + 4, 0xFFFFFFFF), "control_zero": (selected + 4, 0),
        }[corruption]
        gpu.faults[0x20802632, 1] = {"payload": lambda raw: put(raw, offset, value)}
    elif corruption.startswith("native_control_"):
        offset, value = {
            "native_control_mask": (0, 0), "native_control_type": (nc, 0xFF),
            "native_control_request": (nc + 4, gpu.original + 1),
        }[corruption]
        put(gpu.native_control, offset, value)
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == kind and caught.value.rollback is None
    assert not gpu.set_payloads and gpu.requested == gpu.original


@pytest.mark.parametrize("mutation", ["tail", "selected_opaque", "peer_opaque", "unknown_request"])
def test_rm26_unexplained_native_change_refuses_success_and_full_recovery(mutation):
    gpu = Rm26Gpu(pinned=True)
    offset = {"tail": -1, "selected_opaque": 4 + gpu.native * 52 + 11,
              "peer_opaque": 4 + gpu.peers[0] * 52 + 9,
              "unknown_request": 4 + gpu.unknown * 52 + 4}[mutation]
    gpu.faults[0x20802633, 1] = {
        "mutate": lambda target: target.native_control.__setitem__(offset, target.native_control[offset] ^ 1),
    }
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "readback"
    assert caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2
    gpu.restore_peer.assert_not_called()


@pytest.mark.parametrize("peer_problem", ["version", "units", "bounds", "type", "missing"])
def test_rm26_peer_recomputation_is_only_accepted_for_understood_existing_requests(peer_problem):
    gpu = Rm26Gpu()
    index = gpu.peers[0]
    at, ctl = 52 + index * 116, 4 + index * 52
    if peer_problem == "version":
        gpu.native_info[at] = 0x31
    elif peer_problem == "units":
        gpu.native_info[at + 6] = 9
    elif peer_problem == "bounds":
        put(gpu.native_info, at + 8, 80000)
    elif peer_problem == "type":
        put(gpu.native_control, ctl, 201)
    else:
        gpu.peer_requests[index] = (0xFFFFFFFF, 60000)
        gpu._update(gpu.original)
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "readback"
    assert caught.value.rollback["verified"] and gpu.requested == gpu.original
    assert len(gpu.set_payloads) == 2


@pytest.mark.parametrize("event", [{"nt": -1}, {"rm": 0x55}, {"completion": 0}])
def test_rm26_transient_read_recaptures_once_and_requires_two_fresh_snapshots(event):
    gpu = Rm26Gpu()
    gpu.faults[0x20802630, 1] = event
    result = cp.ClientPowerController(gpu).read()
    assert result["requested_mw"] == gpu.original
    assert gpu.counts[0x20802630] == 3 and gpu.captures == 2
    assert not gpu.set_payloads


def test_rm26_persistent_read_failure_is_bounded_and_never_dispatches_set():
    gpu = Rm26Gpu()
    for attempt in (1, 2):
        gpu.faults[0x20802630, attempt] = {"rm": 0x55}
    with pytest.raises(cp.ClientPowerError, match="RM 0x55") as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "transient" and caught.value.rollback is None
    assert gpu.counts[0x20802630] == 2 and not gpu.set_payloads


def test_rm26_size_refusal_does_not_probe_modern_namespace_or_retry_a_set():
    gpu = Rm26Gpu()
    gpu.faults[0x20802630, 1] = {"rm": 0x1F}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "unsupported" and caught.value.rollback is None
    assert [command for command, _ in gpu.calls] == [0x20802630]


@pytest.mark.parametrize("architecture", [5, 7, 8, 10])
def test_rm26_unqualified_architecture_is_refused_before_any_exchange(architecture):
    gpu = Rm26Gpu()
    gpu.architecture = architecture
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "unsupported" and caught.value.rollback is None
    assert not gpu.calls and not gpu.set_payloads


@pytest.mark.parametrize("defect", ["envelope", "header_length", "fields", "field_value", "unknown_geometry"])
def test_rm26_malformed_or_different_transport_prevents_any_set(defect):
    gpu = Rm26Gpu()

    def damage(target):
        header, fields = target._current_limit_transport
        if defect == "envelope":
            header[0] = 0
        elif defect == "header_length":
            header.pop()
        elif defect == "fields":
            fields["unknown"] = 0
        elif defect == "field_value":
            fields["hAdapter"] = True
        else:
            header[2], header[15] = 3552, 3484

    gpu.transport_faults[1] = damage
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind in ("malformed", "unsupported")
    assert caught.value.rollback is None and not gpu.calls and not gpu.set_payloads


@pytest.mark.parametrize("defect", ["pci_slot", "device", "subsystem", "handle"])
def test_rm26_target_pairing_failure_prevents_any_exchange(defect):
    gpu = Rm26Gpu()
    if defect == "pci_slot":
        gpu.nvml.selected["slot"] = "0000:53:1d.3"
    elif defect == "device":
        gpu.nvml.selected["devid"] ^= 1
    elif defect == "subsystem":
        gpu.nvml.selected["subsys"] ^= 1
    else:
        gpu.nvapi.selected["handle"] += 1
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "identity" and caught.value.rollback is None
    assert not gpu.calls and not gpu.set_payloads


@pytest.mark.parametrize("value", [True, 1.5, "351000", 0, -1, 0xFFFFFFFF, 0x100000000])
def test_rm26_invalid_request_values_are_refused_without_hardware_access(value):
    gpu = Rm26Gpu()
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(value)
    assert caught.value.kind == "invalid" and caught.value.rollback is None
    assert not gpu.calls and not gpu.transport_reads and not gpu.set_payloads


def test_rm26_unstable_opaque_get_state_prevents_set_without_retrying_it():
    gpu = Rm26Gpu()
    gpu.faults[0x20802632, 2] = {"payload": lambda raw: raw.__setitem__(99, raw[99] ^ 1)}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "unstable" and caught.value.rollback is None
    assert gpu.counts[0x20802632] == 2 and not gpu.set_payloads


def test_rm26_response_envelope_corruption_prevents_set():
    gpu = Rm26Gpu()
    gpu.faults[0x20802630, 1] = {"header": (14, 0x20802633)}
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == "malformed" and caught.value.rollback is None
    assert not gpu.set_payloads
