"""Offline transport and observable session behavior; never loads a GPU."""

import struct
import threading
from types import SimpleNamespace

import pytest

from druta import gr_gating_live as live


@pytest.fixture
def device(monkeypatch):
    data = {
        "identity": "paired",
        "root": 0x1234,
        "parent": 0x5678,
        "masks": {2: [0x20, 0x80], 7: [0x40, 0x100], 19: [0x81, 0x200]},
        "selected": (2, 7),
        "writes": [],
        "stub": False,
        "global_status": 0,
        "selected_status": 0,
        "corrupt": None,
    }
    header = [
        0x4E564441,
        0x10002,
        72,
        0x4E562A2A,
        0x0500002B,
        0,
        91,
        92,
        93,
        94,
        95,
        1,
        data["root"],
        data["parent"],
        0x2080A612,
        4,
        0,
    ]
    fields = {key: i for i, key in enumerate(("hAdapter", "hDevice", "Type", "Flags", "hContext"))}
    gpu = SimpleNamespace(_lock=threading.RLock())

    class Reader:
        def __init__(self, gpu):
            self.gpu = gpu

        def _transport(self):
            result = header[:]
            result[12], result[13] = data["root"], data["parent"]
            return result, dict(fields)

    monkeypatch.setattr(live, "PerfDiagnosticsReader", Reader)
    monkeypatch.setattr(live.clock, "_binding", lambda reader: data["identity"])

    def escape(packet, actual_fields):
        assert actual_fields == fields
        words = tuple(packet)
        opcode = packet[4]
        data["writes"].append(words)
        packet[5] = packet[11] = 1
        if opcode == 0x0500002A:
            assert len(packet) == 22 and packet[15] == 0xB2CC
            packet[19] = 0
        elif opcode == 0x0500000C:
            assert len(packet) == 16
            packet[15] = 0
        else:
            assert opcode == 0x0500002B
            packet[16] = 0
            if packet[14] == 0xB0CC0301:
                assert len(packet) == 20 and packet[15] == 12 and packet[18] == 0x40
                packet[17] = data["global_status"]
                packet[19] = 0xFFFFFC00 | (data["selected_status"] << 6)
                if not data["stub"] and not data["global_status"] and not data["selected_status"]:
                    for index in data["selected"]:
                        data["masks"][index][0] |= 1
            elif packet[14] == 0xB0CC0302:
                assert len(packet) == 18 and packet[15] == 4 and packet[17] == 0xC0
                if not data["stub"]:
                    for index in data["selected"]:
                        data["masks"][index][0] &= ~1
        if data.get("corrupt") is not None:
            index, value = data.pop("corrupt")
            packet[index] = value
        return 0

    gpu._legacy_clk_escape = escape

    def get(reader, identity, command, size, request):
        assert identity == data["identity"]
        assert command == 0x2080A801 and size == 0x404
        raw = bytearray(request)
        count = struct.unpack_from("<I", request)[0]
        for number in range(count):
            at = 4 + 16 * number
            _, index, attr = struct.unpack_from("<HHH", request, at)
            if attr == 0x39:
                value = int(index in data["masks"])
            else:
                assert index in data["masks"]
                value = 1 if attr == 0x37 else data["masks"][index][attr - 0x48]
            struct.pack_into("<HI", raw, at + 6, 1, value)
        return bytes(raw)

    monkeypatch.setattr(live.lpwr, "_get", get)
    return live.GrGatingController(gpu), data


def commands(data):
    return [words[14] if words[4] == 0x0500002B else words[4] for words in data["writes"]]


