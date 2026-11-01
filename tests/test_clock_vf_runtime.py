"""Synthetic live Clock VF requests, partial writes and exact compensation."""

import copy
import ctypes

import pytest

from druta import clock_vf_registry as vf
from druta import clock_vf_runtime as runtime
from tests.test_clock_vf_registry import FakeGPU as ReadGPU
from tests.test_clock_vf_registry import raw_header

MONO, SMOOTH = vf.MONOTONICITY_KEY, vf.SMOOTHENING_KEY


class FakeGPU(ReadGPU):
    def __init__(self, *, get_actions=(), set_actions=(), **kwargs):
        super().__init__(**kwargs)
        self.get_actions, self.set_actions = list(get_actions), list(set_actions)
        self.writes, self.reads = [], 0

    def _legacy_clk_escape(self, packet, fields):
        assert packet[2] == 68 + vf._SIZE
        assert packet[15] == vf._SIZE
        assert packet[18] == 0
        assert packet[5] == packet[11] == 0
        previous = bytes(self.raw)
        if packet[14] == vf._COMMAND:
            self.reads += 1
            ctypes.memmove(ctypes.addressof(packet) + 68, previous, len(previous))
            actions = self.get_actions
        else:
            assert packet[14] == runtime._SET
            payload = bytes(packet)[68:]
            assert payload[60:] == bytes([0xA5]) * (vf._SIZE - 60)
            self.writes.append((payload, copy.deepcopy(fields)))
            self.raw[:] = payload[:60]
            actions = self.set_actions
        packet[5], packet[11], packet[16] = 1, 1, 0
        action = actions.pop(0) if actions else None
        return action(self, packet, previous) if action else 0


def reject(code, *, partial=False):
    def action(gpu, packet, previous):
        if not partial:
            gpu.raw[:] = previous
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


def raises_after_write(gpu, packet, previous):
    raise OSError("synthetic interrupted escape")


def test_read_observation_matches_existing_registry_schema_without_other_capabilities():
    gpu = FakeGPU()
    gpu.nvml = None
    result = runtime.ClockVfController(gpu).read()
    vf._shape(result, writable=True)
    assert result["identity"]["slot"] == "0000:72:1d.3"
    assert result["identity"]["device_id"] == 0xABCD
    assert result["header_hex"] == gpu.raw.hex()
    assert result["stable_reads"] == 2
    assert gpu.reads == 2 and not gpu.writes


@pytest.mark.parametrize("architecture", [6, 8, 10])
@pytest.mark.parametrize("changes", [{MONO: False}, {SMOOTH: False}, {MONO: False, SMOOTH: False}])
def test_exact_projection_preserves_flags_opaque_block_and_empty_rows(architecture, changes):
    gpu = FakeGPU(architecture=architecture)
    gpu.raw[8:10] = bytes((1, 1))
    gpu.raw[12:14] = bytes((1, 1))
    gpu.raw[0x38] = 1
    original = bytes(gpu.raw)
    result = runtime.ClockVfController(gpu).set(changes)
    expected = bytearray(original)
    for key, value in changes.items():
        field, offset, _, _ = vf._EDITABLE[key]
        expected[offset] = int(value)
        assert result[field] is value
    assert result["header_hex"] == expected.hex()
    assert gpu.writes[0][0][:60] == expected
    assert len(gpu.writes) == 1 and gpu.reads == 4
    assert gpu.transport_count == 1


def test_identity_request_returns_fresh_observation_without_dispatch():
    gpu = FakeGPU()
    result = runtime.ClockVfController(gpu).set({MONO: True, SMOOTH: True})
    assert result["vf_monotonicity_enforced"] is True
    assert gpu.reads == 2 and not gpu.writes


@pytest.mark.parametrize("changes", [None, [], {}, {"unknown": True}, {MONO: 1}, {MONO: None}, {SMOOTH: "false"}])
def test_invalid_changes_fail_before_any_get_or_set(changes):
    gpu = FakeGPU()
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set(changes)
    assert caught.value.kind == "invalid"
    assert caught.value.rollback is None
    assert gpu.reads == 0 and not gpu.writes


