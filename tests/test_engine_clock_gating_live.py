"""Offline ELCG packet, ownership, and independent engine-mask checks."""

import struct
import threading
from types import SimpleNamespace

import pytest

from druta import engine_clock_gating_live as live
from druta.gr_gating_live import _ProfilerTransport


@pytest.fixture
def device(monkeypatch):
    supported = (1 << 3) | (1 << 11) | (1 << 35) | (1 << 63)
    data = {
        "identity": "paired",
        "parent": 0x5678,
        "supported": supported,
        "eligible": supported,
        "selected": (1 << 3) | (1 << 35),
        "writes": [],
        "gets": 0,
        "stub": False,
        "global": 0,
        "status": 0,
        "unavailable": None,
        "malformed": None,
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
                assert packet[18] == 1
                packet[17] = data["global"]
                packet[19] = 0xFFFFFFFC | data["status"]
                if not data["stub"] and not data["global"] and not data["status"]:
                    data["eligible"] &= ~data["selected"]
            elif packet[14] == 0xB0CC0302:
                assert packet[17] == 3
                if not data["stub"]:
                    data["eligible"] |= data["selected"]
        return 0

    def get(reader, identity, command, size, request):
        assert identity == data["identity"] and command == 0x2080A801 and size == 0x404
        data["gets"] += 1
        assert len(request) == 0x404 and struct.unpack_from("<I", request)[0] == 4
        raw = bytearray(request)
        for row, (attribute, chunk) in enumerate(((1, 0), (1, 1), (0x801, 0), (0x801, 1))):
            at = 4 + row * 16
            assert struct.unpack_from("<HHHHII", request, at) == (10, 0, attribute, 0, 0, chunk)
            value = data["supported"] if attribute == 1 else data["eligible"]
            struct.pack_into("<HI", raw, at + 6, int(data["unavailable"] != row), (value >> (32 * chunk)) & 0xFFFFFFFF)
        if data["malformed"] == "flags":
            struct.pack_into("<H", raw, 10, 2)
        elif data["malformed"] == "chunk":
            struct.pack_into("<I", raw, 16, 1)
        elif data["malformed"] == "opaque":
            raw[-1] = 1
        elif data["malformed"] == "size":
            raw.pop()
        return bytes(raw)

    gpu._legacy_clk_escape = escape
    monkeypatch.setattr(live.lpwr, "_get", get)
    return live.EngineClockGatingController(gpu), data


def commands(data):
    return [words[14] if words[4] == 0x0500002B else words[4] for words in data["writes"]]


def test_full_64_bit_masks_and_alternate_engines_with_balanced_session(device):
    controller, data = device
    snapshot = controller.read()
    assert snapshot["supported_engines"] == data["supported"] > (1 << 63)
    assert snapshot["eligible_engines"] == data["supported"]
    assert snapshot["support"] and not data["writes"]
    held = controller.hold()
    assert held["observed_engines"] == [3, 35]
    assert held["owned_request"] and not held["uncertain_outcome"]
    controller.hold()
    assert commands(data) == [0x0500002A, 0xB0CC0301]
    # A different reason disables another engine while our request is active.
    data["eligible"] &= ~(1 << 63)
    result = controller.release()
    assert not result["owned_request"] and not result["uncertain_outcome"]
    assert result["eligible_engines"] == data["supported"] & ~(1 << 63)
    assert commands(data) == [0x0500002A, 0xB0CC0301, 0xB0CC0302, 0x0500000C]
    assert controller.close() is None
    allocation, request, release, free = data["writes"]
    assert allocation[14] == request[13] == release[13] == free[14]
    assert allocation[12:14] == free[12:14] == (0x1234, 0x5678)


def test_noop_stub_is_never_verified_and_explicit_cleanup_frees(device):
    controller, data = device
    data["stub"] = True
    with pytest.raises(live.EngineClockGatingError, match="no independent"):
        controller.hold()
    assert controller.read()["uncertain_outcome"]
    with pytest.raises(live.EngineClockGatingError, match="uncertain"):
        controller.hold()
    assert not controller.release()["owned_request"]
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]


@pytest.mark.parametrize("row", range(4))
def test_partial_unavailable_metadata_is_honest_and_cannot_hold(device, row):
    controller, data = device
    data["unavailable"] = row
    state = controller.read()
    assert not state["support"] and not state["available"]
    assert state["supported_engines" if row < 2 else "eligible_engines"] is None
    with pytest.raises(live.EngineClockGatingError, match="unavailable or unsupported"):
        controller.hold()
    assert not data["writes"]


