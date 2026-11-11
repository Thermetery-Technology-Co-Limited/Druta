#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in native-backend smoke test; no GPU access occurs on import or --help.

Run from the checkout, with its built native library discoverable:
    python experiments/native_live_smoke_3070ti.py --slot 0000:01:00.0 --output report.json
Add --write to test small ordinary requests, restoring each before the next.
The default 180-second soak is telemetry polling, NOT a GPU workload/stress test.
Only one process may tune this adapter while this script runs. The card mutex
coordinates this process only; it cannot exclude other tuning applications.

The filename names the first test target, not a device compatibility gate.
Results apply only to the captured adapter, driver and operating conditions.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import traceback


class SkipControl(RuntimeError):
    """This experiment lacks a readable, exactly replayable baseline."""


class RestorationFailed(RuntimeError):
    """Do not issue another tuning request after this exception."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def plain(value):
    if isinstance(value, (ctypes.Structure, ctypes.Union)):
        return {name: plain(getattr(value, name)) for name, *_ in value._fields_}
    if isinstance(value, ctypes.Array):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if type(value) is float:
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    if value is None or type(value) in (bool, int, str):
        return value
    raise TypeError(f"unsupported report value {type(value).__name__}")


class Report:
    def __init__(self, path, args):
        self.path = path.resolve()
        self.data = {
            "schema": 1, "started_utc": utc_now(), "status": "running",
            "arguments": vars(args), "operations": [], "soak": [], "diagnostics": [],
            "scope": "Native backend, captured adapter and operating point only; no load generator.",
            "limitations": [
                "This is an ordinary-control smoke test, not full hardware sign-off.",
                "Clock/VF and power values are requested state; telemetry is separate.",
                "Automatic fan duty may vary; restoration verifies policy and manual duty only.",
                "Other tuning processes are not excluded by the in-process card mutex.",
                "No voltage, rail, current, policy, registry or profile write is exercised.",
            ],
        }
        self.flush()

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(plain(self.data), indent=2, allow_nan=False) + "\n", encoding="utf-8")
        # Windows readers may briefly hold the destination without delete sharing.
        # Keep the atomic evidence update, but tolerate a bounded reader collision.
        for attempt in range(6):
            try:
                temporary.replace(self.path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2 ** attempt))


def observe(function):
    """Bounded read-only retry, recording every failed attempt."""
    errors = []
    for attempt in range(3):
        try:
            return {"ok": True, "value": plain(function()), "retry_errors": errors}
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt != 2:
                time.sleep(0.15)
    return {"ok": False, "errors": errors}


def required(snapshot, name):
    observation = snapshot[name]
    if not observation["ok"] or observation.get("value") is None:
        raise SkipControl(f"{name} is unavailable: {observation}")
    return observation["value"]


def integer(value, label):
    if type(value) is not int:
        raise SkipControl(f"{label} is not a whole integer")
    return value


def pstates(gpu):
    info, error = gpu._read_pstates20()
    if error or info is None:
        raise RuntimeError(error or "Pstates20 is unavailable")
    rows = plain(info)
    rows["pstates"] = rows["pstates"][:info.num_pstates]
    for state in rows["pstates"]:
        state["clocks"] = state["clocks"][:info.num_clocks]
        state["voltages"] = state["voltages"][:info.num_voltages]
    if "ov_voltages" in rows:
        rows["ov_voltages"] = rows["ov_voltages"][:info.num_ov_voltages]
    rows["raw_hex"] = bytes(info).hex()
    return rows


def snapshot(gpu):
    readers = {
        "telemetry": lambda: gpu.read(), "capabilities": lambda: gpu.capabilities(),
        "core_range": lambda: gpu.clock_offset_range(0),
        "memory_range": lambda: gpu.clock_offset_range(2),
        "memory_scale": lambda: gpu.mem_offset_scale(),
        "memory_step_units": lambda: gpu.memory_offset_step_units(),
        "core_step_khz": lambda: gpu.clock_step_khz(),
        "core_step_measured": lambda: gpu.step_is_measured(),
        "pstates20": lambda: pstates(gpu), "vf_curve": lambda: gpu.read_vf_curve(),
        "vf_delta_snapshot": lambda: gpu.vf_delta_snapshot(),
        "vf_other_rows": lambda: gpu.read_vfp_other_rows(),
        "vf_lock": lambda: gpu.read_vf_lock_status(), "clock_lock": lambda: gpu.read_clk_lock_status(),
        "owned_clock_lock_request": lambda: gpu.last_clock_lock_request(),
        "power_request_mw": lambda: gpu.read_power_limit_mw(),
        "nvml_power_request_mw": lambda: gpu._read_power_limit_nvml(),
        "power_bounds_mw": lambda: gpu.power_limit_bounds_mw(xoc=False),
        "enforced_power_mw": lambda: gpu.read_enforced_power_limit_mw(),
        "live_power_request": lambda: gpu.read_live_power_request(),
        "power_policy_pins": lambda: gpu._power_policy_pins,
        "fan_state": lambda: gpu.read_fan_control_state(), "fan_capabilities": lambda: gpu.fan_capabilities(),
    }
    return {name: observe(reader) for name, reader in readers.items()}


def identity(gpu):
    return {
        "requested_slot": gpu.requested_slot, "slot": gpu.slot(),
        "static": dict(gpu.static), "architecture": gpu.arch(), "architecture_name": gpu.arch_name(),
        "pairing_error": gpu.pairing_error, "available": gpu.available(),
        "nvapi": {"ok": gpu.nvapi.ok, "selected": gpu.nvapi.selected},
        "nvml": {"ok": gpu.nvml.ok, "selected": gpu.nvml.selected, "path": gpu.nvml.dll_path},
    }


def paired(gpu, requested_slot, parse_slot):
    """Require exact parsed PCI addresses, not the backend's bus/device shorthand."""
    requested = parse_slot(requested_slot)
    if requested is None or gpu.pairing_error or not gpu.nvapi.ok or not gpu.nvml.ok:
        raise SkipControl("a readable NVAPI/NVML pairing is required for this write experiment")
    api, nvml = gpu.nvapi.selected, gpu.nvml.selected
    if not api or not nvml:
        raise SkipControl("both selected-handle identity records are required")
    if any(parse_slot(slot) != requested for slot in (gpu.slot(), api.get("slot"), nvml.get("slot"))):
        raise SkipControl("requested, GPU, NVAPI and NVML PCI addresses do not match exactly")
    for key in ("devid", "subsys"):
        if type(api.get(key)) is not int or api[key] != nvml.get(key):
            raise SkipControl(f"NVAPI/NVML {key} pairing is incomplete or disagrees")
    return {"slot": list(requested), "devid": api["devid"], "subsys": api["subsys"]}