def test_transient_initial_get_recaptures_transport_once_and_then_succeeds():
    gpu = FakeGPU(get_actions=[lambda *args: 0xC0000001])
    result = runtime.ClockVfController(gpu).set({MONO: False})
    assert result["vf_monotonicity_enforced"] is False
    assert gpu.transport_count == 2
    assert gpu.reads == 5 and len(gpu.writes) == 1


def test_missing_handle_is_bounded_and_never_becomes_false():
    gpu = FakeGPU()
    gpu.nvapi.ok = False
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).read()
    assert caught.value.kind == "transient"
    assert gpu.reads == 0 and not gpu.writes


def test_transient_transport_capture_failure_is_retried_before_dispatch(monkeypatch):
    gpu = FakeGPU()
    original = gpu._current_limit_transport_layouts
    failures = [True, False]

    def capture():
        if failures.pop(0):
            raise OSError("synthetic temporary capture failure")
        original()

    monkeypatch.setattr(gpu, "_current_limit_transport_layouts", capture)
    result = runtime.ClockVfController(gpu).set({MONO: False})
    assert result["vf_monotonicity_enforced"] is False
    assert failures == [] and len(gpu.writes) == 1


def test_two_transient_get_failures_do_not_dispatch():
    gpu = FakeGPU(get_actions=[lambda *args: -1, lambda *args: -1])
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "transient"
    assert gpu.reads == 2 and gpu.transport_count == 2 and not gpu.writes


def test_unsupported_get_is_not_retried():
    gpu = FakeGPU(get_actions=[word(16, 0x56)])
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "unsupported"
    assert gpu.reads == 1 and not gpu.writes


@pytest.mark.parametrize("offset,value", [(4, 1), (10, 2), (14, 0), (0x38, 1), (60, 0), (-69, 0), (vf._SIZE, 0)])
def test_malformed_get_masks_flags_reserved_bytes_body_and_guards_prevent_write(offset, value):
    gpu = FakeGPU(get_actions=[byte(offset, value)])
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "malformed"
    assert gpu.reads == 1 and not gpu.writes


def test_unstable_reads_fail_without_retry_or_write():
    gpu = FakeGPU(get_actions=[None, byte(10, 0)])
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "unstable"
    assert gpu.reads == 2 and not gpu.writes


@pytest.mark.parametrize("code", [0x56, 0x6C])
def test_known_mode_rejection_is_an_error_with_exact_compensating_readback(code):
    gpu = FakeGPU(set_actions=[reject(code)])
    original = bytes(gpu.raw)
    with pytest.raises(runtime.ClockVfError, match="current startup mode") as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "unsupported" and caught.value.status == code
    assert caught.value.rollback["attempted"] and caught.value.rollback["verified"]
    assert gpu.writes[1][0][:60] == original
    assert bytes(gpu.raw) == original
    assert len(gpu.writes) == 2 and gpu.transport_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        reject(0x40, partial=True),
        raises_after_write,
        word(11, 0),
        word(5, 2),
        word(14, 0),
        byte(10, 1),
        byte(-69, 0),
        byte(vf._SIZE, 0),
    ],
)
def test_partial_write_errors_restore_exact_original_once_on_same_transport(failure):
    gpu = FakeGPU(set_actions=[failure])
    original = bytes(gpu.raw)
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.rollback["verified"] is True
    assert bytes(gpu.raw) == original
    assert len(gpu.writes) == 2 and gpu.writes[1][0][:60] == original
    assert gpu.writes[0][1] == gpu.writes[1][1]
    assert gpu.transport_count == 1


def test_ignored_set_is_detected_and_compensated():
    def ignore(gpu, packet, previous):
        gpu.raw[:] = previous
        return 0

    gpu = FakeGPU(set_actions=[ignore])
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "readback"
    assert caught.value.rollback["verified"]
    assert len(gpu.writes) == 2


