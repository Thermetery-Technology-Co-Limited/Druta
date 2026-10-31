"""Offline controller tests: live REL binding, preservation and compensation."""

import copy
import threading
from types import SimpleNamespace

import pytest

from druta import vfe_equation_runtime as equations
from druta import voltage_rel_live as live
from tests import test_voltage_rel_runtime as fixtures


@pytest.fixture
def scenario(monkeypatch):
    obs = fixtures.observation(boost=37, slot="0000:91:10.4", device_id=0xB987)
    inspection = fixtures.inspection(obs)
    state = {
        "voltage": copy.deepcopy(obs),
        "inspection": inspection,
        "image_sha256": "a" * 64,
        "clocks": {"pstates": "p", "domains": "d", "boost": "b", "curve": "original"},
    }
    gpu = SimpleNamespace(_lock=threading.RLock())
    monkeypatch.setattr(live, "PerfDiagnosticsReader", lambda gpu: object())
    controller = live.VoltageRelController(gpu)
    calls = []
    monkeypatch.setattr(controller, "_capture", lambda: copy.deepcopy(state))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)

    def write(gpu, saved, *, restore=False, on_attempt=None):
        if on_attempt:
            on_attempt()
        calls.append(restore)
        state["voltage"]["equations"] = copy.deepcopy(saved["before" if restore else "after"])
        return state["voltage"]["equations"]

    monkeypatch.setattr(equations, "write_upper_clamps", write)
    return SimpleNamespace(controller=controller, state=state, calls=calls, writer=write, before=copy.deepcopy(state))


def test_repeated_edits_bind_loaded_upper_without_changing_stored_definition(scenario):
    c = scenario.controller
    first = c.set(0, 1115000)
    second = c.set(0, 1110000)
    assert first["rows"][0]["upper_uv"] == 1115000
    assert second["rows"][0]["upper_uv"] == 1110000
    assert second["rows"][0]["stored_upper_uv"] == 1120000
    assert scenario.calls == [False, False]
    assert scenario.state["voltage"]["boost_percent"] == 37


def test_partial_capability_and_alternate_identity_remain_independent(scenario):
    summary = scenario.controller.read()
    assert summary["rows"][0]["status"] == "ready"
    assert summary["rows"][1]["status"] == "unsupported"
    assert summary["identity"] == scenario.before["voltage"]["equations"]["identity"]
    assert summary["identity"]["slot"] == "0000:91:10.4"


def test_noop_never_dispatches(scenario):
    assert scenario.controller.set(0, 1120000)["rows"][0]["upper_uv"] == 1120000
    assert not scenario.calls


def test_changed_loaded_terms_refused_before_dispatch(scenario):
    field = scenario.state["inspection"]["fields"][0]
    field["upper_clamp_binding"]["coefficients"][0]["value"] ^= 1
    with pytest.raises(live.VoltageRelLiveError) as exc:
        scenario.controller.set(0, 1115000)
    assert not exc.value.attempted and not scenario.calls


@pytest.mark.parametrize("section", ["rail", "clock", "image", "unknown"])
def test_activation_peer_mismatch_compensates_once(scenario, monkeypatch, section):
    original_writer = scenario.writer

    def write(gpu, saved, *, restore=False, on_attempt=None):
        result = original_writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if restore:
            scenario.state.update(copy.deepcopy(scenario.before))
        elif section == "rail":
            scenario.state["voltage"]["policy"]["rails"]["1"]["policy_uv"]["overvoltage"] += 1
        elif section == "clock":
            scenario.state["clocks"]["boost"] = "changed"
        elif section == "image":
            scenario.state["image_sha256"] = "b" * 64
        else:
            raw = bytearray.fromhex(scenario.state["voltage"]["equations"]["control_hex"])
            raw[-1] ^= 1
            scenario.state["voltage"]["equations"]["control_hex"] = raw.hex()
        return result

    monkeypatch.setattr(equations, "write_upper_clamps", write)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        scenario.controller.set(0, 1115000)
    assert exc.value.attempted and exc.value.rollback["verified"]
    assert scenario.calls == [False, True]


def test_error_before_dispatch_never_compensates(scenario, monkeypatch):
    def reject(*args, **kwargs):
        raise RuntimeError("preflight refused")

    monkeypatch.setattr(equations, "write_upper_clamps", reject)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        scenario.controller.set(0, 1115000)
    assert not exc.value.attempted and exc.value.rollback is None and not scenario.calls


def test_dispatched_failure_compensates_once_and_curve_can_settle(scenario, monkeypatch):
    captures = 0
    original_capture = scenario.controller._capture
    original_writer = scenario.writer

    def capture():
        nonlocal captures
        captures += 1
        result = original_capture()
        if captures == 2:
            result["clocks"]["curve"] = "transient"
        return result

    def write(gpu, saved, *, restore=False, on_attempt=None):
        original_writer(gpu, saved, restore=restore, on_attempt=on_attempt)
        if not restore:
            raise RuntimeError("dispatched failure")

    monkeypatch.setattr(scenario.controller, "_capture", capture)
    monkeypatch.setattr(equations, "write_upper_clamps", write)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        scenario.controller.set(0, 1115000)
    assert exc.value.rollback["verified"] and scenario.calls == [False, True]
    assert captures == 3


