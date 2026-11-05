"""Offline contract, identity, concurrency and compensating-write coverage."""

import json
from types import SimpleNamespace

import pytest

from druta import voltage_request_gate as gate


class Lock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True

    def __exit__(self, *args):
        self.held = False


class FakeGpu:
    def __init__(self, *, enabled=True, architecture=8, slot="0000:72:1d.0", handle=928):
        self._lock = Lock()
        self.enabled = enabled
        self.architecture = architecture
        self.pci_slot = slot
        self.pairing_error = ""
        self.nvapi = SimpleNamespace(
            ok=True, gpu=handle,
            selected={"slot": slot, "handle": handle, "devid": 0xABCD, "subsys": 0x98765432},
        )
        self.nvml = SimpleNamespace(selected={"slot": slot, "devid": 0xABCD, "subsys": 0x98765432})
        self._current_limit_transport = None
        self._current_limit_layout_choice = "cached-choice"
        self.calls = []
        self.events = []
        self.transport_reads = 0
        self.captures = 0
        self.capture_events = {}

    def slot(self):
        return self.pci_slot

    def arch(self):
        return self.architecture

    def change_target(self):
        self.pci_slot = "0000:01:02.0"
        self.nvapi.selected["slot"] = self.pci_slot
        self.nvml.selected["slot"] = self.pci_slot

    def _current_limit_transport_layouts(self):
        assert self._lock.held
        self.transport_reads += 1
        effect = self.capture_events.get(self.transport_reads)
        if effect:
            effect(self)
        if self._current_limit_transport is None:
            self.captures += 1
            header = list(range(17))
            for index, value in gate._ENVELOPE.items():
                header[index] = value
            header[5] = header[11] = 0
            header[2], header[14], header[15], header[16] = 54420, 0x2080A612, 54352, 0
            self._current_limit_transport = (
                header, {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0}
            )
        return ({},)

    def _legacy_clk_escape(self, packet, fields):
        assert self._lock.held
        command = int(packet[14])
        request = tuple(packet[17:])
        assert packet[2] == len(packet) * 4
        assert packet[5] == packet[11] == 0
        assert (command, packet[15]) in ((gate._GET, 4), (gate._SET, 8))
        assert request == (gate._SENTINEL,) if command == gate._GET else request in ((0, 0), (0, 1))
        self.calls.append((command, request))
        event = self.events.pop(0) if self.events else {}
        if "state_before" in event:
            self.enabled = event["state_before"]
        ntstatus = event.get("ntstatus", 0)
        rm_status = event.get("rm_status", 0)
        packet[5] = event.get("context_flag", 1)
        packet[11] = event.get("escape_status", 1)
        packet[16] = rm_status
        if event.get("apply", ntstatus == 0 and rm_status == 0):
            packet[17] = int(self.enabled)
            if command == gate._SET:
                self.enabled = bool(request[1])
        if "payload" in event:
            for index, word in enumerate(event["payload"], 17):
                packet[index] = word
        if "header" in event:
            index, value = event["header"]
            packet[index] = value
        if event.get("change_target"):
            self.change_target()
        if "raise" in event:
            raise event["raise"]
        return ntstatus

    def sets(self):
        return [request for command, request in self.calls if command == gate._SET]


def error(call, kind):
    with pytest.raises(gate.VoltageRequestGateError) as caught:
        call()
    assert caught.value.kind == kind
    return caught.value


@pytest.mark.parametrize("architecture,slot,handle", [
    (4, "0000:02:00.0", 9991), (7, "0000:aa:1f.7", 781), (10, "0000:72:1d.0", 928),
])
@pytest.mark.parametrize("enabled", [False, True])
def test_read_uses_two_fresh_gets_on_alternate_identities(architecture, slot, handle, enabled):
    gpu = FakeGpu(enabled=enabled, architecture=architecture, slot=slot, handle=handle)
    snapshot = gate.VoltageRequestGate(gpu).read()
    assert snapshot == {
        "enabled": enabled,
        "identity": {"slot": slot, "architecture": architecture, "device_id": 0xABCD, "subsystem_id": 0x98765432},
    }
    assert gpu.calls == [(gate._GET, (gate._SENTINEL,))] * 2
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("architecture", [0, 1, 2, 3, 99])
def test_unsupported_architecture_is_not_false(architecture):
    gpu = FakeGpu(architecture=architecture)
    exc = error(gate.VoltageRequestGate(gpu).read, "unsupported")
    assert exc.rollback is None and not gpu.calls


