#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in native headroom lifecycle test; default is read-only.

Select an existing V/F point at/below the current rail ceiling, never an
arbitrary maximum point. Test at most one measured voltage grain (<=6.25 mV)
inside normal absolute bounds, using the production write-ahead marker and
verification_p0 exact target restoration. No load generator or performance claim.
Only one process may tune the adapter. No GPU access on import or --help.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import math
import os
from pathlib import Path
import sys
import traceback

import native_limits_live_3070ti as limits
from native_live_smoke_3070ti import (
    Report, SkipControl, diagnostics, identity, observe, paired, plain, utc_now,
)

MAX_TEST_RAISE_MV = 6.25  # Experiment size bound, not a device compatibility gate.
HEADROOM_FIELDS = ("reliability", "alt_reliability", "overvoltage")
MARKER_FIELDS = ("rail", "hold_mv", "margin_mv", "ceiling_mv", "written_uv", "prior_uv")


def sync_file(path):
    """Flush an already atomically installed evidence file before the GPU SET."""
    with Path(path).open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


class DurableReport(Report):
    def flush(self):
        super().flush()
        sync_file(self.path)


def capture(gpu, p0):
    state = limits.capture(gpu)
    readers = {
        "vf_target": lambda: p0._vf_lock_entry(gpu, p0.VF_LOCK_DOMAIN),
        "vf_recovery": lambda: p0.vf_lock_recovery(gpu),
        "headroom_architecture": lambda: gpu.hold_headroom_architecture(),
        "headroom_grain": lambda: gpu.hold_headroom_grain_mv(),
        "headroom_unavailable": lambda: gpu.hold_headroom_rail_unavailable(0),
    }
    state.update({key: observe(reader) for key, reader in readers.items()})
    return state


def configured(state, keys=None):
    base_keys = None if keys is None else [k for k in keys if k not in ("vf_target", "vf_recovery", "flags")]
    result = limits.requests(state, base_keys)
    result["vf_target"] = limits.value(state, "vf_target")
    result["vf_recovery"] = limits.value(state, "vf_recovery", nullable=True)
    result["flags"] = limits.value(state, "flags")
    return result


def marker_view(record):
    return {key: copy.deepcopy(record[key]) for key in MARKER_FIELDS if key in record}


def plan_trial(gpu, state, marker_state, p0):
    """Read-only planning over this adapter's actual curve and native rail planner."""
    if marker_state[0] != "missing":
        raise SkipControl("an existing or unreadable production headroom marker must remain untouched")
    if limits.value(state, "headroom_architecture") is not True:
        raise SkipControl("native headroom architecture support is unavailable")
    if limits.value(state, "headroom_unavailable", nullable=True) is not None:
        raise SkipControl("current rail getters do not qualify native headroom")
    for key in ("hold", "foreign_hold", "vf_recovery", "owned_clock_lock_request"):
        if limits.value(state, key, nullable=True) is not None:
            raise SkipControl(f"existing {key} is outside this isolated test")
    for key in ("vf_lock", "clock_lock"):
        if limits.value(state, key) != [None, None]:
            raise SkipControl("an existing held point or frequency lock will not be overridden")
    previous = limits.value(state, "vf_target")
    if len(previous) != 2 or previous[0] != p0.VF_LOCK_MODE_OFF:
        raise SkipControl("the exact prior V/F target is unavailable or already in use")
    if not limits.value(state, "rail_supported"):
        raise SkipControl("rail 0 limits are not currently qualified")
    row = limits.value(state, "rail_limits").get("0")
    if not row or row.get("_absolute_ok") is not True:
        raise SkipControl("rail 0 has no valid current-card absolute reference")
    if not set(HEADROOM_FIELDS).issubset(limits.value(state, "rail_fields")):
        raise SkipControl("all headroom ceiling fields must be validated")
    limits.integer(limits.value(state, "boost"), "voltage boost")
    live = limits.value(state, "rail_state").get("0", {}).get("live")
    if type(live) not in (float, int) or not math.isfinite(live) or live <= 0:
        raise SkipControl("a live rail observation is required for the native live ceiling check")
    ceiling = float(gpu.hold_headroom_ceiling_mv(row))
    grain = limits.value(state, "headroom_grain")
    if type(grain) not in (float, int) or not math.isfinite(grain) or not 0 < grain <= MAX_TEST_RAISE_MV:
        raise SkipControl("the measured voltage grain exceeds this experiment's small-raise bound")
    points, error = limits.value(state, "vf_curve")
    if error or not points or not math.isfinite(ceiling):
        raise SkipControl("the native ceiling or V/F curve is unreadable")
    low, high = float(gpu.VOLT_LIMIT_MIN_MV), float(gpu.VOLT_LIMIT_MAX_MV)
    candidates = [p for p in points if low <= p["volt_mv"] <= min(ceiling, high)
                  and 0 < p["volt_mv"] + grain - ceiling <= grain + 1e-6]
    if not candidates:
        raise SkipControl("no validated point at/below the current ceiling yields a one-grain raise")
    # Closest point below the active ceiling, not the curve's maximum entry.
    point = max(candidates, key=lambda p: p["volt_mv"])
    hold = float(point["volt_mv"])
    if hold + grain > high:
        raise SkipControl("the one-grain headroom target would exceed normal absolute bounds")
    targets, error = gpu.hold_headroom_targets(row, hold, grain, high)
    if error or not targets or not set(targets).issubset(HEADROOM_FIELDS):
        raise SkipControl(f"native planner offers no bounded ceiling change: {error}")
    raw = configured(state)["rail_deltas"]["0"]
    prior, written = {}, {}
    for key, target in targets.items():
        base = row["_base_mv"][key]
        original_abs = base + raw[key] / 1000
        if not low <= original_abs < target <= high or target - original_abs > grain + 1e-6:
            raise SkipControl(f"{key} raise/restoration is outside the one-grain normal envelope")
        prior[key], written[key] = raw[key], limits.microvolts(target - base)
    return {"rail": 0, "point": point, "hold_mv": hold, "hold_uv": limits.microvolts(hold),
            "margin_mv": grain, "ceiling_mv": ceiling, "targets_mv": targets,
            "prior_uv": prior, "written_uv": written,
            "input_uv": {key: raw[key] for key in HEADROOM_FIELDS},
            "boost_mv": row["_boost_mv"], "previous_target": previous,
            "reference": "current-card quantized first-read estimate; not a factory default"}


