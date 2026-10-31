"""Hardware-free live fixed REL bounds and restoration tests."""
import copy

import pytest

from druta import vfe_equation_runtime as equations
from druta import voltage_rel_live as live
from tests import test_voltage_rel_runtime as fixtures

pytest_plugins = ["tests.test_voltage_rel_live"]


@pytest.fixture
def bounds(scenario, monkeypatch):
    scenario.state["inspection"]["fields"].extend(
        fixtures.inspection(scenario.state["voltage"], operation="value")["fields"]
    )
    scenario.before = copy.deepcopy(scenario.state)
    monkeypatch.setattr(equations, "write_voltage_bounds", scenario.writer)
    return scenario


def test_repeated_fixed_upper_and_restore_rom(bounds):
    c = bounds.controller
    row = c.set_fixed(0, 1100000)["rows"][0]
    assert (row["lower_uv"], row["upper_uv"], row["stored_lower_uv"], row["stored_upper_uv"]) == (
        1100000, 1100000, 900000, 1120000
    )
    assert c.set(0, 1110000)["rows"][0]["lower_uv"] == 1100000
    row = c.set_fixed(0, 1110000)["rows"][0]
    assert (row["lower_uv"], row["upper_uv"]) == (1110000, 1110000)
    row = c.restore_bounds(0)["rows"][0]
    assert (row["lower_uv"], row["upper_uv"]) == (900000, 1120000)
    assert bounds.calls == [False] * 4
    assert bounds.state["voltage"]["boost_percent"] == 37
    assert c.restore_bounds(0)["rows"][0]["status"] == "ready"
    assert bounds.calls == [False] * 4


def test_ineligible_rail_and_changed_identity_refused(bounds):
    with pytest.raises(live.VoltageRelLiveError) as exc:
        bounds.controller.set_fixed(1, 1050000)
    assert not exc.value.attempted and not bounds.calls
    bounds.state["inspection"]["fields"][1]["upper_clamp_binding"]["coefficients"][0]["value"] ^= 1
    with pytest.raises(live.VoltageRelLiveError) as exc:
        bounds.controller.set_fixed(0, 1050000)
    assert not exc.value.attempted and not bounds.calls


def test_entered_failure_compensates_once(bounds, monkeypatch):
    def fail(gpu, saved, *, restore=False, on_attempt=None):
        bounds.writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if not restore:
            raise RuntimeError("entered failure")

    monkeypatch.setattr(equations, "write_voltage_bounds", fail)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        bounds.controller.set_fixed(0, 1050000)
    assert exc.value.attempted and exc.value.rollback["verified"]
    assert bounds.calls == [False, True]


def test_restore_failure_retains_errors(bounds, monkeypatch):
    def fail(gpu, saved, *, restore=False, on_attempt=None):
        if on_attempt:
            on_attempt()
        bounds.calls.append(restore)
        raise RuntimeError("restore failed" if restore else "write failed")

    monkeypatch.setattr(equations, "write_voltage_bounds", fail)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        bounds.controller.set_fixed(0, 1050000)
    assert exc.value.attempted and not exc.value.rollback["verified"]
    assert "restore failed" in exc.value.rollback["errors"]
    assert bounds.calls == [False, True]

@pytest.mark.parametrize("peer", ["rail", "clock", "image", "unknown"])
def test_fixed_peer_change_compensates(bounds, monkeypatch, peer):
    def write(gpu, saved, *, restore=False, on_attempt=None):
        result = bounds.writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if restore:
            bounds.state.update(copy.deepcopy(bounds.before))
        elif peer == "rail":
            bounds.state["voltage"]["rail_control"]["rails"]["0"]["payload_hex"] = "00"
        elif peer == "clock":
            bounds.state["clocks"]["boost"] = "changed"
        elif peer == "image":
            bounds.state["image_sha256"] = "b" * 64
        else:
            data = bytearray.fromhex(bounds.state["voltage"]["equations"]["info_hex"])
            data[-1] ^= 1
            bounds.state["voltage"]["equations"]["info_hex"] = data.hex()
        return result

    monkeypatch.setattr(equations, "write_voltage_bounds", write)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        bounds.controller.set_fixed(0, 1050000)
    assert exc.value.attempted and exc.value.rollback["verified"]
    assert bounds.calls == [False, True]


def test_restore_bounds_failure_compensates_to_prior_loaded_bounds(bounds, monkeypatch):
    bounds.controller.set_fixed(0, 1050000)
    prior = copy.deepcopy(bounds.state)

    def write(gpu, saved, *, restore=False, on_attempt=None):
        result = bounds.writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if not restore:
            bounds.state["clocks"]["boost"] = "bad"
        else:
            bounds.state.update(copy.deepcopy(prior))
        return result

    monkeypatch.setattr(equations, "write_voltage_bounds", write)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        bounds.controller.restore_bounds(0)
    assert exc.value.attempted and exc.value.rollback["verified"]
    assert bounds.calls == [False, False, True]
    row = bounds.controller.read()["rows"][0]
    assert (row["lower_uv"], row["upper_uv"]) == (1050000, 1050000)


def test_independent_fixed_capability_and_alternate_identity(bounds):
    summary = bounds.controller.read()
    assert summary["identity"]["slot"] == "0000:91:10.4"
    assert summary["rows"][0]["fixed_status"] == "ready"
    assert summary["rows"][1]["fixed_status"] == "unsupported"
    bounds.state["inspection"]["fields"][1]["capability"] = "read-only"
    row = bounds.controller.read()["rows"][0]
    assert row["status"] == "ready" and row["fixed_status"] == "unsupported"

@pytest.mark.parametrize("missing", ["boost", "affected_rail"])
def test_restore_refuses_missing_prerequisite_before_dispatch(bounds, missing):
    bounds.controller.set_fixed(0, 1050000)
    bounds.calls.clear()
    if missing == "boost":
        bounds.state["voltage"]["boost_percent"] = None
    else:
        bounds.state["voltage"]["policy"]["rails"].pop("0")
    with pytest.raises(live.VoltageRelLiveError) as exc:
        bounds.controller.restore_bounds(0)
    assert not exc.value.attempted and not bounds.calls

def test_second_rail_can_be_fixed_independently(monkeypatch):
    import threading
    from types import SimpleNamespace

    obs = fixtures.observation(rail=1, partial=True, slot="0000:88:00.2", device_id=321)
    inspection = fixtures.inspection(obs, rail=1)
    inspection["fields"].extend(fixtures.inspection(obs, rail=1, operation="value")["fields"])
    state = {"voltage": obs, "inspection": inspection, "image_sha256": "a" * 64,
             "clocks": {"pstates": "p", "domains": "d", "boost": "b", "curve": "c"}}
    monkeypatch.setattr(live, "PerfDiagnosticsReader", lambda gpu: object())
    controller = live.VoltageRelController(SimpleNamespace(_lock=threading.RLock()))
    monkeypatch.setattr(controller, "_capture", lambda: copy.deepcopy(state))
    writes = []

    def write(gpu, saved, *, restore=False, on_attempt=None):
        if on_attempt:
            on_attempt()
        writes.append(restore)
        state["voltage"]["equations"] = copy.deepcopy(saved["before" if restore else "after"])

    monkeypatch.setattr(equations, "write_voltage_bounds", write)
    summary = controller.set_fixed(1, 1050000)
    assert summary["rows"][0]["fixed_status"] == "unsupported"
    assert summary["rows"][1]["fixed_status"] == "ready"
    assert summary["rows"][1]["lower_uv"] == 1050000 and writes == [False]
