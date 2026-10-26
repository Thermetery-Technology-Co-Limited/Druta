"""Synthetic PWM switch-delay qualification and singleton SET restoration."""

import copy
import ctypes
import struct

import pytest

from druta import voltage_device_runtime as vd
from tests.test_clock_vf_registry import FakeGPU as PairedGPU


def packets(rows=None):
    rows = rows if rows is not None else {5: (3, 3, 4, 37), 19: (3, 1, 1, 301), 28: (9, 0, 0, 0)}
    info = bytearray((index * 7 + 11) % 256 for index in range(vd._INFO_SIZE))
    control = bytearray((index * 13 + 17) % 256 for index in range(vd._CONTROL_SIZE))
    info[0] = control[0] = 255
    mask = sum(1 << index for index in rows)
    struct.pack_into("<I", info, 4, mask)
    struct.pack_into("<I", control, 4, mask)
    for index, (kind, domain, operation, delay) in rows.items():
        at, ct = 8 + index * 56, 8 + index * 12
        info[at : at + 5] = bytes((kind, kind, domain, 255, operation))
        control[ct : ct + 2] = bytes((kind, kind))
        struct.pack_into("<III", info, at + 8, 500000, 1500000, 6250)
        struct.pack_into("<I", control, ct + 4, delay)
    return info, control


class FakeGPU(PairedGPU):
    def __init__(self, *, rows=None, get_actions=(), set_actions=(), **kwargs):
        super().__init__(**kwargs)
        self.info, self.control = packets(rows)
        self.get_actions, self.set_actions = list(get_actions), list(set_actions)
        self.writes, self.reads = [], []

    def _legacy_clk_escape(self, packet, fields):
        command, size = packet[14], packet[15]
        assert packet[2] == 68 + size and packet[5] == packet[11] == 0
        previous = bytes(self.control)
        if command == vd._SET:
            assert size == vd._CONTROL_SIZE
            payload = bytes(packet)[68:]
            mask = vd._u32(payload, 4)
            assert mask and not mask & (mask - 1)
            assert mask & vd._u32(self.info, 4) == mask
            index = mask.bit_length() - 1
            at = 8 + index * 12 + 4
            self.control[at : at + 4] = payload[at : at + 4]
            self.writes.append((payload, copy.deepcopy(fields)))
            actions = self.set_actions
        else:
            assert command in (vd._INFO, vd._CONTROL)
            if command == vd._CONTROL:
                assert size == vd._CONTROL_SIZE and packet[18] == vd._u32(self.info, 4)
                raw = self.control
            else:
                assert size == vd._INFO_SIZE and packet[18] == 0
                raw = self.info
            self.reads.append((command, packet[18]))
            ctypes.memmove(ctypes.addressof(packet) + 68, bytes(raw), len(raw))
            actions = self.get_actions
        packet[5], packet[11], packet[16] = 1, 1, 0
        action = actions.pop(0) if actions else None
        return action(self, packet, previous) if action else 0


def set_delay(gpu, delay=38, index=5, domain=3, operation=4):
    return vd.VoltageDeviceController(gpu).set(index, delay, voltage_domain=domain, operation_type=operation)


def reject(code, *, partial=False):
    def action(gpu, packet, previous):
        if not partial:
            gpu.control[:] = previous
        packet[16] = code
        return 0

    return action


def word(index, value):
    def action(gpu, packet, previous):
        packet[index] = value
        return 0

    return action


def byte(offset, value):
    def action(gpu, packet, previous):
        ctypes.c_ubyte.from_address(ctypes.addressof(packet) + 68 + offset).value = value
        return 0

    return action


def interrupted(gpu, packet, previous):
    raise OSError("synthetic interrupted escape")


