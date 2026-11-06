"""Offline checks for shared adaptive-power requests and aggregate readback."""

import struct
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import adaptive_power_live as live
from druta.perf_diagnostics import PerfDiagnosticError


@pytest.fixture
def device(monkeypatch):
    state = {"arch": (0x1B0, 3, 1, 0), "roles": {0, 4, 5, 6, 7}, "masks": dict.fromkeys((0, 2, 4, 5, 6, 7), 0),
             "sets": [], "malformed": None, "read_failure": 0, "set_failure": False, "no_effect": False}
    gpu = SimpleNamespace(_lock=threading.RLock())
    monkeypatch.setattr(live, "PerfDiagnosticsReader", lambda gpu: SimpleNamespace(gpu=gpu))
    monkeypatch.setattr(live.clock, "_binding", lambda reader: "paired")

    def get(reader, identity, command, size, packet):
        assert reader.gpu is gpu and identity == "paired"
        if command == 0x20801701:
            assert size == 16 and packet == bytes(16)
            return struct.pack("<4I", *state["arch"])
        assert size == 0x404 and len(packet) == size
        count, kind, index, attribute, flags, value, reserved = struct.unpack_from("<IHHHHII", packet)
        assert count == 1 and kind == 4 and flags == reserved == 0
        result = bytearray(packet)
        if command == lp_get:
            if state["read_failure"]:
                state["read_failure"] -= 1
                raise PerfDiagnosticError("transient", "transient")
            if index in state["roles"]:
                struct.pack_into("<HI", result, 10, 1, 1 if attribute == 9 else int(state["masks"][index] == 0))
            if state["malformed"] == "get_opaque":
                result[-1] = 1
            if state["malformed"] == "get_bool":
                struct.pack_into("<I", result, 12, 3)
            return bytes(result)
        assert command == live._SET and attribute == 10 and value in (0, 1)
        state["sets"].append((index, value))
        if state["set_failure"]:
            raise RuntimeError("set failed")
        if not state["no_effect"]:
            state["masks"][index] = (state["masks"][index] & ~2) if value else (state["masks"][index] | 2)
        struct.pack_into("<H", result, 10, 1)
        if state["malformed"] == "set_opaque":
            result[-1] = 1
        return bytes(result)

    lp_get = live.lpwr.ATTRIBUTE_COMMAND
    monkeypatch.setattr(live.lpwr, "_get", get)
    return live.AdaptivePowerController(gpu), state


@pytest.mark.parametrize("role", [0, 2, 4, 5, 6, 7])
def test_supported_and_unavailable_are_per_role(device, role):
    controller, state = device
    c = live.AdaptivePowerController(controller.gpu, role)
    snapshot = c.read()
    assert snapshot["index"] == role
    assert snapshot["available"] == (role in state["roles"])
    assert snapshot["enabled"] is (True if role in state["roles"] else None)


def test_disable_and_allow_report_observed_transitions(device):
    controller, state = device
    disabled = controller.set_allowed(False)
    assert state["sets"] == [(0, 0)] and not disabled["enabled"]
    assert disabled["request_accepted"] and disabled["verification"] == "aggregate_transition"
    allowed = controller.set_allowed(True)
    assert state["sets"] == [(0, 0), (0, 1)] and allowed["enabled"]
    assert allowed["verification"] == "aggregate_transition"


def test_shared_reason_and_noop_are_not_claimed_as_verified(device):
    controller, state = device
    state["masks"][0] = 4
    disabled = controller.set_allowed(False)
    assert state["masks"][0] == 6 and disabled["verification"] == "aggregate_only"
    allowed = controller.set_allowed(True)
    assert state["masks"][0] == 4 and not allowed["enabled"] and allowed["verification"] == "aggregate_only"
    state["masks"][0] = 0
    noop = controller.set_allowed(True)
    assert not noop["request_accepted"] and noop["verification"] == "already_allowed"
    assert state["sets"] == [(0, 0), (0, 1)]