@pytest.mark.parametrize("key,value", [("devid", None), ("devid", -1), ("devid", True),
                                      ("devid", 65536), ("subsys", None), ("subsys", 2**32)])
def test_incomplete_or_invalid_identity_is_not_used(key, value):
    gpu = FakeGpu()
    gpu.nvapi.selected[key] = value
    gpu.nvml.selected[key] = value
    error(gate.VoltageRequestGate(gpu).read, "identity")
    assert not gpu.calls


def test_pairing_failure_stops_before_transport():
    gpu = FakeGpu()
    gpu.nvml.selected["slot"] = "0000:05:00.0"
    error(gate.VoltageRequestGate(gpu).read, "identity")
    assert not gpu.calls and gpu.transport_reads == 0


def test_transient_get_discards_transport_and_retries_pair_once():
    gpu = FakeGpu()
    gpu.events = [{"ntstatus": -5}, {}, {}]
    assert gate.VoltageRequestGate(gpu).read()["enabled"]
    assert len(gpu.calls) == 3 and gpu.captures == 2
    assert gpu._current_limit_layout_choice is None


def test_transient_second_get_retries_both_fresh_reads():
    gpu = FakeGpu()
    gpu.events = [{}, {"rm_status": 0x55}, {}, {}]
    assert gate.VoltageRequestGate(gpu).read()["enabled"]
    assert len(gpu.calls) == 4 and gpu.captures == 2


@pytest.mark.parametrize("event", [{"ntstatus": None}, {"ntstatus": False}, {"ntstatus": 0.0},
                                    {"ntstatus": -1}, {"rm_status": 0x55},
                                    {"raise": RuntimeError("interrupted")}])
def test_transient_retries_are_bounded(event):
    gpu = FakeGpu()
    gpu.events = [event, event, {}]
    error(gate.VoltageRequestGate(gpu).read, "transient")
    assert len(gpu.calls) == 2 and gpu.captures == 2
    assert gpu._current_limit_transport is None


@pytest.mark.parametrize("rm_status", [0x1F, 0x56])
def test_capability_rejection_does_not_retry_or_return_false(rm_status):
    gpu = FakeGpu()
    gpu.events = [{"rm_status": rm_status}]
    exc = error(gate.VoltageRequestGate(gpu).read, "unsupported")
    assert exc.status == rm_status and len(gpu.calls) == 1


@pytest.mark.parametrize("value", [2, 0xFFFFFFFF, gate._SENTINEL])
def test_nonboolean_get_response_is_rejected(value):
    gpu = FakeGpu()
    gpu.events = [{"payload": [value]}]
    error(gate.VoltageRequestGate(gpu).read, "malformed")
    assert len(gpu.calls) == 1


def test_success_without_overwriting_get_output_is_rejected():
    gpu = FakeGpu(enabled=False)
    gpu.events = [{"apply": False}]
    error(gate.VoltageRequestGate(gpu).read, "malformed")
    assert not gpu.sets()


@pytest.mark.parametrize("index", [index for index in range(16) if index != 11])
def test_every_returned_header_word_is_checked(index):
    gpu = FakeGpu()
    gpu.events = [{"header": (index, 0xFFFFFFFF)}]
    error(gate.VoltageRequestGate(gpu).read, "malformed")
    assert len(gpu.calls) == 1


@pytest.mark.parametrize("context_flag", [0, 1])
def test_understood_envelope_output_fields_are_validated(context_flag):
    gpu = FakeGpu()
    gpu.events = [{"context_flag": context_flag}, {"context_flag": context_flag}]
    assert gate.VoltageRequestGate(gpu).read()["enabled"]
    assert len(gpu.calls) == 2


@pytest.mark.parametrize("escape_status", [0, 2, 0xFFFFFFF4, 0xFFFFFFFF])
def test_escape_failure_cannot_be_mistaken_for_rm_capability_or_valid_payload(escape_status):
    gpu = FakeGpu()
    event = {"escape_status": escape_status, "rm_status": 0x56, "payload": [0]}
    gpu.events = [event, event]
    exc = error(gate.VoltageRequestGate(gpu).read, "transient")
    assert exc.status == escape_status and "NVDA" in str(exc)
    assert len(gpu.calls) == 2 and not gpu.sets()


@pytest.mark.parametrize("index", gate._ENVELOPE)
def test_unknown_captured_envelope_stops_before_dispatch(index):
    gpu = FakeGpu()
    # Fill the fake capture without a hardware call, then corrupt its schema.
    with gpu._lock:
        gpu._current_limit_transport_layouts()
    gpu._current_limit_transport[0][index] ^= 0x80
    error(gate.VoltageRequestGate(gpu).read, "malformed")
    assert not gpu.calls


