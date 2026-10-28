# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Modern RM packet capacity is independent of a V3 firmware policy table."""

import struct
from unittest.mock import Mock, call

import pytest

from druta import client_power_runtime as cp
from tests.test_client_power_runtime import FakeGpu, put


def modern_v30_gpu(*, client=37, native=31, slot="0000:72:1d.3",
                   device=0xABCD, subsystem=0x87654321):
    gpu = FakeGpu(client_index=client, native_index=native, architecture=6, slot=slot)
    # The wire model retains the modern 20512-byte INFO/13876-byte CONTROL
    # geometry and a 260-byte row carrying a V3 firmware policy definition.
    gpu.native_info[204 + native * 260] = 0x30
    for api in (gpu.nvapi, gpu.nvml):
        api.selected.update(devid=device, subsys=subsystem)
    return gpu


class ModernCoupledGpu(FakeGpu):
    """Modern buffers with driver-derived power and current peer requests."""

    def __init__(self, *, pinned=False):
        super().__init__(client_index=63, native_index=29, architecture=6)
        self.native_info[204 + self.native_index * 260] = 0x30
        self.other_peer = 7
        self.peer_requests = {self.native_peer: (50000, 60000), self.other_peer: (80000, 90000)}
        self.native_mask |= 1 << self.other_peer
        put(self.native_info, 4, self.native_mask)
        put(self.native_control, 16, self.native_mask)
        for index, kind, unit, rated in (
            (self.native_peer, 3, 1, 50000), (self.other_peer, 10, 0, 80000),
        ):
            at = 204 + index * 260
            self.native_info[at:at + 7] = bytes([0x30, 0x12, 0x23, 0x34, kind, 12, unit])
            struct.pack_into("<III", self.native_info, at + 8, 1000, rated, rated + 5000)
            put(self.native_control, 20 + index * 196, kind)
            self.native_control[20 + index * 196 + 13] = 0xA7
        self._update(self.original)
        if pinned:
            for index, (baseline, _) in self.peer_requests.items():
                put(self.native_control, 20 + index * 196 + 4, baseline + 1000)
        self.restore_peer = Mock(side_effect=self._restore_peer)
        self._write_policy_limit = self.restore_peer

    def _update(self, value):
        super()._update(value)
        for index, (baseline, raised) in getattr(self, "peer_requests", {}).items():
            put(self.native_control, 20 + index * 196 + 4, baseline if value == self.original else raised)

    def _restore_peer(self, index, value):
        assert self._lock.held and index in self.peer_requests
        put(self.native_control, 20 + index * 196 + 4, value)
        return True, "peer request restored"


@pytest.mark.parametrize("client,native,slot,device,subsystem", [
    (37, 31, "0000:72:1d.3", 0xABCD, 0x87654321),
    (63, 29, "0000:ab:1f.7", 0xFFFF, 0x10203040),
    (0, 2, "0000:09:03.1", 0x1234, 0xABCDEF01),
])
@pytest.mark.parametrize("requested", [550001, 1999000])
def test_modern_packets_with_v30_definition_write_and_restore_exact_state(
    client, native, slot, device, subsystem, requested,
):
    gpu = modern_v30_gpu(client=client, native=native, slot=slot,
                         device=device, subsystem=subsystem)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    assert before["identity"] == {
        "slot": slot, "architecture": 6, "device_id": device, "subsystem_id": subsystem,
    }
    assert before["binding"]["client_index"] == client
    assert before["binding"]["native_index"] == native
    assert "layout" not in before["binding"]
    assert before["binding"]["maximum_mw"] == 550000
    assert bytes.fromhex(before["role_info_hex"])[4] == 0x30
    assert not gpu.set_payloads

    expected_client = bytearray.fromhex(before["control_hex"])
    expected_native = bytearray.fromhex(before["native_control_hex"])
    struct.pack_into("<I", expected_client, 40 + client * 12 + 4, requested)
    struct.pack_into("<I", expected_native, 20 + native * 196 + 4, requested)
    result = controller.set_request(requested)
    assert gpu.set_payloads == [bytes(expected_client)]
    assert result["after"]["requested_mw"] == requested
    assert bytes.fromhex(result["after"]["control_hex"]) == expected_client
    assert bytes.fromhex(result["after"]["native_control_hex"]) == expected_native
    for key in ("identity", "binding", "info_hex", "role_info_hex", "native_info_hex"):
        assert result["after"][key] == before[key]
    # V4-only arbitration diagnostics must not erase a valid V3 power control.
    policy = next(row for row in result["after"]["arbitration"]["policies"]
                  if row["policy"] == native)
    assert not policy["available"]

    controller.set_request(gpu.original)
    assert cp._state(controller.read()) == cp._state(before)
    assert len(gpu.set_payloads) == 2
    assert all(len(raw) == cp.SIZES[command] for command, raw in gpu.calls)