@pytest.mark.parametrize("malformed", ["flags", "chunk", "opaque", "size", "subset"])
def test_malformed_attribute_packets_never_enable_control(device, malformed):
    controller, data = device
    data["malformed"] = malformed
    if malformed == "subset":
        data["eligible"] |= 1 << 54
    with pytest.raises(live.EngineClockGatingError):
        controller.hold()
    assert not data["writes"]


@pytest.mark.parametrize(
    ("global_status", "selected_status", "message"),
    [(1, 0, "global status"), (0, 1, "rejected"), (0, 2, "unsupported"), (0, 3, "failed")],
)
def test_global_and_selected_status_are_required(device, global_status, selected_status, message):
    controller, data = device
    data.update({"global": global_status, "status": selected_status})
    with pytest.raises(live.EngineClockGatingError, match=message):
        controller.hold()
    controller.close()
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]


def test_zero_support_refuses_before_allocation(device):
    controller, data = device
    data.update(supported=0, eligible=0)
    assert not controller.read()["support"]
    with pytest.raises(live.EngineClockGatingError, match="unsupported"):
        controller.hold()
    assert not data["writes"]


@pytest.mark.parametrize("change", ["support", "adds_eligible"])
def test_hold_requires_same_support_and_only_cleared_eligible_bits(device, monkeypatch, change):
    controller, data = device
    data["eligible"] &= ~(1 << 11)
    request = controller._profiler.request

    def changed_request():
        request()
        if change == "support":
            data["supported"] |= 1 << 40
        else:
            data["eligible"] |= 1 << 11

    monkeypatch.setattr(controller._profiler, "request", changed_request)
    with pytest.raises(live.EngineClockGatingError, match="support changed|added eligible"):
        controller.hold()
    with pytest.raises(live.EngineClockGatingError, match="exact expected"):
        controller.close()
    assert controller._profiler.handle is None


def test_repeated_hold_checks_observed_request_remains_visible(device):
    controller, data = device
    controller.hold()
    data["eligible"] |= 1 << 35
    with pytest.raises(live.EngineClockGatingError, match="no longer independently visible"):
        controller.hold()
    assert commands(data) == [0x0500002A, 0xB0CC0301]


def test_release_readback_mismatch_still_frees_and_reports_error_once(device):
    controller, data = device
    controller.hold()
    data["stub"] = True
    with pytest.raises(live.EngineClockGatingError, match="exact expected"):
        controller.release()
    state = controller.read()
    assert not state["owned_request"] and not state["uncertain_outcome"]
    assert state["last_verification_error"]
    assert commands(data)[-1] == 0x0500000C


def test_cleanup_after_read_failure_frees_only_owned_object(device, monkeypatch):
    controller, data = device
    controller.hold()

    def fail():
        raise RuntimeError("read failed")

    monkeypatch.setattr(controller, "_read_locked", fail)
    with pytest.raises(RuntimeError, match="read failed"):
        controller.close()
    assert commands(data)[-1] == 0x0500000C
    assert not controller._owned and not controller._uncertain
    assert controller.close() is None


@pytest.mark.parametrize("target", ["identity", "parent"])
def test_target_drift_blocks_mutations_and_never_reapplies(device, target):
    controller, data = device
    controller.hold()
    data[target] = "changed" if target == "identity" else data[target] + 1
    for action in (controller.hold, controller.read, controller.close):
        with pytest.raises(live.EngineClockGatingError, match="changed"):
            action()
    assert commands(data) == [0x0500002A, 0xB0CC0301]


def test_engine_bitmap_must_be_stable_before_control(device, monkeypatch):
    controller, data = device
    original = controller._once
    calls = []

    def unstable():
        result = original()
        calls.append(None)
        if len(calls) % 2:
            result["eligible_engines"] &= ~(1 << 11)
        return result

    monkeypatch.setattr(controller, "_once", unstable)
    with pytest.raises(live.EngineClockGatingError, match="not stable"):
        controller.hold()
    assert len(calls) == 2 and not data["writes"]


@pytest.mark.parametrize("feature", ["SLCG", "IDLE", None])
def test_transport_accepts_only_explicitly_understood_named_features(feature):
    with pytest.raises(ValueError, match="Only named ELPG, ELCG, and BLCG"):
        _ProfilerTransport(None, None, feature=feature)


def test_no_eligible_engine_refuses_before_any_write(device):
    controller, data = device
    data["eligible"] = 0
    assert controller.read()["support"]
    with pytest.raises(live.EngineClockGatingError, match="No eligible engine"):
        controller.hold()
    assert not data["writes"]
