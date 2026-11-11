#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in full-profile/Undo and Reset all live qualification, without a workload.

    python experiments/native_full_profile_reset_live_3070ti.py --slot 0000:01:00.0 --output profiles.json

Default: fresh read-only eligibility checks. --write exercises the real Druta
named-save, explicit-load, Undo and Reset all callbacks over the native backend.
Only rendering/refresh helpers are replaced. Profile files and autosaves stay in
a new isolated directory beside the report. No startup setting, registry value,
I2C controller, or opaque driver packet is replayed. No hardware is opened on
import or --help. Close other tuning applications before an opted-in run.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import math
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

try:
    from .native_clock_live_3070ti import (
        capture, projection, require_released, result_rows, differences,
        Report, RestorationFailed, SkipControl, diagnostics, fan_request,
        identity, observe, paired, plain, required, utc_now,
    )
except ImportError:
    from native_clock_live_3070ti import (
        capture, projection, require_released, result_rows, differences,
        Report, RestorationFailed, SkipControl, diagnostics, fan_request,
        identity, observe, paired, plain, required, utc_now,
    )


def check_profile(gpu, state, before):
    """Require a complete replayable profile, cross-checked with independent getters."""
    from druta import profiles

    missing = profiles.incomplete(state)
    error = profiles.preflight(gpu, state)
    if missing or error:
        raise SkipControl(f"full profile is not replayable: {missing}; {error}")
    if state.get("scope") is not None or state.get("i2c"):
        raise SkipControl("only a full native profile without I2C is eligible")
    require_released(before)
    if any(row["mode"] != 0 for row in before["locks"].values()):
        raise SkipControl("the lock table contains an active request outside the released-status getters")
    if (gpu.legacy_p0_owned() or gpu.hold_headroom_record()
            or getattr(gpu, "_foreign_hold_headroom", None)):
        raise SkipControl("legacy P0 or voltage-headroom ownership is active")
    from druta import vfheadroom
    if vfheadroom.marker_state(gpu.static.get("uuid"))[0] != "missing":
        raise SkipControl("an existing headroom recovery marker must be resolved before this test")
    expected = {
        "core_off_mhz": before["core_range"][2],
        "mem_off_units": before["memory_range"][2],
        "mem_off_scale": before["memory_scale"][0],
        "power_limit_mw": before["power_request_mw"],
        "volt_boost_pct": before["voltage_boost"],
        "vf_deltas": before["vf_delta_snapshot"]["deltas"],
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise SkipControl(f"full profile disagrees with independent request getters: {differences(expected, {k: state.get(k) for k in expected})}")
    if before["power_request_mw"] != before["nvml_power_request_mw"]:
        raise SkipControl("board-power request getters disagree")
    if fan_request(state.get("fan_control_state")) != before["fans"]:
        raise SkipControl("full profile lacks exact requested fan policy/duty")
    saved_domains = state.get("clock_domain_offsets_mhz", {})
    saved_msvdd = state.get("msvdd_offsets_mv", {})
    for key, row in before["domains"].items():
        if key in saved_domains:
            if saved_domains[key] * 1000 != row["freq_khz"] or row["freq_khz"] % 1000:
                raise SkipControl(f"domain {key} cannot be restored exactly by its whole-MHz setter")
        elif row["freq_khz"]:
            raise SkipControl(f"nonzero domain {key} is not captured by full profiles")
        if row.get("msvdd_uv", 0) != saved_msvdd.get(key, 0) * 1000:
            raise SkipControl(f"MSVDD request {key} is missing or inexact in the profile")
    for key, fields in before["rail_limits"].items():
        selected = gpu.volt_rail_limit_fields(int(key))
        if {field: fields[field] * 1000 for field in selected} != state.get("rail_limits_uv", {}).get(key):
            raise SkipControl(f"raw rail {key} fields are not fully captured")
    for key, value in before["rail_offsets"].items():
        if key == "0":
            if value != state.get("nvvdd_offset_mv"):
                raise SkipControl("NVVDD offset capture disagrees")
        elif value not in (None, 0, state.get("nvvdd_offset_mv")):
            raise SkipControl(f"uncaptured rail-0 request on private domain {key}")
    for row in before["current_limits"]:
        if state.get("current_limits_ma", {}).get(str(row["policy"])) != row["requested_ma"]:
            raise SkipControl("named current request is not captured exactly")
    for row in before["power_policies"]:
        saved = state.get("power_policies", {}).get(str(row["policy"]))
        if saved is not None and saved != {"value": row["requested"], "channel": row["channel"], "type": row["type"]}:
            raise SkipControl("power policy request capture disagrees")
    if {str(k): v["value"] for k, v in state.get("power_policy_pins", {}).items()} != before["power_policy_pins"]:
        raise SkipControl("power policy pin intent is not captured exactly")
    return state


def prepare(gpu, snapshot):
    from druta import profiles

    before = projection(snapshot)
    state = check_profile(gpu, profiles.capture(gpu), before)
    scale = before["memory_scale"][0]
    core, memory = before["core_range"], before["memory_range"]
    if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
        raise SkipControl("memory unit scale is invalid")
    targets = {"core_mhz": core[2] - 15, "memory_mhz": memory[2] / scale - 1,
               "power_mw": before["power_request_mw"] - 5000, "fan_pct": 40}
    step = required(snapshot, "core_step_khz")
    if type(step) is not int or step <= 0 or 15000 % step or core[2] * 1000 % step:
        raise SkipControl("15 MHz staging and exact baseline are not on the reported core grid")
    if not core[0] <= targets["core_mhz"] < core[2]:
        raise SkipControl("small core decrease is outside reported bounds")
    units = targets["memory_mhz"] * scale
    if units != round(units) or not memory[0] <= units < memory[2]:
        raise SkipControl("one displayed MHz memory decrease is not exactly representable/in bounds")
    caps = required(snapshot, "capabilities")
    if not caps["pl_min_mw"] <= targets["power_mw"] <= caps["pl_max_mw"]:
        raise SkipControl("5 W board decrease is outside reported bounds")
    if any(not row["min"] <= 40 <= row["max"] for row in before["fans"]["fans"]):
        raise SkipControl("40% fan request is outside a fan's own bounds")
    reset = reset_expected(gpu, snapshot, before)
    return before, state, targets, reset


def reset_expected(gpu, snapshot, before):
    """Known reset semantics only; preserve private modes and non-GPU VF rows."""
    wanted = copy.deepcopy(before)
    default = gpu.static.get("pl_def_mw")
    if type(default) is not int or not 0 < default:
        raise SkipControl("Reset all's board-power default is unreadable")
    wanted["core_range"][2] = wanted["memory_range"][2] = 0
    wanted["vf_delta_snapshot"]["deltas"] = {key: 0 for key in wanted["vf_delta_snapshot"]["deltas"]}
    for row in wanted["domains"].values():
        row["freq_khz"] = 0
        if "msvdd_uv" in row:
            row["msvdd_uv"] = 0
    for key, value in wanted["rail_offsets"].items():
        if value is not None:
            wanted["rail_offsets"][key] = 0
    wanted["power_request_mw"] = wanted["nvml_power_request_mw"] = default
    wanted["power_policy_pins"] = {}
    wanted["voltage_boost"] = 0
    for row in wanted["fans"]["fans"]:
        row.update(manual=False, policy=row["default_policy"], level=None)
    for row in wanted["current_limits"]:
        row["requested_ma"] = row["default_ma"]
    raw = {row["policy"]: row for row in result_rows(snapshot, "power_policies")}
    current_defaults = {row["policy"]: row["default_ma"] for row in wanted["current_limits"]}
    for row in wanted["power_policies"]:
        row["pinned"] = None
        if raw[row["policy"]].get("board"):
            row["requested"] = default
        elif row["policy"] in current_defaults:
            row["requested"] = current_defaults[row["policy"]]
        elif raw[row["policy"]]["writable"]:
            if not row["minimum"] <= row["default"] <= row["maximum"]:
                raise SkipControl(f"policy {row['policy']} lacks a resettable default")
            row["requested"] = row["default"]
    # Rail reset means this new GPU session's first-read raw controls. The
    # named-profile case never changes these fields, so baseline is authoritative.
    return wanted


class RenderOnly:
    """Small headless DPG boundary: callbacks, guards and write gates remain real."""
    mvAll = mvThemeCol_ChildBg = mvThemeCol_FrameBg = mvThemeCol_Border = mvThemeCol_TitleBgActive = 0

    def __init__(self):
        self.values = {"unlock": True, "xoc_mode": False, "vlim_mode": False,
                       "i2c_mode": False, "prof_name": "baseline-full"}

    def does_item_exist(self, key):
        return key in self.values

    def get_value(self, key):
        return self.values.get(key)

    def set_value(self, key, value):
        self.values[key] = value

    def configure_item(self, *args, **kwargs):
        pass

    def theme(self, *args, **kwargs):
        return contextlib.nullcontext(1)

    theme_component = theme

    def add_theme_color(self, *args, **kwargs):
        pass

    bind_item_theme = add_theme_color


@contextlib.contextmanager
def frontend(gpu, directory, record):
    from druta import druta, profiles

    ui = RenderOnly()
    flags = {key: getattr(gpu, key, False) for key in
             ("msvdd_write_enabled", "voltage_xoc_enabled", "volt_limits_write_enabled")}
    with contextlib.ExitStack() as stack:
        for owner, key, value in ((druta, "dpg", ui), (druta, "open_gpu", lambda slot: gpu),
                                  (druta, "enumerate_gpus", lambda: []), (druta, "dpi_scale", lambda: 1),
                                  (druta.shuntmod, "load", lambda: []),
                                  (druta.vfheadroom, "load", lambda: (False, 0)),
                                  (profiles, "DIR", directory)):
            stack.enter_context(patch.object(owner, key, value))
        app = druta.Druta(gpu.slot())
        record["messages"], record["restore_calls"], record["autosaves"] = [], [], []
        app.log = lambda message, ok=None: record["messages"].append({"ok": ok, "message": message})
        # Retain real sync_risk_ui, set_lock_state, refresh_full_profile and all
        # ownership paths. These children only render or refresh widget values.
        for name in ("refresh_profile_list", "sync_sliders_from_gpu", "sync_profile_rail_sliders",
                     "sync_knob_boxes", "refresh_volt_limits", "refresh_current_limits", "vf_read",
                     "refresh_power_request_range", "sync_slider_ranges", "draw_hold_banner"):
            setattr(app, name, lambda *args, **kwargs: None)
        limits = gpu.read_volt_rail_limits()
        app._rail_writable = {r for r, yes in app.rail_limit_support(limits, app.rail_limit_diagnostics()).items() if yes}
        restore = profiles.restore

        def tracked_restore(*args, **kwargs):
            item = {"profile": plain(args[1]), "started_utc": utc_now()}
            record["restore_calls"].append(item)
            try:
                item["results"] = plain(restore(*args, **kwargs))
                return item["results"]
            except BaseException as exc:
                item["exception"] = f"{type(exc).__name__}: {exc}"
                raise

        stack.enter_context(patch.object(profiles, "restore", tracked_restore))
        autosave = app.autosave_before

        def protected_autosave(action, **kwargs):
            # The production manual callback deliberately permits a failed Undo
            # capture. This qualification requires a durable complete capture.
            ok = autosave(action, **kwargs)
            entries = [row for row in profiles.list_profiles() if row[3]]
            saved = profiles.load(entries[0][0]) if ok and entries else None
            record["autosaves"].append({"action": action, "ok": ok, "state": saved})
            if not ok or saved is None:
                raise RuntimeError("qualification stopped before write: production Undo capture failed")
            check_profile(gpu, saved, projection(capture(gpu)))
            return True

        app.autosave_before = protected_autosave
        try:
            yield app, ui
        finally:
            for key, value in flags.items():
                setattr(gpu, key, value)


def readback(gpu, expected, record, key):
    snapshots = record.setdefault(key + "_readbacks", [])
    actual = None
    for attempt in range(3):
        state = capture(gpu)
        snapshots.append(state)
        try:
            actual = projection(state)
        except SkipControl:
            actual = None
        if actual == expected:
            break
        if attempt != 2:
            time.sleep(0.2)
    record[key + "_actual"] = actual
    record[key + "_differences"] = differences(expected, actual or {})
    return actual == expected


def restore_original(gpu, app, ui, original, expected, record, report):
    from druta import profiles

    record["restoration_started_utc"] = utc_now()
    # No fallible report I/O may prevent this mandatory restoration. The
    # original profile was journaled before the first write.
    try:
        ui.set_value("xoc_mode", bool(original.get("xoc")))
        ui.set_value("vlim_mode", bool(original.get("rail_limits_uv") or original.get("rail_limits_mv")))
        ui.set_value("i2c_mode", False)
        app.sync_risk_ui()
        results = profiles.restore(gpu, original)
        record["restore_original_results"] = results
        exact = readback(gpu, expected, record, "restored")
        record["restoration_verified"] = bool(results) and all(row[0] for row in results) and exact
    except BaseException as exc:
        record.update(restoration_verified=False, restoration_exception=f"{type(exc).__name__}: {exc}")
    if not record["restoration_verified"]:
        record["status"] = "restoration_failed"
        report.flush()
        raise RestorationFailed("original full profile was not restored exactly; further writes aborted")


def stage(gpu, targets, before, record, report, settle):
    """Only the four already-qualified ordinary controls are changed here."""
    steps = [
        ("core", lambda: gpu.set_clock_offset(0, targets["core_mhz"]), {"core_range", "vf_delta_snapshot"}),
        ("memory", lambda: gpu.set_clock_offset(2, targets["memory_mhz"]), {"memory_range"}),
        ("board", lambda: gpu.set_power_limit_mw(targets["power_mw"]),
         {"power_request_mw", "nvml_power_request_mw", "current_limits", "power_policies"}),
        ("fans", lambda: gpu.set_fan(targets["fan_pct"]), {"fans"}),
    ]
    previous = before
    record["staging"] = []
    for name, writer, allowed in steps:
        row = {"name": name, "before": previous}
        record["staging"].append(row)
        report.flush()
        row["result"] = plain(writer())
        time.sleep(settle)
        row["snapshot"] = capture(gpu)
        actual = projection(row["snapshot"])
        unexpected = set(differences(previous, actual)) - allowed
        target_ok = {
            "core": actual["core_range"][2] == targets["core_mhz"],
            "memory": actual["memory_range"][2] == targets["memory_mhz"] * actual["memory_scale"][0],
            "board": actual["power_request_mw"] == actual["nvml_power_request_mw"] == targets["power_mw"],
            "fans": all(f["manual"] and f["level"] == targets["fan_pct"] for f in actual["fans"]["fans"]),
        }[name]
        if name == "core":
            delta = (targets["core_mhz"] - previous["core_range"][2]) * 1000 * previous["vf_delta_snapshot"]["freq_div"]
            target_ok = target_ok and actual["vf_delta_snapshot"]["deltas"] == {
                key: value + delta for key, value in previous["vf_delta_snapshot"]["deltas"].items()}
        row.update(actual=actual, unexpected_fields=sorted(unexpected), verified=bool(row["result"][0]) and target_ok and not unexpected)
        report.flush()
        if not row["verified"]:
            raise RuntimeError(f"{name} staging failed independent request/preservation checks")
        previous = actual
    return previous


def named_profile(gpu, app, ui, original, before, targets, record, report, args):
    from druta import profiles

    app.save_profile()
    saved = profiles.load(ui.get_value("prof_name"))
    record["named_profile"] = check_profile(gpu, saved, before)
    if not readback(gpu, before, record, "saved"):
        raise RuntimeError("named profile save changed configured requests")
    changed = stage(gpu, targets, before, record, report, args.settle_seconds)
    changed_profile = check_profile(gpu, profiles.capture(gpu), changed)
    record["staged_profile"] = changed_profile
    prior = len(record["restore_calls"])
    app.load_profile(ui.get_value("prof_name"))
    calls = record["restore_calls"][prior:]
    record["explicit_load_verified"] = len(calls) == 1 and bool(calls[0].get("results")) and all(r[0] for r in calls[0]["results"]) and readback(gpu, before, record, "loaded")
    if not record["explicit_load_verified"]:
        raise RuntimeError("explicit named-profile load failed")
    autos = [row for row in profiles.list_profiles() if row[3]]
    undo = profiles.load(autos[0][0])
    record["undo_selected_name"], record["undo_selected_profile"] = autos[0][0], undo
    check_profile(gpu, undo, changed)
    prior = len(record["restore_calls"])
    app.undo_last_write()
    calls = record["restore_calls"][prior:]
    record["undo_verified"] = len(calls) == 1 and bool(calls[0].get("results")) and all(r[0] for r in calls[0]["results"]) and readback(gpu, changed, record, "undone")
    if not record["undo_verified"]:
        raise RuntimeError("Undo did not restore the exact staged full profile")


def reset_fields(name, gpu, before, snapshot):
    from druta.nvbackend import CLKDOM_NAMES

    direct = {"core offset": ["core_range"], "mem offset": ["memory_range"],
              "clock-domain offsets": ["domains"], "core rail offset": ["rail_offsets"],
              "rail limits": ["rail_limits"], "current limits": ["current_limits"],
              "power limit": ["power_request_mw", "nvml_power_request_mw"],
              "power policies": ["power_policies"], gpu.LOCK_STEP: ["clock_lock", "locks"],
              gpu.VF_LOCK_STEP: ["vf_lock", "locks"], "fan": ["fans"],
              "voltage boost": ["voltage_boost"], "vf curve": ["vf_delta_snapshot", "vf_other_rows"]}
    if name in direct:
        return direct[name]
    if name.startswith("MSVDD request ") or any(name == f"{CLKDOM_NAMES.get(int(key), 'domain ' + key)} offset" for key in before["domains"]):
        return ["domains"]
    if name.startswith("power policy "):
        return ["power_policies"]
    if any(name == row["label"].lower() for row in required(snapshot, "current_limits")):
        return ["current_limits"]
    raise RuntimeError(f"unrecognized Reset all result name: {name!r}")


def reset_case(gpu, app, expected, before, snapshot, record, report):
    real_reset = gpu.reset_all
    record["named_reset_results"] = []

    def tracked_reset():
        results = real_reset()
        record["named_reset_results"] = [{"name": row.name, "ok": bool(row[0]), "message": row[1]} for row in results]
        report.flush()
        return results

    with patch.object(gpu, "reset_all", tracked_reset):
        app.reset_all()  # real confirmation arm; no GPU write
        if not app._reset_armed or not readback(gpu, before, record, "armed"):
            raise RuntimeError("Reset all confirmation arm changed requests or did not arm")
        report.flush()
        app.reset_all()
    exact = readback(gpu, expected, record, "reset")
    actual = record["reset_actual"] or {}
    for row in record["named_reset_results"]:
        fields = reset_fields(row["name"], gpu, before, snapshot)
        row["expected"] = {key: expected[key] for key in fields}
        row["actual"] = {key: actual.get(key) for key in fields}
        row["readback_verified"] = row["expected"] == row["actual"]
        # A driver refusal is retained even if the already-released hold is
        # unchanged. This harness never relabels unsupported SET as success.
        row["passed"] = row["ok"] and row["readback_verified"]
    names = {row["name"] for row in record["named_reset_results"]}
    record["required_named_steps_present"] = {"core offset", "mem offset", "power limit", "fan", "vf curve", gpu.LOCK_STEP}.issubset(names)
    if not exact or not record["required_named_steps_present"] or not all(row["passed"] for row in record["named_reset_results"]):
        raise RuntimeError("Reset all has failed, missing, or mismatched named steps; inspect individual results")


def operation(kind, gpu, report, args, backend, parse_slot, directory):
    record = {"kind": kind, "started_utc": utc_now(), "status": "preflight"}
    report.data["operations"].append(record)
    report.flush()
    try:
        record["pairing"] = paired(gpu, args.slot, parse_slot)
        record["before"] = capture(gpu)
        before, original, targets, reset = prepare(gpu, record["before"])
        record.update(original_profile=original, baseline_request=before, targets=targets,
                      expected_reset=reset, status="eligible_read_only")
        if not args.write:
            return
        with frontend(gpu, directory, record) as (app, ui):
            # Recheck after callback setup and durably save all restoration
            # material before the first potentially partial write.
            if projection(capture(gpu)) != before:
                raise SkipControl("configured requests moved between capture and first write")
            record["status"] = "running"
            report.flush()
            try:
                if kind == "named_profile_load_undo":
                    named_profile(gpu, app, ui, original, before, targets, record, report, args)
                else:
                    changed = stage(gpu, targets, before, record, report, args.settle_seconds)
                    record["staged_before_reset"] = capture(gpu)
                    if projection(record["staged_before_reset"]) != changed:
                        raise RuntimeError("configured requests moved after reset staging")
                    reset_case(gpu, app, reset, changed, record["staged_before_reset"], record, report)
                record["status"] = "passed"
            except BaseException as exc:
                record.update(status="failed", exception=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            finally:
                restore_original(gpu, app, ui, original, before, record, report)
    except SkipControl as exc:
        record.update(status="skipped", reason=str(exc))
    finally:
        diagnostics(report, backend)
        record["finished_utc"] = utc_now()
        report.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--settle-seconds", type=float, default=0.25)
    args = parser.parse_args(argv)
    if not math.isfinite(args.settle_seconds) or not 0 <= args.settle_seconds <= 5:
        parser.error("settle-seconds must be finite and within 0..5")
    if Path(args.output).exists():
        parser.error("Choose a new output file; existing evidence is not overwritten")
    report = Report(Path(args.output), args)
    report.data["scope"] = "Manual native full-profile, Undo and named Reset all qualification on the captured adapter."
    report.data["limitations"] = [
        "No workload or stability claim; requested controls are independently verified.",
        "The real Druta manual callbacks run with rendering/refresh helpers replaced.",
        "Full restoration replays only production-profile understood controls; no opaque packets.",
        "Reset rail limits mean this session's first-read values, not verified factory defaults.",
        "No startup configuration, registry or I2C write; isolated profile files remain as evidence.",
        "Other tuning applications are not excluded by this process's card mutex.",
    ]
    directory = Path(tempfile.mkdtemp(prefix="full-profile-evidence-", dir=report.path.parent))
    report.data["profile_directory"] = str(directory)
    gpu = backend = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from druta import gpu_backend as backend, native, native_frontend

        gpu = backend.open_gpu(args.slot, kind="native")
        library = native.load()
        report.data["library"] = {"path": library.path, "sha256": hashlib.sha256(Path(library.path).read_bytes()).hexdigest()}
        report.data["identity"] = identity(gpu)
        report.data["surface_missing_members"] = list(native_frontend.missing_members())
        report.data["initial"] = capture(gpu)
        for kind in ("named_profile_load_undo", "reset_all"):
            operation(kind, gpu, report, args, backend, native.parse_slot, directory)
        report.data["status"] = "completed_with_failures" if report.data["diagnostics"] or any(row["status"] == "failed" for row in report.data["operations"]) else "completed"
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