def test_public_enum_names_and_stable_alternate_identity_read_without_telemetry():
    assert vd.DOMAIN_LABELS == {1: "LOGIC", 2: "SRAM", 3: "MSVDD"}
    assert vd.OPERATION_LABELS == {1: "DEFAULT", 2: "LPWR_STEADY_STATE", 3: "LPWR_SLEEP_STATE", 4: "IPC_VMIN"}
    gpu = FakeGPU()
    gpu.nvml = None
    observation = vd.VoltageDeviceController(gpu).read()
    assert set(observation) == {"identity", "mask", "info_hex", "control_hex", "rows", "stable_reads"}
    assert observation["identity"]["device_id"] == 0xABCD
    assert observation["identity"]["slot"] == "0000:72:1d.3"
    assert observation["stable_reads"] == 2
    assert [row["status"] for row in observation["rows"]] == ["ready", "ready", "unsupported"]
    assert observation["rows"][0]["switch_delay_us"] == 37
    assert len(gpu.reads) == 4 and not gpu.writes


@pytest.mark.parametrize("index,domain,operation,architecture", [(0, 1, 1, 6), (11, 2, 2, 8), (31, 3, 4, 10)])
@pytest.mark.parametrize("wanted", [1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF])
def test_full_uint32_range_singleton_mask_and_exact_opaque_projection(index, domain, operation, architecture, wanted):
    gpu = FakeGPU(rows={index: (3, domain, operation, 77)}, architecture=architecture)
    info, control = bytes(gpu.info), bytes(gpu.control)
    result = set_delay(gpu, wanted, index, domain, operation)
    expected = bytearray(control)
    struct.pack_into("<I", expected, 8 + index * 12 + 4, wanted)
    assert result["control_hex"] == expected.hex() and result["info_hex"] == info.hex()
    assert gpu.writes[0][0] == expected
    assert len(gpu.writes) == 1 and len(gpu.reads) == 8 and gpu.transport_count == 1


def test_selected_mask_does_not_change_full_inventory_and_preserves_unknown_peer_bytes():
    gpu = FakeGPU()
    original = bytes(gpu.control)
    result = set_delay(gpu)
    expected = bytearray(original)
    struct.pack_into("<I", expected, 8 + 5 * 12 + 4, 38)
    request = bytearray(expected)
    struct.pack_into("<I", request, 4, 1 << 5)
    assert gpu.writes[0][0] == request
    assert result["control_hex"] == expected.hex()
    assert result["mask"] == vd._u32(original, 4)
    assert all(mask == result["mask"] for command, mask in gpu.reads if command == vd._CONTROL)


@pytest.mark.parametrize("peer", ["type", "duplicate", "domain", "operation", "bounds", "zero"])
def test_malformed_or_unknown_peer_does_not_hide_qualified_target(peer):
    gpu = FakeGPU()
    pos, ctl = 8 + 19 * 56, 8 + 19 * 12
    if peer == "type":
        gpu.info[pos : pos + 2] = gpu.control[ctl : ctl + 2] = bytes((9, 9))
    elif peer == "duplicate":
        gpu.control[ctl + 1] = 7
    elif peer == "domain":
        gpu.info[pos + 2] = 255
    elif peer == "operation":
        gpu.info[pos + 4] = 255
    elif peer == "bounds":
        struct.pack_into("<I", gpu.info, pos + 8, 1600000)
    else:
        struct.pack_into("<I", gpu.control, ctl + 4, 0)
    before = bytes(gpu.control)
    result = set_delay(gpu)
    assert result["rows"][0]["status"] == "ready"
    assert result["rows"][1]["status"] in ("malformed", "unsupported")
    assert bytes(gpu.control)[ctl : ctl + 12] == before[ctl : ctl + 12]
    assert len(gpu.writes) == 1


@pytest.mark.parametrize(
    "rows,kind",
    [
        ({}, "unsupported"),
        ({5: (9, 3, 4, 37)}, "unsupported"),
        ({5: (3, 255, 4, 37)}, "unsupported"),
        ({5: (3, 3, 255, 37)}, "unsupported"),
        ({5: (3, 3, 4, 0)}, "malformed"),
    ],
)
def test_absent_unknown_or_zero_target_remains_readable_but_not_writable(rows, kind):
    gpu = FakeGPU(rows=rows)
    result = vd.VoltageDeviceController(gpu).read()
    if rows:
        assert result["rows"][0]["status"] == kind
    else:
        assert result["mask"] == 0 and result["rows"] == []
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == kind and caught.value.rollback is None
    assert not gpu.writes


