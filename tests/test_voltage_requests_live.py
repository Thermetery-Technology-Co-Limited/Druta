"""Offline selected-field legacy requests through the full replay contract."""

import copy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import voltage_requests_live as live
from tests.test_voltage_request_replay import Gpu


@pytest.fixture
def gpu(monkeypatch):
    gpu = Gpu()
    gpu.gate_enabled = True
    monkeypatch.setattr(live.replay.voltage, "read", lambda gpu, **kwargs: gpu.policy())
    gate_read = Mock(side_effect=lambda: {"identity": gpu.identity.copy(), "enabled": gpu.gate_enabled})
    monkeypatch.setattr(live, "VoltageRequestGate", lambda obj: SimpleNamespace(read=gate_read))
    gpu.gate_read = gate_read
    return gpu


def test_read_captures_all_six_words_without_set_or_gate_change(gpu):
    snapshot = live.read(gpu, slot=gpu.slot())
    assert snapshot["values"]["0"] == dict(zip(live.FIELDS, gpu.offsets["0"], strict=True))
    assert snapshot["gate_enabled"] is True
    assert not gpu.set_calls
    assert gpu.gate_read.call_count == 1


@pytest.mark.parametrize("rail", ["0", "1"])
@pytest.mark.parametrize("field", live.FIELDS)
def test_selected_field_uses_fresh_peers_boost_and_preserves_opaque(gpu, rail, field):
    displayed = live.read(gpu)
    # Another supported app operation after the displayed read must survive.
    gpu.offsets["1"][2] += 91
    gpu.offsets["0"][5] -= 27
    gpu.boost = 41
    gpu.native_opaque ^= 1
    gpu.public_opaque ^= 1
    before = copy.deepcopy(gpu.offsets)
    position = live.FIELDS.index(field)
    result = live.set_request(gpu, rail, field, -5000, expected=displayed)
    before[rail][position] = -5000
    assert gpu.offsets == before and gpu.boost == 41
    assert result["changed"] and result["verified"] and len(gpu.set_calls) == 1
    assert result["after"]["info_hex"] == result["before"]["info_hex"]
    assert live.replay._opaque(result["after"]) == live.replay._opaque(result["before"])


def test_no_op_is_explicit_and_never_sends_set(gpu):
    displayed = live.read(gpu)
    result = live.set_request(gpu, "0", "REL", gpu.offsets["0"][0], expected=displayed)
    assert not result["attempted"] and not result["verified"] and not result["changed"]
    assert not gpu.set_calls


def test_disabled_gate_rejects_without_set_and_never_enables(gpu):
    displayed = live.read(gpu)
    gpu.gate_enabled = False
    with pytest.raises(live.VoltageRequestsError, match="disabled.*suppressed") as error:
        live.set_request(gpu, "0", "REL", 5000, expected=displayed)
    assert error.value.kind == "suppressed" and not gpu.set_calls and gpu.gate_enabled is False


def test_success_but_unchanged_set_is_failure_with_readback_detail(gpu):
    displayed = live.read(gpu)
    gpu.set_fault = lambda *args: 0
    with pytest.raises(live.replay.ReplayError, match="unchanged from before") as error:
        live.set_request(gpu, "0", "REL", 5000, expected=displayed)
    assert error.value.attempted and not error.value.result["verified"]
    assert len(gpu.set_calls) == 1


def test_readback_failure_is_uncertain_and_never_reports_success(gpu):
    displayed = live.read(gpu)

    def write_then_lose_readback(gpu, data):
        gpu.apply(data)
        gpu.get_fault = lambda *args: -1
        return 0

    gpu.set_fault = write_then_lose_readback
    with pytest.raises(live.replay.ReplayError) as error:
        live.set_request(gpu, "1", "VCRIT high", 5000, expected=displayed)
    assert error.value.attempted and not error.value.result["verified"]
    assert error.value.result["readback_error"] and len(gpu.set_calls) == 1


@pytest.mark.parametrize("change", ["slot", "identity", "rail"])
def test_stale_target_or_controller_identity_never_writes(gpu, change):
    displayed = live.read(gpu)
    if change == "slot":
        displayed["slot"] = "0000:02:00.0"
    elif change == "identity":
        gpu.identity["device_id"] ^= 1
        gpu.nvapi.selected["devid"] = gpu.identity["device_id"]
        gpu.nvml.selected["devid"] = gpu.identity["device_id"]
    else:
        # Change understood row identity without relying on local device IDs.
        displayed["request"]["rail_identity"]["0"]["name"] = "Different rail"
        displayed["request"]["policy"]["rails"]["0"]["name"] = "Different rail"
    with pytest.raises((live.VoltageRequestsError, live.replay.ReplayError)):
        live.set_request(gpu, "0", "REL", 5000, expected=displayed)
    assert not gpu.set_calls


def test_fresh_read_failure_does_not_apply_displayed_full_packet(gpu):
    displayed = live.read(gpu)
    gpu.get_fault = lambda *args: -1
    with pytest.raises(live.replay.ReplayError):
        live.set_request(gpu, "0", "REL", 5000, expected=displayed)
    assert not gpu.set_calls


def test_partial_capability_keeps_present_rail_readable_but_never_drops_peer(gpu):
    gpu.info[12] = 4
    displayed = live.read(gpu)
    assert set(displayed["values"]) == {"1"} and not displayed["request"]["complete"]
    with pytest.raises(live.VoltageRequestsError, match="Complete"):
        live.set_request(gpu, "1", "OV", 5000, expected=displayed)
    assert not gpu.set_calls


def test_gate_read_failure_does_not_remove_request_capability(gpu):
    gpu.gate_read.side_effect = live.VoltageRequestGateError("transient", "gate GET unavailable")
    displayed = live.read(gpu)
    assert len(displayed["values"]) == 2 and displayed["gate_enabled"] is None
    assert displayed["gate_error"] == "gate GET unavailable" and not gpu.set_calls
    with pytest.raises(live.VoltageRequestGateError):
        live.set_request(gpu, "0", "REL", 5000, expected=displayed)
    assert not gpu.set_calls


@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "Infinity",
        "-Infinity",
        "0.0001",
        "2147483.648",
        "-2147483.649",
        "1.0000000000000000000000000000000000000001",
        "1e-1000000",
        "1e1000000",
    ],
)
def test_millivolts_reject_nonfinite_fractional_uv_and_int32_overflow(value):
    with pytest.raises(live.VoltageRequestsError):
        live.millivolts_to_uv(value)


@pytest.mark.parametrize(
    "value,expected", [("-1.234", -1234), ("2147483.647", 2**31 - 1), ("-2147483.648", -(2**31)), (Decimal("5"), 5000)]
)
def test_exact_signed_millivolt_conversion(value, expected):
    assert live.millivolts_to_uv(value) == expected


@pytest.mark.parametrize("value", [True, 1.0, float("nan"), float("inf"), 2**31, -(2**31) - 1])
def test_direct_uv_api_rejects_invalid_request_before_read(gpu, value):
    displayed = live.read(gpu)
    calls = len(gpu.calls)
    with pytest.raises(live.VoltageRequestsError):
        live.set_request(gpu, "0", "REL", value, expected=displayed)
    assert len(gpu.calls) == calls and not gpu.set_calls
