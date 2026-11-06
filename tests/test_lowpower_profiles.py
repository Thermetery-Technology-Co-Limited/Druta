from threading import RLock
from types import SimpleNamespace

import pytest

from druta import lowpower_profiles as lp

IDENTITY = {"slot": "0000:01:00.0", "architecture": 0x170, "device_id": 1, "subsystem_id": 2}


class Controller:
    def __init__(self, gpu, mode="released"):
        self.gpu, self.identity, self.mode = gpu, dict(IDENTITY), mode
        self.uncertain = False
        self.calls = []
        self.fail_hold = False

    def read(self):
        self.calls.append("read")
        return {
            "owned_request": self.mode != "released",
            "owned_mode": None if self.mode == "released" else self.mode,
            "uncertain_outcome": self.uncertain,
            "reason_masks": {1: 123},
            "handle": 123,
        }

    def release(self):
        self.calls.append("release")
        self.mode, self.uncertain = "released", False
        return self.read()

    def hold(self):
        self.calls.append("hold")
        if self.fail_hold:
            raise RuntimeError("acquire failed")
        self.mode = "disabled"
        return self.read()

    def hold_enabled(self):
        self.calls.append("hold_enabled")
        self.mode = "enabled"
        return self.read()


@pytest.fixture
def setup_app(monkeypatch):
    gpu = SimpleNamespace(
        _lock=RLock(), static={"uuid": "one", "slot": IDENTITY["slot"]}, slot=lambda: IDENTITY["slot"]
    )
    ctl = Controller(gpu)
    logs, saved = [], []
    app = SimpleNamespace(gpu=gpu, _engine_clock_gating_controllers={id(gpu): ctl}, log=lambda *args: logs.append(args))
    monkeypatch.setattr(
        lp.profiles, "autosave_state", lambda action, state: saved.append(state) or ("undo", "path", [])
    )
    return app, ctl, logs, saved


def test_capture_only_owned_intent_and_reuses_controller(setup_app):
    app, ctl, _, _ = setup_app
    ctl.mode = "enabled"
    state = lp.capture(app, "elcg")
    assert lp.controller(app, "elcg") is ctl
    assert state["lowpower_requests"] == {"kind": "elcg", "index": None, "identity": IDENTITY, "mode": "enabled"}
    assert "reason_masks" not in str(state) and "handle" not in str(state)
    assert lp.preflight(app, state) is None


def test_manual_change_saves_previous_state_and_skips_noop(setup_app):
    app, ctl, _, saved = setup_app
    lp.change(app, "elcg", "disabled")
    assert saved[0]["lowpower_requests"]["mode"] == "released"
    assert ctl.mode == "disabled"
    lp.change(app, "elcg", "disabled")
    assert len(saved) == 1
    lp.change(app, "elcg", "enabled")
    assert saved[-1]["lowpower_requests"]["mode"] == "disabled"
    assert ctl.calls.index("release") < ctl.calls.index("hold_enabled")


def test_restore_failure_reports_partial_transition(setup_app):
    app, ctl, _, _ = setup_app
    state = lp.capture(app, "elcg")
    state["lowpower_requests"]["mode"] = "disabled"
    ctl.mode, ctl.fail_hold = "enabled", True
    result = lp.restore(app, state)
    assert not result[0][0] and "state may have changed" in result[0][1]
    assert ctl.mode == "released"


def test_uncertain_release_remains_available_without_undo(setup_app):
    app, ctl, logs, saved = setup_app
    ctl.mode, ctl.uncertain = "disabled", True
    lp.change(app, "elcg", "released")
    assert ctl.mode == "released" and not saved
    assert "without a verified undo" in logs[-1][0]


def test_capture_or_save_failure_blocks_acquire_but_allows_release(setup_app, monkeypatch):
    app, ctl, logs, _ = setup_app

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(lp.profiles, "autosave_state", fail)
    with pytest.raises(OSError):
        lp.change(app, "elcg", "disabled")
    assert ctl.mode == "released"
    ctl.mode = "enabled"
    lp.change(app, "elcg", "released")
    assert ctl.mode == "released" and "without a verified undo" in logs[-1][0]