def test_cached_envelope_outputs_are_cleared_for_every_fresh_call():
    gpu = FakeGpu()
    with gpu._lock:
        gpu._current_limit_transport_layouts()
    gpu._current_limit_transport[0][5] = 1
    gpu._current_limit_transport[0][11] = 1
    # FakeGpu asserts both output fields start at zero for every call.
    assert gate.VoltageRequestGate(gpu).set_enabled(False)["after"] is False
    assert len(gpu.calls) == 5


def test_failed_set_escape_status_triggers_one_verified_compensation():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"escape_status": 0xFFFFFFF4}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "transient")
    assert exc.rollback["verified"] and exc.rollback["target"] is True
    assert gpu.sets() == [(0, 0), (0, 1)] and gpu.enabled


def test_unstable_get_pair_is_a_distinct_failure():
    gpu = FakeGpu()
    gpu.events = [{}, {"state_before": False}]
    error(gate.VoltageRequestGate(gpu).read, "unstable")
    assert len(gpu.calls) == 2


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("requested", [False, True])
def test_toggle_and_identity_writes_are_dispatched_and_verified(enabled, requested):
    gpu = FakeGpu(enabled=enabled)
    result = gate.VoltageRequestGate(gpu).set_enabled(requested, expected=enabled)
    assert result["before"] is enabled
    assert result["after"] is result["readback"]["enabled"] is requested
    assert result["atomic_previous"] is enabled
    assert gpu.sets() == [(0, int(requested))]
    assert [command for command, _ in gpu.calls] == [gate._GET, gate._GET, gate._SET, gate._GET, gate._GET]
    assert gpu.enabled is requested
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("value", [None, 0, 1, 2, "false", [], object()])
def test_only_real_booleans_are_accepted(value):
    gpu = FakeGpu()
    with pytest.raises(TypeError):
        gate.VoltageRequestGate(gpu).set_enabled(value)
    assert not gpu.calls


@pytest.mark.parametrize("value", [0, 1, "true", [], object()])
def test_expected_must_be_boolean_or_none(value):
    gpu = FakeGpu()
    with pytest.raises(TypeError):
        gate.VoltageRequestGate(gpu).set_enabled(False, expected=value)
    assert not gpu.calls


def test_stale_expected_state_does_not_write():
    gpu = FakeGpu(enabled=False)
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(True, expected=True), "stale")
    assert exc.rollback is None and not gpu.sets()


def test_identity_change_during_transport_before_set_stops_dispatch():
    gpu = FakeGpu()
    gpu.capture_events[2] = lambda target: target.change_target()
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "identity")
    assert exc.rollback is None and not gpu.sets()


def test_reader_never_rebinds_after_a_successful_read():
    gpu = FakeGpu()
    reader = gate.VoltageRequestGate(gpu)
    reader.read()
    gpu.change_target()
    error(reader.read, "identity")
    assert len(gpu.calls) == 2


@pytest.mark.parametrize("event_index", [2, 3])
def test_identity_change_after_write_never_rolls_back_another_gpu(event_index):
    gpu = FakeGpu()
    gpu.events = [{}] * event_index + [{"change_target": True}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "identity")
    assert gpu.sets() == [(0, 0)]
    assert exc.rollback["attempted"] is False and exc.rollback["verified"] is False
    assert "binding changed" in exc.rollback["error"]
    assert gpu.enabled is False


@pytest.mark.parametrize("failure", [
    {"ntstatus": -5, "apply": True}, {"rm_status": 0x55, "apply": True},
    {"raise": RuntimeError("lost response"), "apply": True},
])
def test_failed_set_is_not_retried_and_previous_state_is_restored(failure):
    gpu = FakeGpu()
    gpu.events = [{}, {}, failure]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "transient")
    assert gpu.sets() == [(0, 0), (0, 1)]
    assert gpu.enabled is True
    assert exc.rollback["attempted"] and exc.rollback["verified"]
    assert exc.rollback["target"] is True
    assert exc.rollback["readback"]["enabled"] is True
    assert "restored and verified" in str(exc)


@pytest.mark.parametrize("header_index", [0, 2, 14, 15])
def test_malformed_set_header_triggers_verified_rollback(header_index):
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"header": (header_index, 0xFFFFFFFF)}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "malformed")
    assert exc.rollback["verified"] and gpu.enabled is True


