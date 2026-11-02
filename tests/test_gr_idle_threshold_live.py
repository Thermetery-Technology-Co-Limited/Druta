"""Offline tests of GR threshold native packets and verification."""

import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import gr_idle_threshold_live as live
from druta.perf_diagnostics import PerfDiagnosticError


@pytest.fixture
def device(monkeypatch):
    data = {"arch": (0x1B0, 6, 0, 0), "raw": [10800, 108000000], "factor": 108000,
            "sets": 0, "skip": False, "missing": set(), "peer_change": False,
            "no_effect": False, "bad_ack": False, "transient_get": 0,
            "transient_set": False, "bad_header": False, "bad_guard": False,
            "bad_identity": False}
    gpu = SimpleNamespace(_lock=threading.RLock())
    class Reader:
        def __init__(self, gpu):
            self.gpu = gpu
    monkeypatch.setattr(live, "PerfDiagnosticsReader", Reader)
    monkeypatch.setattr(live.clock, "_binding", lambda reader: "paired")

    def get(reader, identity, command, size, request):
        assert identity == "paired"
        if data["bad_identity"]:
            raise PerfDiagnosticError("identity", "target changed")
        if command != live._SET and data["transient_get"]:
            data["transient_get"] -= 1
            raise PerfDiagnosticError("transient", "retry")
        if command == 0x20801701:
            return struct.pack("<4I", *data["arch"])
        if command == live._RAW:
            count, pointer = struct.unpack("<I4xQ", request)
            assert count == 2 and size == 16
            entries = (ctypes.c_ubyte * 24).from_address(pointer)
            if not data["skip"]:
                for i, value in enumerate(data["raw"]):
                    struct.pack_into("<I", entries, i * 12 + 8, value)
            if data["bad_guard"]:
                ctypes.c_ubyte.from_address(pointer - 1).value = 0
            return bytes(16) if data["bad_header"] else request
        attr = struct.unpack_from("<H", request, 8)[0]
        response = bytearray(request)
        if command == lpwr_command:
            if attr not in data["missing"]:
                values = {0x39: 1, 0x3A: data["raw"][0] * 1000 // data["factor"],
                          0x3B: data["raw"][1] * 1000 // data["factor"], 0x4C: data["raw"][0]}
                struct.pack_into("<HI", response, 10, 1, values[attr])
            return bytes(response)
        assert command == live._SET and attr == 0x3A
        data["sets"] += 1
        if data["transient_set"]:
            raise PerfDiagnosticError("transient", "uncertain SET")
        value = struct.unpack_from("<I", request, 12)[0]
        if not data["no_effect"]:
            data["raw"][0] = max(1, value * data["factor"] // 1000)
        if data["peer_change"]:
            data["raw"][1] += 1
        if not data["bad_ack"]:
            struct.pack_into("<H", response, 10, 1)
        return bytes(response)

    lpwr_command = live.lpwr.ATTRIBUTE_COMMAND
    monkeypatch.setattr(live.lpwr, "_get", get)
    return live.GrIdleThresholdController(gpu), data


def test_apply_exact_pair_and_noop(device):
    controller, data = device
    state = controller.apply(101)
    assert state["changed"] and state["requested_us"] == 101
    assert state["raw_pair"] == [10908, 108000000] and state["scale_factor"] == 108000
    assert not controller.apply(101)["changed"] and data["sets"] == 1


@pytest.mark.parametrize("failure", ["peer_change", "no_effect", "bad_ack"])
def test_unverified_set_fails_without_retry(device, failure):
    controller, data = device
    data[failure] = True
    with pytest.raises(live.GrIdleThresholdError):
        controller.apply(101)
    assert data["sets"] == 1


@pytest.mark.parametrize("failure", ["skip", "bad_header", "bad_guard", "bad_identity"])
def test_invalid_get_never_sets(device, failure):
    controller, data = device
    data[failure] = True
    with pytest.raises((live.GrIdleThresholdError, PerfDiagnosticError)):
        controller.apply(101)
    assert data["sets"] == 0


def test_partial_and_unsupported_capabilities(device):
    controller, data = device
    data["missing"].add(0x3B)
    assert controller.read()["status"] == "unavailable"
    with pytest.raises(live.GrIdleThresholdError):
        controller.apply(101)
    data["missing"].clear()
    data["arch"] = (0x170, 6, 0, 0)
    assert controller.read()["status"] == "unsupported_layout"
    data["arch"] = (0x1B0, 4, 0, 0)
    assert not controller.read()["available"]
    assert data["sets"] == 0


def test_ambiguous_and_zero_scale_edges():
    assert live._scale([0, 0], [0, 0]) is None
    assert live._scale([1, 0], [0, 0]) is None
    assert live._scale([10800, 108000000], [100, 1000000]) == 108000
    with pytest.raises(live.GrIdleThresholdError):
        live._scale([0, 0], [1, 0])


def test_validation_and_quantized_noop(device):
    controller, data = device
    for value in (True, 0, 2**32, 1.0):
        with pytest.raises(ValueError):
            controller.apply(value)
    with pytest.raises(ValueError):
        controller.apply(100000000)
    assert data["sets"] == 0


def test_transient_get_retry_but_not_set(device):
    controller, data = device
    data["transient_get"] = 1
    assert controller.read()["available"]
    data["transient_set"] = True
    with pytest.raises(PerfDiagnosticError):
        controller.apply(101)
    assert data["sets"] == 1


def test_quantized_request_reports_actual_and_compares_raw(device):
    controller, data = device
    data["factor"], data["raw"] = 1, [1, 100]
    result = controller.apply(1001)
    assert not result["changed"] and result["applied_us"] == 1000
    result = controller.apply(2001)
    assert result["changed"] and result["requested_us"] == 2001
    assert result["applied_us"] == 2000 and data["sets"] == 1


def test_ambiguous_scale_remains_readable_but_cannot_apply(device):
    controller, data = device
    data["raw"] = [0, 0]
    state = controller.read()
    assert state["available"] and state["status"] == "ambiguous_scale"
    assert state["scale_factor"] is None and state["maximum_us"] is None
    with pytest.raises(live.GrIdleThresholdError):
        controller.apply(1)
    assert data["sets"] == 0


def test_sentinel_equal_to_real_value_is_not_a_skip(device):
    controller, data = device
    data["raw"] = [0xA5A5A5A5, 0x5A5A5A5A]
    assert controller._raw_pair(0xA5A5A5A5) == data["raw"]
    assert controller._raw_pair(0x5A5A5A5A) == data["raw"]


@pytest.mark.parametrize("implementation", [2, 3, 5, 6, 7])
def test_source_qualified_native_implementations(device, implementation):
    controller, data = device
    data["arch"] = (0x1B0, implementation, 0, 0)
    assert controller.read()["available"]
