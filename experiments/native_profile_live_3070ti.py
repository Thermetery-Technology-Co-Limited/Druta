"""Opt-in live save/change/load/Undo test through the real memory UI path.

Only one GPU writer may run. Profiles are isolated from the owner's directory.
The captured memory request is restored in finally; no startup replay is enabled.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from native_live_smoke_3070ti import Report, identity, paired, plain, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--soak-seconds", type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.soak_seconds <= 600:
        parser.error("soak must be between 0 and 600 seconds")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    os.environ["DRUTA_BACKEND"] = "native"
    from druta import gpu_backend, native, profiles
    from druta.druta import Druta, dpg
    report = Report(Path(args.output), args)
    report.data["scope"] = "Manual scoped memory profile and Undo through actual UI methods on the captured adapter."
    report.data["limitations"] = ["No full-tune or combined-profile replay; no startup/driver-reset replay."]
    app = None
    baseline = None
    attempted = False
    context = False
    stop = threading.Event()
    polling = None
    try:
        app = Druta(args.slot)
        gpu = app.gpu
        paired(gpu, args.slot, native.parse_slot)
        report.data["identity"] = identity(gpu)
        report.data["before"] = snapshot(gpu)
        original = profiles.capture_tuning_action(gpu, "memory")
        baseline = original["tuning_action"]["value"]
        scale = original["tuning_action"]["scale"]
        report.data["original_profile"] = original
        report.flush()
        if args.write:
            lower, upper, now = gpu.clock_offset_range(2)
            assert now == baseline and lower <= baseline - scale <= upper
            changed = baseline - scale
            folder = Path(args.output).resolve().with_suffix("").with_name(Path(args.output).stem + "-profiles")
            folder.mkdir()
            profiles.DIR = str(folder)
            dpg.create_context()
            context = True
            with dpg.window():
                dpg.add_checkbox(tag="unlock", default_value=True)
                dpg.add_input_text(tag="prof_name", default_value="native-live-memory-baseline")
                dpg.add_input_float(tag="sl_mem", default_value=baseline / scale)
                dpg.add_input_float(tag="in_mem", default_value=baseline / scale)
            app._profile_save_scope = "tuning_action"
            app._profile_save_kind = "memory"
            app._profile_save_index = None
            if args.soak_seconds:
                def poll():
                    while not stop.is_set():
                        try:
                            telemetry = gpu.read()
                            report.data["soak"].append({"elapsed": time.monotonic() - soak_start,
                                                       "telemetry": plain(telemetry)})
                        except Exception:
                            report.data["soak"].append({"error": traceback.format_exc()})
                        stop.wait(0.5)
                soak_start = time.monotonic()
                polling = threading.Thread(target=poll, daemon=True)
                polling.start()
            app.save_profile()
            saved = profiles.load("native-live-memory-baseline")
            assert saved["tuning_action"] == original["tuning_action"]
            assert gpu.clock_offset_range(2)[2] == baseline
            report.data["saved_profile"] = saved
            report.data["save_changed_gpu"] = False
            for label, callback, expected in (
                ("change", lambda: app.apply_mem(changed / scale), changed),
                ("load", lambda: app.load_profile("native-live-memory-baseline"), baseline),
                ("undo_load", app.undo_last_write, changed),
                ("undo_undo", app.undo_last_write, baseline),
            ):
                operation = {"name": label, "expected_wire": expected, "status": "pending"}
                report.data["operations"].append(operation)
                report.flush()
                start = len(app.log_lines)
                attempted = True
                callback()
                operation["actual_wire"] = gpu.clock_offset_range(2)[2]
                operation["shown_mhz"] = dpg.get_value("in_mem")
                operation["log"] = app.log_lines[start:]
                operation["undo_files"] = [row[0] for row in profiles.list_profiles() if row[3]]
                assert operation["actual_wire"] == expected, operation
                assert operation["shown_mhz"] == expected / scale, operation
                assert not any(line.startswith("[!!]") for line in operation["log"]), operation
                operation["status"] = "passed"
                report.flush()
            if args.soak_seconds:
                # Existing read-only discovery, scoped to the route established
                # by prior identification; no regulator write/Verify is called.
                from druta import railctl
                messages = []
                with gpu._lock:
                    candidates = railctl.discover(gpu.nvapi, architecture=gpu.arch(),
                        controller="MPS MP2888A", routes=[(1, 0x20)],
                        log=lambda message, ok=None: messages.append({"message": message, "ok": ok}))
                report.data["i2c"] = {"scope": {"controller": "MPS MP2888A", "routes": [[1, 32]]},
                    "messages": messages, "candidates": [{"type": type(c).__name__,
                    "name": c.p.name, "port": getattr(c, "port", None), "addr7": getattr(c, "addr7", None),
                    "telemetry": getattr(c, "discovery_telemetry", None)} for c in candidates]}
                next_load = 30.0
                while time.monotonic() - soak_start < args.soak_seconds:
                    elapsed = time.monotonic() - soak_start
                    if elapsed >= next_load:
                        operation = {"name": "concurrent_profile_cycle", "elapsed": elapsed}
                        report.data["operations"].append(operation)
                        app.apply_mem(changed / scale)
                        operation["changed_wire"] = gpu.clock_offset_range(2)[2]
                        assert operation["changed_wire"] == changed
                        app.load_profile("native-live-memory-baseline")
                        operation["restored_wire"] = gpu.clock_offset_range(2)[2]
                        assert operation["restored_wire"] == baseline
                        operation["status"] = "passed"
                        report.flush()
                        next_load += 30
                    stop.wait(0.1)
                assert not any("error" in row for row in report.data["soak"])
        report.data["status"] = "completed"
    except BaseException:
        report.data["status"] = "failed"
        report.data["error"] = traceback.format_exc()
    finally:
        stop.set()
        if polling:
            polling.join(5)
            report.data["poll_thread_stopped"] = not polling.is_alive()
            if polling.is_alive():
                report.data["status"] = "poll_thread_failed_to_stop"
        if app is not None:
            if attempted:
                try:
                    report.data["restore_result"] = app.gpu.set_clock_offset(2, baseline / scale)
                    report.data["restored_wire"] = app.gpu.clock_offset_range(2)[2]
                    if not report.data["restore_result"][0] or report.data["restored_wire"] != baseline:
                        report.data["status"] = "restoration_failed"
                except Exception:
                    report.data["restore_error"] = traceback.format_exc()
                    report.data["status"] = "restoration_failed"
            report.data["after"] = snapshot(app.gpu)
            if attempted:
                # Curve frequencies are sampled; requested deltas are separate.
                names = set(report.data["before"]) - {"telemetry", "vf_curve"}
                report.data["changed_configured_sections"] = [key for key in sorted(names)
                    if report.data["before"][key] != report.data["after"][key]]
                if report.data["changed_configured_sections"]:
                    report.data["status"] = "restoration_failed"
            report.data["log"] = app.log_lines
            app.gpu.close()
        if context:
            dpg.destroy_context()
        report.data["diagnostics"] = gpu_backend.drain_diagnostics()
        report.flush()
    print(json.dumps({key: report.data.get(key) for key in
                     ("status", "error", "operations", "restored_wire", "changed_configured_sections", "diagnostics")}, default=plain, indent=2))
    return 0 if report.data["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
