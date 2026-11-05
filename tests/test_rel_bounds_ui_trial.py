"""Offline framebuffer trial of actual REL panel callbacks."""
import copy
import json
import struct
import threading
from unittest.mock import Mock

import pytest

from druta import registry_panel, rel_bounds_ui_trial, voltage_rel_live
from druta.rel_bounds_ui_trial import LiveRelBoundsCapture
from druta.vfe_equation_runtime import CONTROL_STRIDE, HEADER
from tests.test_ui_capture import FakeDpg, app, run


@pytest.fixture
def setup(monkeypatch):
    clock = iter(range(10000))
    monkeypatch.setattr(rel_bounds_ui_trial.time, "monotonic", lambda: next(clock))
    dpg, application = FakeDpg(), app()
    dpg.stop_dearpygui = Mock()
    dpg.values.update(registry_header=False, reg_rel_live_header=False, reg_constants_live_header=False,
                      reg_rel_live_select="", reg_constants_live_select="", reg_rel_live_value="",
                      reg_constants_live_value="", reg_rel_live_status="", reg_constants_live_status="",
                      reg_rel_live_inventory="")
    for tag in ("reg_rel_live_select", "reg_rel_live_value", "reg_rel_live_apply", "reg_rel_live_restore",
                "reg_rel_live_fixed", "reg_rel_live_restore_bounds"):
        dpg.config[tag] = {}
    application.gpu._lock = threading.RLock()
    application.gpu.slot = lambda: "fake"
    application.guard = lambda: True
    application.log = Mock()
    application._rel_live_initial_states = {}
    monkeypatch.setattr(registry_panel, "dpg", dpg)

    class Control:
        def __init__(self, gpu):
            self.gpu, self.bounds, self.calls = gpu, (900000, 1120000), []
            self.identity, self.image, self.index = {"slot": "fake", "architecture": 10,
                "device_id": 1, "subsystem_id": 2}, "image", 3
            self.fail_fixed = self.fail_restore = False

        def _capture(self):
            raw = bytearray(HEADER + 4 * CONTROL_STRIDE)
            struct.pack_into("<ff", raw, HEADER + 3 * CONTROL_STRIDE + 4, *self.bounds)
            return {"voltage": {"equations": {"identity": copy.deepcopy(self.identity),
                    "info_hex": "aa", "control_hex": raw.hex()}, "rail_control": {"opaque": "retained"},
                    "boost_percent": 0}, "bounds": self.bounds}

        def _summary(self, raw):
            lo, hi = raw["bounds"]
            return {"identity": copy.deepcopy(self.identity), "image_sha256": self.image, "unavailable_peers": [],
                "rows": [{"rail": 0, "label": "NVVDD", "status": "ready", "fixed_status": "ready",
                          "equation": self.index, "lower_uv": lo, "upper_uv": hi,
                          "stored_lower_uv": 900000, "stored_upper_uv": 1120000,
                          "policy_uv": {"vmin": 800000, "overvoltage": 1200000,
                                        "reliability": 1040000, "alt_reliability": 1060000,
                                        "effective": 1040000}}]}

        def read(self):
            return self._summary(self._capture())

        def set_fixed(self, rail, value):
            self.calls.append(("fixed", rail, value))
            self.bounds = (value, value)
            if self.fail_fixed:
                raise RuntimeError("injected fixed failure")
            return self.read()

        def restore_bounds(self, rail):
            self.calls.append(("restore", rail))
            if self.fail_restore:
                raise RuntimeError("injected restore failure")
            self.bounds = (900000, 1120000)
            return self.read()

    monkeypatch.setattr(voltage_rel_live, "VoltageRelController", Control)
    panel = registry_panel.RegistryPanel(application)
    panel.constants_live_choices = {}
    application._registry_panel = panel
    return dpg, application, panel