def fan_request(state):
    """Only requested fields; automatic duty and RPM are observations, not requests."""
    if not isinstance(state, dict) or state.get("source") not in ("nvml", "nvapi_client", "nvapi_cooler"):
        raise SkipControl("fan request source is unknown")
    if not state.get("fans"):
        raise SkipControl("fan request inventory is empty")
    result = []
    for fan in state["fans"]:
        if type(fan.get("manual")) is not bool:
            raise SkipControl("fan policy is unknown")
        policy, default = integer(fan.get("policy"), "fan policy"), integer(fan.get("default_policy"), "default policy")
        level = integer(fan.get("level"), "fan requested duty")
        low, high = integer(fan.get("min"), "fan minimum"), integer(fan.get("max"), "fan maximum")
        if not 0 <= low <= high <= 100 or (fan["manual"] and not low <= level <= high):
            raise SkipControl("fan baseline cannot be replayed within its reported bounds")
        if not fan["manual"] and policy != default:
            raise SkipControl("a non-default automatic fan policy cannot be replayed safely")
        result.append({"id": integer(fan.get("id"), "fan ID"), "manual": fan["manual"],
                       "policy": policy, "default_policy": default,
                       "level": level if fan["manual"] else None, "min": low, "max": high})
    if len({fan["id"] for fan in result}) != len(result):
        raise SkipControl("duplicate fan identities")
    return {"source": state["source"], "fans": result}


