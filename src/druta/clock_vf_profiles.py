# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit profiles for the two understood live Clock VF policies."""

from . import clock_vf_registry as vf
from .clock_vf_runtime import ClockVfController


def capture(snapshot, selection=None):
    try:
        shaped = vf._shape(snapshot, writable=True)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    if selection is None:
        keys = list(vf._EDITABLE)
    else:
        try:
            keys = list(selection)
        except TypeError as exc:
            raise ValueError("invalid clock VF selection") from exc
    if not keys or any(key not in vf._EDITABLE for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("invalid clock VF selection")
    return {"identity": shaped["identity"], "policies": {
        key: snapshot[vf._EDITABLE[key][0]] for key in keys}}


def validate(state):
    allowed = {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "clock_vf"}
    if not isinstance(state, dict) or state.get("scope") != "clock_vf" or type(state.get("schema")) is not int or state["schema"] != 2:
        raise ValueError("unsupported clock VF profile scope")
    if set(state) - allowed:
        raise ValueError("clock VF snapshot contains unrelated profile fields")
    missing = state.get("incomplete", [])
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ValueError("invalid completeness metadata")
    data = state.get("clock_vf")
    if not isinstance(data, dict) or set(data) != {"identity", "policies"}:
        raise ValueError("invalid clock VF profile")
    try:
        identity = vf._identity(data["identity"])
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    policies = data["policies"]
    if not isinstance(policies, dict) or not policies or any(
        key not in vf._EDITABLE or type(value) is not bool for key, value in policies.items()
    ):
        raise ValueError("invalid clock VF policies")
    return {"identity": identity, "policies": dict(policies)}


def check_live(data, snapshot):
    current = capture(snapshot, data["policies"])
    if current["identity"] != data["identity"]:
        raise ValueError("clock VF adapter identity changed")
    return snapshot


def restore(gpu, data):
    try:
        controller = ClockVfController(gpu)
        # Also check on a no-op; the callback only runs for a real write.
        check_live(data, controller.read())
        controller.set(data["policies"], before_write=lambda snapshot: check_live(data, snapshot))
        return [(True, "clock VF policies restored")]
    except Exception as exc:
        return [(False, f"clock VF policies failed: {exc}")]