def test_real_callbacks_capture_and_stop(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    assert panel.rel_live_control.calls == []
    run(capture, dpg)
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert capture.manifest["state"] == "complete" and capture.manifest["restoration_verified"]
    assert [x["stage"] for x in capture.manifest["captures"]] == [
        "rel-before", "rel-active", "rel-restored", "restored-display"]
    assert all((tmp_path / "run" / (x + ".json")).is_file() for x in ("before", "active", "restored"))
    assert json.loads((tmp_path / "run" / "active.json").read_text())["summary"]["rows"][0]["lower_uv"] == 1045000
    dpg.stop_dearpygui.assert_called_once()


def test_bounds_refusal_never_writes(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1300000)
    assert capture.manifest["state"] == "failed" and panel.rel_live_control.calls == []
    dpg.stop_dearpygui.assert_called_once()


def test_interrupted_after_apply_restores(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._stage("rel-active")
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert capture.manifest["restoration_verified"] and "interrupted" in capture.manifest["errors"]
    dpg.stop_dearpygui.assert_called_once()


def test_known_changed_identity_never_restores(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._stage("rel-active")
    panel.rel_live_control.image = "other"
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000)]
    assert not capture.manifest["restoration_verified"]
    assert any("identity" in s or "image" in s for s in capture.manifest["errors"])


def test_failed_panel_restore_not_retried(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._stage("rel-active")
    panel.rel_live_control.fail_restore = True
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert not capture.manifest["restoration_verified"]
    assert "interrupted" in capture.manifest["errors"]


def test_failed_apply_retains_error_and_restores(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    panel.rel_live_control.fail_fixed = True
    with pytest.raises(RuntimeError, match="did not produce"):
        capture._stage("rel-active")
    capture._finish(RuntimeError("fixed callback failed"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert capture.manifest["restoration_verified"]
    assert "fixed callback failed" in capture.manifest["errors"]


def test_changed_identity_during_restore_refresh_is_not_written(setup, tmp_path, monkeypatch):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._stage("rel-active")
    refresh = panel.refresh_rel_live

    def changed():
        panel.rel_live_control.image = "changed during refresh"
        refresh()

    monkeypatch.setattr(panel, "refresh_rel_live", changed)
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000)]
    assert not capture.manifest["restoration_verified"] and capture.identity_changed


def test_transient_cleanup_read_uses_one_restore_fallback(setup, tmp_path, monkeypatch):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._stage("rel-active")
    read = capture._read_state

    def transient(name):
        if name == "before-restore":
            raise RuntimeError("temporary read failure")
        return read(name)

    monkeypatch.setattr(capture, "_read_state", transient)
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert capture.manifest["recovery_fallback_attempted"] and capture.manifest["restoration_verified"]


def test_already_fixed_noop_cannot_qualify(setup, tmp_path, monkeypatch):
    dpg, application, panel = setup
    original = voltage_rel_live.VoltageRelController

    def fixed(gpu):
        control = original(gpu)
        control.bounds = (1045000, 1045000)
        summary = control._summary

        def fixed_summary(raw):
            result = summary(raw)
            result["rows"][0]["stored_lower_uv"] = 1045000
            result["rows"][0]["stored_upper_uv"] = 1045000
            return result

        control._summary = fixed_summary
        return control

    monkeypatch.setattr(voltage_rel_live, "VoltageRelController", fixed)
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    assert capture.manifest["state"] == "failed" and panel.rel_live_control.calls == []


def test_active_observation_window_waits_without_blocking(setup, tmp_path, monkeypatch):
    dpg, application, panel = setup
    now = [100.0]
    monkeypatch.setattr(rel_bounds_ui_trial.time, "monotonic", lambda: now[0])
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    now[0] = 127.0
    while capture.stages[capture.stage] != "rel-active":
        dpg.frame += 1
        capture.tick()
    assert capture.manifest["active_observation_window_seconds"] == 27.0
    assert capture.manifest["active_deadline_monotonic"] == 154.0
    for moment in (127.0, 140.0, 153.99):
        now[0] = moment
        dpg.frame += 1
        capture.tick()
        assert panel.rel_live_control.calls == [("fixed", 0, 1045000)]
        assert capture.stages[capture.stage] == "rel-active"
    now[0] = 154.0
    run(capture, dpg)
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert capture.manifest["state"] == "complete"


def test_interruption_during_observation_window_restores(setup, tmp_path, monkeypatch):
    dpg, application, panel = setup
    now = [100.0]
    monkeypatch.setattr(rel_bounds_ui_trial.time, "monotonic", lambda: now[0])
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    now[0] = 127.0
    while capture.stages[capture.stage] != "rel-active":
        dpg.frame += 1
        capture.tick()
    capture.tick()
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000)]
    capture._finish(RuntimeError("window interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert capture.manifest["restoration_verified"] and "window interrupted" in capture.manifest["errors"]


def test_interrupted_guard_blocked_uses_one_fallback_restore(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._stage("rel-active")
    application.guard = lambda: False
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000), ("restore", 0)]
    assert capture.manifest["recovery_fallback_attempted"]
    assert capture.manifest["restoration_verified"]
    assert capture.manifest["state"] == "failed" and "interrupted" in capture.manifest["errors"]


def test_guard_block_and_changed_target_never_fallback(setup, tmp_path):
    dpg, application, panel = setup
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._stage("rel-active")
    application.guard = lambda: False
    panel.rel_live_control.image = "changed"
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000)]
    assert capture.identity_changed and not capture.manifest["restoration_verified"]
    assert not capture.manifest.get("recovery_fallback_attempted", False)


def test_before_observation_window_delays_all_writes(setup, tmp_path, monkeypatch):
    dpg, application, panel = setup
    now = [100.0]
    monkeypatch.setattr(rel_bounds_ui_trial.time, "monotonic", lambda: now[0])
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    assert capture.manifest["before_observation_window_seconds"] == 27.0
    assert capture.manifest["before_deadline_monotonic"] == 127.0
    for moment in (100.0, 115.0, 126.99):
        now[0] = moment
        dpg.frame += 1
        capture.tick()
        assert capture.stages[capture.stage] == "rel-before"
        assert panel.rel_live_control.calls == []
    now[0] = 127.0
    while capture.stages[capture.stage] != "rel-active":
        dpg.frame += 1
        capture.tick()
    assert panel.rel_live_control.calls == [("fixed", 0, 1045000)]
    capture._finish(RuntimeError("interrupted"))
    assert panel.rel_live_control.calls[-1] == ("restore", 0)


def test_interruption_during_before_window_never_writes(setup, tmp_path, monkeypatch):
    dpg, application, panel = setup
    monkeypatch.setattr(rel_bounds_ui_trial.time, "monotonic", lambda: 100.0)
    capture = LiveRelBoundsCapture(dpg, application, tmp_path / "run", 0, 1045000)
    capture._finish(RuntimeError("before interrupted"))
    assert panel.rel_live_control.calls == []
    assert "before interrupted" in capture.manifest["errors"]
