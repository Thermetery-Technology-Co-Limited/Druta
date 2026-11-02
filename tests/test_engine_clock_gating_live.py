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
        "requests": {},
        "conflict_global": 0x63,
        "conflict_status": 1,
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
            mode = data["requests"].pop(packet[14], None)
            if mode == "disabled" and not data["stub"]:
                data["eligible"] |= data["selected"]
        else:
            packet[16] = 0
            if packet[14] == 0xB0CC0301:
                assert packet[18] in (1, 2)
                conflict = packet[18] == 1 and "enabled" in data["requests"].values()
                global_status = data["conflict_global"] if conflict else data["global"]
                selected_status = data["conflict_status"] if conflict else data["status"]
                packet[17] = global_status
                packet[19] = 0xFFFFFFFC | selected_status
                if not data["stub"] and not global_status and not selected_status:
                    data["requests"][packet[13]] = "enabled" if packet[18] == 2 else "disabled"
                    if packet[18] == 1:
                        data["eligible"] &= ~data["selected"]
            elif packet[14] == 0xB0CC0302:
                assert packet[17] == 3
                mode = data["requests"].pop(packet[13], None)
                if not data["stub"]:
                    if mode != "enabled":
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


@pytest.mark.parametrize("feature", ["IDLE", None])
def test_transport_accepts_only_explicitly_understood_named_features(feature):
    with pytest.raises(ValueError, match="Only named ELPG, ELCG, BLCG, and SLCG"):
        _ProfilerTransport(None, None, feature=feature)


def test_no_eligible_engine_refuses_before_any_write(device):
    controller, data = device
    data["eligible"] = 0
    assert controller.read()["support"]
    with pytest.raises(live.EngineClockGatingError, match="No eligible engine"):
        controller.hold()
    assert not data["writes"]


def test_enable_preserves_complete_64_bit_baseline_and_verifies_distinct_conflict_probe(device):
    controller, data = device
    before = controller.read()
    held = controller.hold_enabled()
    assert held["supported_engines"] == held["eligible_engines"] == before["supported_engines"]
    assert held["eligible_engines"] & (1 << 63)
    assert held["owned_mode"] == "enabled" and held["arbitration_verified"]
    assert held["observed_engines"] == [] and "not unique ownership" in held["verification_scope"]
    assert commands(data) == [0x0500002A, 0xB0CC0301, 0x0500002A, 0xB0CC0301, 0x0500000C]
    main, enable, probe, disable, probe_free = data["writes"]
    assert enable[18] == 2 and disable[18] == 1
    assert main[14] == enable[13] == controller._profiler.handle
    assert probe[14] == disable[13] == probe_free[14] != main[14]
    assert probe_free[14] not in main[12:14]
    after = controller.release()
    assert after["supported_engines"] == after["eligible_engines"] == before["eligible_engines"]
    assert after["owned_mode"] is None and not after["arbitration_verified"]
    assert data["writes"][-2][17] == 3 and data["writes"][-1][14] == main[14]
    assert not data["requests"] and controller.close() is None


@pytest.mark.parametrize("supported,eligible", [(0, 0), (1 << 63, 0), ((1 << 3) | (1 << 63), 1 << 3)])
def test_enable_requires_nonzero_complete_all_eligible_baseline_before_mutation(device, supported, eligible):
    controller, data = device
    data.update(supported=supported, eligible=eligible)
    with pytest.raises(live.EngineClockGatingError, match="unsupported|No eligible|supported==eligible"):
        controller.hold_enabled()
    assert not data["writes"]


@pytest.mark.parametrize("row", range(4))
def test_enable_refuses_each_unavailable_64_bit_chunk_without_mutation(device, row):
    controller, data = device
    data["unavailable"] = row
    with pytest.raises(live.EngineClockGatingError, match="unavailable or unsupported"):
        controller.hold_enabled()
    assert not data["writes"]


@pytest.mark.parametrize("global_status,selected_status", [(0, 0), (0, 1), (99, 0), (1, 1), (99, 2), (99, 3)])
def test_enable_requires_exact_feature_conflict_rejection(device, global_status, selected_status):
    controller, data = device
    baseline = data["eligible"]
    data.update(conflict_global=global_status, conflict_status=selected_status)
    with pytest.raises(live.EngineClockGatingError, match="ELCG opposing DISABLE.*exact arbitration rejection"):
        controller.hold_enabled()
    assert controller.read()["uncertain_outcome"] and not controller.read()["arbitration_verified"]
    assert controller._arbitration_probe is None
    controller.close()
    assert data["eligible"] == baseline and not data["requests"]


