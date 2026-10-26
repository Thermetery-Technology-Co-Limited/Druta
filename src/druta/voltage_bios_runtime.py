# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent effective-policy checks for constant BIOS VMIN equations.

The rail status is VID-quantized. We require exact requested readback and never
derive a universal voltage step from one card or silently accept rounding.
"""

import copy
import math
import re
from collections.abc import Mapping

from .perf_diagnostics import PerfDiagnosticsReader
from .registry_flags import _same_state

_FIELDS = ("reliability", "alt_reliability", "overvoltage", "effective", "vmin")
_OFFSETS = ("reliability", "alt_reliability", "overvoltage", "vmin")
_RAILS = {0: ("NVVDD", 1), 1: ("MSVDD", 3)}


class VoltageBiosError(RuntimeError):
    pass


def _uv(value, *, signed=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise VoltageBiosError("Voltage readback is not a finite number")
    scaled = value * 1000
    if abs(scaled - round(scaled)) > 1e-6 or not (-(2**31) if signed else 0) <= scaled <= (
        2**31 - 1 if signed else 2**32 - 1
    ):
        raise VoltageBiosError("Voltage readback does not have understood microvolt units")
    return round(scaled)


def _identity(reader):
    api, slot, architecture = reader._binding()
    result = {"slot": slot, "architecture": architecture}
    for key, source, maximum in (("device_id", "devid", 0xFFFF), ("subsystem_id", "subsys", 0xFFFFFFFF)):
        value = api.selected.get(source)
        if type(value) is not int or not 0 <= value <= maximum:
            raise VoltageBiosError("Voltage policy needs a complete paired adapter identity")
        result[key] = value
    return result


def _once(gpu, reader):
    identity = _identity(reader)
    state, offsets = gpu.read_volt_rail_state() or {}, gpu._read_volt_rail_deltas() or {}
    if _identity(reader) != identity:
        raise VoltageBiosError("Voltage policy target changed during readback")
    rails, issues = {}, {}
    for rail, (name, discriminator) in _RAILS.items():
        try:
            row, delta = state[rail], offsets[rail]
            if type(row.get("type")) is not int or row["type"] != discriminator:
                raise VoltageBiosError("Rail status identity is not understood")
            if type(delta.get("type")) is not int or not 0 <= delta["type"] <= 0xFFFFFFFF:
                raise VoltageBiosError("Rail control identity is not understood")
            policy = {key: _uv(row[key]) for key in _FIELDS}
            if any(value <= 0 for value in policy.values()):
                raise VoltageBiosError("Rail policy contains an unavailable voltage")
            rails[str(rail)] = {
                "name": name,
                "type": row["type"],
                "control_type": delta["type"],
                "boost_uv": _uv(row["_boost_mv"]),
                "policy_uv": policy,
                "offsets_uv": {key: _uv(delta[key], signed=True) for key in _OFFSETS},
            }
        except (KeyError, TypeError, VoltageBiosError) as exc:
            issues[str(rail)] = str(exc)
    return {"identity": identity, "rails": rails}, issues


def read(gpu):
    """Two stable fresh reads; missing peers do not remove an understood rail."""
    reader = PerfDiagnosticsReader(gpu)
    last = None
    try:
        with gpu._lock:
            for attempt in range(2):
                try:
                    first, _ = _once(gpu, reader)
                    second, issues = _once(gpu, reader)
                    if not second["rails"]:
                        raise VoltageBiosError("No understood rail policy: " + str(issues))
                    if not _same_state(first, second):
                        raise VoltageBiosError("Voltage policy changed between fresh reads")
                    return second, None
                except Exception as exc:
                    last = str(exc)
                    if attempt == 0:
                        # Retry capability discovery without changing captured
                        # session restore targets or issuing a SET.
                        refresh = getattr(gpu, "_refresh_volt_rail_capabilities", None)
                        if refresh:
                            refresh()
    except Exception as exc:
        last = str(exc)
    return None, last or "Voltage policy is unavailable"


def contracts(gpu, inspection, changes):
    selected = {key: value for key, value in changes.items() if re.fullmatch(r"voltage\.[01]\.VMIN", key)}
    if not selected:
        return {}
    if any(key.startswith("voltage.") and key not in selected for key in changes):
        raise VoltageBiosError("Apply VMIN and other voltage-equation changes in separate transactions")
    observation, error = read(gpu)
    if error:
        raise VoltageBiosError(error)
    fields = {field["key"]: field for field in inspection["fields"]}
    expected = copy.deepcopy(observation)
    result = {}
    for key, wanted in selected.items():
        field, index = fields[key], key.split(".")[1]
        rail = observation["rails"].get(index)
        if (
            not rail
            or field.get("rail") != rail["name"]
            or field.get("role") != "VMIN"
            or field.get("equation_type") != 1
            or field.get("shared") is not False
            or field.get("units") != "uV"
            or field.get("capability") != "editable"
        ):
            raise VoltageBiosError(key + ": no independent constant-equation/rail binding")
        if type(wanted) not in (int, float) or not math.isfinite(wanted) or wanted != int(wanted):
            raise VoltageBiosError("VMIN requires whole microvolts")
        offset = rail["offsets_uv"]["vmin"]
        if field["current"] + offset != rail["policy_uv"]["vmin"]:
            raise VoltageBiosError(key + ": BIOS plus retained offset differs from effective VMIN")
        after = int(wanted) + offset
        if not 0 < after <= min(rail["policy_uv"][name] for name in _FIELDS if name != "vmin"):
            raise VoltageBiosError(key + ": VMIN would exceed a currently reported upper limit")
        expected["rails"][index]["policy_uv"]["vmin"] = after
        result[key] = {"before_bios_uv": field["current"], "after_bios_uv": int(wanted)}
    return {"schema": 1, "selected": result, "before": observation, "after": expected}


def validate_policy(observation):
    """Validate a saved policy without reading hardware or requiring every rail."""
    if not isinstance(observation, dict) or set(observation) != {"identity", "rails"}:
        raise VoltageBiosError("Malformed captured voltage policy")
    identity = observation["identity"]
    if not isinstance(identity, dict) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        raise VoltageBiosError("Malformed captured voltage adapter identity")
    from .clock_vf_registry import _identity as validate_identity

    try:
        validate_identity(identity)
    except (ValueError, RuntimeError) as exc:
        raise VoltageBiosError(str(exc)) from exc
    if not isinstance(observation["rails"], dict) or not observation["rails"]:
        raise VoltageBiosError("Malformed captured voltage rails")
    for index, row in observation["rails"].items():
        if (
            index not in ("0", "1")
            or not isinstance(row, dict)
            or set(row) != {"name", "type", "control_type", "boost_uv", "policy_uv", "offsets_uv"}
        ):
            raise VoltageBiosError("Malformed captured voltage rail")
        if (row["name"], row["type"]) != _RAILS[int(index)] or type(row["type"]) is not int:
            raise VoltageBiosError("Malformed captured voltage rail identity")
        if any(type(row[k]) is not int or not 0 <= row[k] <= 0xFFFFFFFF for k in ("boost_uv", "control_type")):
            raise VoltageBiosError("Malformed captured voltage metadata")
        for name, names, low, high in (
            ("policy_uv", _FIELDS, 1, 0xFFFFFFFF),
            ("offsets_uv", _OFFSETS, -(2**31), 2**31 - 1),
        ):
            values = row[name]
            if (
                not isinstance(values, dict)
                or set(values) != set(names)
                or any(type(v) is not int or not low <= v <= high for v in values.values())
            ):
                raise VoltageBiosError("Malformed captured voltage values")
    return copy.deepcopy(observation)


def validate_contracts(changes, saved):
    """Pure receipt check before registry cleanup, retaining legacy receipts."""
    selected = {key: value for key, value in changes.items() if re.fullmatch(r"voltage\.[01]\.VMIN", key)}
    if saved == {}:
        if selected:
            raise VoltageBiosError("Selected VMIN changes have no saved contract")
        return
    if not isinstance(saved, Mapping) or set(saved) != {"schema", "selected", "before", "after"}:
        raise VoltageBiosError("Malformed BIOS voltage contract")
    if type(saved["schema"]) is not int or saved["schema"] != 1 or not isinstance(saved["selected"], dict):
        raise VoltageBiosError("Malformed BIOS voltage contract")
    if not selected or set(selected) != set(saved["selected"]):
        raise VoltageBiosError("VMIN changes and saved contract disagree")
    before, after = saved["before"], copy.deepcopy(saved["before"])
    validate_policy(before)
    for key, wanted in selected.items():
        entry, index = saved["selected"][key], key.split(".")[1]
        if (
            not isinstance(entry, dict)
            or set(entry) != {"before_bios_uv", "after_bios_uv"}
            or any(type(v) is not int or not 0 < v <= 0xFFFFFFFF for v in entry.values())
            or type(wanted) not in (int, float)
            or wanted != entry["after_bios_uv"]
            or index not in before["rails"]
        ):
            raise VoltageBiosError("Malformed selected VMIN binding")
        row = before["rails"][index]
        offset = row["offsets_uv"]["vmin"]
        if entry["before_bios_uv"] + offset != row["policy_uv"]["vmin"]:
            raise VoltageBiosError("Captured VMIN baseline disagrees")
        desired = entry["after_bios_uv"] + offset
        if not 0 < desired <= min(row["policy_uv"][name] for name in _FIELDS if name != "vmin"):
            raise VoltageBiosError("Saved VMIN exceeds the captured upper limits")
        after["rails"][index]["policy_uv"]["vmin"] = desired
    if not _same_state(after, saved["after"]):
        raise VoltageBiosError("Saved VMIN changes do not preserve peer policy")


def verify(observation, saved, *, restore=False):
    if not saved:
        return []
    if not _same_state(observation, saved["before" if restore else "after"]):
        raise VoltageBiosError("Effective VMIN or preserved voltage policy differs from the exact request")
    return list(saved["selected"])
