"""Bounded rail protocol verification; reads only unless --write is explicit.

Runs one field at a time and restores the exact entry control block and boost.
This verifies driver requests, not physical voltage or overclock stability.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from druta.nvbackend import GPU


def verify(slot, write=False):
    gpu = GPU(slot)
    raw = gpu.read_volt_rail_limits() or {}
    boost = gpu.read_voltage_boost()
    records = {rail: [round(row[k] * 1000) for k in GPU.VOLT_LIMIT_FIELDS]
               for rail, row in raw.items() if row.get("_base_mv")}
    report = {"slot": slot, "name": gpu.static.get("name"),
              "driver": gpu.static.get("driver"), "architecture": gpu.arch(),
              "entry": raw, "boost": boost, "before": gpu.volt_rail_diagnostics(),
              "writes_requested": write, "checks": []}
    # Simulate only the missing NVML interface, on a separate backend object.
    fallback = GPU(slot)
    fallback.nvml = SimpleNamespace(ok=False)
    fallback._arch_cache = None
    report["nvapi_only"] = {"architecture": fallback.arch(),
                            "source": getattr(fallback, "_arch_source", None),
                            "rail_available": fallback.volt_rail_limits_supported(0)}
    if not write:
        return report
    if not records or boost is None or not gpu.volt_rail_limits_supported(0):
        raise RuntimeError("Cannot establish exact entry rail state; no writes issued")
    gpu.volt_limits_write_enabled = True
    try:
        for field in GPU.VOLT_LIMIT_FIELDS:
            initial = gpu.abs_limit_mv(raw[0], field)
            # Reduce ceilings; raise only the idle floor by one small VID step.
            request = initial + (6.25 if field == "vmin" else -6.25)
            ok, message = gpu.set_volt_rail_limits(0, **{field: request})
            check = {"field": field, "request_mv": request, "ok": ok,
                     "message": message, "readback": gpu.read_volt_rail_limits(),
                     "status": gpu.read_volt_rail_state(),
                     "protocol": getattr(gpu, "_volt_rail_write_protocol", None)}
            report["checks"].append(check)
            if not ok:
                break
            restored, message = gpu.reset_volt_rail_limits(0, (field,))
            check["restored"] = restored
            check["restore_message"] = message
            if not restored:
                break
    finally:
        # Always issue the captured raw values, without reconstructing defaults.
        sent, status = gpu._write_rail_records(records)
        back, error = gpu._verify_rail_records(records, boost)
        report["restoration"] = {"dispatched": sent, "status": status,
                                 "verified": back is not None and error is None,
                                 "error": error}
        report["after"] = gpu.volt_rail_diagnostics()
    report["passed"] = (len(report["checks"]) == 4
                        and all(c["ok"] and c.get("restored") for c in report["checks"])
                        and report["restoration"]["verified"]
                        and report["nvapi_only"]["rail_available"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(args.slot, args.write)
    content = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 1 if args.write and not report.get("passed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
