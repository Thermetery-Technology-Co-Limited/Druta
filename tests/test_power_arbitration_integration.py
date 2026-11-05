"""Paired read-only arbitration integration, independent of local identities."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import nvbackend, power_arbitration


@pytest.fixture
def gpu(monkeypatch):
    gpu = object.__new__(nvbackend.GPU)
    gpu._lock = threading.RLock()
    selected = {"slot": "0000:09:03.1", "handle": 123, "devid": 0xABCD, "subsys": 0x12345678}
    gpu.nvapi = SimpleNamespace(ok=True, gpu=123, selected=selected)
    gpu.nvml = SimpleNamespace(selected=selected.copy())
    gpu.slot = lambda: selected["slot"]
    gpu.arch = lambda: 8
    gpu._power_policy_table = Mock(return_value=("layout", "info", "control", "status"))
    monkeypatch.setattr(power_arbitration, "decode", Mock(return_value={"available": True, "policies": []}))
    return gpu


def test_paired_read_decodes_only_fresh_table(gpu):
    report, error = gpu.read_power_arbitration()
    assert error is None and report["available"]
    assert report["identity"]["device_id"] == 0xABCD
    assert report["identity"]["slot"] == "0000:09:03.1"
    assert report["captured_at"] > 0
    power_arbitration.decode.assert_called_once_with("layout", "info", "control", "status")


@pytest.mark.parametrize("mismatch", ["handle", "nvml", "architecture"])
def test_bad_pairing_or_generation_stops_before_table(gpu, mismatch):
    if mismatch == "handle":
        gpu.nvapi.gpu = 321
    elif mismatch == "nvml":
        gpu.nvml.selected["slot"] = "0000:03:00.0"
    else:
        gpu.arch = lambda: 1
    report, error = gpu.read_power_arbitration()
    assert report is None and error
    gpu._power_policy_table.assert_not_called()


def test_binding_change_during_get_discards_result_without_retry(gpu):
    def read():
        gpu.nvapi.gpu = 456
        gpu.nvapi.selected["handle"] = 456
        return ("layout", "info", "control", "status")

    gpu._power_policy_table.side_effect = read
    report, error = gpu.read_power_arbitration()
    assert report is None and "binding changed" in error
    assert gpu._power_policy_table.call_count == 1
    power_arbitration.decode.assert_not_called()


def test_transient_get_retries_once_with_fresh_transport(gpu):
    gpu._current_limit_transport = "stale"
    gpu._current_limit_layout_choice = "stale"

    def read():
        if gpu._power_policy_table.call_count == 1:
            raise ValueError("temporarily unavailable")
        assert gpu._current_limit_transport is None
        assert gpu._current_limit_layout_choice is None
        return ("layout", "info", "control", "status")

    gpu._power_policy_table.side_effect = read
    assert gpu.read_power_arbitration()[1] is None
    assert gpu._power_policy_table.call_count == 2


@pytest.mark.parametrize(
    "failure,count", [(ValueError("transient"), 2), (nvbackend.CurrentLimitUnsupported("unsupported"), 1)]
)
def test_bounded_failures(gpu, failure, count):
    gpu._power_policy_table.side_effect = failure
    assert gpu.read_power_arbitration() == (None, str(failure))
    assert gpu._power_policy_table.call_count == count


def test_unsupported_decoder_preserves_existing_control_state(gpu):
    gpu._current_limit_established = {3, 7}
    gpu._current_limit_layout_choice = "known"
    power_arbitration.decode.return_value = {"available": False, "reason": "unknown arbitration layout"}
    report, error = gpu.read_power_arbitration()
    assert not report["available"] and error == "unknown arbitration layout"
    assert gpu._current_limit_established == {3, 7}
    assert gpu._current_limit_layout_choice == "known"