@pytest.mark.parametrize("offset", [8, 9, 11, 12, 13, 20, 55])
def test_unselected_peer_or_opaque_drift_is_not_accepted(offset):
    def mutate_peer(gpu, packet, previous):
        gpu.raw[offset] ^= 1
        if offset == 8:
            gpu.raw[0x38] = gpu.raw[8]
        return 0

    gpu = FakeGPU(set_actions=[mutate_peer])
    original = bytes(gpu.raw)
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "readback" and caught.value.rollback["verified"]
    assert bytes(gpu.raw) == original


def test_post_write_transient_get_retries_fixed_transport_only():
    gpu = FakeGPU(get_actions=[None, None, lambda *args: -1])
    result = runtime.ClockVfController(gpu).set({SMOOTH: False})
    assert result["vf_smoothening_enforced"] is False
    assert gpu.reads == 5 and len(gpu.writes) == 1 and gpu.transport_count == 1


def test_exhausted_post_write_gets_trigger_one_restore_and_fresh_verification():
    gpu = FakeGPU(get_actions=[None, None, lambda *args: -1, lambda *args: -1])
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "transient" and caught.value.rollback["verified"]
    assert len(gpu.writes) == 2 and gpu.reads == 6 and gpu.transport_count == 1


def test_compensating_failure_is_explicit_and_still_attempts_fresh_get():
    gpu = FakeGPU(set_actions=[reject(0x40, partial=True), reject(0x40)])
    with pytest.raises(runtime.ClockVfError, match="rollback was not verified") as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    rollback = caught.value.rollback
    assert rollback["attempted"] and not rollback["verified"]
    assert rollback["readback"]["vf_monotonicity_enforced"] is False
    assert "did not match" in rollback["error"]
    assert len(gpu.writes) == 2 and gpu.reads == 4


def test_unreadable_rollback_is_reported_without_more_set_or_transport_capture():
    gpu = FakeGPU(
        set_actions=[reject(0x40, partial=True)], get_actions=[None, None, lambda *args: -1, lambda *args: -1]
    )
    with pytest.raises(runtime.ClockVfError, match="rollback was not verified") as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert not caught.value.rollback["verified"]
    assert caught.value.rollback["readback"] is None
    assert len(gpu.writes) == 2 and gpu.reads == 4 and gpu.transport_count == 1


def test_target_change_after_dispatch_never_writes_compensation_to_other_gpu():
    def change_identity(gpu, packet, previous):
        gpu.nvapi.gpu = gpu.nvapi.selected["handle"] = 789
        return 0

    gpu = FakeGPU(set_actions=[change_identity])
    with pytest.raises(runtime.ClockVfError) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.kind == "identity"
    assert not caught.value.rollback["attempted"] and not caught.value.rollback["verified"]
    assert len(gpu.writes) == 1 and gpu.transport_count == 1


def test_controller_keeps_sticky_target_between_calls():
    gpu = FakeGPU()
    controller = runtime.ClockVfController(gpu)
    controller.read()
    gpu.nvapi.gpu = gpu.nvapi.selected["handle"] = 789
    with pytest.raises(runtime.ClockVfError) as caught:
        controller.set({MONO: False})
    assert caught.value.kind == "identity" and caught.value.rollback is None
    assert not gpu.writes


def test_keyboard_interrupt_after_dispatch_also_compensates():
    def interrupt(gpu, packet, previous):
        raise KeyboardInterrupt("synthetic interruption")

    gpu = FakeGPU(set_actions=[interrupt])
    with pytest.raises(KeyboardInterrupt) as caught:
        runtime.ClockVfController(gpu).set({MONO: False})
    assert caught.value.rollback["verified"]
    assert bytes(gpu.raw) == raw_header()
    assert len(gpu.writes) == 2
