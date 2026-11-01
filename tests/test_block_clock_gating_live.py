"""Offline BLCG source-layout, read-only packet, and session lifecycle checks."""

import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import block_clock_gating_live as live
from druta.perf_diagnostics import PerfDiagnosticError


@pytest.fixture
def device(monkeypatch):
    data = {
        "identity": "paired",
        "parent": 0x5678,
        "native": (0x1B0, 5, 0xB2, 0x1357),
        "values": {0x4041F0: 0x1000C646, 0x409890: 0x4000007F, 0x405BF0: 0x2000C044, 0x407000: 0x5000C042},
        "selected": {0x4041F0: 0x8000, 0x409890: 5, 0x405BF0: 0x8000, 0x407000: 0x40000000},
        "writes": [],
        "reads": 0,
        "arch_reads": 0,
        "transient": 0,
        "malformed": None,
        "rejected_row": None,
        "stub": False,
        "global": 0,
        "status": 0,
    }
    header = (
        0x4E564441,
        0x10002,
        72,
        0x4E562A2A,
        0x0500002B,
        0,
        71,
        72,
        73,
        74,
        75,
        1,
        0x1234,
        data["parent"],
        0x2080A612,
        4,
        0,
    )
    fields = {key: i for i, key in enumerate(("hAdapter", "hDevice", "Type", "Flags", "hContext"))}
    gpu = SimpleNamespace(_lock=threading.RLock())

    class Reader:
        def __init__(self, gpu):
            self.gpu = gpu

        def _transport(self):
            words = list(header)
            words[13] = data["parent"]
            return words, dict(fields)

    monkeypatch.setattr(live, "PerfDiagnosticsReader", Reader)
    monkeypatch.setattr(live.clock, "_binding", lambda reader: data["identity"])

    def escape(packet, actual_fields):
        assert actual_fields == fields
        data["writes"].append(tuple(packet))
        packet[5] = packet[11] = 1
        if packet[4] == 0x0500002A:
            packet[19] = 0
        elif packet[4] == 0x0500000C:
            packet[15] = 0
        else:
            packet[16] = 0
            if packet[14] == 0xB0CC0301:
                assert packet[18] == 4
                packet[17] = data["global"]
                packet[19] = 0xFFFFFFF3 | (data["status"] << 2)
                if not data["stub"] and not data["global"] and not data["status"]:
                    for address, mask in data["selected"].items():
                        data["values"][address] &= ~mask
            elif packet[14] == 0xB0CC0302:
                assert packet[17] == 12
                if not data["stub"]:
                    for address, mask in data["selected"].items():
                        data["values"][address] |= mask
        return 0

    def get(reader, identity, command, size, request):
        assert identity == data["identity"]
        if command == 0x20801701:
            assert size == 16 and request == bytes(16)
            data["arch_reads"] += 1
            result = struct.pack("<4I", *data["native"])
            return result[:-4] if data["malformed"] == "architecture_size" else result
        assert command == 0x20800122 and size == 48 and len(request) == 48
        data["reads"] += 1
        if data["transient"]:
            data["transient"] -= 1
            raise PerfDiagnosticError("transient", "temporary register read failure")
        if data["malformed"] == "rmstatus":
            raise PerfDiagnosticError("unsupported", "RM status 0x56", 0x56)
        a, b, c, d, e, count, pointer, route, pad, route_value = struct.unpack("<6IQIIQ", request)
        assert (a, b, c, d, e, count, route, pad, route_value) == (0, 0, 0, 0, 0, 4, 0, 0, 0)
        assert pointer
        ops = (ctypes.c_ubyte * (32 * count)).from_address(pointer)
        for index, address in enumerate(live._ALLOWED_CLEAR):
            at = index * 32
            assert struct.unpack_from("<BBBB7I", ops, at) == (0, 0, 0, 0, 0, 0, address, 0, 0, 0, 0)
            struct.pack_into("<I", ops, at + 20, data["values"][address])
        if data["rejected_row"] is not None:
            ops[data["rejected_row"] * 32 + 2] = 1
        if data["malformed"] == "opaque":
            ops[28] = 1
        elif data["malformed"] == "operation":
            ops[0] = 1
        elif data["malformed"] == "address":
            ops[12] ^= 4
        elif data["malformed"] == "guard":
            ctypes.c_ubyte.from_address(pointer + 128).value = 0
        if data["malformed"] == "params":
            return request[:-1] + b"\x01"
        if data["malformed"] == "parameter_size":
            return request[:-4]
        return request

    gpu._legacy_clk_escape = escape
    monkeypatch.setattr(live.lpwr, "_get", get)
    return live.BlockClockGatingController(gpu), data