def test_identity_and_schema_rejections(setup_app):
    app, ctl, _, _ = setup_app
    state = lp.capture(app, "elcg")
    ctl.identity["device_id"] += 1
    assert "identity changed" in lp.preflight(app, state)
    ctl.identity["device_id"] -= 1
    state["lowpower_requests"]["handle"] = 123
    with pytest.raises(ValueError):
        lp.validate(state)


def test_index_and_modes_are_strict(setup_app):
    app, _, _, _ = setup_app
    with pytest.raises(ValueError):
        lp.controller(app, "individual", True)
    with pytest.raises(ValueError):
        lp.change(app, "difr", "enabled")


@pytest.mark.parametrize(
    "kind,attr,module_name,class_name,index",
    [
        ("difr", "_difr_live_controllers", "difr_live_control", "DifrController", None),
        ("elpg", "_gr_gating_live_controllers", "gr_gating_live", "GrGatingController", None),
        ("elcg", "_engine_clock_gating_controllers", "engine_clock_gating_live", "EngineClockGatingController", None),
        ("blcg", "_block_clock_gating_controllers", "block_clock_gating_live", "BlockClockGatingController", None),
        (
            "slcg",
            "_second_level_clock_gating_controllers",
            "second_level_clock_gating_live",
            "SecondLevelClockGatingController",
            None,
        ),
        (
            "individual",
            "_individual_power_gating_controllers",
            "individual_power_gating_live",
            "IndividualPowerGatingController",
            12,
        ),
    ],
)
def test_all_factories_cache_selected_controller(monkeypatch, setup_app, kind, attr, module_name, class_name, index):
    import importlib

    app, _, _, _ = setup_app
    app.__dict__.pop(attr, None)
    module = importlib.import_module("druta." + module_name)
    made = []

    def factory(gpu, *args):
        ctl = Controller(gpu)
        made.append((ctl, args))
        return ctl

    monkeypatch.setattr(module, class_name, factory)
    ctl = lp.controller(app, kind, index)
    assert lp.controller(app, kind, index) is ctl
    assert made == [(ctl, (index,) if index is not None else ())]
    key = (id(app.gpu), index) if index is not None else id(app.gpu)
    assert getattr(app, attr)[key] is ctl
    state = lp.capture(app, kind, index)
    assert state["lowpower_requests"]["mode"] == "released"
    lp.change(app, kind, "disabled", index)
    assert lp.capture(app, kind, index)["lowpower_requests"]["mode"] == "disabled"
    if kind in ("elcg", "slcg"):
        lp.change(app, kind, "enabled", index)
        assert lp.capture(app, kind, index)["lowpower_requests"]["mode"] == "enabled"
    lp.change(app, kind, "released", index)
    assert ctl.mode == "released"


def test_preflight_rejects_incomplete_and_changed_uuid(setup_app):
    app, _, _, _ = setup_app
    state = lp.capture(app, "elcg")
    state["saved_at"] = "metadata is allowed"
    state["saved_ts"] = 42.0
    assert lp.preflight(app, state) is None
    state["incomplete"] = ["missing"]
    assert "incomplete" in lp.preflight(app, state)
    state["incomplete"] = []
    app.gpu.static["uuid"] = "another"
    assert "UUID" in lp.preflight(app, state)


@pytest.mark.parametrize("mode", ["disabled", "enabled"])
def test_noop_owned_request_rechecks_witness_without_undo(setup_app, monkeypatch, mode):
    app, ctl, _, saved = setup_app
    ctl.mode = mode

    def lost_witness():
        raise RuntimeError("request witness lost")

    monkeypatch.setattr(ctl, "hold_enabled" if mode == "enabled" else "hold", lost_witness)
    with pytest.raises(RuntimeError, match="witness lost"):
        lp.change(app, "elcg", mode)
    assert not saved
    state = lp.capture(app, "elcg")
    assert not lp.restore(app, state)[0][0]
