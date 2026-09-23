"""Bounded, reversible rail tests on the two named TITAN boards / R470.

Legacy RM command/geometry were captured from the existing NVAPI voltage-boost
setter: 0x20803213 GET and 0x20803214 SET, 648-byte parameters, mask/boost then
32 records of type plus four deltas. Never reuse Blackwell's type-5 packet.
"""
import argparse
import ctypes
import json
from pathlib import Path
import sys
import time

# When run as module, parent package is accessible
from .. import nvbackend as n
from .probe_volt_rails import rm_call, snapshot, stable_fields, transport_ok

CONTROL_VERSION = 0x10AC8


def snap(gpu):
    return snapshot(gpu, CONTROL_VERSION)


def write(gpu, params):
    out = rm_call(gpu, params, CONTROL_VERSION)
    if not transport_ok(out):
        raise RuntimeError(f"Rail transport failed: {out}")
    return out


def load_tests(gpu, report, save, params):
    from ..gpuload import BandwidthLoad

    load = BandwidthLoad(max_seconds=60, slot=gpu.slot())
    report["load_tests"] = run = {"offset_trials": [], "ceiling_trials": []}

    def require(result):
        if not result[0]:
            raise RuntimeError(result[1])
        return result

    def samples():
        rows = []
        for _ in range(6):
            data = gpu.read()
            if data.get("temp_edge", 0) >= 80:
                raise RuntimeError("Temperature stop")
            rows.append({"telemetry": {k: data.get(k) for k in
                                      ("core", "vcore_mv", "power_w", "temp_edge", "pstate")},
                         "rails": snap(gpu)["VoltRailsAbs"][1]["words"][18:27]})
            time.sleep(.15)
        return rows

    try:
        load.start()
        if not load.wait_started() or load.device_name != gpu.static["name"]:
            raise RuntimeError("CUDA load target mismatch")
        run["frequency_lock"] = gpu.lock_gpu_clocks(1500, 1500)
        if not run["frequency_lock"][0] and gpu.nvapi.selected["devid"] != 0x1B02:
            raise RuntimeError(run["frequency_lock"][1])
        original_offset = gpu.read_rail_offset_mv()
        if original_offset is None:
            raise RuntimeError("Cannot preserve NVVDD offset")
        for add_mv in (0, 12.5, 0, 12.5, 0):
            trial = {"added_offset_mv": add_mv,
                     "write": require(gpu.set_rail_offset_mv(original_offset + add_mv))}
            run["offset_trials"].append(trial)
            time.sleep(.3)
            trial["readback_mv"] = gpu.read_rail_offset_mv()
            trial["samples"] = samples()
            print("offset", add_mv, [s["telemetry"]["vcore_mv"] for s in trial["samples"]], flush=True)
            save()
        if run["frequency_lock"][0]:
            require(gpu.reset_gpu_clocks())
        points, err = gpu.read_vf_curve()
        if err:
            raise RuntimeError(err)
        changed, old_top, new_top, meta = n.GPU.compute_deflatten(
            points, 1112.5, step_khz=gpu.clock_step_khz())
        if not meta.get("unique") or new_top - old_top > gpu.clock_step_khz() / 1000 + .001:
            raise RuntimeError("Unexpected de-flatten plan")
        before_by_idx = {p["idx"]: p for p in points}
        divisor = gpu.vfp_layout().freq_div
        deltas = {c[0]: before_by_idx[c[0]]["delta_khz"]
                  + round((c[4] - before_by_idx[c[0]]["delta_khz"]) * divisor)
                  for c in changed}
        run["curve_before"], run["plan"] = points, changed
        run["curve_write"] = require(gpu.apply_vf_deltas(deltas))
        run["curve_after"], err = gpu.read_vf_curve()
        resolved = n.GPU.resolve_vf_point(run["curve_after"], 1112.5)
        if err or not resolved or resolved["volt_mv"] != 1112.5:
            raise RuntimeError("Curve did not make 1112.5 mV unique")
        run["point_lock"] = require(gpu.set_vf_lock(1112500))
        for raised in (False, True, False, True):
            changed = params.copy()
            changed[1] = 100
            changed[3] = changed[4] = 31250 if raised else 0
            trial = {"raised": raised, "write": write(gpu, changed)}
            run["ceiling_trials"].append(trial)
            time.sleep(.35)
            trial["samples"] = samples()
            print("ceiling", raised, [s["telemetry"]["vcore_mv"] for s in trial["samples"]], flush=True)
            save()
    finally:
        load.stop()
        load.join(3)