def test_failed_compensation_is_reported_even_if_baseline_reads_match(scenario, monkeypatch):
    def write(gpu, saved, *, restore=False, on_attempt=None):
        if on_attempt:
            on_attempt()
        scenario.calls.append(restore)
        raise RuntimeError("restore failed" if restore else "apply failed")

    monkeypatch.setattr(equations, "write_upper_clamps", write)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        scenario.controller.set(0, 1115000)
    assert not exc.value.rollback["verified"] and exc.value.rollback["errors"]
    assert scenario.calls == [False, True]


def test_read_retries_transient_but_not_identity(scenario, monkeypatch):
    baseline = scenario.controller._capture
    attempts = []

    def capture():
        attempts.append(1)
        if len(attempts) == 1:
            raise live.VoltageRelLiveError("transient", "temporary")
        return baseline()

    monkeypatch.setattr(scenario.controller, "_capture", capture)
    assert scenario.controller.read()["rows"][0]["status"] == "ready" and len(attempts) == 2
    attempts.clear()

    def mismatch():
        attempts.append(1)
        raise live.VoltageRelLiveError("identity", "wrong adapter")

    monkeypatch.setattr(scenario.controller, "_capture", mismatch)
    with pytest.raises(live.VoltageRelLiveError, match="wrong adapter"):
        scenario.controller.read()
    assert len(attempts) == 1


def test_binding_rejects_changed_adapter_after_first_capture(scenario, monkeypatch):
    identity = copy.deepcopy(scenario.before["voltage"]["equations"]["identity"])
    monkeypatch.setattr(live.clock, "_binding", lambda reader: identity)
    assert scenario.controller._binding() == identity
    identity = {**identity, "device_id": identity["device_id"] + 1}
    with pytest.raises(live.VoltageRelLiveError) as exc:
        scenario.controller._binding()
    assert exc.value.kind == "identity"


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interruption_after_dispatch_compensates_and_propagates(scenario, monkeypatch, interrupt):
    original = scenario.writer

    def write(gpu, saved, *, restore=False, on_attempt=None):
        original(gpu, saved, restore=restore, on_attempt=on_attempt)
        if not restore:
            raise interrupt("interrupted")

    monkeypatch.setattr(equations, "write_upper_clamps", write)
    with pytest.raises(interrupt) as exc:
        scenario.controller.set(0, 1115000)
    assert exc.value.rollback["verified"] and scenario.calls == [False, True]


def test_set_retries_transient_capture_without_repeating_write(scenario, monkeypatch):
    original = scenario.controller._capture
    calls = []

    def capture():
        calls.append(1)
        if len(calls) in (1, 3):
            raise live.VoltageRelLiveError("transient", "temporary")
        return original()

    monkeypatch.setattr(scenario.controller, "_capture", capture)
    assert scenario.controller.set(0, 1115000)["rows"][0]["upper_uv"] == 1115000
    assert len(calls) == 4 and scenario.calls == [False]


def test_optional_clock_getters_report_absence_but_present_failure_is_transient():
    api = SimpleNamespace(Pstates20Get=None, VfpCurve=None, BoostTableGet=None)
    gpu = SimpleNamespace(
        nvapi=api,
        _read_pstates20=lambda: (None, "unavailable"),
        clkdom_layout=lambda: None,
        clkdom_domains=lambda: (),
        vfp_layout=lambda: None,
        vf_curve_applicable=lambda: True,
    )
    assert live._clocks(gpu) == {"pstates": None, "domains": None, "boost": None, "curve": None}
    api.Pstates20Get = object()
    with pytest.raises(live.VoltageRelLiveError) as exc:
        live._clocks(gpu)
    assert exc.value.kind == "transient"
    api.Pstates20Get = None
    api.VfpCurve = object()
    with pytest.raises(live.VoltageRelLiveError) as exc:
        live._clocks(gpu)
    assert exc.value.kind == "transient"


def test_summary_exposes_unavailable_clock_peers(scenario):
    scenario.state["clocks"]["boost"] = None
    scenario.state["clocks"]["curve"] = None
    summary = scenario.controller.read()
    assert summary["rows"][0]["status"] == "ready"
    assert summary["unavailable_peers"] == ["boost", "curve"]


def test_binding_preserves_typed_transient_error(scenario, monkeypatch):
    def failed(reader):
        raise live.VoltageRelLiveError("transient", "temporary pairing read")

    monkeypatch.setattr(live.clock, "_binding", failed)
    with pytest.raises(live.VoltageRelLiveError) as exc:
        scenario.controller._binding()
    assert exc.value.kind == "transient"