def test_balanced_session_transport_preserves_peers_and_alternate_identities(device):
    controller, data = device
    initial = controller.read()
    assert initial["support"] and data["writes"] == []
    held = controller.hold()
    assert held["owned_request"] and not held["uncertain_outcome"]
    assert held["observed_controllers"] == ["2", "7"]
    assert held["controllers"]["19"]["reason_masks"] == {0x48: 0x81, 0x49: 0x200}
    assert held["controllers"]["2"]["enabled_predicate"] is True
    controller.hold()
    # New unrelated reasons while held must survive release.
    data["masks"][2][0] |= 0x400
    data["masks"][7][1] |= 0x800
    result = controller.release()
    assert not result["owned_request"] and not result["uncertain_outcome"]
    assert data["masks"][2] == [0x420, 0x80]
    assert data["masks"][7] == [0x40, 0x900]
    assert commands(data) == [0x0500002A, 0xB0CC0301, 0xB0CC0302, 0x0500000C]
    allocation, request, release, free = data["writes"]
    assert allocation[12:14] == free[12:14] == (data["root"], data["parent"])
    assert allocation[14] == request[13] == release[13] == free[14]
    assert allocation[14] not in (data["root"], data["parent"])
    assert allocation[16:] == (8, 0x77FEF81E, 0, 0xFFFFFFFF, 0, 0)
    assert allocation[6:11] == request[6:11] == release[6:11] == free[6:11] == (91, 92, 93, 94, 95)
    controller.close()
    assert len(data["writes"]) == 4


def test_successful_windows_stub_never_verifies_and_explicit_close_frees(device):
    controller, data = device
    data["stub"] = True
    with pytest.raises(live.GrGatingError, match="no independent"):
        controller.hold()
    assert controller.read()["uncertain_outcome"]
    with pytest.raises(live.GrGatingError, match="uncertain"):
        controller.hold()
    controller.close()
    assert commands(data) == [0x0500002A, 0xB0CC0301, 0xB0CC0302, 0x0500000C]


@pytest.mark.parametrize(
    ("global_status", "selected_status", "message"),
    [(1, 0, "global status"), (0, 1, "rejected"), (0, 2, "unsupported"), (0, 3, "failed")],
)
def test_request_status_failure_is_not_capability_success(device, global_status, selected_status, message):
    controller, data = device
    data.update(global_status=global_status, selected_status=selected_status)
    with pytest.raises(live.GrGatingError, match=message):
        controller.hold()
    assert controller.read()["uncertain_outcome"]
    controller.close()
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]


@pytest.mark.parametrize("target", ["identity", "root", "parent"])
def test_target_drift_never_reapplies_or_frees_into_new_target(device, target):
    controller, data = device
    controller.hold()
    data[target] = "new" if target == "identity" else data[target] + 1
    for action in (controller.read, controller.hold, controller.release, controller.close):
        with pytest.raises(live.GrGatingError, match="changed"):
            action()
    assert commands(data) == [0x0500002A, 0xB0CC0301]


def test_allocated_object_owned_before_geometry_validation_and_only_owned_object_freed(device):
    controller, data = device
    data["corrupt"] = (6, 999)
    with pytest.raises(live.GrGatingError, match="geometry"):
        controller.hold()
    assert controller._profiler.handle is not None
    controller.close()
    assert commands(data) == [0x0500002A, 0x0500000C]


def test_changed_peer_rejected_but_explicit_close_still_frees_owned_object(device, monkeypatch):
    controller, data = device
    request = controller._profiler.request

    def change_peer():
        request()
        data["masks"][19][1] |= 4

    monkeypatch.setattr(controller._profiler, "request", change_peer)
    with pytest.raises(live.GrGatingError, match="unrelated reason"):
        controller.hold()
    # No observed set was verified, so cleanup cannot claim exact inversion.
    with pytest.raises(live.GrGatingError, match="exact expected"):
        controller.close()
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]
    assert controller._profiler.handle is None
    state = controller.read()
    assert not state["uncertain_outcome"] and not state["owned_request"]
    assert state["last_verification_error"]


