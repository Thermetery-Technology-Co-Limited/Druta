import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import profiles
from druta import voltage_requests_live as live
from tests.test_voltage_request_replay import Gpu


@pytest.fixture
def gpu(monkeypatch):
    gpu = Gpu()
    gpu.static = {"name": "test", "vbios": "v", "driver": "d", "uuid": "u", "slot": gpu.slot()}
    gpu.gate_enabled = True
    monkeypatch.setattr(live.replay.voltage, "read", lambda gpu, **kwargs: gpu.policy())
    monkeypatch.setattr(
        live,
        "VoltageRequestGate",
        lambda obj: SimpleNamespace(read=lambda: {"identity": gpu.identity.copy(), "enabled": gpu.gate_enabled}),
    )
    return gpu


def test_scoped_capture_and_restore_preserves_unselected_requests(gpu):
    state = profiles.capture_voltage_requests(gpu, selection=("0", "REL"))
    state = json.loads(json.dumps(state))
    assert set(state["voltage_requests"]["rails"]) == {"0"}
    assert "native_hex" not in json.dumps(state) and "boost" not in json.dumps(state)
    wanted = gpu.offsets["0"][0]
    gpu.offsets["0"][0] += 10
    gpu.offsets["0"][1] += 99
    gpu.offsets["1"][4] -= 43
    gpu.boost = 42
    other = copy.deepcopy(gpu.offsets)
    assert profiles.preflight(gpu, state) is None
    assert all(ok for ok, _ in profiles.restore(gpu, state))
    other["0"][0] = wanted
    assert gpu.offsets == other and gpu.boost == 42


@pytest.mark.parametrize("mutation", ["bool", "range", "extra", "rail", "identity", "gate"])
def test_preflight_refuses_before_any_set(gpu, mutation):
    state = profiles.capture_voltage_requests(gpu)
    data = state["voltage_requests"]
    if mutation == "bool":
        data["rails"]["0"]["offsets_uv"]["REL"] = True
    elif mutation == "range":
        data["rails"]["0"]["offsets_uv"]["REL"] = 2**31
    elif mutation == "extra":
        state["core_off_mhz"] = 200
    elif mutation == "rail":
        data["rails"]["0"]["identity"]["name"] = "other"
    elif mutation == "identity":
        data["identity"]["device_id"] ^= 1
    else:
        gpu.gate_enabled = False
    assert profiles.preflight(gpu, state)
    assert not profiles.restore(gpu, state)[0][0]
    assert not gpu.set_calls


def test_callback_gets_fresh_state_only_for_write_and_can_abort(gpu):
    shown = live.read(gpu)
    callback = Mock(side_effect=ValueError("save failed"))
    live.set_request(gpu, "0", "REL", gpu.offsets["0"][0], expected=shown, before_write=callback)
    callback.assert_not_called()
    gpu.offsets["0"][0] += 9
    with pytest.raises(ValueError, match="save failed"):
        live.set_request(gpu, "0", "REL", 5000, expected=shown, before_write=callback)
    assert callback.call_args.args[0]["values"]["0"]["REL"] == gpu.offsets["0"][0]
    assert not gpu.set_calls


def test_autosave_for_load_captures_only_template_fields(gpu, tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "DIR", str(tmp_path))
    template = profiles.capture_voltage_requests(gpu, selection=("1", "VCRIT low"))
    gpu.offsets["1"][4] += 123
    name, _, missing = profiles.autosave_voltage_requests(gpu, "load", template)
    saved = profiles.load(name)
    assert not missing
    assert saved["voltage_requests"]["rails"]["1"]["offsets_uv"] == {"VCRIT low": gpu.offsets["1"][4]}


def test_restore_reports_partial_failure_and_stops(gpu):
    state = profiles.capture_voltage_requests(gpu)
    gpu.offsets["0"][0] += 1
    gpu.offsets["0"][1] += 2
    gpu.offsets["0"][2] += 3

    def fail_second(gpu, data):
        if len(gpu.set_calls) == 1:
            gpu.apply(data)
        return 0

    gpu.set_fault = fail_second
    results = profiles.restore(gpu, state)
    assert results[0][0] and not results[1][0]
    assert "remaining requests not applied" in results[1][1]
    assert len(gpu.set_calls) == 2
    assert gpu.offsets["0"][2] != state["voltage_requests"]["rails"]["0"]["offsets_uv"]["OV"]


def test_incomplete_current_inventory_rejects_capture_and_restore(gpu):
    state = profiles.capture_voltage_requests(gpu)
    gpu.info[12] = 4
    with pytest.raises(ValueError, match="complete"):
        profiles.capture_voltage_requests(gpu)
    assert profiles.preflight(gpu, state)
    assert not profiles.restore(gpu, state)[0][0]
    assert not gpu.set_calls


def test_scoped_profile_ignores_descriptive_driver_vbios_and_name_changes(gpu):
    state = profiles.capture_voltage_requests(gpu, selection=("0", "REL"))
    gpu.static.update(driver="new driver", vbios="new label", name="new label")
    gpu.offsets["0"][0] += 12
    assert profiles.preflight(gpu, state) is None
    assert all(ok for ok, _ in profiles.restore(gpu, state))
    assert gpu.offsets["0"][0] == state["voltage_requests"]["rails"]["0"]["offsets_uv"]["REL"]


def test_scoped_profile_rejects_different_uuid_before_set(gpu):
    state = profiles.capture_voltage_requests(gpu)
    gpu.static["uuid"] = "different"
    assert "UUID" in profiles.preflight(gpu, state)
    assert not profiles.restore(gpu, state)[0][0]
    assert not gpu.set_calls
