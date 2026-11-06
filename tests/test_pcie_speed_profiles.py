import copy
import threading

import pytest

from druta import pcie_speed_profiles as profiles


class GPU:
    def __init__(self):
        self._lock = threading.RLock()
        self.static = {"name": "GPU", "driver": "driver", "vbios": "bios", "uuid": "uuid"}
        self.live = {"identity": {"slot": "slot", "architecture": 10, "device_id": 1, "subsystem_id": 2},
                     "available": True, "allowed_generations": [1, 2, 3, 4],
                     "target_generation": 1, "generation": 1, "root_generation": 1,
                     "target_register": 0x80011060, "width": 16}
        self.calls = []
        self.verification = "target_and_link"

    def slot(self):
        return "slot"


class Controller:
    def __init__(self, gpu):
        self.gpu = gpu

    def read(self):
        return copy.deepcopy(self.gpu.live)

    def set_generation(self, value, expected_identity=None):
        self.gpu.calls.append((value, expected_identity))
        state = self.gpu.live
        assert state["identity"] == expected_identity
        if self.gpu.verification == "target_and_link":
            state.update(target_generation=value, generation=value, root_generation=value)
        return {**self.read(), "requested_generation": value,
                "verification": self.gpu.verification, "status": "test status"}


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(profiles, "PcieSpeedController", Controller)


def test_capture_only_requested_state_and_validation():
    gpu = GPU()
    state = profiles.capture(gpu)
    assert state["pcie_speed"] == {"identity": gpu.live["identity"], "target_generation": 1}
    assert set(state["pcie_speed"]) == {"identity", "target_generation"}
    for bad in (True, 0, 6, 1.0, None):
        invalid = copy.deepcopy(state)
        invalid["pcie_speed"]["target_generation"] = bad
        with pytest.raises(ValueError):
            profiles.validate(invalid)
    invalid = copy.deepcopy(state)
    invalid["pcie_speed"]["target_register"] = 123
    with pytest.raises(ValueError):
        profiles.validate(invalid)
    gpu.live["allowed_generations"] = [2, 3]
    with pytest.raises(ValueError, match="capabilities"):
        profiles.capture(gpu)


def test_preflight_and_restore_verified_and_noop():
    gpu = GPU()
    state = profiles.capture(gpu)
    gpu.live.update(target_generation=2, generation=2, root_generation=2)
    assert profiles.preflight(gpu, state) is None
    assert profiles.restore(gpu, state)[0][0]
    assert gpu.calls == [(1, state["pcie_speed"]["identity"])]
    gpu.verification = "already_matches"
    assert profiles.restore(gpu, state)[0][0]


def test_unverified_and_identity_capability_failures():
    gpu = GPU()
    state = profiles.capture(gpu)
    gpu.live.update(target_generation=2, generation=2, root_generation=2)
    gpu.verification = "unverified"
    assert profiles.restore(gpu, state)[0][0] is False
    assert gpu.live["target_generation"] == 2
    gpu.calls.clear()
    gpu.live["identity"]["device_id"] = 123
    assert "identity" in profiles.preflight(gpu, state).lower()
    assert not profiles.restore(gpu, state)[0][0] and not gpu.calls
    gpu.live["identity"]["device_id"] = 1
    gpu.live["allowed_generations"] = [2, 3]
    assert "capabilities" in profiles.preflight(gpu, state).lower()
    assert not profiles.restore(gpu, state)[0][0] and not gpu.calls
    state["incomplete"] = ["missing"]
    assert "incomplete" in profiles.preflight(gpu, state)


def test_forged_success_cannot_verify_wrong_target():
    gpu = GPU()
    state = profiles.capture(gpu)
    gpu.live.update(target_generation=2, generation=2, root_generation=2)
    gpu.verification = "already_matches"
    assert not profiles.restore(gpu, state)[0][0]
