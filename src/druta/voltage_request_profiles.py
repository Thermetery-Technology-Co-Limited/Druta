# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scoped, explicitly applied profiles for understood voltage request words."""

from . import voltage_request_replay as replay
from . import voltage_requests_live as live


def capture(snapshot, selection=None):
    request = replay.validate(snapshot["request"])
    if not request["complete"]:
        raise ValueError("complete rail request preservation is unavailable")
    if snapshot.get("identity") != request["identity"] or snapshot.get("slot") != request["identity"]["slot"]:
        raise ValueError("rail request identity changed")
    values = live._values(request)
    if selection is None:
        selected = {rail: dict(fields) for rail, fields in values.items()}
    else:
        if (
            not isinstance(selection, tuple)
            or len(selection) != 2
            or selection[0] not in values
            or selection[1] not in live.FIELDS
        ):
            raise ValueError("invalid selected rail request field")
        rail, field = selection
        selected = {rail: {field: values[rail][field]}}
    return {
        "identity": request["identity"].copy(),
        "rails": {
            rail: {"identity": request["rail_identity"][rail].copy(), "offsets_uv": fields}
            for rail, fields in selected.items()
        },
    }


def validate(state):
    allowed = {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "voltage_requests"}
    if state.get("scope") != "voltage_requests" or type(state.get("schema")) is not int or state["schema"] != 2:
        raise ValueError("unsupported voltage request profile scope")
    if set(state) - allowed:
        raise ValueError("voltage request snapshot contains unrelated profile fields")
    missing = state.get("incomplete", [])
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        raise ValueError("invalid completeness metadata")
    data = state.get("voltage_requests")
    if not isinstance(data, dict) or set(data) != {"identity", "rails"}:
        raise ValueError("invalid voltage request profile")
    identity = data["identity"]
    if not isinstance(identity, dict) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        raise ValueError("invalid voltage request adapter identity")
    if (
        not isinstance(identity["slot"], str)
        or not identity["slot"]
        or any(type(identity[k]) is not int or identity[k] < 0 for k in ("architecture", "device_id", "subsystem_id"))
    ):
        raise ValueError("invalid voltage request adapter identity")
    rails = data["rails"]
    if not isinstance(rails, dict) or not rails:
        raise ValueError("voltage request profile has no rails")
    for rail, row in rails.items():
        if rail not in ("0", "1") or not isinstance(row, dict) or set(row) != {"identity", "offsets_uv"}:
            raise ValueError("invalid voltage request rail")
        ident, offsets = row["identity"], row["offsets_uv"]
        if (
            not isinstance(ident, dict)
            or set(ident) != {"name", "policy_type", "control_type"}
            or not isinstance(ident["name"], str)
            or not ident["name"]
            or type(ident["policy_type"]) is not int
            or type(ident["control_type"]) is not int
        ):
            raise ValueError("invalid rail identity")
        if not isinstance(offsets, dict) or not offsets:
            raise ValueError("no voltage request fields")
        for field, value in offsets.items():
            if field not in live.FIELDS or type(value) is not int or not -(2**31) <= value < 2**31:
                raise ValueError("invalid voltage request field or signed microvolt value")
    return data


def check_live(data, snapshot):
    request = replay.validate(snapshot["request"])
    if not request["complete"]:
        raise ValueError("complete rail request preservation is unavailable")
    if (
        snapshot.get("identity") != data["identity"]
        or request["identity"] != data["identity"]
        or snapshot.get("slot") != data["identity"]["slot"]
    ):
        raise ValueError("voltage request adapter identity changed")
    if snapshot.get("gate_enabled") is not True:
        raise ValueError("voltage request gate is disabled or unavailable")
    for rail, row in data["rails"].items():
        if request["rail_identity"].get(rail) != row["identity"]:
            raise ValueError(f"rail {rail} is missing or its identity changed")
    return snapshot


def restore(gpu, data):
    results = []
    snapshot = check_live(data, live.read(gpu))
    for rail, row in data["rails"].items():
        for field, value in row["offsets_uv"].items():
            try:
                outcome = live.set_request(gpu, rail, field, value, expected=snapshot, slot=snapshot["slot"])
                snapshot = outcome["snapshot"]
                results.append(
                    (
                        True,
                        f"rail {rail} {field} request restored ({value} uV)"
                        + ("; effective threshold unverified" if field.startswith("VCRIT") else ""),
                    )
                )
            except Exception as exc:
                results.append((False, f"rail {rail} {field} request failed: {exc}; remaining requests not applied"))
                return results
    return results
