# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent driver readback for understood BIOS overrides."""

import ctypes
import math
import re


class BiosRuntimeError(RuntimeError):
    pass


# Public NVAPI clock domain identifiers; BIOS names come from typed descriptors.
_CLOCKS = {"GPC": (0, 0), "DRAM": (4, 2)}


def read_clock_ranges(gpu):
    """Return independent P0 offset ranges in public NVAPI kHz, without setters."""
    if gpu.pairing_error or not gpu.nvapi.ok:
        return {}, "A paired NVAPI adapter is unavailable"
    error = None
    with gpu._lock:
        for _ in range(2):
            info, error = gpu._read_pstates20()
            if info is not None:
                break
        if info is None:
            return {}, error or "Public P-state metadata is unavailable"
        rows = {}
        for name, (domain, ctype) in _CLOCKS.items():
            clock = gpu._pstate20_clock(info, ctype)
            if clock is None:
                continue
            if clock.domain != domain or clock.kind not in (0, 1) or not clock.flags & 1:
                continue
            if clock.delta.minimum > clock.delta.maximum:
                continue
            rows[name] = {
                "domain": domain,
                "kind": int(clock.kind),
                "flags": int(clock.flags),
                "minimum": int(clock.delta.minimum),
                "maximum": int(clock.delta.maximum),
            }
        return rows, None


def clock_contracts(gpu, inspection, changes):
    """Bind understood selected BIOS ranges to current public driver metadata."""
    fields = {field["key"]: field for field in (inspection or {}).get("fields", [])}
    selected = [
        key
        for key in changes
        if re.fullmatch(r"clock\.\d+\.(minimum|maximum)", key) and fields[key].get("domain") in _CLOCKS
    ]
    if not selected:
        return {}
    rows, error = read_clock_ranges(gpu)
    if error:
        raise BiosRuntimeError("Clock range metadata is unavailable: " + str(error))
    contracts = {}
    for key in selected:
        field = fields[key]
        name = field["domain"]
        row = rows.get(name)
        if row is None:
            raise BiosRuntimeError(f"{key}: public P0 offset range is unavailable")
        prefix, role = key.rsplit(".", 1)
        for bound in ("minimum", "maximum"):
            peer = fields.get(prefix + "." + bound)
            if (
                peer is None
                or peer.get("domain") != name
                or peer.get("units") != "MHz"
                or row[bound] != peer["current"] * 1000
            ):
                raise BiosRuntimeError(f"{key}: driver clock range differs from the decoded BIOS range")
        if name not in contracts:
            contracts[name] = {
                "identity": {part: row[part] for part in ("domain", "kind", "flags")},
                "before": {bound: row[bound] for bound in ("minimum", "maximum")},
                "after": {bound: row[bound] for bound in ("minimum", "maximum")},
                "keys": [],
            }
        contract = contracts[name]
        contract["after"][role] = changes[key] * 1000
        contract["keys"].append(key)
    return contracts


def verify_clock_ranges(snapshot, contracts, *, restore=False, before=False):
    """Require both bounds and identity; return only semantically checked keys."""
    checked = []
    for name, contract in contracts.items():
        row = (snapshot.get("clock_ranges") or {}).get(name)
        if row is None:
            raise BiosRuntimeError(f"{name}: public clock range readback is unavailable")
        if any(row.get(key) != value for key, value in contract["identity"].items()):
            raise BiosRuntimeError(f"{name}: public clock range identity changed")
        expected = contract["before" if restore or before else "after"]
        if any(row.get(key) != value for key, value in expected.items()):
            raise BiosRuntimeError(f"{name}: public clock range does not match the expected BIOS bounds")
        checked.extend(contract["keys"])
    return checked


def read_thermal_thresholds(gpu):
    """Read absolute Celsius thresholds; unsupported peers remain independent.

    This documented NVML getter is capability-probed, not version-gated. NVIDIA
    may remove it on newer architectures; T.Limit field values are relative and
    must not be substituted as absolute Celsius without their reference.
    """
    if gpu.pairing_error or not gpu.nvml.ok:
        return {}, "A paired NVML adapter is unavailable"
    if not gpu.nvml.has("nvmlDeviceGetTemperatureThreshold"):
        return {}, "The absolute temperature-threshold getter is unavailable"
    getter = gpu.nvml.dll.nvmlDeviceGetTemperatureThreshold
    getter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
    getter.restype = ctypes.c_int
    rows = {}
    with gpu._lock:
        for index, name in enumerate(("shutdown", "slowdown", "memory_max", "gpu_max")):
            code, error = None, None
            for _ in range(2):
                value = ctypes.c_uint()
                try:
                    code = int(getter(gpu.nvml.dev, index, ctypes.byref(value)))
                    error = None
                except Exception as exc:
                    code, error = None, str(exc)
                    continue
                if code in (0, 3):  # success / unsupported
                    break
            rows[name] = {"status": code, "degrees_c": int(value.value) if code == 0 else None}
            if error is not None:
                rows[name]["error"] = error
    return rows, None


def thermal_contracts(gpu, inspection, changes):
    key = "thermal.operating.maximum"
    if key not in changes:
        return {}
    fields = {field["key"]: field for field in inspection["fields"]}
    field = fields[key]
    wanted = changes[key]
    if type(wanted) not in (int, float) or not math.isfinite(wanted) or not float(wanted).is_integer():
        raise BiosRuntimeError("Operating-temperature readback requires a whole Celsius value")
    rows, error = read_thermal_thresholds(gpu)
    current = rows.get("gpu_max", {})
    if error or current.get("status") != 0:
        raise BiosRuntimeError("Operating-temperature metadata is unavailable: " + str(error or current.get("status")))
    if field.get("units") != "C" or field["current"] != current["degrees_c"]:
        raise BiosRuntimeError("The driver operating-temperature limit differs from the decoded BIOS value")
    return {
        key: {
            "before": current["degrees_c"],
            "after": int(wanted),
            "unchanged": {
                name: row["degrees_c"] for name, row in rows.items() if name != "gpu_max" and row["status"] == 0
            },
        }
    }


def verify_thermal_thresholds(snapshot, contracts, *, restore=False):
    checked = []
    for key, contract in contracts.items():
        if key != "thermal.operating.maximum":
            raise BiosRuntimeError("Unknown thermal readback contract")
        expected = {**contract["unchanged"], "gpu_max": contract["before" if restore else "after"]}
        rows = snapshot.get("thermal_thresholds") or {}
        for name, wanted in expected.items():
            row = rows.get(name, {})
            if row.get("status") != 0 or row.get("degrees_c") != wanted:
                raise BiosRuntimeError(f"{name}: temperature-threshold readback differs from the expected value")
        checked.append(key)
    return checked