@pytest.mark.parametrize("event,kind", [
    ({"rm": 0x55}, "transient"),
    ({"nt": -1}, "transient"),
    ({"ignore": True}, "readback"),
])
def test_modern_v30_failed_write_restores_opaque_bytes_and_requests(event, kind):
    gpu = modern_v30_gpu(client=63, native=29)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    gpu.faults[cp.SET, 1] = event
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    error = caught.value
    assert error.kind == kind
    assert error.rollback["attempted"] and error.rollback["verified"]
    assert error.rollback["error"] is None
    assert cp._state(error.rollback["readback"]) == cp._state(before)
    assert cp._state(controller.read()) == cp._state(before)
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2


@pytest.mark.parametrize("corruption,kind", [
    ("unknown_table_version", "malformed"),
    ("client_units", "unsupported"),
    ("native_units", "malformed"),
    ("unordered_client_bounds", "malformed"),
    ("native_bounds_differ", "malformed"),
    ("role_selector", "malformed"),
    ("role_definition", "malformed"),
    ("unoccupied_native_target", "malformed"),
    ("duplicate_native_definition", "malformed"),
])
def test_modern_v30_compatibility_keeps_metadata_validation_before_set(corruption, kind):
    gpu = modern_v30_gpu()
    client_at, native_at = 36 + gpu.client_index * 36, 204 + gpu.native_index * 260
    if corruption == "unknown_table_version":
        gpu.native_info[native_at] = 0x31
    elif corruption == "client_units":
        gpu.info[client_at + 3] = 1
    elif corruption == "native_units":
        gpu.native_info[native_at + 6] = 1
    elif corruption == "unordered_client_bounds":
        put(gpu.info, client_at + 4, 400000)
    elif corruption == "native_bounds_differ":
        put(gpu.native_info, native_at + 16, 550001)
    elif corruption == "role_selector":
        gpu.faults[cp.ROLE_INFO, 1] = {"payload": lambda raw: put(raw, 0, 2)}
    elif corruption == "role_definition":
        gpu.faults[cp.ROLE_INFO, 1] = {"payload": lambda raw: raw.__setitem__(204, 0xD7)}
    elif corruption == "unoccupied_native_target":
        put(gpu.native_info, 4, 1 << gpu.native_peer)
    elif corruption == "duplicate_native_definition":
        peer_at = 204 + gpu.native_peer * 260
        gpu.native_info[peer_at:peer_at + 260] = gpu.native_info[native_at:native_at + 260]
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    assert caught.value.kind == kind and caught.value.rollback is None
    assert not gpu.set_payloads and gpu.requested == gpu.original


