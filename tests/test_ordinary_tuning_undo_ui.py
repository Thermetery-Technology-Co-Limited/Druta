# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ordinary control Undo at the UI boundary, with no hardware access."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import druta.druta as ui
from druta import profiles
from druta.druta import Druta


class FakeGPU:
    def __init__(self):
        self._lock = threading.RLock()
        self.static = {"name": "card", "vbios": "v", "driver": "d", "uuid": "uuid", "slot": "slot"}
        self.nvml = SimpleNamespace(ok=True, has=lambda name: True)
        self.nvapi = SimpleNamespace(ok=True, Pstates20Set=True, ClkDomCtlSet=True, VoltCtrlGet=True, VoltCtrlSet=True)
        self.memory, self.boost = 16, 20
        self.domains = {7: {"freq_khz": 12000, "mode": 1, "msvdd_uv": 42}}
        self.fan = 35
        self.fail = False

    def slot(self):
        return "slot"

    def mem_offset_scale(self):
        return (16, "MHz")

    def memory_offset_step_units(self):
        return 1

    def _offset_range(self, kind):
        return (-2000, 2000, self.memory)

    def clkdom_ok(self):
        return True

    def clkdom_layout(self):
        return object()

    def clkdom_domains(self):
        return list(self.domains)

    def clkdom_is_blackwell(self):
        return False

    def read_clk_domain_offsets(self):
        return self.domains, None

    def read_voltage_boost(self):
        return self.boost

    def set_clock_offset(self, kind, value):
        if self.fail:
            return False, "rejected"
        self.memory = int(value * 16)
        return True, "memory set"

    def set_clk_domain_offset(self, index, value):
        if self.fail:
            return False, "rejected"
        self.domains[index]["freq_khz"] = round(value) * 1000
        return True, "domain set"

    def set_voltage_boost(self, value):
        if self.fail:
            return False, "rejected"
        self.boost = value
        return True, "boost set"

    def read_fan_control_state(self):
        raise ValueError("fan state unavailable")

    def set_fan(self, value):
        self.fan = value
        return True, "fan set"

    def reset_fan(self):
        self.fan = None
        return True, "fan auto"


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "DIR", str(tmp_path))
    monkeypatch.setattr(ui.dpg, "does_item_exist", lambda tag: False)
    app = Druta.__new__(Druta)
    app.gpu = FakeGPU()
    app.guard = lambda: True
    app.log = Mock()
    app.report = Mock()
    app.refresh_profile_list = Mock()
    app.sync_hold_headroom = Mock()
    app._i2c_busy = False
    app._pending_load = None
    app.set_pending_load = lambda value: setattr(app, "_pending_load", value)
    return app


def saved():
    return [json.loads(Path(row[1]).read_text(encoding="utf-8")) for row in profiles.list_profiles() if row[3]]


@pytest.mark.parametrize("kind", ["memory", "domain", "boost"])
def test_apply_callbacks_save_previous_selected_request_and_noop(app, kind):
    gpu = app.gpu
    app.step_mhz = lambda: 1
    if kind == "memory":
        action, original = lambda: app.apply_mem(2), 16
    elif kind == "domain":
        action, original = lambda: app.apply_domain_offset(7, "xbar", 18), 12000
    else:
        action, original = lambda: app.apply_volt(30), 20
    action()
    snapshots = saved()
    assert len(snapshots) == 1
    assert snapshots[0]["tuning_action"]["kind"] == kind
    assert snapshots[0]["tuning_action"]["value"] == original
    assert (gpu.memory, gpu.domains[7]["freq_khz"], gpu.boost) == {
        "memory": (32, 12000, 20),
        "domain": (16, 18000, 20),
        "boost": (16, 12000, 30),
    }[kind]
    action()
    assert len(saved()) == 1
    assert app.report.call_args.args[0][0] is True
    assert "nothing written" in app.report.call_args.args[0][1]


