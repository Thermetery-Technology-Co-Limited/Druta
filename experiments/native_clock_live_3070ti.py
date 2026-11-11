#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in native clock-control qualification with per-case restoration.

    python experiments/native_clock_live_3070ti.py --slot 0000:01:00.0 --output clocks.json

The default only reads and prepares cases. Add --write to exercise each offered
private clock offset, one decreasing V/F delta, a temporary V/F point hold and
one enumerated frequency hold. No GPU is accessed on import or --help. This
script generates no workload and never replays opaque driver buffers. Close
other tuning applications first: the card mutex covers this process only.
The filename describes the first target, not a device compatibility gate.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import math
from pathlib import Path
import sys
import time
import traceback

try:
    from .native_live_smoke_3070ti import (
        Report, RestorationFailed, SkipControl, diagnostics, fan_request,
        identity, observe, paired, plain, required, snapshot, utc_now,
    )
except ImportError:
    from native_live_smoke_3070ti import (
        Report, RestorationFailed, SkipControl, diagnostics, fan_request,
        identity, observe, paired, plain, required, snapshot, utc_now,
    )


def lock_entries(gpu):
    """Read the paired handle's lock table; these bytes are never replayed."""
    from druta import nvbackend

    api = gpu.nvapi
    if not api.ok or not api.BoostLock:
        raise SkipControl("lock-table getter unavailable")
    table = nvbackend._ClockLock(version=api.ver(nvbackend._ClockLock, nvbackend.VF_LOCK_VERSION))
    status = api.BoostLock(api.gpu, ctypes.byref(table))
    if status or not 0 < table.count <= 32:
        raise SkipControl(f"lock-table read failed or invalid count: status={status}, count={table.count}")
    entries = {}
    for row in table.locks[:table.count]:
        if row.domain in entries:
            raise SkipControl("lock table repeats a domain")
        entries[row.domain] = {"mode": int(row.lockMode), "value": int(row.volt_uV)}
    return entries


def capture(gpu):
    state = snapshot(gpu)
    extras = {
        "domain_requests": gpu.read_clk_domain_offsets,
        "offered_controls": gpu.clkdom_controls_for_ui,
        "lock_entries": lambda: lock_entries(gpu),
        "lockable_clocks": gpu.lockable_clocks_by_mem,
        "vcore_mv": gpu.read_vcore_mv,
        "voltage_boost": gpu.read_voltage_boost,
        "rail_offsets": lambda: {c: gpu.read_rail_offset_mv(c, 0) for c in gpu.clkdom_domains()},
        "rail_limits": gpu.read_volt_rail_limits,
        "current_limits": gpu.get_current_limits,
        "power_policies": gpu.read_power_policies,
        "vf_recovery_pending": gpu.vf_lock_recovery_pending,
    }
    state.update({name: observe(reader) for name, reader in extras.items()})
    return state


def answered(state, name):
    """A successful getter may legitimately return None; failed reads cannot."""
    row = state[name]
    if not row["ok"]:
        raise SkipControl(f"{name} read unavailable: {row}")
    return row["value"]


def result_rows(state, name):
    result = required(state, name)
    if not isinstance(result, list) or len(result) != 2 or result[1] is not None or result[0] is None:
        raise SkipControl(f"{name} unavailable: {result}")
    return result[0]