def commands(data):
    return [words[14] if words[4] == 0x0500002B else words[4] for words in data["writes"]]


def test_alternate_baseline_source_bits_and_reserved_fields_preserved(device):
    controller, data = device
    baseline = dict(data["values"])
    snapshot = controller.read()
    assert snapshot["support"] and snapshot["architecture"]["raw_auxiliary"] == 0x1357
    assert snapshot["registers"] == {hex(address): value for address, value in baseline.items()}
    assert not data["writes"]
    held = controller.hold()
    assert held["owned_request"] and not held["uncertain_outcome"]
    assert held["observed_register_bits"] == {hex(address): mask for address, mask in data["selected"].items()}
    controller.hold()
    assert commands(data) == [0x0500002A, 0xB0CC0301]
    # A different source reason clears a peer bit while this request is held.
    data["values"][0x409890] &= ~0x40
    released = controller.release()
    expected = {hex(address): value for address, value in baseline.items()}
    expected[hex(0x409890)] &= ~0x40
    assert released["registers"] == expected
    assert not released["owned_request"] and not released["uncertain_outcome"]
    assert commands(data) == [0x0500002A, 0xB0CC0301, 0xB0CC0302, 0x0500000C]
    assert controller.close() is None


@pytest.mark.parametrize("implementation", [2, 3, 5, 6, 7])
def test_source_mapped_native_implementations_and_arbitrary_revisions(device, implementation):
    controller, data = device
    data["native"] = (0x1B0, implementation, 0x12345678, 0x87654321)
    assert controller.read()["support"]
    assert not data["writes"]


@pytest.mark.parametrize("native", [(0x1B0, 4, 0, 0), (0x1A0, 3, 0, 0), (0x1B0, 99, 0, 0)])
def test_unknown_register_layout_never_touches_registers_or_requests(device, native):
    controller, data = device
    data["native"] = native
    state = controller.read()
    assert not state["support"] and state["registers"] == {}
    with pytest.raises(live.BlockClockGatingError, match="layout is not understood"):
        controller.hold()
    assert data["reads"] == 0 and not data["writes"]


@pytest.mark.parametrize("row", range(4))
def test_partial_register_access_is_a_read_error_not_missing_peer_support(device, row):
    controller, data = device
    controller.hold()
    data["rejected_row"] = row
    with pytest.raises(live.BlockClockGatingError, match="READ32.*rejected"):
        controller.read()
    assert controller._owned
    data["rejected_row"] = None
    assert not controller.release()["owned_request"]


@pytest.mark.parametrize(
    "malformed",
    ["architecture_size", "rmstatus", "opaque", "operation", "address", "params", "parameter_size", "guard"],
)
def test_malformed_or_rejected_read_never_dispatches_profiler_request(device, malformed):
    controller, data = device
    data["malformed"] = malformed
    with pytest.raises((live.BlockClockGatingError, PerfDiagnosticError)):
        controller.hold()
    assert not data["writes"]


@pytest.mark.parametrize("poison", [0xFFFFFFFF, 0xBAD00000, 0xBAD01234])
def test_poison_register_values_rejected_before_any_write(device, poison):
    controller, data = device
    data["values"][0x4041F0] = poison
    with pytest.raises(live.BlockClockGatingError, match="unusable"):
        controller.hold()
    assert not data["writes"]


def test_transient_reads_retry_boundedly_without_losing_held_request(device):
    controller, data = device
    data["transient"] = 1
    assert controller.hold()["owned_request"]
    data["transient"] = 2
    reads = data["reads"]
    with pytest.raises(PerfDiagnosticError, match="temporary"):
        controller.read()
    assert data["reads"] == reads + 2 and controller._owned
    assert not controller.release()["owned_request"]