def live_field_tests(gpu, report, save, params, include_idle=True):
    """Separate each ceiling's live effect, then verify the idle floor."""
    from ..gpuload import BandwidthLoad
    load = BandwidthLoad(max_seconds=45, slot=gpu.slot())
    report["live_fields"] = trials = []

    def samples():
        rows = []
        for _ in range(6):
            data = gpu.read()
            if data.get("temp_edge", 0) >= 80:
                raise RuntimeError("Temperature stop")
            rows.append({k: data.get(k) for k in
                         ("core", "vcore_mv", "power_w", "temp_edge", "pstate")})
            time.sleep(.15)
        return rows

    try:
        load.start()
        if not load.wait_started() or load.device_name != gpu.static["name"]:
            raise RuntimeError("CUDA load target mismatch")
        # A bandwidth-only workload need not request enough core voltage on
        # Turing. Select an existing 1000 mV point so each lower clamp has a
        # baseline above it; main() restores the captured lock bytes.
        demand = gpu.set_vf_lock(1000000)
        report["live_field_demand"] = demand
        if not demand[0]:
            raise RuntimeError(demand[1])
        time.sleep(.8)
        for field, word, absolute_word in (
                ("reliability", 3, 20), ("alt_reliability", 4, 21),
                ("overvoltage", 5, 22)):
            before = snap(gpu)
            trial = {"field": field, "target_mv": 875,
                     "baseline_samples": samples()}
            trials.append(trial)
            changed = params.copy()
            changed[word] = (changed[word] + 875000
                             - before["VoltRailsAbs"][1]["words"][absolute_word]) & 0xffffffff
            try:
                trial["write"] = write(gpu, changed)
                time.sleep(.3)
                trial["after"] = snap(gpu)
                trial["samples"] = samples()
                trial["absolute_matches"] = trial["after"]["VoltRailsAbs"][1]["words"][absolute_word] == 875000
                trial["live_effect"] = (max(s["vcore_mv"] for s in trial["samples"]) <= 875
                                         and min(s["vcore_mv"] for s in trial["baseline_samples"]) > 925)
                print(field, [s["vcore_mv"] for s in trial["samples"]], flush=True)
                if not trial["absolute_matches"] or not trial["live_effect"]:
                    raise RuntimeError("Individual live clamp was not established")
            finally:
                trial["restore"] = write(gpu, params)
                time.sleep(.25)
                trial["restored_samples"] = samples()
                save()
        load.stop()
        load.join(3)
        if not include_idle:
            return
        # The driver can retain P2 for several seconds after CUDA teardown.
        # A high baseline cannot demonstrate an idle minimum, so wait for
        # the independently reported voltage to leave that retained state.
        for _ in range(100):
            idle = gpu.read()
            if idle.get("vcore_mv", 9999) < 850:
                break
            time.sleep(.1)
        else:
            raise RuntimeError("GPU did not reach a low-voltage idle baseline")
        trial = {"field": "vmin", "target_mv": 875,
                 "baseline_samples": samples()}
        trials.append(trial)
        before = snap(gpu)
        changed = params.copy()
        changed[6] = (changed[6] + 875000 - before["VoltRailsAbs"][1]["words"][24]) & 0xffffffff
        try:
            trial["write"] = write(gpu, changed)
            time.sleep(.3)
            trial["after"] = snap(gpu)
            trial["samples"] = samples()
            trial["live_effect"] = (min(s["vcore_mv"] for s in trial["samples"]) >= 875
                                     and max(s["vcore_mv"] for s in trial["baseline_samples"]) < 850)
            print("vmin", [s["vcore_mv"] for s in trial["samples"]], flush=True)
            if not trial["live_effect"]:
                raise RuntimeError("Live idle floor was not established")
        finally:
            trial["restore"] = write(gpu, params)
            time.sleep(.3)
            trial["restored_samples"] = samples()
            save()
    finally:
        load.stop()
        load.join(3)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gpu", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--fields", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--live-fields", action="store_true")
    ap.add_argument("--live-clamps", action="store_true")
    args = ap.parse_args()
    gpu = n.GPU(args.gpu)
    a = gpu.nvapi
    board = (a.selected["devid"], a.selected["subsys"], gpu.static["vbios"].lower())
    if gpu.static["driver"] != "472.12" or board not in (
            (0x1E02, 312676574, "90.02.1e.00.02"),
            (0x1B02, 299831518, "86.02.3d.00.01")):
        raise RuntimeError("Unvalidated driver/board for this probe")
    if not a.ok or not gpu.nvml.ok or gpu.pairing_error:
        raise RuntimeError("Pairing unavailable")
    before = snap(gpu)
    baseline = rm_call(gpu, control_version=CONTROL_VERSION)
    if not transport_ok(baseline):
        raise RuntimeError("Legacy rail getter failed")
    params = baseline["output_params"]
    if len(params) != 162 or params[0] != 1 or params[2] != 1:
        raise RuntimeError("Unexpected legacy rail layout")
    table = n._BoostTable(version=a.ver(n._BoostTable, 1))
    layout = gpu.vfp_layout()
    if layout is None:
        raise RuntimeError("Cannot preserve V/F layout")
    n._set_point_masks(table, layout.n_entries)
    if a.BoostTableGet(a.gpu, ctypes.byref(table)) != 0:
        raise RuntimeError("Cannot preserve V/F table")
    lock = gpu._vf_lock_read_raw()
    status, domains = gpu._clkdom_get(1)
    if lock is None or status != 0:
        raise RuntimeError("Cannot preserve locks and domain controls")
    report = {"static": gpu.static, "before": before, "baseline_rm": baseline,
              "original_table_hex": bytes(table).hex(),
              "original_lock_hex": bytes(lock).hex(),
              "original_domain_hex": bytes(domains).hex(), "trials": []}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)

    def save():
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        report["identity"] = write(gpu, params)
        report["identity_after"] = snap(gpu)
        if stable_fields(before) != stable_fields(report["identity_after"]):
            raise RuntimeError("Identity write changed rail state")
        if args.fields:
            for field, word, absolute_word, delta in (
                    ("reliability", 3, 20, -12500),
                    ("alt_reliability", 4, 21, -12500),
                    ("overvoltage", 5, 22, -12500),
                    ("vmin", 6, 24, 12500)):
                trial = {"field": field, "delta_uv": delta}
                report["trials"].append(trial)
                changed = params.copy()
                changed[word] = (changed[word] + delta) & 0xffffffff
                try:
                    trial["write"] = write(gpu, changed)
                    time.sleep(.25)
                    trial["after"] = snap(gpu)
                    expected = before["VoltRailsAbs"][1]["words"][absolute_word] + delta
                    actual = trial["after"]["VoltRailsAbs"][1]["words"][absolute_word]
                    trial["absolute_matches"] = actual == expected
                    print(field, "expected", expected, "actual", actual, flush=True)
                    if not trial["absolute_matches"]:
                        raise RuntimeError("Absolute rail field did not move as expected")
                finally:
                    trial["restore"] = write(gpu, params)
                    trial["after_restore"] = snap(gpu)
                    trial["restored"] = stable_fields(trial["after_restore"]) == stable_fields(before)
                    save()
                    if not trial["restored"]:
                        raise RuntimeError("Field restoration mismatch")
            for pct in (100, 0, 100):
                trial = {"boost_percent": pct}
                report["trials"].append(trial)
                changed = params.copy()
                changed[1] = pct
                trial["write"] = write(gpu, changed)
                trial["after"] = snap(gpu)
                print("boost", pct, "absolute", trial["after"]["VoltRailsAbs"][1]["words"][20:25], flush=True)
                save()
        if args.load:
            load_tests(gpu, report, save, params)
        if args.live_fields:
            live_field_tests(gpu, report, save, params)
        elif args.live_clamps:
            live_field_tests(gpu, report, save, params, include_idle=False)
    finally:
        report["restore"] = write(gpu, params)
        report["domain_restore_status"] = a.ClkDomCtlSet(a.gpu, ctypes.byref(domains))
        report["table_restore_status"] = a.BoostTableSet(a.gpu, ctypes.byref(table))
        report["lock_restore_status"] = a.VfLockSet(a.gpu, ctypes.byref(lock))
        report["after"] = snap(gpu)
        readback = n._BoostTable(version=a.ver(n._BoostTable, 1))
        n._set_point_masks(readback, layout.n_entries)
        report["table_restored"] = (a.BoostTableGet(a.gpu, ctypes.byref(readback)) == 0
                                    and bytes(readback) == bytes(table))
        report["locks_restored"] = bytes(gpu._vf_lock_read_raw()) == bytes(lock)
        ds, db = gpu._clkdom_get(1)
        report["domains_restored"] = ds == 0 and bytes(db) == bytes(domains)
        report["rails_restored"] = stable_fields(report["after"]) == stable_fields(before)
        save()
        if not all(report[k] for k in ("table_restored", "locks_restored", "domains_restored", "rails_restored")):
            raise RuntimeError("Restoration mismatch; inspect saved evidence")
    print(path, "all restored", flush=True)


if __name__ == "__main__":
    main()