@pytest.mark.parametrize("pinned", [False, True])
def test_modern_v30_accepts_derived_peer_votes_and_restores_captured_pins(pinned):
    gpu = ModernCoupledGpu(pinned=pinned)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    result = controller.set_request(551000)
    after = result["after"]
    assert after["requested_mw"] == 551000
    expected = bytearray.fromhex(before["native_control_hex"])
    put(expected, 20 + gpu.native_index * 196 + 4, 551000)
    for index, (_, raised) in gpu.peer_requests.items():
        put(expected, 20 + index * 196 + 4, raised)
        info_at = 204 + index * 260
        assert raised > cp._u32(gpu.native_info, info_at + 16)
    assert bytes.fromhex(after["native_control_hex"]) == expected
    assert after["info_hex"] == before["info_hex"]
    assert after["role_info_hex"] == before["role_info_hex"]
    assert after["native_info_hex"] == before["native_info_hex"]
    gpu.restore_peer.assert_not_called()

    with gpu._lock:
        restored = controller._rollback(before)
    assert restored["attempted"] and restored["verified"] and restored["error"] is None
    assert cp._state(restored["readback"]) == cp._state(before)
    assert cp._state(controller.read()) == cp._state(before)
    if pinned:
        assert gpu.restore_peer.call_args_list == [
            call(index, baseline + 1000) for index, (baseline, _) in sorted(gpu.peer_requests.items())
        ]
    else:
        gpu.restore_peer.assert_not_called()
    assert len(gpu.set_payloads) == 2


@pytest.mark.parametrize("event,kind", [
    ({"rm": 0x55}, "transient"),
    ({"nt": -1}, "transient"),
    ({"ignore": True}, "readback"),
])
def test_modern_v30_failed_board_write_restores_preexisting_peer_pins(event, kind):
    gpu = ModernCoupledGpu(pinned=True)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    gpu.faults[cp.SET, 1] = event
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    error = caught.value
    assert error.kind == kind
    assert error.rollback["attempted"] and error.rollback["verified"]
    assert cp._state(error.rollback["readback"]) == cp._state(before)
    assert cp._state(controller.read()) == cp._state(before)
    assert gpu.restore_peer.call_args_list == [
        call(index, baseline + 1000) for index, (baseline, _) in sorted(gpu.peer_requests.items())
    ]
    assert len(gpu.set_payloads) == 2


@pytest.mark.parametrize("corruption", ["units", "table_version", "type", "bounds"])
def test_modern_v30_rejects_recalculated_unknown_peer_and_compensates(corruption):
    gpu = ModernCoupledGpu()
    info_at, control_at = 204 + gpu.native_peer * 260, 20 + gpu.native_peer * 196
    if corruption == "units":
        gpu.native_info[info_at + 6] = 9
    elif corruption == "table_version":
        gpu.native_info[info_at] = 0x31
    elif corruption == "type":
        put(gpu.native_control, control_at, 0xFE)
    elif corruption == "bounds":
        put(gpu.native_info, info_at + 16, 0)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    error = caught.value
    assert error.kind == "readback"
    assert error.rollback["attempted"] and error.rollback["verified"]
    assert cp._state(error.rollback["readback"]) == cp._state(before)
    assert cp._state(controller.read()) == cp._state(before)
    gpu.restore_peer.assert_not_called()
    assert len(gpu.set_payloads) == 2


@pytest.mark.parametrize("surface", ["peer_opaque", "selected_opaque", "tail"])
def test_modern_v30_opaque_changes_fail_without_claiming_complete_restoration(surface):
    gpu = ModernCoupledGpu()
    offset = {"peer_opaque": 20 + gpu.native_peer * 196 + 13,
              "selected_opaque": 20 + gpu.native_index * 196 + 13, "tail": -1}[surface]
    gpu.faults[cp.SET, 1] = {
        "mutate": lambda model: model.native_control.__setitem__(offset, model.native_control[offset] ^ 1),
    }
    with pytest.raises(cp.ClientPowerError) as caught:
        cp.ClientPowerController(gpu).set_request(551000)
    error = caught.value
    assert error.kind == "readback"
    assert error.rollback["attempted"] and not error.rollback["verified"]
    assert error.rollback["error"]
    assert gpu.requested == gpu.original
    gpu.restore_peer.assert_not_called()
    assert len(gpu.set_payloads) == 2


def test_modern_v40_definition_does_not_inherit_v30_peer_recalculation_permission():
    gpu = ModernCoupledGpu()
    gpu.native_info[204 + gpu.native_index * 260] = 0x40
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    with pytest.raises(cp.ClientPowerError) as caught:
        controller.set_request(551000)
    assert caught.value.kind == "readback"
    assert caught.value.rollback["verified"]
    assert cp._state(caught.value.rollback["readback"]) == cp._state(before)
    assert len(gpu.set_payloads) == 2