@pytest.mark.parametrize("offset", [0, 1])
def test_selected_type_mismatch_cannot_reach_permissive_inner_setter(offset):
    gpu = FakeGPU()
    gpu.control[8 + 5 * 12 + offset] = 7
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "malformed" and not gpu.writes


@pytest.mark.parametrize("domain,operation", [(1, 4), (3, 1)])
def test_semantic_target_binding_checked_even_for_noop(domain, operation):
    gpu = FakeGPU()
    with pytest.raises(vd.VoltageDeviceError, match="domain or operation changed") as caught:
        set_delay(gpu, 37, domain=domain, operation=operation)
    assert caught.value.kind == "identity" and not gpu.writes


def test_noop_is_fresh_read_only():
    gpu = FakeGPU()
    result = set_delay(gpu, 37)
    assert result["rows"][0]["switch_delay_us"] == 37
    assert len(gpu.reads) == 4 and not gpu.writes


@pytest.mark.parametrize(
    "key,value",
    [
        ("index", True),
        ("index", -1),
        ("index", 32),
        ("delay_us", True),
        ("delay_us", 0),
        ("delay_us", -1),
        ("delay_us", 1 << 32),
        ("delay_us", 1.0),
        ("delay_us", "38"),
        ("voltage_domain", True),
        ("voltage_domain", 0),
        ("operation_type", True),
        ("operation_type", 5),
    ],
)
def test_invalid_requests_reject_before_any_get(key, value):
    gpu = FakeGPU()
    request = {"index": 5, "delay_us": 38, "voltage_domain": 3, "operation_type": 4, key: value}
    with pytest.raises(vd.VoltageDeviceError) as caught:
        vd.VoltageDeviceController(gpu).set(**request)
    assert caught.value.kind == "invalid" and caught.value.rollback is None
    assert not gpu.reads and not gpu.writes


def test_transient_get_retries_one_whole_capture_before_write():
    gpu = FakeGPU(get_actions=[lambda *args: -1])
    assert set_delay(gpu)["rows"][0]["switch_delay_us"] == 38
    assert len(gpu.reads) == 9 and gpu.transport_count == 2 and len(gpu.writes) == 1


def test_exhausted_transient_read_and_missing_handle_cannot_write():
    gpu = FakeGPU(get_actions=[lambda *args: -1, lambda *args: -1])
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "transient" and len(gpu.reads) == 2 and not gpu.writes
    gpu.nvapi.ok = False
    with pytest.raises(vd.VoltageDeviceError) as missing:
        set_delay(gpu)
    assert missing.value.kind == "transient" and not gpu.writes


def test_unsupported_get_is_not_retried():
    gpu = FakeGPU(get_actions=[word(16, 0x56)])
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "unsupported" and caught.value.status == 0x56
    assert len(gpu.reads) == 1 and not gpu.writes


@pytest.mark.parametrize(
    "actions",
    [[None, byte(0, 0)], [None, word(18, 0)], [byte(-69, 0)], [byte(vd._INFO_SIZE, 0)], [word(14, 0)], [word(5, 2)]],
)
def test_global_packet_corruption_blocks_all_writes(actions):
    gpu = FakeGPU(get_actions=actions)
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "malformed" and not gpu.writes


def test_unstable_info_is_not_used_to_bind_target():
    gpu = FakeGPU(get_actions=[None, None, byte(8 + 5 * 56 + 2, 1)])
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "unstable" and len(gpu.reads) == 4 and not gpu.writes


@pytest.mark.parametrize(
    "failure",
    [
        reject(0x40, partial=True),
        reject(0x56),
        interrupted,
        word(11, 0),
        word(5, 2),
        word(14, 0),
        byte(8 + 5 * 12 + 4, 0),
        byte(-69, 0),
        byte(vd._CONTROL_SIZE, 0),
    ],
)
def test_dispatched_failure_compensates_once_with_original_singleton_and_fixed_transport(failure):
    gpu = FakeGPU(set_actions=[failure])
    original_info, original_control = bytes(gpu.info), bytes(gpu.control)
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.rollback["attempted"] and caught.value.rollback["verified"]
    assert bytes(gpu.info) == original_info and bytes(gpu.control) == original_control
    expected = bytearray(original_control)
    struct.pack_into("<I", expected, 4, 1 << 5)
    assert gpu.writes[1][0] == expected
    assert len(gpu.writes) == 2 and gpu.writes[0][1] == gpu.writes[1][1] and gpu.transport_count == 1