def test_partial_witness_does_not_reject_unrelated_existing_reason_zero(device):
    controller, data = device
    data["masks"][2][0] |= 1
    held = controller.hold()
    assert held["observed_controllers"] == ["7"]
    # Simulate another client's counted reason remaining on controller 2.
    original_escape = controller.gpu._legacy_clk_escape

    def escape(packet, fields):
        result = original_escape(packet, fields)
        if packet[4] == 0x0500002B and packet[14] == 0xB0CC0302:
            data["masks"][2][0] |= 1
        return result

    controller.gpu._legacy_clk_escape = escape
    assert not controller.release()["uncertain_outcome"]


def test_release_mismatch_reports_uncertainty_and_frees_our_object(device):
    controller, data = device
    controller.hold()
    data["stub"] = True
    with pytest.raises(live.GrGatingError, match="exact expected"):
        controller.release()
    assert data["writes"][-1][4] == 0x0500000C
    state = controller.read()
    assert not state["uncertain_outcome"] and not state["owned_request"]
    assert state["last_verification_error"]


def test_close_frees_owned_object_after_read_failure_and_preserves_error(device, monkeypatch):
    controller, data = device
    controller.hold()

    def failed_read():
        raise RuntimeError("predicate read failed")

    monkeypatch.setattr(controller, "_read_locked", failed_read)
    with pytest.raises(RuntimeError, match="predicate read failed"):
        controller.close()
    assert commands(data)[-1] == 0x0500000C
    assert controller._profiler.handle is None
    assert not controller._profiler.requested
    assert not controller._owned and not controller._uncertain
    assert controller.close() is None


def test_close_without_object_performs_no_getter_or_transport_call(device, monkeypatch):
    controller, data = device

    def forbidden():
        raise AssertionError("close without owned state must not call GPU")

    monkeypatch.setattr(controller, "_read_locked", forbidden)
    monkeypatch.setattr(controller._profiler, "check", forbidden)
    assert controller.close() is None
    assert not data["writes"]


@pytest.mark.parametrize("failure", ["ntstatus", "completion", "rmstatus"])
def test_allocation_failure_never_frees_borrowed_objects(device, failure):
    controller, data = device
    escape = controller.gpu._legacy_clk_escape

    def fail(packet, fields):
        result = escape(packet, fields)
        if failure == "completion":
            packet[11] = 0
        elif failure == "rmstatus":
            packet[19] = 0x56
        return 0xC0000001 if failure == "ntstatus" else result

    controller.gpu._legacy_clk_escape = fail
    with pytest.raises(live.GrGatingError, match="operation failed"):
        controller.hold()
    assert controller._profiler.handle is None
    controller.close()
    assert commands(data) == [0x0500002A]


def test_dynamic_enabled_predicate_does_not_gate_stable_masks_or_requests(device, monkeypatch):
    controller, data = device
    original = controller._once
    calls = []

    def alternating_predicate():
        state = original()
        calls.append(None)
        state["controllers"]["19"]["enabled_predicate"] = bool(len(calls) % 2)
        return state

    monkeypatch.setattr(controller, "_once", alternating_predicate)
    snapshot = controller.read()
    assert snapshot["controllers"]["19"]["enabled_predicate"] is False
    assert snapshot["enabled_predicate_scope"] == "latest observation; stability not required"
    assert controller.hold()["owned_request"]
    assert not controller.release()["owned_request"]
    assert commands(data) == [0x0500002A, 0xB0CC0301, 0xB0CC0302, 0x0500000C]


def test_changing_reason_mask_still_fails_bounded_stability_before_write(device, monkeypatch):
    controller, data = device
    original = controller._once
    calls = []

    def changing_mask():
        state = original()
        calls.append(None)
        state["controllers"]["19"]["reason_masks"][0x49] |= len(calls) << 12
        return state

    monkeypatch.setattr(controller, "_once", changing_mask)
    with pytest.raises(live.GrGatingError, match="reason-mask readbacks were not stable"):
        controller.hold()
    assert len(calls) == 2
    assert not data["writes"]
    assert not controller._owned and not controller._uncertain