def test_successful_noop_stub_is_not_verified_and_can_be_explicitly_freed(device):
    controller, data = device
    data["stub"] = True
    with pytest.raises(live.BlockClockGatingError, match="no independent"):
        controller.hold()
    assert controller.read()["uncertain_outcome"]
    with pytest.raises(live.BlockClockGatingError, match="uncertain"):
        controller.hold()
    controller.close()
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]


@pytest.mark.parametrize(
    ("global_status", "status", "message"),
    [(1, 0, "global status"), (0, 1, "rejected"), (0, 2, "unsupported"), (0, 3, "failed")],
)
def test_public_profiler_status_must_accept_selected_blcg_field(device, global_status, status, message):
    controller, data = device
    data.update({"global": global_status, "status": status})
    with pytest.raises(live.BlockClockGatingError, match=message):
        controller.hold()
    controller.close()
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]


@pytest.mark.parametrize("change", ["added", "outside_mask", "layout"])
def test_hold_requires_cleared_only_source_bits_and_unchanged_native_layout(device, monkeypatch, change):
    controller, data = device
    request = controller._profiler.request

    def changed_request():
        request()
        if change == "added":
            data["values"][0x409890] |= 0x80
        elif change == "outside_mask":
            data["values"][0x4041F0] &= ~0x10000000
        else:
            data["native"] = (0x1B0, 6, 0xB2, 0x1357)

    monkeypatch.setattr(controller._profiler, "request", changed_request)
    with pytest.raises(live.BlockClockGatingError, match="outside|layout changed"):
        controller.hold()
    with pytest.raises(live.BlockClockGatingError, match="exact expected"):
        controller.close()
    assert controller._profiler.handle is None


def test_baseline_without_source_bits_refuses_before_any_write(device):
    controller, data = device
    data["values"] = {address: value & ~live._ALLOWED_CLEAR[address] for address, value in data["values"].items()}
    with pytest.raises(live.BlockClockGatingError, match="No source-defined BLCG bit"):
        controller.hold()
    assert not data["writes"]


def test_changed_register_during_stability_pair_refuses_without_retries(device, monkeypatch):
    controller, data = device
    original = controller._once
    calls = []

    def unstable():
        result = original()
        calls.append(None)
        result["registers"]["0x409890"] ^= len(calls)
        return result

    monkeypatch.setattr(controller, "_once", unstable)
    with pytest.raises(live.BlockClockGatingError, match="not stable"):
        controller.hold()
    assert len(calls) == 2 and not data["writes"]


def test_release_exact_witness_mismatch_frees_and_reports_error(device):
    controller, data = device
    controller.hold()
    # A concurrent request keeps a formerly cleared bit down on our release.
    data["selected"][0x409890] &= ~1
    with pytest.raises(live.BlockClockGatingError, match="exact expected"):
        controller.release()
    state = controller.read()
    assert state["last_verification_error"]
    assert not state["owned_request"] and not state["uncertain_outcome"]
    assert commands(data)[-1] == 0x0500000C


def test_shutdown_after_read_failure_frees_owned_object_without_replay(device, monkeypatch):
    controller, data = device
    controller.hold()

    def fail():
        raise RuntimeError("register read failed")

    monkeypatch.setattr(controller, "_read_locked", fail)
    with pytest.raises(RuntimeError, match="register read failed"):
        controller.close()
    assert commands(data)[-1] == 0x0500000C
    assert not controller._owned and not controller._uncertain
    assert controller.close() is None


@pytest.mark.parametrize("target", ["identity", "parent"])
def test_target_drift_blocks_writes_and_never_replays(device, target):
    controller, data = device
    controller.hold()
    data[target] = "changed" if target == "identity" else data[target] + 1
    for action in (controller.hold, controller.read, controller.close):
        with pytest.raises(live.BlockClockGatingError, match="changed"):
            action()
    assert commands(data) == [0x0500002A, 0xB0CC0301]