def validate_record(record, plan, baseline):
    """Admit the actual native callback record only if it matches our captured request."""
    if not isinstance(record, dict):
        raise RuntimeError("native before_write record is absent")
    for key in ("rail", "hold_mv", "margin_mv", "ceiling_mv", "prior_uv", "written_uv", "input_uv", "boost_mv"):
        if record.get(key) != plan[key]:
            raise RuntimeError(f"native before_write {key} differs from the bounded plan")
    if record.get("grain_mv") != plan["margin_mv"]:
        raise RuntimeError("native headroom grain changed during preparation")
    offset = record.get("live_offset_mv")
    if type(offset) not in (int, float) or not math.isfinite(offset) or not 0 <= offset <= plan["margin_mv"]:
        raise RuntimeError("native live offset does not fit the bounded margin")
    captured = baseline["rail_deltas"]["0"]
    for key, prior in record["prior_uv"].items():
        target = record["written_uv"][key]
        if (key not in HEADROOM_FIELDS or type(prior) is not int or type(target) is not int
                or prior != captured[key] or not 0 < target - prior <= round(plan["margin_mv"] * 1000)):
            raise RuntimeError("native record does not preserve captured prior rail requests")


def clear_owned_marker(markers, uuid, record):
    state, current = markers.marker_state(uuid)
    if state == "missing":
        return {"ok": True, "message": "no marker remains"}
    if (state != "ok" or markers.owner_state(current.get("owner")) != "self"
            or marker_view(current) != marker_view(record)):
        return {"ok": False, "message": "marker ownership/content changed; evidence left untouched"}
    ok, message = markers.clear_active(uuid, force=False)
    after, _ = markers.marker_state(uuid)
    return {"ok": ok is True and after == "missing", "message": message, "state_after": after}