def test_ignored_success_is_not_reported_as_applied():
    def ignore(gpu, packet, previous):
        gpu.control[:] = previous
        return 0

    gpu = FakeGPU(set_actions=[ignore])
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "readback" and caught.value.rollback["verified"]
    assert len(gpu.writes) == 2


@pytest.mark.parametrize(
    "source,offset",
    [("info", 8 + 5 * 56 + 20), ("control", 8 + 19 * 12 + 4), ("control", 8 + 5 * 12 + 8), ("control", 3)],
)
def test_peer_and_opaque_mutations_are_rejected_and_not_hidden_by_target_undo(source, offset):
    def mutate(gpu, packet, previous):
        getattr(gpu, source)[offset] ^= 1
        return 0

    gpu = FakeGPU(set_actions=[mutate])
    with pytest.raises(vd.VoltageDeviceError, match="rollback was not verified") as caught:
        set_delay(gpu)
    assert caught.value.kind == "readback" and not caught.value.rollback["verified"]
    assert caught.value.rollback["readback"]["rows"][0]["switch_delay_us"] == 37
    assert len(gpu.writes) == 2


def test_post_write_transient_gets_retry_without_recapturing_transport():
    gpu = FakeGPU(get_actions=[None] * 4 + [lambda *args: -1])
    assert set_delay(gpu)["rows"][0]["switch_delay_us"] == 38
    assert len(gpu.reads) == 9 and gpu.transport_count == 1 and len(gpu.writes) == 1


def test_post_write_read_failure_triggers_exact_compensation():
    gpu = FakeGPU(get_actions=[None] * 4 + [lambda *args: -1, lambda *args: -1])
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "transient" and caught.value.rollback["verified"]
    assert len(gpu.writes) == 2 and len(gpu.reads) == 10 and gpu.transport_count == 1


def test_undo_set_failure_still_reads_and_reports_remaining_request():
    gpu = FakeGPU(set_actions=[reject(0x40, partial=True), reject(0x40)])
    with pytest.raises(vd.VoltageDeviceError, match="rollback was not verified") as caught:
        set_delay(gpu)
    assert caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert caught.value.rollback["readback"]["rows"][0]["switch_delay_us"] == 38
    assert len(gpu.writes) == 2 and len(gpu.reads) == 8


def test_identity_switch_after_dispatch_cannot_write_undo_to_another_adapter():
    def switch(gpu, packet, previous):
        gpu.nvapi.gpu = gpu.nvapi.selected["handle"] = 789
        return 0

    gpu = FakeGPU(set_actions=[switch])
    with pytest.raises(vd.VoltageDeviceError) as caught:
        set_delay(gpu)
    assert caught.value.kind == "identity"
    assert not caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert len(gpu.writes) == 1 and gpu.transport_count == 1


def test_reused_controller_refuses_changed_paired_handle_before_write():
    gpu = FakeGPU()
    controller = vd.VoltageDeviceController(gpu)
    controller.read()
    gpu.nvapi.gpu = gpu.nvapi.selected["handle"] = 789
    with pytest.raises(vd.VoltageDeviceError) as caught:
        controller.set(5, 38, voltage_domain=3, operation_type=4)
    assert caught.value.kind == "identity" and caught.value.rollback is None and not gpu.writes


def test_interruption_after_dispatch_also_restores():
    def interrupt(gpu, packet, previous):
        raise KeyboardInterrupt("synthetic")

    gpu = FakeGPU(set_actions=[interrupt])
    original = bytes(gpu.control)
    with pytest.raises(KeyboardInterrupt) as caught:
        set_delay(gpu)
    assert caught.value.rollback["verified"] and bytes(gpu.control) == original
    assert len(gpu.writes) == 2