def clock_request(state):
    core, memory = required(state, "core_range"), required(state, "memory_range")
    for label, bounds in (("core", core), ("memory", memory)):
        if len(bounds) != 3 or any(type(item) is not int for item in bounds) or not bounds[0] <= bounds[2] <= bounds[1]:
            raise SkipControl(f"invalid {label} request/range")
    vf = required(state, "vf_delta_snapshot")
    if type(vf.get("freq_div")) is not int or vf["freq_div"] <= 0 or not vf.get("deltas"):
        raise SkipControl("V/F delta geometry is unreadable")
    if any(type(delta) is not int for delta in vf["deltas"].values()):
        raise SkipControl("V/F deltas are not exact integers")
    locks = {name: required(state, name) for name in ("vf_lock", "clock_lock")}
    if any(value != [None, None] for value in locks.values()):
        raise SkipControl("this narrow offset experiment requires readable, released clock locks")
    return {"core": core, "memory": memory, "vf": vf, "locks": locks}


def power_request(state):
    current = integer(required(state, "nvml_power_request_mw"), "NVML board request")
    if required(state, "power_request_mw") != current:
        raise SkipControl("configured board-power getters disagree")
    caps = required(state, "capabilities")
    low, high = integer(caps.get("pl_min_mw"), "reported power minimum"), integer(caps.get("pl_max_mw"), "reported power maximum")
    if not 0 < low <= current <= high:
        raise SkipControl("board request is outside reported normal NVML bounds")
    return {"current_mw": current, "bounds_mw": [low, high]}