def projection(state):
    """Configured requests only; operating clocks, enforcement and fan RPM vary."""
    domains = result_rows(state, "domain_requests")
    if not isinstance(domains, dict) or not domains:
        raise SkipControl("private clock-request inventory is empty")
    for key, row in domains.items():
        if (not isinstance(row, dict) or type(row.get("mode")) is not int
                or type(row.get("freq_khz")) is not int):
            raise SkipControl(f"invalid clock-request row {key}")
    locks = copy.deepcopy(required(state, "lock_entries"))
    # The frequency-lock release API may leave its inactive numeric field in
    # place. Mode OFF has no request. Raw entry values remain in the evidence;
    # the V/F context's exact target restoration is checked separately below.
    for row in locks.values():
        if row["mode"] == 0:
            row["value"] = None
    rails = required(state, "rail_limits")
    policies = result_rows(state, "power_policies")
    current = required(state, "current_limits")
    result = {
        key: required(state, key) for key in (
            "core_range", "memory_range", "memory_scale", "vf_delta_snapshot", "vf_other_rows",
            "vf_lock", "clock_lock", "power_request_mw", "nvml_power_request_mw", "power_policy_pins",
        )
    }
    if (not result["vf_delta_snapshot"].get("deltas")
            or type(result["vf_delta_snapshot"].get("freq_div")) is not int):
        raise SkipControl("V/F delta snapshot lacks a replayable layout")
    result.update(
        domains=domains, locks=locks, fans=fan_request(required(state, "fan_state")),
        voltage_boost=required(state, "voltage_boost"),
        rail_offsets=required(state, "rail_offsets"),
        rail_limits={key: {field: value for field, value in row.items() if not field.startswith("_")}
                     for key, row in rails.items()},
        current_limits=[{key: row.get(key) for key in ("policy", "channel", "minimum_ma", "maximum_ma",
                                                       "default_ma", "requested_ma")} for row in current],
        power_policies=[{key: row.get(key) for key in ("policy", "type", "channel", "unit", "minimum",
                                                       "default", "maximum", "requested", "pinned")}
                        for row in policies],
        recovery_pending=answered(state, "vf_recovery_pending"),
    )
    return result


def require_released(before):
    if before["vf_lock"] != [None, None] or before["clock_lock"] != [None, None]:
        raise SkipControl("this experiment requires readable, released point and frequency locks")
    if before["recovery_pending"]:
        raise SkipControl("an earlier V/F lock still requires recovery")


