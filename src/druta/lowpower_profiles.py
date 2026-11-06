# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit snapshots of this application's session-owned low-power requests."""

import time

from . import profiles

_KINDS = {
    "difr": ("_difr_live_controllers", "difr_live_control", "DifrController"),
    "elpg": ("_gr_gating_live_controllers", "gr_gating_live", "GrGatingController"),
    "elcg": ("_engine_clock_gating_controllers", "engine_clock_gating_live", "EngineClockGatingController"),
    "blcg": ("_block_clock_gating_controllers", "block_clock_gating_live", "BlockClockGatingController"),
    "slcg": (
        "_second_level_clock_gating_controllers",
        "second_level_clock_gating_live",
        "SecondLevelClockGatingController",
    ),
    "individual": (
        "_individual_power_gating_controllers",
        "individual_power_gating_live",
        "IndividualPowerGatingController",
    ),
}
_INDICES = (0, 4, 10, 11, 12, 13, 14, 15)


def _selection(kind, index):
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError("unknown low-power request kind")
    if kind == "individual":
        if type(index) is not int or index not in _INDICES:
            raise ValueError("invalid individual controller index")
    elif index is not None:
        raise ValueError("index is only valid for an individual request")


def controller(app, kind, index=None):
    """Reuse the exact live objects owned by the corresponding panels."""
    _selection(kind, index)
    attr, _, _ = _KINDS[kind]
    gpu = app.gpu
    controllers = getattr(app, attr, None)
    if not isinstance(controllers, dict):
        controllers = {}
        setattr(app, attr, controllers)
    key = (id(gpu), index) if kind == "individual" else id(gpu)
    ctl = controllers.get(key)
    if ctl is None or ctl.gpu is not gpu:
        # Static imports keep each controller visible to frozen-app bundlers.
        if kind == "difr":
            from .difr_live_control import DifrController as ControllerClass
        elif kind == "elpg":
            from .gr_gating_live import GrGatingController as ControllerClass
        elif kind == "elcg":
            from .engine_clock_gating_live import EngineClockGatingController as ControllerClass
        elif kind == "blcg":
            from .block_clock_gating_live import BlockClockGatingController as ControllerClass
        elif kind == "slcg":
            from .second_level_clock_gating_live import SecondLevelClockGatingController as ControllerClass
        else:
            from .individual_power_gating_live import IndividualPowerGatingController as ControllerClass
        ctl = ControllerClass(gpu, index) if kind == "individual" else ControllerClass(gpu)
        controllers[key] = ctl
    return ctl


def _identity(value):
    keys = {"slot", "architecture", "device_id", "subsystem_id"}
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or not isinstance(value["slot"], str)
        or not value["slot"]
        or any(type(value[k]) is not int or not 0 <= value[k] <= 0xFFFFFFFF for k in keys - {"slot"})
    ):
        raise ValueError("invalid low-power controller identity")
    return dict(value)


def _mode(snapshot, kind):
    if not isinstance(snapshot, dict) or snapshot.get("uncertain_outcome") is not False:
        raise ValueError("request ownership is uncertain")
    owned = snapshot.get("owned_request")
    if type(owned) is not bool:
        raise ValueError("request ownership is unavailable")
    if not owned:
        return "released"
    if kind in ("elcg", "slcg"):
        mode = snapshot.get("owned_mode")
        if mode not in ("enabled", "disabled"):
            raise ValueError("owned request mode is unknown")
        return mode
    return "disabled"


def capture(app, kind, index=None):
    _selection(kind, index)
    gpu = app.gpu
    with gpu._lock:
        ctl = controller(app, kind, index)
        identity = _identity(ctl.identity)
        mode = _mode(ctl.read(), kind)
        now = time.time()
        state = {
            "schema": 2,
            "scope": "lowpower_requests",
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "saved_ts": now,
            "device": {k: gpu.static.get(k) for k in ("name", "vbios", "driver", "uuid", "slot")},
            "incomplete": [],
            "lowpower_requests": {"kind": kind, "index": index, "identity": identity, "mode": mode},
        }
        validate(state)
        error = _device_error(app, state)
        if error:
            raise ValueError(error)
        return state


