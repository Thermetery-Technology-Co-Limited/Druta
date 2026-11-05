import struct
import threading
from types import SimpleNamespace

import pytest

from druta import pcie_speed_live as pcie
from druta.perf_diagnostics import PerfDiagnosticError


@pytest.fixture
def card(monkeypatch):
    state = {"target": 1, "gpu": 1, "root": 1, "width": 16,
             "gpu_max": 4, "root_max": 4, "register_other": 0x80001060,
             "sets": [], "mode": "normal", "identity": {"slot": "other", "architecture": 10}}
    gpu = SimpleNamespace(_lock=threading.RLock())
    monkeypatch.setattr(pcie, "PerfDiagnosticsReader", lambda gpu: object())
    monkeypatch.setattr(pcie.clock, "_binding", lambda reader: dict(state["identity"]))
    monkeypatch.setattr(pcie.time, "sleep", lambda delay: None)

    def get(reader, identity, command, size, request):
        if command == pcie.ARCH:
            return struct.pack("<4I", 0x1B0, 2, 0, 0)
        if command == pcie.INFO:
            result = bytearray(request)
            values = (state["gpu_max"], state["root_max"],
                      state["gpu"] << 16 | state["width"] << 20,
                      state["root"] << 16 | state["width"] << 20)
            for i, value in enumerate(values):
                struct.pack_into("<I", result, 8 + 8 * i, value)
            if state["mode"] == "bad_geometry":
                result[4] ^= 1
            return bytes(result)
        assert command == pcie.SET and size == 4
        value = struct.unpack("<I", request)[0]
        state["sets"].append(value)
        if state["mode"] == "fail":
            raise PerfDiagnosticError("transient", "RM failure")
        if state["mode"] != "ignored":
            state["target"] = value
            state["gpu"] = value
            state["root"] = value
        if state["mode"] == "opaque_change":
            state["register_other"] ^= 0x100
        return request

    def registers(reader, identity, masks):
        assert masks == {pcie.REGISTER: 0}
        return {hex(pcie.REGISTER): state["register_other"] | state["target"] << 16}

    monkeypatch.setattr(pcie.lpwr, "_get", get)
    monkeypatch.setattr(pcie, "_read_registers", registers)
    return pcie.PcieSpeedController(gpu), state


def test_qualified_request_and_noop(card):
    controller, state = card
    initial = controller.read()
    assert initial["available"] and initial["allowed_generations"] == [1, 2, 3, 4]
    same = controller.set_generation(1, expected_identity=initial["identity"])
    assert same["verification"] == "already_matches" and state["sets"] == []
    result = controller.set_generation(2, expected_identity=initial["identity"])
    assert result["verification"] == "target_and_link" and result["request_accepted"]
    assert result["before"]["target_generation"] == 1 and result["target_generation"] == 2
    assert state["sets"] == [2]


def test_unverified_does_not_claim_change(card):
    controller, state = card
    state["mode"] = "ignored"
    result = controller.set_generation(2)
    assert result["verification"] == "unverified" and result["request_accepted"]
    assert result["target_generation"] == 1 and state["sets"] == [2]
    state["mode"] = "opaque_change"
    result = controller.set_generation(2)
    assert result["verification"] == "unverified" and state["sets"] == [2, 2]


def test_rejects_identity_range_geometry_and_failed_set(card):
    controller, state = card
    for value in (True, 0, 5, 7):
        with pytest.raises(pcie.PcieSpeedError):
            controller.set_generation(value)
    with pytest.raises(pcie.PcieSpeedError, match="identity"):
        controller.set_generation(2, expected_identity={})
    state["mode"] = "bad_geometry"
    with pytest.raises(pcie.PcieSpeedError, match="geometry"):
        controller.read()
    state["mode"] = "fail"
    with pytest.raises(pcie.PcieSpeedError, match="SET failed"):
        controller.set_generation(2)
    assert state["sets"] == [2]


def test_nonmatching_caps_limit_and_unsupported_arch(card, monkeypatch):
    controller, state = card
    state.update(gpu_max=6, root_max=3)
    assert controller.read()["allowed_generations"] == [1, 2, 3]
    original = pcie.lpwr._get
    def other(reader, identity, command, size, request):
        return struct.pack("<4I", 0x170, 2, 0, 0) if command == pcie.ARCH else original(reader, identity, command, size, request)
    monkeypatch.setattr(pcie.lpwr, "_get", other)
    assert not controller.read()["available"]
    with pytest.raises(pcie.PcieSpeedError, match="not supported"):
        controller.set_generation(2)
    assert not state["sets"]


def test_read_retry_and_accepted_request_with_failed_readback(card, monkeypatch):
    controller, state = card
    original = pcie.lpwr._get
    failures = [True]
    def transient(reader, identity, command, size, request):
        if command == pcie.ARCH and failures:
            failures.pop()
            raise PerfDiagnosticError("transient", "temporary read")
        return original(reader, identity, command, size, request)
    monkeypatch.setattr(pcie.lpwr, "_get", transient)
    assert controller.read()["available"] and not failures

    def unreadable_after_set(reader, identity, command, size, request):
        if command == pcie.ARCH and state["sets"]:
            raise PerfDiagnosticError("transient", "readback unavailable")
        return original(reader, identity, command, size, request)
    monkeypatch.setattr(pcie.lpwr, "_get", unreadable_after_set)
    result = controller.set_generation(2)
    assert result["verification"] == "unverified" and result["request_accepted"]
    assert not result["available"] and result["generation"] is None
    assert result["before"]["generation"] == 1 and state["sets"] == [2]
