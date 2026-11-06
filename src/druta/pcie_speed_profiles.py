# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual snapshots of the PCIe speed target generation."""

import time

from . import tuning_profiles
from .pcie_speed_live import PcieSpeedController


def validate(state):
    allowed = {"schema", "scope", "saved_at", "saved_ts", "device", "incomplete", "pcie_speed"}
    if not isinstance(state, dict) or set(state) != allowed or state.get("scope") != "pcie_speed":
        raise ValueError("Invalid PCIe speed profile")
    proxy = dict(state, scope="tuning_action", tuning_action={"kind": "boost", "index": None, "value": 0})
    proxy.pop("pcie_speed")
    tuning_profiles.validate(proxy)
    data = state["pcie_speed"]
    if not isinstance(data, dict) or set(data) != {"identity", "target_generation"}:
        raise ValueError("Invalid PCIe speed profile data")
    identity = data["identity"]
    if not isinstance(identity, dict) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        raise ValueError("Invalid PCIe speed identity")
    if not isinstance(identity["slot"], str) or not identity["slot"] or identity["slot"] != state["device"]["slot"]:
        raise ValueError("Invalid PCIe speed identity slot")
    if type(identity["architecture"]) is not int or identity["architecture"] < 0:
        raise ValueError("Invalid PCIe speed architecture")
    for key, maximum in (("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        value = identity[key]
        if value is not None and (type(value) is not int or not 0 <= value <= maximum):
            raise ValueError("Invalid PCIe speed PCI identity")
    target = data["target_generation"]
    if type(target) is not int or not 1 <= target <= 5:
        raise ValueError("Invalid PCIe speed target generation")
    return data


def _check_report(data, report):
    if not isinstance(report, dict) or report.get("available") is not True:
        raise ValueError("PCIe speed readback is unavailable")
    if report.get("identity") != data["identity"]:
        raise ValueError("PCIe speed GPU identity changed")
    allowed = report.get("allowed_generations")
    current = report.get("target_generation")
    if (not isinstance(allowed, (list, tuple)) or not allowed
            or any(type(value) is not int or not 1 <= value <= 5 for value in allowed)
            or type(current) is not int or current not in allowed):
        raise ValueError("PCIe speed current target or capabilities are unavailable")
    if data["target_generation"] not in allowed:
        raise ValueError("Saved PCIe speed target is outside the current capabilities")


def capture(gpu):
    with gpu._lock:
        report = PcieSpeedController(gpu).read()
        if not isinstance(report, dict) or report.get("available") is not True:
            raise ValueError("PCIe speed readback is unavailable")
        now = time.time()
        device = {key: gpu.static.get(key) for key in ("name", "vbios", "driver", "uuid")}
        device["slot"] = tuning_profiles._slot(gpu)
        state = {"schema": 2, "scope": "pcie_speed", "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                 "saved_ts": now, "device": device, "incomplete": [],
                 "pcie_speed": {"identity": report.get("identity"),
                                "target_generation": report.get("target_generation")}}
        data = validate(state)
        _check_report(data, report)
        return state


def preflight(gpu, state):
    try:
        data = validate(state)
        if state["incomplete"]:
            raise ValueError("PCIe speed snapshot is incomplete")
        error = tuning_profiles.device_error(gpu, state)
        if error:
            return error.replace("tuning action", "PCIe speed")
        _check_report(data, PcieSpeedController(gpu).read())
        return None
    except Exception as exc:
        return str(exc)


def restore(gpu, state):
    with gpu._lock:
        error = preflight(gpu, state)
        if error:
            return [(False, f"PCIe speed profile not applied: {error}")]
        data = validate(state)
        try:
            target = data["target_generation"]
            result = PcieSpeedController(gpu).set_generation(target, expected_identity=data["identity"])
            if (not isinstance(result, dict) or result.get("available") is not True
                    or result.get("verification") not in ("target_and_link", "already_matches")
                    or result.get("identity") != data["identity"] or result.get("target_generation") != target
                    or result.get("requested_generation") != target or result.get("generation") != target
                    or result.get("root_generation") != target
                    or any(type(result.get(key)) is not int for key in
                           ("target_generation", "requested_generation", "generation", "root_generation"))):
                detail = result.get("status", "readback unavailable") if isinstance(result, dict) else "readback unavailable"
                return [(False, f"PCIe speed request was not verified: {detail}")]
            return [(True, f"PCIe speed Gen{target} verified ({result['verification']})")]
        except Exception as exc:
            return [(False, f"PCIe speed restore failed: {exc}")]
