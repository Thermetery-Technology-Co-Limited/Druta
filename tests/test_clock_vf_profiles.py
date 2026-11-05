"""Offline scoped Clock VF profile behavior."""
import copy

import pytest

from druta import profiles
from tests.test_clock_vf_runtime import MONO, SMOOTH, FakeGPU, reject


def gpu():
    result = FakeGPU()
    result.static = {"name": "GPU", "uuid": "uuid-a", "slot": result.slot(), "driver": "old", "vbios": "old"}
    return result


def test_capture_excludes_opaque_state_and_restore_preserves_unselected():
    device = gpu()
    state = profiles.capture_clock_vf(device, selection=[MONO])
    assert set(state["clock_vf"]) == {"identity", "policies"}
    assert state["clock_vf"]["policies"] == {MONO: True}
    assert "header_hex" not in str(state) and "startupmode" not in str(state).lower()
    device.raw[10], device.raw[11] = 0, 0
    assert profiles.restore(device, state)[0][0]
    assert device.raw[10] == 1 and device.raw[11] == 0
    assert len(device.writes) == 1


def test_metadata_informational_uuid_and_adapter_checked():
    device = gpu()
    state = profiles.capture_clock_vf(device)
    device.static.update(driver="new", vbios="new", name="new")
    assert profiles.preflight(device, state) is None
    device.static["uuid"] = "uuid-b"
    assert "UUID" in profiles.preflight(device, state)
    device.static["uuid"] = "uuid-a"
    device.nvapi.selected["devid"] += 1
    device.nvml.selected["devid"] += 1
    assert profiles.preflight(device, state)


def test_invalid_or_opaque_fields_rejected_before_write():
    device = gpu()
    state = profiles.capture_clock_vf(device)
    state["clock_vf"]["policies"][SMOOTH] = 1
    assert not profiles.restore(device, state)[0][0]
    assert not device.writes
    state = profiles.capture_clock_vf(device)
    state["header_hex"] = "opaque"
    assert profiles.incomplete(state)
    assert not profiles.restore(device, state)[0][0]
    assert not device.writes


def test_restore_partial_failure_rolls_back_and_reports_failure():
    device = gpu()
    state = profiles.capture_clock_vf(device, selection=[MONO])
    device.raw[10] = 0
    original = bytes(device.raw)
    device.set_actions = [reject(0x40, partial=True)]
    result = profiles.restore(device, state)
    assert not result[0][0] and "restored and verified" in result[0][1]
    assert bytes(device.raw) == original and len(device.writes) == 2


def test_autosave_only_selected_fields(tmp_path, monkeypatch):
    device = gpu()
    state = profiles.capture_clock_vf(device, selection=[SMOOTH])
    monkeypatch.setattr(profiles, "DIR", str(tmp_path))
    name, _, missing = profiles.autosave_clock_vf(device, "test", state)
    saved = profiles.load(name)
    assert not missing and saved["clock_vf"]["policies"] == {SMOOTH: True}
    bad = copy.deepcopy(state)
    bad["clock_vf"]["identity"]["device_id"] += 1
    with pytest.raises(ValueError, match="identity"):
        profiles.autosave_clock_vf(device, "test", bad)