def test_snapshot_failure_blocks_and_failed_write_retains_snapshot(app, monkeypatch):
    original_reader = app.gpu.read_voltage_boost
    app.gpu.read_voltage_boost = lambda: None
    app.apply_volt(40)
    assert app.gpu.boost == 20
    assert app.report.call_args.args[0][0] is False
    app.gpu.read_voltage_boost = original_reader
    real = profiles.autosave_state
    monkeypatch.setattr(profiles, "autosave_state", Mock(side_effect=OSError("disk unavailable")))
    app.apply_volt(40)
    assert app.gpu.boost == 20
    assert app.report.call_args.args[0][0] is False
    monkeypatch.setattr(profiles, "autosave_state", real)
    app.gpu.fail = True
    app.apply_volt(40)
    assert app.gpu.boost == 20
    assert saved()[0]["tuning_action"]["value"] == 20
    assert app.report.call_args.args[0][0] is False


def test_fan_auto_continues_when_undo_unavailable_but_manual_blocks(app):
    app.apply_fan(70)
    assert app.gpu.fan == 35
    assert app.report.call_args.args[0][0] is False
    app.fan_auto()
    assert app.gpu.fan is None
    assert app.report.call_args.args[0][0] is True
    assert any("returning fan control" in c.args[0] for c in app.log.call_args_list)


def test_scoped_load_preflight_once_and_bypasses_unrelated_paths(app, monkeypatch):
    state = profiles.capture_tuning_action(app.gpu, "memory")
    app.gpu.memory = 32
    app.gpu.static.update(name="renamed", driver="new", vbios="new")
    monkeypatch.setattr(profiles, "load", lambda name: state)
    preflight = Mock(return_value=None)
    monkeypatch.setattr(profiles, "preflight", preflight)
    monkeypatch.setattr(profiles, "device_mismatch", Mock(side_effect=AssertionError("generic metadata")))
    monkeypatch.setattr(profiles, "autosave_tuning_action", lambda gpu, action, template: ("undo", "", []))
    app.finish_profile_load = Mock()
    app.rail_for_profile = Mock(side_effect=AssertionError("I2C"))
    app._i2c_discovery_complete = False
    app.load_profile("saved")
    preflight.assert_called_once_with(app.gpu, state)
    app.finish_profile_load.assert_called_once_with("saved", state, False)


def test_manual_load_and_undo_restore_only_selection_and_refresh(app, monkeypatch):
    state = profiles.capture_tuning_action(app.gpu, "memory")
    app.gpu.memory = 32
    monkeypatch.setattr(profiles, "load", lambda name: state)
    changes = []
    monkeypatch.setattr(ui.dpg, "does_item_exist", lambda tag: True)
    monkeypatch.setattr(ui.dpg, "set_value", lambda tag, value: changes.append((tag, value)))
    app.rail_for_profile = Mock(side_effect=AssertionError("unrelated I2C"))
    app.load_profile("old")
    assert (app.gpu.memory, app.gpu.boost, app.gpu.domains[7]["freq_khz"]) == (16, 20, 12000)
    assert changes == [("sl_mem", 1), ("in_mem", 1)]
    undo = saved()[0]
    assert undo["tuning_action"]["value"] == 32
    changes.clear()
    app.begin_profile_load("undo", undo)
    assert app.gpu.memory == 32
    assert changes == [("sl_mem", 2), ("in_mem", 2)]


def test_scoped_automatic_startup_is_blocked(app):
    state = profiles.capture_tuning_action(app.gpu, "boost")
    app.gpu.boost = 45
    app._startup_manager = SimpleNamespace(block=Mock())
    app.begin_profile_load("saved", state, automatic=True)
    assert app.gpu.boost == 45
    assert not saved()
    app._startup_manager.block.assert_called_once()


