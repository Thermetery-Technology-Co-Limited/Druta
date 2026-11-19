"""Read-only native-backed advanced-control inventory; no tuning requests.

Run from any directory with DRUTA_NATIVE_LIB selecting the reviewed DLL:
  python experiments/native_advanced_inventory_3070ti.py --slot 0000:01:00.0 --output advanced-inventory.json

The runtime controllers below retain their Python policy logic while using the
selected native GPU's paired driver transport. Reading support is not evidence
that a write works, and an unavailable field is preserved as reported.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments"))
from native_live_smoke_3070ti import identity, paired, plain, utc_now


# Constructors and read() were traced in source. Profiler-controller
# constructors capture routing only; allocation/request starts in hold(),
# which this script never calls. Their close()/release() methods are likewise
# deliberately absent because no live request or profiler object is owned.
CONTROLLERS = (
    ("elpg", "gr_gating_live", "GrGatingController"),
    ("elcg", "engine_clock_gating_live", "EngineClockGatingController"),
    ("blcg", "block_clock_gating_live", "BlockClockGatingController"),
    ("slcg", "second_level_clock_gating_live", "SecondLevelClockGatingController"),
    ("difr", "difr_live_control", "DifrController"),
    ("pcie_speed", "pcie_speed_live", "PcieSpeedController"),
    ("vf_policies", "clock_vf_runtime", "ClockVfController"),
    ("voltage_rel", "voltage_rel_live", "VoltageRelController"),
    ("voltage_constants", "voltage_constants_live", "VoltageConstantsController"),
    ("voltage_switch_delay", "voltage_device_runtime", "VoltageDeviceController"),
    ("power_integral", "power_integral_runtime", "PowerIntegralController"),
    ("voltage_request_gate", "voltage_request_gate", "VoltageRequestGate"),
)
INDIVIDUAL_INDICES = (0, 4, 10, 11, 12, 13, 14, 15)
ADAPTIVE_INDICES = (0, 2, 4, 5, 6, 7)


def flush(path, report):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(plain(report), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Windows readers can briefly prevent replacement without delete sharing.
    for attempt in range(10):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def class_read(gpu, module, name, args):
    controller_type = getattr(importlib.import_module("druta." + module), name)
    controller = controller_type(gpu, *args)
    result = controller.read()
    profiler = getattr(controller, "_profiler", None)
    ownership = {
        "owned_request": getattr(controller, "_owned", None),
        "profiler_handle": getattr(profiler, "handle", None),
        "profiler_requested": getattr(profiler, "requested", None),
    }
    if ownership["owned_request"] is True or ownership["profiler_handle"] is not None or ownership["profiler_requested"] is True:
        raise RuntimeError("Read-only inventory unexpectedly owns a live request/object")
    return {"snapshot": result, "read_only_ownership": ownership}


def observe(report, path, key, source, reader):
    started = time.monotonic()
    entry = {"source": source, "started_utc": utc_now()}
    report["reads"][key] = entry
    flush(path, report)
    try:
        entry.update(ok=True, value=plain(reader()))
    except Exception as exc:
        entry.update(
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc),
            error_kind=getattr(exc, "kind", None),
            error_status=getattr(exc, "status", None),
            traceback=traceback.format_exc(),
        )
    entry["elapsed_seconds"] = time.monotonic() - started
    entry["finished_utc"] = utc_now()
    flush(path, report)
    print(f"{key}: {'read' if entry['ok'] else entry['error_type'] + ': ' + entry['error']}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    path = Path(args.output).resolve()
    if path.exists():
        parser.error("Choose a new output file; existing evidence is not overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": 1, "status": "running", "started_utc": utc_now(),
        "requested_slot": args.slot, "script": str(Path(__file__).resolve()),
        "scope": "Read-only advanced runtime controllers over a forced-native paired GPU",
        "limitations": [
            "No setter, hold, release, reset, profiler allocation, profile apply, registry write or I2C scan is requested.",
            "A successful read may contain unavailable controls; exact returned capability fields are retained.",
            "Runtime controllers remain Python consumers of native transport; this does not establish a full C++ port.",
            "No physical voltage, power-gating residency, stability or performance claim is made.",
        ],
        "reads": {}, "diagnostics": [],
    }
    flush(path, report)
    gpu = backend = None
    try:
        from druta import gpu_backend as backend, native, native_frontend

        report["surface_missing_members"] = list(native_frontend.missing_members())
        gpu = backend.open_gpu(args.slot, kind="native")
        report["identity_before"] = plain(identity(gpu))
        report["pairing"] = paired(gpu, args.slot, native.parse_slot)
        report["backend"] = {"description": backend.describe(), "choice": vars(backend.choice())}
        library = native.load()
        report["library"] = {
            "path": str(library.path), "loaded_abi": library.dc_abi_version(),
            "expected_abi": native.ABI_VERSION,
            "sha256": hashlib.sha256(Path(library.path).read_bytes()).hexdigest(),
        }
        flush(path, report)
        for key, module, name in CONTROLLERS:
            observe(report, path, key, f"druta.{module}.{name}(gpu).read()",
                    lambda m=module, n=name: class_read(gpu, m, n, ()))
        for key, module, name, indices in (
            ("individual", "individual_power_gating_live", "IndividualPowerGatingController", INDIVIDUAL_INDICES),
            ("idle_threshold", "gr_idle_threshold_live", "IdleThresholdController", INDIVIDUAL_INDICES),
            ("adaptive_power", "adaptive_power_live", "AdaptivePowerController", ADAPTIVE_INDICES),
        ):
            for index in indices:
                observe(report, path, f"{key}_{index}", f"druta.{module}.{name}(gpu, {index}).read()",
                        lambda m=module, n=name, i=index: class_read(gpu, m, n, (i,)))
        from druta import voltage_requests_live

        observe(report, path, "voltage_rail_requests", "druta.voltage_requests_live.read(gpu, slot=slot)",
                lambda: voltage_requests_live.read(gpu, slot=args.slot))
        report["identity_after"] = plain(identity(gpu))
        report["pairing_after"] = paired(gpu, args.slot, native.parse_slot)
        report["read_successes"] = sum(entry["ok"] for entry in report["reads"].values())
        report["read_errors"] = sum(not entry["ok"] for entry in report["reads"].values())
        report["status"] = "completed_inventory"
    except BaseException as exc:
        report.update(status="aborted", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
    finally:
        if gpu is not None:
            # No request was acquired; close only the selected native handle.
            try:
                gpu.close()
                report["gpu_close"] = {"ok": True}
            except Exception as exc:
                report["gpu_close"] = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
                report["status"] = "completed_with_close_error" if report["status"] == "completed_inventory" else report["status"]
        if backend is not None:
            report["diagnostics"].extend(backend.drain_diagnostics())
            if report["diagnostics"] and report["status"] == "completed_inventory":
                report["status"] = "completed_with_diagnostics"
        report["finished_utc"] = utc_now()
        flush(path, report)
        print(f"{report['status']}: {path}", flush=True)
    return 0 if report["status"] == "completed_inventory" else 1


if __name__ == "__main__":
    raise SystemExit(main())