@pytest.mark.parametrize("payload", [[2, 0], [1, 1]])
def test_malformed_set_payload_triggers_verified_rollback(payload):
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"payload": payload}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "malformed")
    assert exc.rollback["verified"] and gpu.enabled is True


def test_failed_readback_restores_previous_state_after_bounded_get_recovery():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {}, {"ntstatus": -1}, {"ntstatus": -1}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "transient")
    assert exc.rollback["verified"] and gpu.enabled is True
    assert gpu.sets() == [(0, 0), (0, 1)]


def test_matching_but_wrong_postwrite_reads_trigger_rollback():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"apply": False, "payload": [1, 0]}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "readback")
    assert exc.rollback["verified"] and gpu.enabled is True


@pytest.mark.parametrize("initial", [False, True])
def test_concurrent_previous_state_is_preserved_instead_of_older_pre_read(initial):
    gpu = FakeGpu(enabled=initial)
    gpu.events = [{}, {}, {"state_before": not initial}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(not initial), "stale")
    assert exc.rollback["target"] is (not initial)
    assert exc.rollback["verified"] and gpu.enabled is (not initial)
    assert gpu.sets() == [(0, int(not initial)), (0, int(not initial))]


def test_rollback_set_failure_is_explicit_and_not_retried():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"ntstatus": -5, "apply": True}, {"ntstatus": -6}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "transient")
    assert exc.rollback["attempted"] and not exc.rollback["verified"]
    assert "-6" in exc.rollback["error"] and "not verified" in str(exc)
    assert gpu.sets() == [(0, 0), (0, 1)] and gpu.enabled is False


def test_rollback_read_failure_does_not_claim_restoration_verified():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"ntstatus": -5, "apply": True}, {}, {"ntstatus": -6}, {"ntstatus": -6}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "transient")
    assert exc.rollback["attempted"] and not exc.rollback["verified"]
    assert exc.rollback["readback"] is None
    assert gpu.enabled is True and len(gpu.sets()) == 2


def test_interruption_after_dispatch_restores_then_reraises_cancellation():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"raise": KeyboardInterrupt(), "apply": True}]
    with pytest.raises(KeyboardInterrupt) as caught:
        gate.VoltageRequestGate(gpu).set_enabled(False)
    assert caught.value.rollback["verified"] and gpu.enabled is True
    assert gpu.sets() == [(0, 0), (0, 1)]


def test_transient_transport_capture_is_retried_without_rebinding():
    gpu = FakeGpu()
    def fail(target):
        raise RuntimeError("capture interrupted")
    gpu.capture_events[1] = fail
    assert gate.VoltageRequestGate(gpu).read()["enabled"]
    assert gpu.transport_reads == 2 and len(gpu.calls) == 2


def test_corrupt_captured_transport_is_not_dispatched():
    gpu = FakeGpu()
    gpu._current_limit_transport = ([0] * 17, {})
    error(gate.VoltageRequestGate(gpu).read, "unsupported")
    assert not gpu.calls


def test_invalid_escape_field_encoding_is_rejected():
    gpu = FakeGpu()
    def corrupt(target):
        header = [0] * 17
        header[2], header[14], header[15] = 54420, 0x2080A612, 54352
        target._current_limit_transport = (
            header, {"hAdapter": None, "hDevice": 0, "Type": 0, "Flags": 0, "hContext": 0}
        )
    gpu.capture_events[1] = corrupt
    error(gate.VoltageRequestGate(gpu).read, "malformed")
    assert not gpu.calls


def test_unstable_postwrite_pair_triggers_rollback():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {}, {}, {"state_before": True}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "unstable")
    assert exc.rollback["verified"] and gpu.enabled is True
    assert gpu.sets() == [(0, 0), (0, 1)]


def test_atomic_previous_is_retained_even_when_postwrite_identity_check_fails():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"state_before": False, "change_target": True}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "identity")
    assert exc.rollback["target"] is False
    assert not exc.rollback["attempted"] and not exc.rollback["verified"]
    assert gpu.sets() == [(0, 0)]


def test_failed_set_capability_check_carries_rollback_outcome():
    gpu = FakeGpu()
    gpu.events = [{}, {}, {"rm_status": 0x56}, {"rm_status": 0x56}]
    exc = error(lambda: gate.VoltageRequestGate(gpu).set_enabled(False), "unsupported")
    assert exc.status == 0x56 and exc.rollback["attempted"]
    assert not exc.rollback["verified"] and gpu.enabled is True
    assert gpu.sets() == [(0, 0), (0, 1)]