def prepare(kind, control, gpu, state):
    before = projection(state)
    require_released(before)
    expected = copy.deepcopy(before)
    request = {}
    undo = None
    if kind == "domain":
        from druta import profiles

        offered = required(state, "offered_controls")
        if control not in offered:
            raise SkipControl("control is no longer offered by the runtime UI")
        row = before["domains"].get(str(control))
        if row is None or row["freq_khz"] % 1000:
            raise SkipControl("domain baseline cannot be restored exactly by the whole-MHz setter")
        target = row["freq_khz"] // 1000 - 15
        if not -(1 << 31) <= target * 1000 < (1 << 31):
            raise SkipControl("decreasing domain request exceeds signed kHz representation")
        undo = profiles.capture_tuning_action(gpu, "domain", control)
        if undo["tuning_action"]["value"] != row["freq_khz"]:
            raise SkipControl("domain Undo capture disagrees with the baseline")
        request = {"control": control, "label": gpu.clkdom_control_label(control), "mhz": target}
        expected["domains"][str(control)]["freq_khz"] = target * 1000
        # No interpretation or replay of the private mode field. Both apply
        # and restoration must preserve it and every other captured field.
    elif kind == "vf_delta":
        points = result_rows(state, "vf_curve")
        points = sorted(points, key=lambda p: (p["volt_mv"], p["idx"]))
        step = required(state, "core_step_khz")
        divisor = before["vf_delta_snapshot"]["freq_div"]
        if type(step) is not int or step <= 0 or divisor <= 0:
            raise SkipControl("measured V/F step or frequency divisor unavailable")
        if any(a["freq_mhz"] > b["freq_mhz"] for a, b in zip(points, points[1:])):
            raise SkipControl("existing V/F curve is not monotonic; no shape repair is part of this test")
        for pos, point in enumerate(points):
            old = before["vf_delta_snapshot"]["deltas"].get(str(point["idx"]))
            target = old - step * divisor if type(old) is int else None
            freq = point["freq_mhz"] - step / 1000
            if (target is not None and -gpu.MAX_ABS_DELTA_KHZ <= target <= gpu.MAX_ABS_DELTA_KHZ
                    and freq > 0 and (pos == 0 or points[pos - 1]["freq_mhz"] <= freq)):
                request = {"index": point["idx"], "delta_khz": target, "previous_delta_khz": old,
                           "step_khz": step, "frequency_divisor": divisor,
                           "predicted_freq_mhz": freq, "volt_mv": point["volt_mv"]}
                expected["vf_delta_snapshot"]["deltas"][str(point["idx"])] = target
                break
        if not request:
            raise SkipControl("no single GPU point permits a bounded monotonic one-bin decrease")
    elif kind == "vf_lock":
        from druta.native_legacy_p0 import VF_LOCK_DOMAIN

        voltage = required(state, "vcore_mv")
        if type(voltage) not in (int, float) or not math.isfinite(voltage):
            raise SkipControl("current rail voltage is unavailable")
        points = result_rows(state, "vf_curve")
        under = [p for p in points if p["volt_mv"] <= voltage]
        entries = required(state, "lock_entries")
        target_key = str(VF_LOCK_DOMAIN)
        if not under or entries.get(target_key, {}).get("mode") != 0:
            raise SkipControl(f"no existing point at/below current voltage, or target domain {VF_LOCK_DOMAIN} is not OFF")
        selected = max(under, key=lambda p: p["volt_mv"])
        uv = int(round(selected["volt_mv"] * 1000))
        request = {"voltage_mv": voltage, "selected_uv": uv, "domain": VF_LOCK_DOMAIN,
                   "previous_entry": entries[target_key]}
        expected["locks"][target_key] = {"mode": 3, "value": uv}
        expected["vf_lock"] = [{"domain": VF_LOCK_DOMAIN, "lockMode": 3, "volt_uV": uv,
                                "volt_mv": uv / 1000, "count": len(entries)}, None]
    elif kind == "frequency_lock":
        rows = required(state, "lockable_clocks")
        low, high = gpu.static.get("gfx_min"), gpu.static.get("gfx_max")
        if type(low) is not int or type(high) is not int or not 0 < low <= high or not rows:
            raise SkipControl("enumerated graphics clocks or current bounds unavailable")
        clocks = sorted(set(v for v in rows[-1][1] if type(v) is int and low <= v <= high))
        # A lower-quarter clock from this adapter's own table, not an invented
        # MHz value or a request to maximize clocks. Exact enumeration avoids snapping.
        if not clocks:
            raise SkipControl("no enumerated graphics clock lies within reported bounds")
        target = clocks[(len(clocks) - 1) // 4]
        if any(before["locks"].get(str(c), {}).get("mode") != 0 for c in (0, 1)):
            raise SkipControl("frequency lock target entries are not both OFF")
        request = {"min_mhz": target, "max_mhz": target, "reported_bounds": [low, high]}
        expected["clock_lock"] = [[target, target], None]
        for c in (0, 1):
            expected["locks"][str(c)] = {"mode": 2, "value": target * 1000}
    else:
        raise ValueError(kind)
    return before, request, expected, undo


def differences(expected, actual):
    return {key: {"expected": expected.get(key), "actual": actual.get(key)}
            for key in sorted(set(expected) | set(actual)) if expected.get(key) != actual.get(key)}


def verify_after(kind, gpu, record, expected):
    record["after"] = capture(gpu)
    actual = projection(record["after"])
    record["actual_request"] = actual
    record["differences"] = differences(expected, actual)
    record["changed_request_verified"] = not record["differences"]
    record["status"] = "passed" if record["set_result"][0] and not record["differences"] else "failed"


def restore_domain(gpu, undo):
    from druta import tuning_profiles

    results = tuning_profiles.restore(gpu, undo)
    return bool(results) and all(row[0] for row in results), results


def operation(kind, control, gpu, report, args, backend, parse_slot):
    record = {"kind": kind, "control": control, "started_utc": utc_now(), "status": "preflight"}
    report.data["operations"].append(record)
    with gpu._lock:
        try:
            record["binding"] = paired(gpu, args.slot, parse_slot)
            record["before"] = capture(gpu)
            before, request, expected, undo = prepare(kind, control, gpu, record["before"])
            record.update(request=request, expected=expected, baseline_request=before, scoped_undo=undo)
            if not args.write:
                raise SkipControl("read-only mode; pass --write to execute this prepared case")
            second = capture(gpu)
            record["baseline_confirmation"] = second
            if projection(second) != before:
                raise SkipControl("configured requests changed between preflight reads")
            # Re-evaluate point availability and any measured-voltage choice.
            _, second_request, _, _ = prepare(kind, control, gpu, second)
            comparable = lambda item: {k: v for k, v in item.items() if kind != "vf_lock" or k != "voltage_mv"}
            if comparable(second_request) != comparable(request):
                raise SkipControl("case selection changed between preflight reads")
            if kind == "vf_lock":
                request["voltage_mv"] = second_request["voltage_mv"]
        except Exception as exc:
            record.update(status="skipped", reason=f"{type(exc).__name__}: {exc}")
            report.flush()
            return

        record["status"] = "write_pending"
        report.flush()
        try:
            if kind == "vf_lock":
                # This existing context records cleanup before the native SET
                # and restores both prior mode and dormant voltage on every exit.
                with gpu.verification_p0(voltage_mv=request["voltage_mv"]) as check_hold:
                    check_hold()
                    record["set_result"] = [True, "verification_p0 entered and exact hold checked"]
                    time.sleep(args.settle_seconds)
                    check_hold()
                    verify_after(kind, gpu, record, expected)
                    report.flush()
                record["restore_result"] = [True, "verification_p0 restored the prior target"]
            else:
                if kind == "domain":
                    result = gpu.set_clk_domain_offset(control, request["mhz"])
                elif kind == "vf_delta":
                    result = gpu.apply_vf_deltas({request["index"]: request["delta_khz"]})
                else:
                    result = gpu.lock_gpu_clocks(request["min_mhz"], request["max_mhz"])
                record["set_result"] = plain(result)
                report.flush()
                time.sleep(args.settle_seconds)
                verify_after(kind, gpu, record, expected)
                report.flush()
        except BaseException as exc:
            record.update(status="failed", exception=f"{type(exc).__name__}: {exc}")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            try:
                if paired(gpu, args.slot, parse_slot) != record["binding"]:
                    raise RestorationFailed("GPU binding changed before restoration")
                if kind == "domain":
                    record["restore_result"] = plain(restore_domain(gpu, undo))
                elif kind == "vf_delta":
                    record["restore_result"] = plain(gpu.apply_vf_deltas(
                        {int(idx): delta for idx, delta in before["vf_delta_snapshot"]["deltas"].items()}))
                elif kind == "frequency_lock":
                    record["restore_result"] = plain(gpu.reset_gpu_clocks())
                elif gpu.vf_lock_recovery_pending():
                    record["restore_result"] = plain(gpu.recover_vf_lock())
                else:
                    record.setdefault("restore_result", [True, "no pending V/F recovery; checking prior state"])
                record["restore_readbacks"] = []
                restored = None
                for attempt in range(3):
                    final = capture(gpu)
                    record["restore_readbacks"].append(final)
                    try:
                        restored = projection(final)
                        exact_target = (kind != "vf_lock" or required(final, "lock_entries").get(str(request["domain"]))
                                        == request["previous_entry"])
                    except SkipControl:
                        restored, exact_target = None, False
                    if restored == before and exact_target:
                        break
                    if attempt != 2:
                        time.sleep(0.25)
                record["restored_request"] = restored
                record["restore_differences"] = differences(before, restored or {})
                record["restoration_verified"] = bool(record["restore_result"][0]) and restored == before and exact_target
            except BaseException as exc:
                record.update(restoration_verified=False, restore_exception=f"{type(exc).__name__}: {exc}")
            diagnostics(report, backend)
            record["finished_utc"] = utc_now()
            if not record["restoration_verified"]:
                record["status"] = "restoration_failed"
            report.flush()
            if not record["restoration_verified"]:
                raise RestorationFailed(f"{kind} control {control}: prior request state not restored; further writes aborted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slot", required=True, help="Explicit PCI slot, e.g. 0000:01:00.0")
    parser.add_argument("--output", required=True, help="Incrementally replaced JSON evidence file")
    parser.add_argument("--write", action="store_true", help="Opt in to bounded requests and checked restoration")
    parser.add_argument("--settle-seconds", type=float, default=0.25)
    args = parser.parse_args(argv)
    if not math.isfinite(args.settle_seconds) or not 0 <= args.settle_seconds <= 5:
        parser.error("settle-seconds must be finite and within 0..5")
    report = Report(Path(args.output), args)
    report.data["scope"] = "Native clock requests, captured adapter and operating point only; no generated load."
    report.data["limitations"] = [
        "SET status is not acceptance: each case requires independent configured-state readback.",
        "Physical clocks and voltage are observations, not exact requested-state acceptance criteria.",
        "Private clock mode fields are preserved, never interpreted or replayed. If supported frequency restoration "
        "cannot return a changed mode, the run stops with restoration_failed.",
        "No power, fan, rail, voltage boost, registry or opaque-buffer write is issued.",
        "Domain Undo is captured and restored through scoped profiles; the evidence file contains its snapshot.",
        "Inactive frequency-lock numeric fields are recorded but are not active requests. V/F target restoration "
        "also requires its original inactive numeric field.",
        "Other tuning processes are not excluded by the in-process card mutex.",
    ]
    gpu = backend = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from druta import gpu_backend as backend, native, native_frontend

        report.data["surface_missing_members"] = list(native_frontend.missing_members())
        gpu = backend.open_gpu(args.slot, kind="native")
        library = native.load()
        report.data["library"] = {"path": library.path, "loaded_abi": library.dc_abi_version(),
                                  "sha256": hashlib.sha256(Path(library.path).read_bytes()).hexdigest()}
        report.data["backend"] = {"description": backend.describe(), "choice": vars(backend.choice())}
        report.data["identity"] = identity(gpu)
        report.data["initial"] = capture(gpu)
        try:
            offered = required(report.data["initial"], "offered_controls")
            if not isinstance(offered, list) or any(type(c) is not int or not 0 <= c < 32 for c in offered):
                raise SkipControl("runtime offered-control list is malformed")
            domain_reason = "no private clock controls offered by runtime UI"
        except SkipControl as exc:
            offered, domain_reason = [], str(exc)
        report.flush()
        for control in sorted(set(offered)):
            operation("domain", control, gpu, report, args, backend, native.parse_slot)
        if not offered:
            report.data["operations"].append({"kind": "domain", "status": "skipped",
                                              "reason": domain_reason})
        for kind in ("vf_delta", "vf_lock", "frequency_lock"):
            operation(kind, None, gpu, report, args, backend, native.parse_slot)
        diagnostics(report, backend)
        failed = any(row["status"] in ("failed", "restoration_failed") for row in report.data["operations"])
        report.data["status"] = "completed_with_failures" if failed or report.data["diagnostics"] else "completed"
    except BaseException as exc:
        report.data.update(status="aborted", exception=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        if gpu is not None:
            report.data["final"] = capture(gpu)
            if backend is not None:
                diagnostics(report, backend)
            report.data["close"] = observe(gpu.close)
            if report.data["status"] == "completed" and (not report.data["close"]["ok"] or report.data["diagnostics"]):
                report.data["status"] = "completed_with_failures"
        report.data["finished_utc"] = utc_now()
        report.flush()
        print(f"{report.data['status']}: {report.path}", flush=True)
    return 0 if report.data["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
