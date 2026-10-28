"""Offline live constant controller tests."""

import copy
import threading
from types import SimpleNamespace

import pytest

from druta import vfe_equation_runtime as equations
from druta import voltage_constants_live as live
from tests import test_voltage_ceiling_runtime as fixtures


@pytest.fixture(params=["OV", "ALT"])
def scenario(monkeypatch, request):
    role = request.param
    obs = fixtures.observation(role, boost=37, slot="0000:91:10.4", device_id=0xB987)
    state = {
        "voltage": copy.deepcopy(obs),
        "inspection": fixtures.inspection(role),
        "image_sha256": "a" * 64,
        "clocks": {"pstates": "p", "domains": "d", "boost": "b", "curve": "original"},
    }
    gpu = SimpleNamespace(_lock=threading.RLock())
    monkeypatch.setattr(live, "PerfDiagnosticsReader", lambda gpu: object())
    c = live.VoltageConstantsController(gpu)
    monkeypatch.setattr(c, "_capture", lambda: copy.deepcopy(state))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    calls = []

    def write(gpu, saved, *, restore=False, on_attempt=None):
        if on_attempt:
            on_attempt()
        calls.append(restore)
        selection = next(iter(saved["selected"].values()))
        delta = selection["after_uv"] - selection["before_uv"]
        if restore:
            delta = -delta
        state["voltage"]["equations"] = copy.deepcopy(saved["before" if restore else "after"])
        name = "alt_reliability" if role == "ALT" else "overvoltage"
        state["voltage"]["policy"]["rails"]["0"]["policy_uv"][name] += delta

    monkeypatch.setattr(equations, "write_constants", write, raising=False)
    return SimpleNamespace(controller=c, state=state, role=role, calls=calls, writer=write, before=copy.deepcopy(state))


def row(summary, role):
    return next(r for r in summary["rows"] if r["rail"] == 0 and r["role"] == role)


def test_repeated_edits_preserve_stored_definition_and_boost(scenario):
    c, role = scenario.controller, scenario.role
    original = row(c.read(), role)["value_uv"]
    assert row(c.set(0, role, original - 5000), role)["value_uv"] == original - 5000
    result = row(c.set(0, role, original - 10000), role)
    assert result["value_uv"] == original - 10000 and result["stored_value_uv"] == original
    assert scenario.calls == [False, False] and scenario.state["voltage"]["boost_percent"] == 37


def test_independent_capability_and_noop(scenario):
    s = scenario.controller.read()
    assert row(s, scenario.role)["status"] == "ready"
    assert next(r for r in s["rows"] if r["rail"] == 1 and r["role"] == scenario.role)["status"] == "unsupported"
    assert s["identity"]["slot"] == "0000:91:10.4"
    scenario.controller.set(0, scenario.role, row(s, scenario.role)["value_uv"])
    assert not scenario.calls


def test_missing_boost_disables_only_alt(scenario):
    scenario.state["voltage"]["boost_percent"] = None
    status = row(scenario.controller.read(), scenario.role)["status"]
    assert status == ("unsupported" if scenario.role == "ALT" else "ready")


@pytest.mark.parametrize("section", ["rail", "clock", "image", "unknown"])
def test_peer_mismatch_compensates(scenario, monkeypatch, section):
    def write(gpu, saved, *, restore=False, on_attempt=None):
        scenario.writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if restore:
            scenario.state.update(copy.deepcopy(scenario.before))
        elif section == "rail":
            scenario.state["voltage"]["rail_control"]["rails"]["0"]["payload_hex"] = "00"
        elif section == "clock":
            scenario.state["clocks"]["boost"] = "changed"
        elif section == "image":
            scenario.state["image_sha256"] = "b" * 64
        else:
            raw = bytearray.fromhex(scenario.state["voltage"]["equations"]["control_hex"])
            raw[-1] ^= 1
            scenario.state["voltage"]["equations"]["control_hex"] = raw.hex()

    monkeypatch.setattr(equations, "write_constants", write)
    val = row(scenario.controller.read(), scenario.role)["value_uv"]
    with pytest.raises(live.VoltageConstantsLiveError) as exc:
        scenario.controller.set(0, scenario.role, val - 5000)
    assert exc.value.attempted and exc.value.rollback["verified"] and scenario.calls == [False, True]