def test_bad_or_missing_acknowledgement_never_retries_set(device):
    controller, state = device
    state["malformed"] = "set_opaque"
    with pytest.raises(live.AdaptivePowerError):
        controller.set_allowed(False)
    assert state["sets"] == [(0, 0)]
    state["malformed"] = None
    state["set_failure"] = True
    with pytest.raises(RuntimeError, match="set failed"):
        controller.set_allowed(False)
    assert state["sets"] == [(0, 0), (0, 0)]


@pytest.mark.parametrize("malformed", ["get_opaque", "get_bool"])
def test_bad_get_fields_rejected_before_write(device, malformed):
    controller, state = device
    state["malformed"] = malformed
    with pytest.raises(live.AdaptivePowerError):
        controller.set_allowed(False)
    assert state["sets"] == []


def test_unsupported_native_layout_and_role_refuse_write(device):
    controller, state = device
    state["arch"] = (0x190, 3, 1, 0)
    assert not controller.read()["available"]
    with pytest.raises(live.AdaptivePowerError):
        controller.set_allowed(False)
    state["arch"] = (0x1B0, 3, 1, 0)
    with pytest.raises(live.AdaptivePowerError):
        live.AdaptivePowerController(controller.gpu, 2).set_allowed(False)
    with pytest.raises(ValueError):
        live.AdaptivePowerController(controller.gpu, 10)
    assert state["sets"] == []


def test_no_effect_is_only_aggregate_observation(device):
    controller, state = device
    state["no_effect"] = True
    result = controller.set_allowed(False)
    assert result["request_accepted"] and result["enabled"]
    assert result["verification"] == "aggregate_only"

def test_transient_read_retries_bounded_without_set(device):
    controller, state = device
    state["read_failure"] = 1
    assert controller.read()["enabled"] is True
    state["read_failure"] = 2
    with pytest.raises(PerfDiagnosticError):
        controller.read()
    assert state["sets"] == []


@pytest.mark.parametrize("implementation", [2, 7])
def test_alternate_supported_blackwell_and_ms_role(device, implementation):
    controller, state = device
    state["arch"] = (0x1B0, implementation, 9, 0)
    state["roles"].add(2)
    ms = live.AdaptivePowerController(controller.gpu, 2)
    assert ms.read()["available"]
    result = ms.set_allowed(False)
    assert result["verification"] == "aggregate_transition" and state["sets"] == [(2, 0)]


def test_selected_role_keeps_all_other_role_masks(device):
    controller, state = device
    state["roles"].add(2)
    state["masks"].update({0: 0, 2: 4, 4: 8, 5: 2, 6: 0x80, 7: 1})
    initial = state["masks"].copy()
    controller.set_allowed(False)
    assert state["masks"] == {**initial, 0: 2}
    controller.set_allowed(True)
    assert state["masks"] == initial
    assert state["sets"] == [(0, 0), (0, 1)]


def test_unknown_implementation_refuses_before_set(device):
    controller, state = device
    state["arch"] = (0x1B0, 0xF, 1, 0)
    assert not controller.read()["available"]
    with pytest.raises(live.AdaptivePowerError):
        controller.set_allowed(False)
    assert state["sets"] == []


@pytest.mark.parametrize("role", [True, False, -1, 3, 8, "0", None])
def test_invalid_role_rejected_before_binding(device, monkeypatch, role):
    controller, _ = device
    binding = Mock()
    monkeypatch.setattr(live.clock, "_binding", binding)
    with pytest.raises(ValueError):
        live.AdaptivePowerController(controller.gpu, role)
    binding.assert_not_called()


@pytest.mark.parametrize("value", [0, 1, None, "true", 2])
def test_request_requires_strict_bool_before_io(device, value):
    controller, state = device
    with pytest.raises(ValueError):
        controller.set_allowed(value)
    assert state["sets"] == []