def run_trial(gpu, report, args, backend, native, p0, markers):
    trial = {"kind": "native_hold_headroom", "status": "preflight", "callback_records": []}
    report.data["operations"].append(trial)
    baseline = plan = None
    marker_record = None
    marker_written = apply_attempted = verification_attempted = False
    rails_restored = False
    with gpu._lock:
        try:
            trial["binding"] = paired(gpu, args.slot, native.parse_slot)
            uuid = gpu.static.get("uuid")
            if not isinstance(uuid, str) or not uuid.strip():
                raise SkipControl("a UUID is required for the production write-ahead marker")
            trial["marker_path"] = str(markers.marker_path(uuid))
            trial["marker_before"] = plain(markers.marker_state(uuid))
            trial["before"] = capture(gpu, p0)
            baseline = configured(trial["before"])
            plan = plan_trial(gpu, trial["before"], trial["marker_before"], p0)
            trial["plan"] = plan
            if not args.write:
                trial["status"] = "read_only"
                return
            trial["confirmation"] = capture(gpu, p0)
            if configured(trial["confirmation"], baseline) != baseline:
                raise SkipControl("configured state changed between preflight reads")
            if markers.marker_state(uuid)[0] != "missing":
                raise SkipControl("a headroom marker appeared during preflight")
            if paired(gpu, args.slot, native.parse_slot) != trial["binding"]:
                raise SkipControl("GPU pairing changed during preflight")
            trial["status"] = "journaled_before_temporary_point"
            report.flush()
            verification_attempted = True
            with gpu.verification_p0(voltage_mv=plan["hold_mv"]) as check_hold:
                check_hold()
                trial["held_before"] = capture(gpu, p0)
                held = configured(trial["held_before"], baseline)
                expected_target = [p0.VF_LOCK_MODE_POINT, plan["hold_uv"]]
                if held["vf_target"] != expected_target:
                    raise RuntimeError("temporary verification point does not match the explicit curve point")
                lock, error = held["vf_lock"]
                if (error or lock is None or lock["domain"] != p0.VF_LOCK_DOMAIN
                        or lock["volt_uV"] != plan["hold_uv"]):
                    raise RuntimeError("temporary point lock is not verified")
                unrelated = limits.differences(baseline, held)
                if any(key not in ("vf_lock", "vf_target") for key in unrelated):
                    raise RuntimeError(f"temporary point changed unrelated requests: {unrelated}")

                def before_write(record):
                    nonlocal marker_written, marker_record
                    check_hold()
                    if trial["callback_records"]:
                        raise RuntimeError("this experiment permits only one native headroom write plan")
                    validate_record(record, plan, held)
                    if markers.marker_state(uuid)[0] != "missing":
                        raise RuntimeError("a marker appeared before native headroom SET")
                    trial["callback_records"].append(copy.deepcopy(record))
                    marker_record = copy.deepcopy(record)
                    trial["status"] = "native_plan_journaled_before_set"
                    report.flush()
                    ok, message = markers.mark_active(uuid, record)
                    trial["marker_write"] = {"ok": ok, "message": message}
                    if not ok:
                        report.flush()
                        return False
                    marker_written = True
                    marker_status, stored = markers.marker_state(uuid)
                    if (marker_status != "ok" or markers.owner_state(stored.get("owner")) != "self"
                            or marker_view(stored) != marker_view(record)):
                        raise RuntimeError("write-ahead marker did not read back with exact self ownership")
                    sync_file(markers.marker_path(uuid))
                    trial["marker_fsynced_before_set"] = True
                    trial["marker_record"] = stored
                    report.flush()
                    return True

                try:
                    apply_attempted = True
                    trial["apply_result"] = plain(gpu.apply_hold_headroom(
                        plan["hold_mv"], plan["margin_mv"], rail=0, before_write=before_write))
                    trial["applied"] = capture(gpu, p0)
                    applied = configured(trial["applied"], baseline)
                    active = applied["hold"]
                    if not limits.success(trial["apply_result"]) or not marker_written or not active:
                        raise RuntimeError("native headroom did not complete a tracked, active raise")
                    validate_record(active, plan, held)
                    if set(active) - set(marker_record) != {"raised_ceiling_mv"}:
                        raise RuntimeError("native active record has unexpected fields")
                    raised = active["raised_ceiling_mv"]
                    if type(raised) not in (float, int) or not math.isfinite(raised) or not (
                            plan["ceiling_mv"] < raised <= plan["hold_mv"] + plan["margin_mv"] + 1e-6):
                        raise RuntimeError("verified raised ceiling exceeds the one-grain plan")
                    expected = copy.deepcopy(held)
                    expected["hold"] = active
                    expected["rail_deltas"]["0"].update(plan["written_uv"])
                    trial["unexpected_request_sections"] = limits.differences(expected, applied)
                    trial["active_requests_exact"] = expected == applied
                    trial["live_check"] = plain(gpu.hold_headroom_live_ok(active))
                    check_hold()
                    if expected != applied or not limits.success(trial["live_check"]):
                        raise RuntimeError("active request preservation or native live ceiling check failed")
                    trial["status"] = "passed"
                finally:
                    # No fallible file operation is allowed before mandatory rail restoration.
                    if apply_attempted:
                        try:
                            trial["restore_result"] = plain(gpu.restore_hold_headroom(force=False))
                        except BaseException as exc:
                            trial["restore_error"] = f"{type(exc).__name__}: {exc}"
                        try:
                            trial["restored_while_held"] = capture(gpu, p0)
                            restored = configured(trial["restored_while_held"], baseline)
                            trial["remaining_held_request_sections"] = limits.differences(held, restored)
                            rails_restored = limits.success(trial.get("restore_result")) and restored == held
                        except BaseException as exc:
                            trial["restore_check_error"] = f"{type(exc).__name__}: {exc}"
                        trial["rails_restored_while_point_held"] = rails_restored
                        if marker_written and rails_restored:
                            trial["marker_clear"] = clear_owned_marker(markers, uuid, marker_record)
                        elif marker_written:
                            trial["marker_retained"] = "rail restoration could not be verified"
                # verification_p0 restores the exact original mode/value on context exit.
        except SkipControl as exc:
            trial.update(status="skipped" if not verification_attempted else "failed", reason=str(exc))
        except BaseException as exc:
            trial.update(status="failed", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        finally:
            if verification_attempted:
                # One bounded production recovery retry, only for the exact temporary request
                # this experiment acquired. Never synthesize or adopt a recovery record.
                pending = observe(lambda: p0.vf_lock_recovery(gpu))
                trial["point_recovery_after_context"] = pending
                record = pending.get("value") if pending["ok"] else None
                if (isinstance(record, dict) and record.get("verification") is True
                        and record.get("domain") == p0.VF_LOCK_DOMAIN
                        and record.get("previous") == plan["previous_target"]
                        and record.get("requested") == [p0.VF_LOCK_MODE_POINT, plan["hold_uv"]]):
                    try:
                        trial["owned_point_recovery_retry"] = plain(gpu.recover_vf_lock())
                    except BaseException as exc:
                        trial["owned_point_recovery_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    trial["final"] = capture(gpu, p0)
                    final = configured(trial["final"], baseline)
                    trial["remaining_request_sections"] = limits.differences(baseline, final)
                    trial["restoration_verified"] = final == baseline and (not apply_attempted or rails_restored)
                except BaseException as exc:
                    trial["restoration_verified"] = False
                    trial["final_check_error"] = f"{type(exc).__name__}: {exc}"
                if marker_written:
                    trial["marker_final"] = plain(markers.marker_state(uuid))
                    if not trial.get("marker_clear", {}).get("ok"):
                        trial["restoration_verified"] = False
                if not trial["restoration_verified"]:
                    trial["status"] = "restoration_failed"
            trial["finished_utc"] = utc_now()
            diagnostics(report, backend)
            report.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true", help="Opt in to one owned point and one tracked headroom raise")
    args = parser.parse_args(argv)
    report = DurableReport(Path(args.output), args)
    report.data.update(scope="Native headroom apply/release and production write-ahead marker lifecycle.",
                       limitations=["No workload or performance-benefit claim.",
                                    "One measured grain, capped at 6.25 mV, within normal absolute bounds.",
                                    "Existing holds, recovery records and markers are never adopted or overridden.",
                                    "Only the production owned-field restore and exact point recovery paths write.",
                                    "Other tuning processes are not excluded by the in-process mutex."])
    gpu = backend = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from druta import gpu_backend as backend, native, native_frontend, native_legacy_p0 as p0, vfheadroom as markers

        gpu = backend.open_gpu(args.slot, kind="native")
        library = native.load()
        report.data.update(identity=identity(gpu), backend=backend.describe(),
                           library={"path": library.path, "expected_abi": native.ABI_VERSION,
                                    "loaded_abi": library.dc_abi_version(),
                                    "sha256": hashlib.sha256(Path(library.path).read_bytes()).hexdigest()},
                           surface_missing_members=list(native_frontend.missing_members()))
        run_trial(gpu, report, args, backend, native, p0, markers)
        status = report.data["operations"][-1]["status"]
        report.data["status"] = "completed" if status in ("passed", "read_only", "skipped") else "completed_with_failures"
    except BaseException as exc:
        report.data.update(status="aborted", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        if gpu is not None:
            report.data["close"] = observe(lambda: gpu.close())
            diagnostics(report, backend)
            if report.data["status"] == "completed" and (not report.data["close"]["ok"] or report.data["diagnostics"]):
                report.data["status"] = "completed_with_failures"
        report.data["finished_utc"] = utc_now()
        report.flush()
        print(f"{report.data['status']}: {report.path}", flush=True)
    return 0 if report.data["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
