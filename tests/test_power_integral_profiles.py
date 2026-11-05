import copy
import threading

import pytest

from druta import power_integral_profiles as profiles

IDENTITY = {"slot": "0000:72:1d.0", "architecture": 8, "device_id": 0x1234, "subsystem_id": 0x22222222}


class GPU:
    def __init__(self):
        self._lock = threading.RLock()
        self.static = {"name": "GPU", "vbios": "1", "driver": "2", "uuid": "abc"}
        self.identity = copy.deepcopy(IDENTITY)
        self.rows = [
            {
                "policy": 3,
                "type": 7,
                "channel": 5,
                "unit": "mW",
                "available": True,
                "lower_raw": 1000,
                "upper_raw": 4000,
            },
            {"policy": 8, "type": 2, "channel": 9, "unit": "mA", "available": True, "lower_raw": 200, "upper_raw": 500},
        ]
        self.calls = []
        self.fail = None
        self.suppress = False

    def slot(self):
        return IDENTITY["slot"]


class Controller:
    def __init__(self, gpu):
        self.gpu = gpu

    def read(self):
        return {"identity": copy.deepcopy(self.gpu.identity), "policies": copy.deepcopy(self.gpu.rows)}

    def _set(self, policy, field, value, expected_identity):
        assert expected_identity == self.gpu.identity
        self.gpu.calls.append((policy, field, value))
        if self.gpu.fail == field:
            raise ValueError("write failed")
        row = next(row for row in self.gpu.rows if row["policy"] == policy)
        lower = value if field == "lower_raw" else row["lower_raw"]
        upper = value if field == "upper_raw" else row["upper_raw"]
        assert lower <= upper
        if not self.gpu.suppress:
            row[field] = value
        return self.read()

    def set_lower(self, policy, value, *, expected_identity=None):
        return self._set(policy, "lower_raw", value, expected_identity)

    def set_upper(self, policy, value, *, expected_identity=None):
        return self._set(policy, "upper_raw", value, expected_identity)


@pytest.fixture(autouse=True)
def fake_controller(monkeypatch):
    monkeypatch.setattr(profiles, "PowerIntegralController", Controller)


def test_capture_selection_and_single_field_undo():
    gpu = GPU()
    state = profiles.capture(gpu, {"3": ["lower_raw"]})
    assert state["power_integral"]["policies"][0]["values"] == {"lower_raw": 1000}
    gpu.rows[0].update(lower_raw=1500, upper_raw=4500)
    assert profiles.restore(gpu, state) == [(True, "Power-integral policy 3 lower_raw applied: 1000")]
    assert gpu.rows[0]["lower_raw"] == 1000 and gpu.rows[0]["upper_raw"] == 4500
    assert gpu.rows[1]["lower_raw"] == 200 and gpu.rows[1]["upper_raw"] == 500
    assert gpu.calls == [(3, "lower_raw", 1000)]
    gpu.calls.clear()
    assert profiles.restore(gpu, state)[0][0] and gpu.calls == []


def test_pair_crossing_orders_and_zero():
    gpu = GPU()
    state = profiles.capture(gpu, {3: ["lower_raw", "upper_raw"]})
    gpu.rows[0].update(lower_raw=0, upper_raw=500)
    assert profiles.restore(gpu, state)[0][0]
    assert [x[1] for x in gpu.calls] == ["upper_raw", "lower_raw"]
    state["power_integral"]["policies"][0]["values"] = {"lower_raw": 0, "upper_raw": 500}
    gpu.calls.clear()
    assert profiles.restore(gpu, state)[0][0]
    assert [x[1] for x in gpu.calls] == ["lower_raw", "upper_raw"]


def test_malformed_and_selected_capability():
    gpu = GPU()
    state = profiles.capture(gpu)
    assert len(profiles.validate(state)["policies"]) == 2
    with pytest.raises(ValueError):
        profiles.capture(gpu, {3: ["lower_raw"], "3": ["upper_raw"]})
    with pytest.raises(ValueError):
        profiles.capture(gpu, {1: ["lower_raw"]})
    broken = copy.deepcopy(state)
    broken["power_integral"]["policies"][0]["values"]["lower_raw"] = True
    with pytest.raises(ValueError):
        profiles.validate(broken)
    broken = copy.deepcopy(state)
    broken["power_integral"]["policies"][0]["values"]["upper_raw"] = 0
    with pytest.raises(ValueError):
        profiles.validate(broken)
    gpu.rows[0]["available"] = False
    assert profiles.preflight(gpu, state)
    assert not profiles.restore(gpu, state)[0][0] and not gpu.calls


def test_identity_and_single_field_combined_bounds():
    gpu = GPU()
    state = profiles.capture(gpu, {3: ["lower_raw"]})
    gpu.identity["device_id"] += 1
    assert "identity" in profiles.preflight(gpu, state)
    gpu.identity = copy.deepcopy(IDENTITY)
    gpu.rows[0]["type"] += 1
    assert "metadata" in profiles.preflight(gpu, state)
    gpu.rows[0]["type"] -= 1
    gpu.rows[0]["upper_raw"] = 500
    assert "bounds" in profiles.preflight(gpu, state)
    gpu.static["uuid"] = "other"
    assert "UUID" in profiles.preflight(gpu, state)


def test_partial_failure_stops_and_readback_required():
    gpu = GPU()
    state = profiles.capture(gpu)
    gpu.rows[0].update(lower_raw=0, upper_raw=500)
    gpu.rows[1]["lower_raw"] = 100
    gpu.fail = "lower_raw"
    results = profiles.restore(gpu, state)
    assert not results[-1][0]
    assert gpu.calls == [(3, "upper_raw", 4000), (3, "lower_raw", 1000)]
    gpu.fail = None
    gpu.suppress = True
    assert not profiles.restore(gpu, state)[-1][0]


def test_partial_policy_reports_applied_field_before_failure():
    gpu = GPU()
    state = profiles.capture(gpu, {3: ["lower_raw", "upper_raw"]})
    state["power_integral"]["policies"][0]["values"] = {"lower_raw": 500, "upper_raw": 1500}
    gpu.rows[0].update(lower_raw=1000, upper_raw=2000)
    gpu.fail = "upper_raw"
    results = profiles.restore(gpu, state)
    assert results[0] == (True, "Power-integral policy 3 lower_raw applied: 500")
    assert results[1][0] is False and "write failed" in results[1][1]
    assert (gpu.rows[0]["lower_raw"], gpu.rows[0]["upper_raw"]) == (500, 2000)