def prepare(kind, state):
    """Pure preflight: returns the exact baseline and requested change."""
    if kind in ("core", "memory"):
        before = clock_request(state)
        selected = before[kind]
        if kind == "core":
            step = integer(required(state, "core_step_khz"), "core grid")
            # The first trial is deliberately limited to an integral MHz grid.
            if not 0 < step <= 30000 or step % 1000 or selected[2] % (step // 1000):
                raise SkipControl("core baseline is not exactly replayable on a small integral-MHz grid")
            delta, scale, unit = step // 1000, 1, "MHz"
        else:
            scale, unit = required(state, "memory_scale")
            if type(scale) not in (int, float) or not math.isfinite(scale) or not 0 < scale < (1 << 31):
                raise SkipControl("memory NVML-units-per-slider-unit scale is invalid")
            step = integer(required(state, "memory_step_units"), "memory unit step")
            if step <= 0 or selected[2] % step or (selected[2] / scale) * scale != selected[2]:
                raise SkipControl("memory baseline cannot be replayed in exact NVML units")
            delta = max(step, int(10 * scale / step) * step)
            if delta / scale > 10:
                raise SkipControl("smallest memory request step exceeds this trial's 10-unit decrease")
        target = selected[2] - delta
        if not selected[0] <= target < selected[2] or (target / scale) * scale != target:
            raise SkipControl("no small exact negative delta inside the reported range")
        return before, {"ctype": 0 if kind == "core" else 2, "nvml_units": target,
                        "slider_value": target / scale, "scale": scale, "unit": unit,
                        "preserve_curve": True}
    if kind == "power":
        before = power_request(state)
        if required(state, "power_policy_pins") != {}:
            raise SkipControl("active policy pins are outside this ordinary board-power trial")
        live = state["live_power_request"]
        # A live GET normally reports the same board request; its existence is
        # not evidence that a separate override is active. Refuse disagreement,
        # while an unavailable unrelated getter does not gate valid NVML control.
        if live["ok"] and live.get("value") and live["value"][0] is not None:
            if live["value"][0].get("requested_mw") != before["current_mw"]:
                raise SkipControl("live and NVML configured board-power requests disagree")
        target = before["current_mw"] - 5000
        low, high = required(state, "power_bounds_mw")
        if not max(before["bounds_mw"][0], low) <= target <= min(before["bounds_mw"][1], high):
            raise SkipControl("a 5 W reduction is outside the reported normal bounds")
        return before, {"milliwatts": target, "entry": "_set_power_limit_nvml",
                        "reason": "Native board request only; no policy-pin replay or live-policy fallback"}
    state_fans = required(state, "fan_state")
    before = fan_request(state_fans)
    caps = required(state, "fan_capabilities")
    if not caps.get("manual") or (any(not fan["manual"] for fan in before["fans"]) and not caps.get("auto")):
        raise SkipControl("manual fan operation and the required restoration are unavailable")
    telemetry = required(state, "telemetry")
    if any(type(telemetry.get(key)) not in (int, float) or not math.isfinite(telemetry[key])
           for key in ("temp_edge", "util_gpu")):
        raise SkipControl("idle fan trial needs readable temperature and utilization")
    if telemetry["temp_edge"] > 65 or telemetry["util_gpu"] > 20:
        raise SkipControl("fan trial is restricted to <=65 C and <=20% utilization")
    for target in (40, 35):
        if (caps["min"] <= target <= caps["max"]
                and all(fan["min"] <= target <= fan["max"] for fan in before["fans"])
                and any(not fan["manual"] or fan["level"] != target for fan in before["fans"])):
            return before, {"percent": target, "restore_state": copy.deepcopy(state_fans)}
    raise SkipControl("no changed 35-40% request inside every fan's reported bounds")


def projection(kind, state):
    if kind in ("core", "memory"):
        return clock_request(state)
    if kind == "power":
        return power_request(state)
    return fan_request(required(state, "fan_state"))


def expected_request(kind, before, request):
    expected = copy.deepcopy(before)
    if kind in ("core", "memory"):
        old = expected[kind][2]
        expected[kind][2] = request["nvml_units"]
        if kind == "core":
            shift = (request["nvml_units"] - old) * 1000 * expected["vf"]["freq_div"]
            expected["vf"]["deltas"] = {index: delta + shift for index, delta in expected["vf"]["deltas"].items()}
    elif kind == "power":
        expected["current_mw"] = request["milliwatts"]
    else:
        for fan in expected["fans"]:
            fan.update(manual=True, policy=1, level=request["percent"])
    return expected


def diagnostics(report, backend):
    for message in backend.drain_diagnostics():
        report.data["diagnostics"].append({"time_utc": utc_now(), "message": message})


def operation(kind, gpu, report, args, backend, parse_slot):
    record = {"kind": kind, "started_utc": utc_now(), "status": "preflight"}
    report.data["operations"].append(record)
    with gpu._lock:
        try:
            record["binding"] = paired(gpu, args.slot, parse_slot)
            record["before"] = snapshot(gpu)
            before, request = prepare(kind, record["before"])
            second = snapshot(gpu)
            record["baseline_confirmation"] = second
            if projection(kind, second) != before:
                raise SkipControl("request baseline changed between two fresh reads")
            # Recheck all bounds/unit/idle conditions immediately before the write.
            second_before, second_request = prepare(kind, second)
            # Automatic fan duty/RPM can change while the requested policy is
            # identical; those observations are not part of the restore contract.
            if second_before != before or any(second_request[key] != value for key, value in request.items()
                                             if key != "restore_state"):
                raise SkipControl("preflight conditions changed between fresh reads")
            expected = expected_request(kind, before, request)
            record.update(request=request, expected=expected, baseline_request=before)
        except SkipControl as exc:
            record.update(status="skipped", reason=str(exc))
            report.flush()
            return

        if kind in ("core", "memory"):
            setter = lambda: gpu.set_clock_offset(request["ctype"], request["slider_value"], preserve_curve=True)
            restore = lambda: gpu.set_clock_offset(request["ctype"], before[kind][2] / request["scale"], preserve_curve=True)
        elif kind == "power":
            setter = lambda: gpu._set_power_limit_nvml(request["milliwatts"])
            restore = lambda: gpu._set_power_limit_nvml(before["current_mw"])
        else:
            setter = lambda: gpu.set_fan(request["percent"])
            restore = lambda: gpu.restore_fan_control_state(request["restore_state"])

        record["status"] = "write_pending"
        report.flush()  # A durable baseline is mandatory before entering any setter.
        try:
            record["set_result"] = plain(setter())
            report.flush()
            time.sleep(args.settle_seconds)
            record["after"] = snapshot(gpu)
            actual = projection(kind, record["after"])
            record["actual_request"] = actual
            record["changed_request_verified"] = actual == expected and actual != before
            record["status"] = "passed" if record["set_result"][0] and record["changed_request_verified"] else "failed"
            report.flush()
        except BaseException as exc:
            record.update(status="failed", exception=f"{type(exc).__name__}: {exc}")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            # A setter that returned failure or raised can still have altered the card.
            # Never replace this with reset_all, reset_vf_curve, or a factory default.
            try:
                if paired(gpu, args.slot, parse_slot) != record["binding"]:
                    raise RestorationFailed("GPU binding changed before restoration")
                record["restore_result"] = plain(restore())
                restored = None
                record["restore_readbacks"] = []
                for attempt in range(3):
                    final = snapshot(gpu)
                    record["restore_readbacks"].append(final)
                    try:
                        restored = projection(kind, final)
                    except SkipControl:
                        restored = None
                    if restored == before:
                        break
                    if attempt != 2:
                        time.sleep(0.25)
                record["restored_request"] = restored
                record["restoration_verified"] = bool(record["restore_result"][0]) and restored == before
            except BaseException as exc:
                record.update(restoration_verified=False, restore_exception=f"{type(exc).__name__}: {exc}")
            diagnostics(report, backend)
            record["finished_utc"] = utc_now()
            if not record["restoration_verified"]:
                record["status"] = "restoration_failed"
            report.flush()
            if not record["restoration_verified"]:
                raise RestorationFailed(f"{kind} restoration was not verified; aborting further writes")


def soak(gpu, report, args, backend):
    started = time.monotonic()
    next_refresh = 0.0
    while time.monotonic() - started < args.soak_seconds:
        elapsed = time.monotonic() - started
        row = {"elapsed_seconds": elapsed, "time_utc": utc_now(), "telemetry": observe(lambda: gpu.read())}
        if elapsed >= next_refresh:
            row["refresh"] = observe(lambda: gpu.refresh_capabilities())
            row["capabilities"] = observe(lambda: gpu.capabilities())
            next_refresh = elapsed + 30
        diagnostics(report, backend)
        report.data["soak"].append(row)
        report.flush()
        remaining = args.soak_seconds - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(min(args.interval_seconds, remaining))
    report.data["soak_elapsed_seconds"] = time.monotonic() - started


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slot", required=True, help="Explicit PCI slot, e.g. 0000:01:00.0")
    parser.add_argument("--output", required=True, help="Incrementally replaced JSON evidence file")
    parser.add_argument("--write", action="store_true", help="Opt in to small ordinary requests and exact restoration")
    parser.add_argument("--soak-seconds", type=float, default=180)
    parser.add_argument("--interval-seconds", type=float, default=1)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.soak_seconds) or args.soak_seconds < 0
            or not math.isfinite(args.interval_seconds) or not 0.1 <= args.interval_seconds <= 30
            or not math.isfinite(args.settle_seconds) or not 0 <= args.settle_seconds <= 5):
        parser.error("soak must be nonnegative; interval must be 0.1-30s; settle must be 0-5s; all must be finite")
    report = Report(Path(args.output), args)
    gpu = None
    backend = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from druta import gpu_backend as backend, native, native_frontend

        report.data["surface_missing_members"] = list(native_frontend.missing_members())
        gpu = backend.open_gpu(args.slot, kind="native")
        report.data["backend"] = {"description": backend.describe(), "choice": vars(backend.choice())}
        library = native.load()
        report.data["library"] = {"path": library.path, "expected_abi": native.ABI_VERSION,
                                  "loaded_abi": library.dc_abi_version(),
                                  "sha256": hashlib.sha256(Path(library.path).read_bytes()).hexdigest()}
        report.data["identity"] = identity(gpu)
        report.data["initial"] = snapshot(gpu)
        report.data["write_binding"] = observe(lambda: paired(gpu, args.slot, native.parse_slot))
        diagnostics(report, backend)
        report.flush()
        if args.write:
            for kind in ("core", "memory", "power", "fan"):
                operation(kind, gpu, report, args, backend, native.parse_slot)
        soak(gpu, report, args, backend)
        failed = any(row["status"] in ("failed", "restoration_failed") for row in report.data["operations"])
        failed |= not report.data["initial"]["telemetry"]["ok"]
        failed |= any(not row["telemetry"]["ok"] for row in report.data["soak"])
        failed |= bool(report.data["diagnostics"])
        report.data["status"] = "completed_with_failures" if failed else "completed"
    except BaseException as exc:
        report.data.update(status="aborted", exception=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        if gpu is not None:
            report.data["final"] = snapshot(gpu)
            if backend is not None:
                diagnostics(report, backend)
            report.data["close"] = observe(lambda: gpu.close())
            if report.data["status"] == "completed" and (
                    not report.data["close"]["ok"] or report.data["diagnostics"]):
                report.data["status"] = "completed_with_failures"
        report.data["finished_utc"] = utc_now()
        report.flush()
        print(f"{report.data['status']}: {report.path}", flush=True)
    return 0 if report.data["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
