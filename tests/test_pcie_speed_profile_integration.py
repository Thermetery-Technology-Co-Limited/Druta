import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import combined_profiles, pcie_speed_profiles, profiles
from tests.test_pcie_speed_profiles import GPU, Controller


@pytest.fixture
def gpu(monkeypatch):
    monkeypatch.setattr(pcie_speed_profiles, "PcieSpeedController", Controller)
    return GPU()


def test_save_load_dispatch_and_selected_undo(gpu, monkeypatch, tmp_path):
    state = profiles.capture_pcie_speed(gpu)
    monkeypatch.setattr(profiles, "DIR", str(tmp_path))
    profiles.save("pcie", state)
    assert profiles.load("pcie") == state and profiles.incomplete(state) == []
    assert "Gen1" in profiles.summarize(state)
    gpu.live.update(target_generation=2, generation=2, root_generation=2)
    name, _, missing = profiles.autosave_pcie_speed(gpu, "speed", state)
    undo = profiles.load(name)
    assert not missing and undo["pcie_speed"]["target_generation"] == 2
    assert set(undo["pcie_speed"]) == {"identity", "target_generation"}
    assert all(ok for ok, _ in profiles.restore(gpu, state))
    assert gpu.calls == [(1, state["pcie_speed"]["identity"])]


def test_combined_duplicate_preflight_and_undo(gpu):
    state = profiles.capture_pcie_speed(gpu)
    with pytest.raises(ValueError, match="overlapping"):
        combined_profiles.compose([("one", state), ("two", copy.deepcopy(state))])
    bundle = combined_profiles.compose([("speed", state)])
    app = SimpleNamespace(gpu=gpu, rail=None, i2c_verified=lambda: False)
    gpu.live.update(target_generation=2, generation=2, root_generation=2)
    assert combined_profiles.preflight(app, bundle) is None
    undo = combined_profiles.capture_undo(app, bundle)
    assert undo["components"][0]["state"]["pcie_speed"]["target_generation"] == 2
    assert all(ok for ok, _ in combined_profiles.restore(app, bundle))
    assert gpu.live["target_generation"] == 1


def test_combined_preflight_stops_without_set(gpu, monkeypatch):
    state = profiles.capture_pcie_speed(gpu)
    bundle = combined_profiles.compose([("speed", state)])
    gpu.live["available"] = False
    app = SimpleNamespace(gpu=gpu, rail=None, i2c_verified=lambda: False)
    write = Mock()
    monkeypatch.setattr(profiles, "restore", write)
    assert combined_profiles.preflight(app, bundle)
    assert not combined_profiles.restore(app, bundle)[0][0]
    write.assert_not_called()


def test_mixed_profile_preflights_all_targets_and_undo_keeps_both_scopes(gpu):
    gpu.nvapi = SimpleNamespace(ok=True, VoltCtrlGet=True, VoltCtrlSet=True)
    gpu.boost = 10
    boost_calls = []
    gpu.read_voltage_boost = lambda: gpu.boost

    def set_boost(value):
        boost_calls.append(value)
        gpu.boost = value
        return True, "boost applied"

    gpu.set_voltage_boost = set_boost
    boost = profiles.capture_tuning_action(gpu, "boost")
    gpu.live.update(target_generation=2, generation=2, root_generation=2)
    speed = profiles.capture_pcie_speed(gpu)
    bundle = combined_profiles.compose([("link", speed), ("boost", boost)])
    gpu.boost = 0
    gpu.live.update(target_generation=1, generation=1, root_generation=1, allowed_generations=[1])
    app = SimpleNamespace(gpu=gpu, rail=None, i2c_verified=lambda: False)
    assert not combined_profiles.restore(app, bundle)[0][0]
    assert boost_calls == [] and gpu.calls == []
    gpu.live["allowed_generations"] = [1, 2, 3, 4]
    undo = combined_profiles.capture_undo(app, bundle)
    saved = {child["state"]["scope"]: child["state"] for child in undo["components"]}
    assert saved["tuning_action"]["tuning_action"]["value"] == 0
    assert saved["pcie_speed"]["pcie_speed"]["target_generation"] == 1
    assert all(ok for ok, _ in combined_profiles.restore(app, bundle))
    assert gpu.boost == 10 and gpu.live["target_generation"] == 2
    assert all(ok for ok, _ in combined_profiles.restore(app, undo))
    assert gpu.boost == 0 and gpu.live["target_generation"] == 1
    assert boost_calls == [10, 0]