def test_undispatched_failure_has_no_compensation(scenario, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("preflight")

    monkeypatch.setattr(equations, "write_constants", fail)
    val = row(scenario.controller.read(), scenario.role)["value_uv"]
    with pytest.raises(live.VoltageConstantsLiveError) as exc:
        scenario.controller.set(0, scenario.role, val - 5000)
    assert not exc.value.attempted and exc.value.rollback is None and not scenario.calls


def test_entered_failure_settles_curve_and_restores(scenario, monkeypatch):
    count = 0
    capture = scenario.controller._capture

    def reading():
        nonlocal count
        count += 1
        state = capture()
        if count == 2:
            state["clocks"]["curve"] = "transient"
        return state

    def write(gpu, saved, *, restore=False, on_attempt=None):
        scenario.writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if not restore:
            raise RuntimeError("failed after dispatch")

    monkeypatch.setattr(scenario.controller, "_capture", reading)
    monkeypatch.setattr(equations, "write_constants", write)
    val = row(scenario.controller.read(), scenario.role)["value_uv"]
    count = 0
    with pytest.raises(live.VoltageConstantsLiveError) as exc:
        scenario.controller.set(0, scenario.role, val - 5000)
    assert exc.value.rollback["verified"] and scenario.calls == [False, True] and count == 3


def test_transient_retry_and_identity_error(scenario, monkeypatch):
    original = scenario.controller._capture
    calls = []

    def capture():
        calls.append(1)
        if len(calls) == 1:
            raise live.VoltageConstantsLiveError("transient", "temporary")
        return original()

    monkeypatch.setattr(scenario.controller, "_capture", capture)
    assert row(scenario.controller.read(), scenario.role)["status"] == "ready" and len(calls) == 2

    def bad():
        raise live.VoltageConstantsLiveError("identity", "wrong adapter")

    monkeypatch.setattr(scenario.controller, "_capture", bad)
    with pytest.raises(live.VoltageConstantsLiveError, match="wrong adapter"):
        scenario.controller.read()


def test_other_rail_can_bind_independently(monkeypatch):
    obs = fixtures.observation("OV", rail=1, boost=None)
    state = {
        "voltage": obs,
        "inspection": fixtures.inspection("OV", rail=1),
        "image_sha256": "a" * 64,
        "clocks": {"pstates": None, "domains": "d", "boost": None, "curve": None},
    }
    monkeypatch.setattr(live, "PerfDiagnosticsReader", lambda gpu: object())
    controller = live.VoltageConstantsController(SimpleNamespace(_lock=threading.RLock()))
    monkeypatch.setattr(controller, "_capture", lambda: copy.deepcopy(state))
    summary = controller.read()
    assert next(r for r in summary["rows"] if r["rail"] == 1 and r["role"] == "OV")["status"] == "ready"
    assert next(r for r in summary["rows"] if r["rail"] == 0 and r["role"] == "OV")["status"] == "unsupported"
    assert summary["unavailable_peers"] == ["pstates", "boost", "curve"]


def test_loaded_identity_mismatch_and_higher_terms_are_rejected(scenario):
    field = scenario.state["inspection"]["fields"][0]
    field["variable_index"] += 1
    with pytest.raises(live.VoltageConstantsLiveError) as exc:
        scenario.controller.set(0, scenario.role, field["current"] - 5000)
    assert not exc.value.attempted and not scenario.calls


def test_restore_failure_is_not_masked_by_exact_readback(scenario, monkeypatch):
    def fail(gpu, saved, *, restore=False, on_attempt=None):
        if on_attempt:
            on_attempt()
        scenario.calls.append(restore)
        raise RuntimeError("restore failed" if restore else "apply failed")

    monkeypatch.setattr(equations, "write_constants", fail)
    val = row(scenario.controller.read(), scenario.role)["value_uv"]
    with pytest.raises(live.VoltageConstantsLiveError) as exc:
        scenario.controller.set(0, scenario.role, val - 5000)
    assert not exc.value.rollback["verified"] and exc.value.rollback["errors"]
    assert scenario.calls == [False, True]


def test_capture_failure_before_dispatch_is_transient(scenario, monkeypatch):
    def fail():
        raise OSError("getter failed")

    monkeypatch.setattr(scenario.controller, "_capture", fail)
    with pytest.raises(live.VoltageConstantsLiveError) as exc:
        scenario.controller.set(0, scenario.role, 15000 if scenario.role == "ALT" else 1195000)
    assert exc.value.kind == "transient" and not exc.value.attempted and not scenario.calls


def test_keyboard_interrupt_still_compensates(scenario, monkeypatch):
    def write(gpu, saved, *, restore=False, on_attempt=None):
        scenario.writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if not restore:
            raise KeyboardInterrupt()

    monkeypatch.setattr(equations, "write_constants", write)
    val = row(scenario.controller.read(), scenario.role)["value_uv"]
    with pytest.raises(KeyboardInterrupt) as exc:
        scenario.controller.set(0, scenario.role, val - 5000)
    assert exc.value.rollback["verified"] and scenario.calls == [False, True]


def test_nonzero_higher_coefficient_prevents_dispatch(scenario):
    import struct

    raw = bytearray.fromhex(scenario.state["voltage"]["equations"]["control_hex"])
    offset = equations.HEADER + 37 * equations.CONTROL_STRIDE + 0x14
    struct.pack_into("<f", raw, offset, 1.0)
    scenario.state["voltage"]["equations"]["control_hex"] = raw.hex()
    value = scenario.state["inspection"]["fields"][0]["current"]
    with pytest.raises(live.VoltageConstantsLiveError) as exc:
        scenario.controller.set(0, scenario.role, value - 5000)
    assert not exc.value.attempted and not scenario.calls
