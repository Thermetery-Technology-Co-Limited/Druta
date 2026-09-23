"""Probe declared policy-14 limits ONLY while the GPU is idle.

Targets are fixed: declared 5001 A and 5001.001 A. Each trial restores
the full original selected record in finally, before any report I/O. Values
above 120 A are API-acceptance tests, not operating-current recommendations.
"""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import nvbackend as n
from probe_5080_policy14_write import (
    capture_suppressed_slider_set, get_control, capture_transport, save_report,
    PARAM_BYTES, SET_CONTROL, GET_CONTROL, RECORD_WORD, LIMIT_WORD, DLL_SHA256,
)
from probe_5080_policy_series import capture as capture_policies

TARGETS = (5001000, 5001001)
OUT = ROOT / "experiments/power-5080-20260908/policy14-maximum"


from probe_5080_policy14_write import snapshot, require_idle


def send(gpu, transport, original, target):
    assert target in TARGETS + (120000,)
    assert len(original) * 4 == PARAM_BYTES
    assert original[RECORD_WORD] == 0x12 and original[LIMIT_WORD] == 120000
    params = list(original)
    params[4], params[LIMIT_WORD] = 0x4000, target
    header, fields = transport
    packet = (n.u32 * (17 + PARAM_BYTES // 4))(*header, *params)
    packet[2], packet[14], packet[15], packet[16] = C.sizeof(packet), SET_CONTROL, PARAM_BYTES, 0
    start = time.monotonic()
    status = gpu._legacy_clk_escape(packet, fields)
    return {"target_ma": target, "ntstatus": status, "rm_status": packet[16],
            "duration_s": time.monotonic() - start, "input_params": params}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    gpu = n.GPU("0000:01:00.0")
    assert (gpu.static["name"], gpu.static["driver"], gpu.static["vbios"]) == (
        "NVIDIA GeForce RTX 5080", "580.97", "98.03.3b.c0.6f")
    assert hashlib.sha256(Path("C:/Windows/System32/nvapi64.dll").read_bytes()).hexdigest() == DLL_SHA256
    first = snapshot(gpu)
    meta = first["policy_14"]
    assert (meta["minimum"], meta["default"], meta["maximum"], meta["limit"]) == (1, 120000, 5001000, 120000)
    print(json.dumps({"declared_min_ma": meta["minimum"], "declared_default_ma": meta["default"],
                      "declared_max_ma": meta["maximum"], "current_limit_ma": meta["limit"],
                      "pstate": first["pstate"], "power_w": first["telemetry"]["power_w"],
                      "current_ma": meta["value"]}, indent=2), flush=True)
    if not args.execute:
        return
    assert not OUT.exists(), "Refusing to overwrite a prior maximum test"
    idle_baseline = []
    deadline = time.monotonic() + 30
    while len(idle_baseline) < 6:
        assert time.monotonic() < deadline, "GPU did not settle in P8; no writes attempted"
        sample = snapshot(gpu)
        try:
            require_idle(sample)
        except AssertionError:
            idle_baseline.clear()
        else:
            idle_baseline.append(sample)
        time.sleep(.35)
    OUT.mkdir()
    path = OUT / "execution.json"
    read_transport = capture_transport(gpu)
    original = get_control(gpu, read_transport)
    report = {"identity": gpu.static, "original_control": original,
              "metadata": meta, "targets_ma": TARGETS, "idle_baseline": idle_baseline, "trials": []}
    save_report(path, report)
    suppressed = capture_suppressed_slider_set(gpu)
    save_report(OUT / "suppressed-slider-identity.json", suppressed)
    setter = next(r for r in suppressed["captures"] if r["suppressed"])
    getter = next(r for r in suppressed["captures"] if r["command"] == GET_CONTROL)
    assert setter["size"] == PARAM_BYTES
    assert {i for i, (a, b) in enumerate(zip(getter["output"], setter["input"])) if a != b} == {4}
    assert setter["input"][4] == 0x100
    assert get_control(gpu, read_transport) == original
    transport = setter["header"], setter["fields"]
    for target in TARGETS:
        trial = {"target_ma": target, "before": snapshot(gpu)}
        require_idle(trial["before"])
        assert get_control(gpu, read_transport) == original
        report["trials"].append(trial)
        report["pending_target_ma"] = target
        save_report(path, report)
        attempted = False
        started = time.monotonic()
        try:
            attempted = True
            trial["set"] = send(gpu, transport, original, target)
            trial["readback_control"] = get_control(gpu, read_transport)
            trial["changed_words"] = [i for i, (a, b) in enumerate(zip(original, trial["readback_control"])) if a != b]
            assert set(trial["changed_words"]) <= {LIMIT_WORD}, "Unexpected other control change"
            trial["active"] = []
            for _ in range(3):
                sample = snapshot(gpu)
                trial["active"].append(sample)
                require_idle(sample)
                time.sleep(.05)
        except BaseException as exc:
            trial["error"] = repr(exc)
            raise
        finally:
            # Restoration is unconditional after a SET attempt, and occurs
            # before GET, logging, summaries, or other fallible diagnostics.
            if attempted:
                trial["restore"] = send(gpu, transport, original, 120000)
            trial["interval_until_restore_s"] = time.monotonic() - started
            report.pop("pending_target_ma", None)
            trial["restored_control"] = get_control(gpu, read_transport)
            trial["all_control_bytes_restored"] = trial["restored_control"] == original
            trial["restored"] = snapshot(gpu)
            trial["effective_limit_restored"] = trial["restored"]["policy_14"]["limit"] == 120000
            save_report(path, report)
            assert trial["all_control_bytes_restored"] and trial["effective_limit_restored"], "Restore did not verify"
        print(json.dumps({"target_ma": target, "ntstatus": trial["set"]["ntstatus"],
                          "rm_status": trial["set"]["rm_status"],
                          "stored_limit_ma": trial["readback_control"][LIMIT_WORD],
                          "effective_limits_ma": sorted({s["policy_14"]["limit"] for s in trial["active"]}),
                          "restored": trial["all_control_bytes_restored"] and trial["effective_limit_restored"],
                          "interval_until_restore_s": trial["interval_until_restore_s"]}), flush=True)
    report["final"] = snapshot(gpu)
    save_report(path, report)


if __name__ == "__main__":
    main()