def test_enable_success_stub_cannot_qualify_from_unchanged_eligible_values(device):
    controller, data = device
    data["stub"] = True
    with pytest.raises(live.EngineClockGatingError, match="exact arbitration rejection"):
        controller.hold_enabled()
    assert not controller.read()["arbitration_verified"]
    controller.close()


@pytest.mark.parametrize("global_status,status", [(1, 0), (0, 1), (0, 2), (0, 3)])
def test_rejected_enable_does_not_start_an_arbitration_probe(device, global_status, status):
    controller, data = device
    data.update({"global": global_status, "status": status})
    with pytest.raises(live.EngineClockGatingError, match="global status|rejected|unsupported|failed"):
        controller.hold_enabled()
    assert commands(data) == [0x0500002A, 0xB0CC0301]
    assert controller._arbitration_probe is None
    controller.close()


def test_duplicate_enabled_only_rechecks_and_opposite_mode_requires_release(device):
    controller, data = device
    controller.hold_enabled()
    count = len(data["writes"])
    assert controller.hold_enabled()["arbitration_verified"]
    with pytest.raises(live.EngineClockGatingError, match="Release"):
        controller.hold()
    assert len(data["writes"]) == count
    controller.release()
    assert controller.hold()["owned_mode"] == "disabled"
    with pytest.raises(live.EngineClockGatingError, match="Release"):
        controller.hold_enabled()
    controller.release()


@pytest.mark.parametrize("change", ["ineligible_high_bit", "supported_high_bit", "unavailable_high_chunk"])
def test_duplicate_enabled_exposes_changed_complete_inventory_and_release_frees_only_own_object(device, change):
    controller, data = device
    controller.hold_enabled()
    if change == "ineligible_high_bit":
        data["eligible"] &= ~(1 << 63)
    elif change == "supported_high_bit":
        data["supported"] &= ~(1 << 63)
        data["eligible"] &= ~(1 << 63)
    else:
        data["unavailable"] = 3
    with pytest.raises(live.EngineClockGatingError, match="support or eligibility changed"):
        controller.hold_enabled()
    count = len(data["writes"])
    with pytest.raises(live.EngineClockGatingError, match="support or eligibility changed"):
        controller.release()
    assert len(data["writes"]) == count + 1 and commands(data)[-1] == 0x0500000C
    assert controller._profiler.handle is None and not controller.read()["owned_request"]


def test_enable_must_leave_all_supported_engines_eligible_before_probe(device, monkeypatch):
    controller, data = device
    request = controller._profiler.request

    def changed_request(*, enabled=False):
        request(enabled=enabled)
        data["eligible"] &= ~(1 << 63)

    monkeypatch.setattr(controller._profiler, "request", changed_request)
    with pytest.raises(live.EngineClockGatingError, match="support or eligibility changed"):
        controller.hold_enabled()
    assert controller._arbitration_probe is None
    with pytest.raises(live.EngineClockGatingError, match="support or eligibility changed"):
        controller.close()
    assert controller._profiler.handle is None


def test_arbitration_must_preserve_complete_eligibility_after_probe_free(device, monkeypatch):
    controller, data = device
    free = _ProfilerTransport.free

    def changed_free(transport):
        free(transport)
        if transport is not controller._profiler:
            data["eligible"] &= ~(1 << 63)

    monkeypatch.setattr(_ProfilerTransport, "free", changed_free)
    with pytest.raises(live.EngineClockGatingError, match="support or eligibility changed"):
        controller.hold_enabled()
    assert not controller.read()["arbitration_verified"]
    with pytest.raises(live.EngineClockGatingError, match="support or eligibility changed"):
        controller.close()


def test_probe_handle_remains_reachable_when_free_fails_and_explicit_close_can_finish(device, monkeypatch):
    controller, data = device
    free = _ProfilerTransport.free
    failures = 2

    def failed_free(transport):
        nonlocal failures
        if transport is not controller._profiler and failures:
            failures -= 1
            raise live.EngineClockGatingError("probe free failed")
        free(transport)

    monkeypatch.setattr(_ProfilerTransport, "free", failed_free)
    with pytest.raises(live.EngineClockGatingError, match="probe free failed"):
        controller.hold_enabled()
    probe = controller._arbitration_probe
    with pytest.raises(live.EngineClockGatingError, match="probe free failed"):
        controller.close()
    assert controller._profiler.handle is None and probe.handle is not None
    assert controller.read()["uncertain_outcome"]
    controller.close()
    assert probe.handle is None and controller._arbitration_probe is None
    assert not controller.read()["uncertain_outcome"] and not data["requests"]
