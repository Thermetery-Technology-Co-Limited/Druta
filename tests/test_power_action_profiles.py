from copy import deepcopy
from threading import RLock

import pytest

from druta import power_action_profiles as power
from druta import profiles


def row(p, value, *, board=False, named=False, writable=True):
    return {"policy": p, "type": 1, "channel": p, "unit": "mW" if board else "mA",
            "minimum": 1, "maximum": 1000, "requested": value, "board": board,
            "named_current": named, "writable": writable, "consistent": True}


class GPU:
    def __init__(self):
        self.board = 100
        self._lock = RLock()
        self.static = {"name": "GPU", "vbios": "bios-a", "driver": "driver-a", "uuid": "uuid-a"}
        self._slot = "0000:01:00.0"
        self.rows = [row(0, 100, board=True, writable=False), row(1, 50),
                     row(2, 60, named=True, writable=False), row(3, 70, writable=False)]
        self._power_policy_pins = {1: 50, 2: 60}
        self.calls = []
        self.fail = False

    def slot(self):
        return self._slot

    def read_power_limit_mw(self):
        return self.board

    def read_power_policies(self):
        return deepcopy(self.rows), None

    def set_power_limit_mw(self, value):
        self.calls.append(("board", value))
        self.board = value
        self.rows[0]["requested"] = value
        self.rows[1]["requested"] = 80
        self.rows[2]["requested"] = 90
        return True, "board set"

    def set_power_policy(self, p, value, *, pin, allow_named):
        self.calls.append((p, value, pin, allow_named))
        if self.fail and p == 1:
            return False, "failed after dispatch"
        self.rows[p]["requested"] = value
        return True, "set"


def envelope(data):
    return {"schema": 2, "scope": "power_action", "power_action": data}


def test_restore_board_coupling_and_original_pins():
    gpu = GPU()
    saved = power.capture(gpu)
    gpu.set_power_limit_mw(200)
    gpu._power_policy_pins = {}
    gpu.calls = []
    result = power.restore(gpu, power.validate(envelope(saved)))
    assert all(ok for ok, _ in result)
    assert gpu.board == 100
    assert [r["requested"] for r in gpu.rows] == [100, 50, 60, 70]
    assert gpu._power_policy_pins == {1: 50, 2: 60}
    assert gpu.calls == [("board", 100), (1, 50, False, False), (2, 60, False, True)]


def test_unchanged_requests_reinstate_removed_pins_without_writes():
    gpu = GPU()
    saved = power.capture(gpu)
    gpu._power_policy_pins = {}
    assert all(ok for ok, _ in power.restore(gpu, saved))
    assert gpu.calls == []
    assert gpu._power_policy_pins == {1: 50, 2: 60}


def test_failed_write_does_not_reinstate_unverified_pin():
    gpu = GPU()
    saved = power.capture(gpu)
    gpu.rows[1]["requested"] = 90
    gpu._power_policy_pins = {1: 90}
    gpu.fail = True
    result = power.restore(gpu, saved)
    assert not all(ok for ok, _ in result)
    assert gpu._power_policy_pins == {2: 60}
    assert any("does not match" in msg for _, msg in result)


def test_readonly_collateral_is_reported():
    gpu = GPU()
    saved = power.capture(gpu)
    gpu.rows[3]["requested"] = 71
    result = power.restore(gpu, saved)
    assert any(not ok and "policy 3" in msg for ok, msg in result)


@pytest.mark.parametrize("field,value", [("channel", 9), ("type", 2), ("maximum", 999)])
def test_changed_geometry_prevents_any_write(field, value):
    gpu = GPU()
    saved = power.capture(gpu)
    gpu.rows[1][field] = value
    result = power.restore(gpu, saved)
    assert not result[0][0]
    assert not gpu.calls
    assert gpu._power_policy_pins == {1: 50, 2: 60}


def test_nonrepresentable_original_prevents_write():
    gpu = GPU()
    gpu.rows[1]["requested"] = 1001
    saved = power.capture(gpu)
    gpu.rows[1]["requested"] = 50
    assert not power.restore(gpu, saved)[0][0]
    assert not gpu.calls


def test_reject_opaque_data():
    gpu = GPU()
    saved = power.capture(gpu)
    saved["rows"][0]["packet"] = "abcd"
    with pytest.raises(ValueError):
        power.validate(envelope(saved))


def test_peer_move_after_restore_is_detected():
    gpu = GPU()
    saved = power.capture(gpu)
    gpu.rows[1]["requested"] = 80
    gpu.rows[2]["requested"] = 90
    original = gpu.set_power_policy
    def coupled(p, value, **kwargs):
        result = original(p, value, **kwargs)
        if p == 2:
            gpu.rows[1]["requested"] = 81
        return result
    gpu.set_power_policy = coupled
    result = power.restore(gpu, saved)
    assert any(not ok and "policy 1 request does not match" in msg for ok, msg in result)
    assert 1 not in gpu._power_policy_pins

