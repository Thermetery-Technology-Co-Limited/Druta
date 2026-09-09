"""Read-only power-policy captures for the measured RTX 5080 / 580.97 ABI.

Calls only existing NVAPI getters. A process-local D3DKMTEscape capture records
their unmodified replies. The hook is restored by tools.probe_volt_rails.rm_call.
No RM command is substituted and no GPU configuration is written.
"""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import statistics
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import nvbackend as n
from tools.probe_volt_rails import rm_call


def packed(words):
    return struct.pack("<" + "I" * len(words), *words)


def decode(info, dynamic):
    # Offsets independently traced through nvapi64.dll 580.97:
    # ClientPowerTopologyGetStatus RVA 0x21ae80 and helper RVA 0x215d40.
    assert len(info) * 4 == 8620 and len(dynamic) * 4 == 172080
    assert dynamic[1] == info[1] and info[1] == 0x3ffff
    rows = []
    for index in range(18):
        start = (0x58 + index * 0xe4) // 4
        metadata = info[start:start + 0xe4 // 4]
        start = (0x70 + index * 0x1454) // 4
        status = dynamic[start:start + 0x1454 // 4]
        assert status[0] & 255 == metadata[1] & 255
        value = max(0, status[2] - status[3])
        default = metadata[3]
        eligible = status[1] != 0 and status[0] & 255 != 4
        # Match integer rounding in the driver's topology helper exactly.
        pcm = (value * 100000 + default // 2) // default if default else None
        rows.append({"index": index, "type_id": metadata[1] & 255,
                     "channel": (metadata[1] >> 8) & 255,
                     "unit_id": (metadata[1] >> 16) & 255,
                     "minimum": metadata[2], "default": default,
                     "maximum": metadata[4], "limit": status[1],
                     "value": status[2], "deduction": status[3],
                     "normalized_pcm": pcm, "topology_eligible": eligible,
                     "status_prefix": status[:72]})
    return rows


def capture(gpu):
    topology = n._PwrTopo(version=gpu.nvapi.ver(n._PwrTopo, 1))
    trace = rm_call(gpu, invoke=lambda: gpu.nvapi.PowerTopo(gpu.nvapi.gpu, C.byref(topology)),
                    capture_multiple=True)
    assert trace["nvapi_status"] == 0
    captures = trace["captures"]
    assert all(c["escape_rm_status"] == 0 for c in captures)
    info = next(c["escape_output_params"] for c in captures if c["escape_header"][14] == 0x2080a618)
    dynamic = [c["escape_output_params"] for c in captures if c["escape_header"][14] == 0x2080a619][-1]
    rows = decode(info, dynamic)
    expected = max(row["normalized_pcm"] for row in rows if row["topology_eligible"])
    actual = next(e.power_pcm for e in topology.entries[:topology.count] if e.domain == 1)
    # The last dynamic reply is the one consumed by the domain-1 helper.
    assert expected == actual, (expected, actual)
    return {"unix_time": time.time(), "policies": rows,
            "topology": [{"domain": e.domain, "pcm": e.power_pcm} for e in topology.entries[:topology.count]],
            "highest_policy": max((r for r in rows if r["topology_eligible"]), key=lambda r: r["normalized_pcm"])["index"],
            "global_clock_words": dynamic[:28]}, info, dynamic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--out", type=Path, default=ROOT / "experiments/power-5080-20260908/series")
    args = parser.parse_args()
    gpu = n.GPU("0000:01:00.0")
    assert (gpu.static["name"], gpu.static["driver"]) == ("NVIDIA GeForce RTX 5080", "580.97"), gpu.static
    assert 1 <= args.samples <= 120
    args.out.mkdir(parents=True, exist_ok=False)
    report = {"identity": gpu.static, "read_only": True, "samples": []}
    for index in range(args.samples):
        sample, info, dynamic = capture(gpu)
        telemetry = {}
        gpu._read_power(telemetry)
        gpu._read_throttle(telemetry)
        sample["telemetry"] = telemetry
        filename = f"dynamic-{index:03}.bin"
        raw = packed(dynamic)
        (args.out / filename).write_bytes(raw)
        sample["raw_file"] = filename
        sample["sha256"] = hashlib.sha256(raw).hexdigest()
        report["samples"].append(sample)
        if index == 0:
            (args.out / "policy-info.bin").write_bytes(packed(info))
        time.sleep(.5)
    summary = {"samples": len(report["samples"]),
               "winning_policy_counts": {str(i): sum(s["highest_policy"] == i for s in report["samples"]) for i in range(18)},
               "topology_ratio_exact_matches": len(report["samples"])}
    for key in ("power_w", "event_mask", "pl_now_mw", "vcore_mv"):
        values = [s["telemetry"][key] for s in report["samples"]]
        summary[key] = {"min": min(values), "mean": statistics.mean(values), "max": max(values)}
    for i in (0, 8, 9, 10, 13, 14):
        values = [s["policies"][i]["value"] for s in report["samples"]]
        summary[f"policy_{i}"] = {"limits": sorted({s["policies"][i]["limit"] for s in report["samples"]}),
                                  "min_value": min(values), "mean_value": statistics.mean(values), "max_value": max(values)}
    report["summary"] = summary
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
