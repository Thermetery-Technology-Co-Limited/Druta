"""INFO-only configured relationships use the existing paired GPU transport."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import nvbackend, power_relationships


@pytest.fixture
def gpu(monkeypatch):
    gpu = object.__new__(nvbackend.GPU)
    gpu._lock = threading.RLock()
    selected = {"slot": "0000:09:03.1", "handle": 123, "devid": 0xABCD, "subsys": 0x12345678}
    gpu.nvapi = SimpleNamespace(ok=True, gpu=123, selected=selected)
    gpu.nvml = SimpleNamespace(selected=selected.copy())
    gpu.slot = lambda: selected["slot"]
    gpu.arch = lambda: 8
    gpu._current_limit_abi = Mock(return_value="layout")
    gpu._current_limit_rm = Mock(return_value="info")
    gpu._power_policy_table = Mock(side_effect=AssertionError("No STATUS or CONTROL read"))
    monkeypatch.setattr(power_relationships, "decode", Mock(return_value={"available": True, "relationships": []}))
    return gpu


def test_info_only_read_and_adapter_identity(gpu):
    report, error = gpu.read_power_relationships()
    assert error is None and report["available"]
    assert report["identity"]["device_id"] == 0xABCD
    assert report["identity"]["slot"] == "0000:09:03.1"
    assert report["captured_at"] > 0
    gpu._current_limit_rm.assert_called_once_with(0x2080A618)
    power_relationships.decode.assert_called_once_with("layout", "info")
    gpu._power_policy_table.assert_not_called()


@pytest.mark.parametrize("mismatch", ["handle", "nvml", "architecture"])
def test_existing_pairing_gate_precedes_info(gpu, mismatch):
    if mismatch == "handle":
        gpu.nvapi.gpu = 321
    elif mismatch == "nvml":
        gpu.nvml.selected["slot"] = "0000:03:00.0"
    else:
        gpu.arch = lambda: 1
    report, error = gpu.read_power_relationships()
    assert report is None and error
    gpu._current_limit_rm.assert_not_called()


def test_transient_info_failure_retries_with_fresh_transport(gpu):
    gpu._current_limit_transport = "stale"
    gpu._current_limit_layout_choice = "stale"

    def read(command):
        assert command == 0x2080A618
        if gpu._current_limit_rm.call_count == 1:
            raise ValueError("temporarily unavailable")
        assert gpu._current_limit_transport is None
        assert gpu._current_limit_layout_choice is None
        return "info"

    gpu._current_limit_rm.side_effect = read
    assert gpu.read_power_relationships()[1] is None
    assert gpu._current_limit_rm.call_count == 2


@pytest.mark.parametrize(
    "failure,count", [(ValueError("transient"), 2), (nvbackend.CurrentLimitUnsupported("unsupported"), 1)]
)
def test_bounded_failures_leave_control_state(gpu, failure, count):
    gpu._current_limit_established = {3, 7}
    gpu._current_limit_rm.side_effect = failure
    assert gpu.read_power_relationships() == (None, str(failure))
    assert gpu._current_limit_rm.call_count == count
    assert gpu._current_limit_established == {3, 7}


def test_unrecognized_info_is_not_a_retry_or_control_failure(gpu):
    gpu._current_limit_established = {3, 7}
    gpu._current_limit_layout_choice = "known"
    power_relationships.decode.return_value = {"available": False, "reason": "unknown INFO layout"}
    report, error = gpu.read_power_relationships()
    assert not report["available"] and error == "unknown INFO layout"
    gpu._current_limit_rm.assert_called_once_with(0x2080A618)
    assert gpu._current_limit_established == {3, 7}
    assert gpu._current_limit_layout_choice == "known"