class BoardOnlyGPU:
    class NV:
        ok = False
    nvml = NV()

    def __init__(self, *, legacy=False):
        self.board = 100
        self.legacy = legacy
        self._power_policy_pins = {}

    def read_power_limit_mw(self):
        return self.board

    def read_power_policies(self):
        return [], "not supported"

    def read_live_power_request(self):
        from druta import client_power_runtime as runtime
        binding = {"client_index": 0, "native_index": 0, "semantic_role": runtime.ROLE,
                   "client": runtime.CLIENT, "units": "mW"}
        if self.legacy:
            binding["layout"] = "legacy32"
        return {"schema": 1, "stable_reads": 2, "binding": binding,
                "info_hex": "00" * runtime.SIZES[runtime.INFO],
                "control_hex": "00" * runtime.SIZES[runtime.CONTROL],
                "native_info_hex": "00" * runtime.SIZES[runtime.NATIVE_INFO],
                "native_control_hex": "00" * runtime.SIZES[runtime.NATIVE_CONTROL]}, None

    def set_power_limit_mw(self, value):
        self.board = value
        return True, "set"


def test_board_only_requires_nonlegacy_fe_path():
    gpu = BoardOnlyGPU()
    saved = power.capture(gpu)
    assert saved["rows"] is None
    gpu.board = 200
    assert all(ok for ok, _ in power.restore(gpu, saved))
    assert gpu.board == 100
    with pytest.raises(ValueError):
        power.capture(BoardOnlyGPU(legacy=True))
    gpu._power_policy_pins = {1: 5}
    with pytest.raises(ValueError):
        power.capture(gpu)


def test_board_only_refuses_nvml_route():
    gpu = BoardOnlyGPU()
    class NV:
        ok = True
        def has(self, name):
            return True
    gpu.nvml = NV()
    gpu._read_power_limit_nvml = lambda: 100
    with pytest.raises(ValueError):
        power.capture(gpu)


def test_pin_intent_can_differ_from_captured_request():
    gpu = GPU()
    gpu._power_policy_pins[1] = 45
    saved = power.capture(gpu)
    gpu.rows[1]["requested"] = 80
    gpu._power_policy_pins = {}
    result = power.restore(gpu, saved)
    assert all(ok for ok, _ in result)
    assert gpu.rows[1]["requested"] == 50
    assert gpu._power_policy_pins == {1: 45, 2: 60}


def test_board_only_rejects_unknown_observation_shape():
    gpu = BoardOnlyGPU()
    gpu.read_live_power_request = lambda: ({"binding": {"native_index": 0}}, None)
    with pytest.raises(ValueError):
        power.capture(gpu)


def test_profile_capture_autosave_and_scoped_restore(tmp_path, monkeypatch):
    gpu = GPU()
    template = profiles.capture_power_action(gpu)
    assert template["scope"] == "power_action"
    assert profiles.incomplete(template) == []
    monkeypatch.setattr(profiles, "DIR", str(tmp_path))
    name, _, missing = profiles.autosave_power_action(gpu, "before-power", template)
    saved = profiles.load(name)
    assert not missing and saved["power_action"] == template["power_action"]
    gpu.set_power_limit_mw(200)
    gpu._power_policy_pins = {}
    gpu.calls = []
    result = profiles.restore(gpu, saved)
    assert all(ok for ok, _ in result)
    assert gpu.board == 100 and [r["requested"] for r in gpu.rows] == [100, 50, 60, 70]
    assert gpu._power_policy_pins == {1: 50, 2: 60}
    # The fake has no full-tune writers. Any attempted unrelated write would
    # produce an error, and only board/policy calls are observed.
    assert gpu.calls == [("board", 100), (1, 50, False, False), (2, 60, False, True)]


@pytest.mark.parametrize("change", ["uuid", "slot", "opaque_top", "opaque_row"])
def test_profile_preflight_rejects_wrong_device_or_opaque_fields(change):
    gpu = GPU()
    state = profiles.capture_power_action(gpu)
    if change == "uuid":
        gpu.static["uuid"] = "uuid-b"
    elif change == "slot":
        gpu._slot = "0000:02:00.0"
    elif change == "opaque_top":
        state["native_control_hex"] = "00"
    else:
        state["power_action"]["rows"][0]["opaque"] = "00"
    assert profiles.preflight(gpu, state)
    result = profiles.restore(gpu, state)
    assert not result[0][0] and not gpu.calls
    with pytest.raises(ValueError):
        profiles.autosave_power_action(gpu, "unsafe", state)


def test_profile_driver_is_informational():
    gpu = GPU()
    state = profiles.capture_power_action(gpu)
    gpu.static.update(driver="driver-b", vbios="bios-b", name="renamed")
    gpu.rows[1]["requested"] = 80
    assert profiles.preflight(gpu, state) is None
    assert all(ok for ok, _ in profiles.restore(gpu, state))
    assert gpu.rows[1]["requested"] == 50