def validate(state):
    allowed = {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "lowpower_requests"}
    if (
        not isinstance(state, dict)
        or set(state) - allowed
        or type(state.get("schema")) is not int
        or state["schema"] != 2
        or state.get("scope") != "lowpower_requests"
    ):
        raise ValueError("unsupported low-power profile scope or fields")
    if not isinstance(state.get("device"), dict):
        raise ValueError("invalid low-power profile device")
    if not isinstance(state.get("incomplete", []), list) or any(
        not isinstance(x, str) for x in state.get("incomplete", [])
    ):
        raise ValueError("invalid completeness metadata")
    data = state.get("lowpower_requests")
    if not isinstance(data, dict) or set(data) != {"kind", "index", "identity", "mode"}:
        raise ValueError("invalid low-power request profile")
    _selection(data["kind"], data["index"])
    _identity(data["identity"])
    modes = ("released", "disabled", "enabled") if data["kind"] in ("elcg", "slcg") else ("released", "disabled")
    if data["mode"] not in modes:
        raise ValueError("invalid low-power request mode")
    return data


def _device_error(app, state):
    gpu = app.gpu
    saved = state["device"].get("uuid")
    current = gpu.static.get("uuid")
    if saved and current and saved != current:
        return "low-power profile belongs to a different GPU UUID"
    if gpu.slot() != state["lowpower_requests"]["identity"]["slot"]:
        return "low-power profile belongs to a different adapter slot"
    return None


def preflight(app, state):
    try:
        data = validate(state)
        if state.get("incomplete"):
            return "low-power profile is incomplete"
        with app.gpu._lock:
            error = _device_error(app, state)
            if error:
                return error
            ctl = controller(app, data["kind"], data["index"])
            if _identity(ctl.identity) != data["identity"]:
                return "low-power controller identity changed"
            _mode(ctl.read(), data["kind"])
        return None
    except Exception as exc:
        return str(exc)


def _apply(ctl, kind, current, target):
    if current == target:
        if target == "released":
            return ctl.read()
        snapshot = ctl.hold_enabled() if target == "enabled" else ctl.hold()
        if _mode(snapshot, kind) != target:
            raise ValueError("owned request did not verify")
        return snapshot
    if current != "released":
        snapshot = ctl.release()
        if _mode(snapshot, kind) != "released":
            raise ValueError("release did not verify; target was not acquired")
    if target == "released":
        return snapshot
    snapshot = ctl.hold_enabled() if target == "enabled" else ctl.hold()
    if _mode(snapshot, kind) != target:
        raise ValueError("target request did not verify")
    return snapshot


def restore(app, state):
    with app.gpu._lock:
        error = preflight(app, state)
        if error:
            return [(False, f"Low-power request restore failed: {error}")]
        data = validate(state)
        ctl = controller(app, data["kind"], data["index"])
        try:
            current = _mode(ctl.read(), data["kind"])
            _apply(ctl, data["kind"], current, data["mode"])
            return [(True, f"{data['kind']} request intent restored to {data['mode']}")]
        except Exception as exc:
            return [(False, f"{data['kind']} request restore failed (state may have changed): {exc}")]


def change(app, kind, mode, index=None):
    _selection(kind, index)
    modes = ("released", "disabled", "enabled") if kind in ("elcg", "slcg") else ("released", "disabled")
    if mode not in modes:
        raise ValueError("invalid low-power request mode")
    with app.gpu._lock:
        ctl = controller(app, kind, index)
        try:
            state = capture(app, kind, index)
            current = state["lowpower_requests"]["mode"]
            if current == mode:
                return _apply(ctl, kind, current, mode)
            name, _, missing = profiles.autosave_state(f"lowpower-{kind}", state)
            if missing:
                raise ValueError("undo snapshot is incomplete")
            app.log(f"Undo point saved as '{name}'", None)
        except Exception as exc:
            if mode != "released":
                raise
            app.log(f"Release proceeding without a verified undo point: {exc}", False)
            return ctl.release()
        return _apply(ctl, kind, current, mode)
