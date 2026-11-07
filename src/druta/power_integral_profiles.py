# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual snapshots of selected live power-integral ratio bounds."""

import time
from collections.abc import Iterable

from . import tuning_profiles
from .power_integral_runtime import PowerIntegralController

FIELDS = ("lower_raw", "upper_raw")


def _selection(selection):
    if not isinstance(selection, dict) or not selection:
        raise ValueError("Power-integral selection is empty or invalid")
    result = {}
    for policy, fields in selection.items():
        if type(policy) is int:
            index = policy
        elif type(policy) is str and policy.isascii() and policy.isdecimal() and str(int(policy)) == policy:
            index = int(policy)
        else:
            raise ValueError("Invalid power-integral policy index")
        if not 0 <= index < 32 or index in result:
            raise ValueError("Invalid or duplicate power-integral policy index")
        if not isinstance(fields, Iterable) or isinstance(fields, (str, bytes, dict)):
            raise ValueError("Invalid power-integral fields")
        values = tuple(fields)
        if not values or any(type(field) is not str or field not in FIELDS for field in values):
            raise ValueError("Invalid power-integral fields")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate power-integral field")
        result[index] = tuple(field for field in FIELDS if field in values)
    return result


def validate(state):
    allowed = {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "power_integral"}
    if not isinstance(state, dict) or set(state) != allowed or state.get("scope") != "power_integral":
        raise ValueError("Invalid power-integral profile")
    proxy = dict(state, scope="tuning_action", tuning_action={"kind": "boost", "index": None, "value": 0})
    proxy.pop("power_integral")
    tuning_profiles.validate(proxy)
    data = state["power_integral"]
    if not isinstance(data, dict) or set(data) != {"identity", "policies"}:
        raise ValueError("Invalid power-integral profile data")
    identity = data["identity"]
    if not isinstance(identity, dict) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        raise ValueError("Invalid power-integral identity")
    if not isinstance(identity["slot"], str) or not identity["slot"] or identity["slot"] != state["device"]["slot"]:
        raise ValueError("Invalid power-integral identity slot")
    if type(identity["architecture"]) is not int or identity["architecture"] < 0:
        raise ValueError("Invalid power-integral architecture")
    for key, maximum in (("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        value = identity[key]
        if value is not None and (type(value) is not int or not 0 <= value <= maximum):
            raise ValueError("Invalid power-integral PCI identity")
    rows = data["policies"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Power-integral profile has no policies")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"policy", "type", "channel", "unit", "values"}:
            raise ValueError("Invalid power-integral policy row")
        policy = row["policy"]
        if type(policy) is not int or not 0 <= policy < 32 or policy in seen:
            raise ValueError("Invalid or duplicate power-integral policy")
        seen.add(policy)
        if any(type(row[key]) is not int or not 0 <= row[key] <= 255 for key in ("type", "channel")):
            raise ValueError("Invalid power-integral policy metadata")
        if row["unit"] not in ("mW", "mA"):
            raise ValueError("Invalid power-integral unit")
        values = row["values"]
        if not isinstance(values, dict) or not values or set(values) - set(FIELDS):
            raise ValueError("Invalid power-integral values")
        for field, value in values.items():
            if type(value) is not int or not 0 <= value <= 65535 or (field == "upper_raw" and value == 0):
                raise ValueError("Invalid power-integral ratio")
        if len(values) == 2 and values["lower_raw"] > values["upper_raw"]:
            raise ValueError("Power-integral bounds are inverted")
    return data


def _rows(report):
    if not isinstance(report, dict) or not isinstance(report.get("policies"), list):
        raise ValueError("Power-integral readback unavailable")
    rows = {}
    for row in report["policies"]:
        if not isinstance(row, dict) or type(row.get("policy")) is not int or row["policy"] in rows:
            raise ValueError("Invalid power-integral policy readback")
        rows[row["policy"]] = row
    return rows


def capture(gpu, selection=None):
    with gpu._lock:
        report = PowerIntegralController(gpu).read()
        rows = _rows(report)
        selected = (
            _selection(selection)
            if selection is not None
            else {index: FIELDS for index, row in rows.items() if row.get("available") is True}
        )
        selected = _selection(selected)
        policies = []
        for index, fields in selected.items():
            row = rows.get(index)
            if not row or row.get("available") is not True:
                raise ValueError(f"Power-integral policy {index} is unavailable")
            policies.append(
                {**{key: row[key] for key in ("policy", "type", "channel", "unit")},
                 "values": {field: row[field] for field in fields}}
            )
        now = time.time()
        device = {key: gpu.static.get(key) for key in ("name", "vbios", "driver", "uuid")}
        device["slot"] = tuning_profiles._slot(gpu)
        state = {
            "schema": 2,
            "scope": "power_integral",
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "saved_ts": now,
            "device": device,
            "incomplete": [],
            "power_integral": {"identity": report["identity"], "policies": policies},
        }
        validate(state)
        return state


def _check_report(data, report):
    if report.get("identity") != data["identity"]:
        raise ValueError("Power-integral GPU identity changed")
    rows = _rows(report)
    for saved in data["policies"]:
        row = rows.get(saved["policy"])
        if not row or row.get("available") is not True:
            raise ValueError(f"Power-integral policy {saved['policy']} is unavailable")
        if any(row.get(key) != saved[key] for key in ("type", "channel", "unit")):
            raise ValueError(f"Power-integral policy {saved['policy']} metadata changed")
        lower = saved["values"].get("lower_raw", row.get("lower_raw"))
        upper = saved["values"].get("upper_raw", row.get("upper_raw"))
        if type(lower) is not int or type(upper) is not int or not 0 <= lower <= upper <= 65535 or upper == 0:
            raise ValueError(f"Power-integral policy {saved['policy']} bounds are invalid")
    return rows


def preflight(gpu, state):
    try:
        data = validate(state)
        if state["incomplete"]:
            raise ValueError("Power-integral snapshot is incomplete")
        error = tuning_profiles.device_error(gpu, state)
        if error:
            return error.replace("tuning action", "power-integral")
        _check_report(data, PowerIntegralController(gpu).read())
        return None
    except Exception as exc:
        return str(exc)


def restore(gpu, state):
    with gpu._lock:
        error = preflight(gpu, state)
        if error:
            return [(False, f"Power-integral profile not applied: {error}")]
        data = validate(state)
        controller = PowerIntegralController(gpu)
        results = []
        try:
            rows = _check_report(data, controller.read())
            for saved in data["policies"]:
                index, values = saved["policy"], saved["values"]
                row = rows[index]
                order = (
                    ("upper_raw", "lower_raw")
                    if values.get("lower_raw", row["lower_raw"]) > row["upper_raw"]
                    else FIELDS
                )
                changed = False
                for field in order:
                    if field not in values or row[field] == values[field]:
                        continue
                    setter = controller.set_lower if field == "lower_raw" else controller.set_upper
                    report = setter(index, values[field], expected_identity=data["identity"])
                    rows = _check_report(data, report)
                    row = rows[index]
                    if row.get(field) != values[field]:
                        raise ValueError(f"Power-integral policy {index} {field} readback mismatch")
                    changed = True
                    results.append((True, f"Power-integral policy {index} {field} applied: {values[field]}"))
                if not changed:
                    results.append((True, f"Power-integral policy {index} already matches"))
            report = controller.read()
            rows = _check_report(data, report)
            for saved in data["policies"]:
                if any(rows[saved["policy"]].get(field) != value for field, value in saved["values"].items()):
                    raise ValueError(f"Power-integral policy {saved['policy']} selected readback mismatch")
            return results
        except Exception as exc:
            results.append((False, f"Power-integral restore failed: {exc}"))
            return results