@pytest.mark.parametrize("operation", ["board", "current", "policy", "stock", "maximum"])
def test_power_callbacks_save_complete_power_state_before_write(app, monkeypatch, operation):
    gpu = app.gpu
    gpu.board = 250000
    gpu.power_rows = [
        {
            "policy": 4,
            "type": 3,
            "channel": 1,
            "unit": "mW",
            "minimum": 1,
            "maximum": 500000,
            "requested": 100000,
            "board": False,
            "named_current": False,
            "writable": True,
            "consistent": True,
        }
    ]
    gpu.pins = {"4": 100000}

    def capture(card):
        return {
            "schema": 2,
            "scope": "power_action",
            "saved_at": "now",
            "saved_ts": 1.0,
            "device": dict(card.static),
            "incomplete": [],
            "power_action": {
                "board_mw": card.board,
                "rows": [dict(row) for row in card.power_rows],
                "pins": dict(card.pins),
            },
        }

    monkeypatch.setattr(profiles, "capture_power_action", capture)

    def changed(result=(True, "set")):
        gpu.board = 300000
        gpu.power_rows[0]["requested"] = 200000
        gpu.pins.clear()
        return result

    gpu.set_power_limit_mw = lambda value: changed()
    gpu.set_current_limit_ma = lambda policy, value: changed()
    gpu.set_power_policy = lambda policy, value: changed()
    gpu.stock_power_policy = lambda policy: changed()
    gpu.max_all_power_policies = lambda: changed([("power limit", True, "set")])
    app.knob_input_value = lambda key, value: value
    app.sync_power_slider_from_card = Mock(return_value=300000)
    app.refresh_power_policies = Mock()
    app.refresh_current_limits = Mock()
    {
        "board": lambda: app.apply_pl(300),
        "current": lambda: app.apply_current_limit(13, 200),
        "policy": lambda: app.apply_power_policy(4, 200),
        "stock": lambda: app.stock_power_policy(4),
        "maximum": app.max_all_power_policies,
    }[operation]()
    undo = saved()[0]["power_action"]
    assert (undo["board_mw"], undo["rows"][0]["requested"], undo["pins"]) == (250000, 100000, {"4": 100000})
    assert (gpu.board, gpu.power_rows[0]["requested"], gpu.pins) == (300000, 200000, {})


def test_power_snapshot_failure_blocks_apply_but_stock_proceeds(app, monkeypatch):
    gpu = app.gpu
    gpu.board = 250000
    monkeypatch.setattr(profiles, "capture_power_action", Mock(side_effect=ValueError("unreadable")))
    gpu.set_power_limit_mw = lambda value: setattr(gpu, "board", value) or (True, "set")
    gpu.stock_power_policy = lambda policy: setattr(gpu, "board", 200000) or (True, "stock")
    app.knob_input_value = lambda key, value: value
    app.sync_power_slider_from_card = Mock()
    app.refresh_power_policies = Mock()
    app.refresh_current_limits = Mock()
    app.apply_pl(300)
    assert gpu.board == 250000
    assert app.report.call_args.args[0][0] is False
    app.stock_power_policy(4)
    assert gpu.board == 200000
    assert any("continuing the explicit Stock action" in c.args[0] for c in app.log.call_args_list)


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("capture_fails", [True, False])
def test_current_stock_routes_save_undo_or_continue_on_capture_failure(app, monkeypatch, native, capture_fails):
    class NativeStockGPU(FakeGPU):
        def stock_power_policy(self, policy):
            self.calls.append(("native", policy))
            self.request = 120000
            return True, "stock"

    class FallbackGPU(FakeGPU):
        def set_current_limit_ma(self, policy, value):
            self.calls.append(("fallback", policy, value))
            self.request = value
            return True, "stock"

    app.gpu = gpu = NativeStockGPU() if native else FallbackGPU()
    gpu.calls = []
    gpu.request = 180000
    app.read_current_limit_rows = lambda: [{"policy": 14, "default_ma": 120000}]
    app.refresh_current_limits = Mock()
    app.refresh_power_policies = Mock()

    def capture(card):
        if capture_fails:
            raise ValueError("unreadable power state")
        row = {
            "policy": 14,
            "type": 3,
            "channel": 1,
            "unit": "mA",
            "minimum": 1,
            "maximum": 500000,
            "requested": card.request,
            "board": False,
            "named_current": True,
            "writable": True,
            "consistent": True,
        }
        return {
            "schema": 2,
            "scope": "power_action",
            "saved_at": "now",
            "saved_ts": 1.0,
            "device": dict(card.static),
            "incomplete": [],
            "power_action": {"board_mw": 250000, "rows": [row], "pins": {"14": card.request}},
        }

    monkeypatch.setattr(profiles, "capture_power_action", capture)
    app.stock_knob("current14")
    assert gpu.request == 120000
    assert gpu.calls == ([("native", 14)] if native else [("fallback", 14, 120000)])
    assert app.report.call_args.args[0][0] is True
    if capture_fails:
        assert saved() == []
        assert any("continuing the explicit Stock action" in call.args[0] for call in app.log.call_args_list)
    else:
        undo = saved()[0]["power_action"]
        assert undo["rows"][0]["requested"] == 180000
        assert undo["pins"] == {"14": 180000}
