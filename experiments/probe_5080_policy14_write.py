"""Bounded idle test of policy 14: 120 A -> 115 A -> restore 120 A.

Default invocation is read-only. --execute performs the one fixed decrease,
then restores the original selected record before diagnostic I/O in finally.
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
from probe_5080_policy13_write import (
    capture_suppressed_slider_set, get_control, capture_transport, save_report,
    PARAM_BYTES, SET_CONTROL, GET_CONTROL, FULL_MASK, DLL_SHA256,
)
from probe_5080_policy13_max import snapshot, require_idle as require_core_idle

POLICY = 14
RECORD_WORD = (0x14 + POLICY * 0x7c) // 4
LIMIT_WORD = RECORD_WORD + 1
BASELINE_MA, TARGET_MA = 120000, 115000
OUT = ROOT / "experiments/power-5080-20260908/policy14-write"


def require_idle(sample):
    require_core_idle(sample)
    assert sample["policy_14"]["value"] < 30000, "Other-rail current is not idle"


def send(gpu, transport, original, target):
    assert target in (BASELINE_MA, TARGET_MA)
    assert len(original) * 4 == PARAM_BYTES and original[4] == FULL_MASK
    assert original[RECORD_WORD] == 0x12 and original[LIMIT_WORD] == BASELINE_MA
    params = list(original)
    params[4], params[LIMIT_WORD] = 1 << POLICY, target
    assert {i for i, (a, b) in enumerate(zip(original, params)) if a != b} <= {4, LIMIT_WORD}
    header, fields = transport
    packet = (n.u32 * (17 + PARAM_BYTES // 4))(*header, *params)
    packet[2], packet[14], packet[15], packet[16] = C.sizeof(packet), SET_CONTROL, PARAM_BYTES, 0
    started = time.monotonic()
    status = gpu._legacy_clk_escape(packet, fields)
    return {"target_ma": target, "ntstatus": status, "rm_status": packet[16],
            "duration_s": time.monotonic() - started, "input_params": params}


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
    assert (meta["type_id"], meta["channel"], meta["unit_id"]) == (0x12, 12, 1)
    assert (meta["minimum"], meta["default"], meta["maximum"], meta["limit"]) == (1, 120000, 5001000, 120000)
    assert first["policy_13"]["limit"] == 300000
    print(json.dumps({"policy": POLICY, "baseline_ma": meta["limit"],
                      "declared_max_ma": meta["maximum"], "target_ma": TARGET_MA,
                      "pstate": first["pstate"], "power_w": first["telemetry"]["power_w"]}), flush=True)
    if not args.execute:
        return
    assert not OUT.exists(), "Refusing to overwrite a prior policy-14 write test"
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
    read_transport = capture_transport(gpu)
    original = get_control(gpu, read_transport)
    assert original[RECORD_WORD] == 0x12 and original[LIMIT_WORD] == BASELINE_MA
    OUT.mkdir()
    path = OUT / "execution.json"
    report = {"identity": gpu.static, "metadata": meta, "original_control": original,
              "idle_baseline": idle_baseline, "target_ma": TARGET_MA}
    save_report(path, report)
    suppressed = capture_suppressed_slider_set(gpu)
    save_report(OUT / "suppressed-slider-identity.json", suppressed)
    setter = next(r for r in suppressed["captures"] if r["suppressed"])
    getter = next(r for r in suppressed["captures"] if r["command"] == GET_CONTROL)
    assert setter["size"] == PARAM_BYTES
    assert {i for i, (a, b) in enumerate(zip(getter["output"], setter["input"])) if a != b} == {4}
    assert setter["input"][4] == 0x100
    transport = setter["header"], setter["fields"]
    report["before"] = snapshot(gpu)
    require_idle(report["before"])
    assert get_control(gpu, read_transport) == original
    report["pending_target_ma"] = TARGET_MA
    save_report(path, report)
    attempted = False
    started = time.monotonic()
    try:
        attempted = True
        report["set"] = send(gpu, transport, original, TARGET_MA)
        report["readback_control"] = get_control(gpu, read_transport)
        report["changed_words"] = [i for i, (a, b) in enumerate(zip(original, report["readback_control"])) if a != b]
        assert set(report["changed_words"]) <= {LIMIT_WORD}, "Unexpected other control change"
        report["active"] = []
        for _ in range(5):
            sample = snapshot(gpu)
            report["active"].append(sample)
            require_idle(sample)
            time.sleep(.05)
    except BaseException as exc:
        report["error"] = repr(exc)
        raise
    finally:
        # No GET, assertions, or report I/O can prevent this restoration.
        if attempted:
            report["restore"] = send(gpu, transport, original, BASELINE_MA)
        report["interval_until_restore_s"] = time.monotonic() - started
        report.pop("pending_target_ma", None)
        report["final_control"] = get_control(gpu, read_transport)
        report["all_control_bytes_restored"] = report["final_control"] == original
        report["restored"] = snapshot(gpu)
        report["effective_limit_restored"] = report["restored"]["policy_14"]["limit"] == BASELINE_MA
        save_report(path, report)
        assert report["all_control_bytes_restored"] and report["effective_limit_restored"], "Restore did not verify"
    print(json.dumps({"target_ma": TARGET_MA, "ntstatus": report["set"]["ntstatus"],
                      "rm_status": report["set"]["rm_status"],
                      "stored_limit_ma": report["readback_control"][LIMIT_WORD],
                      "changed_words": report["changed_words"],
                      "effective_limits_ma": sorted({s["policy_14"]["limit"] for s in report["active"]}),
                      "all_control_bytes_restored": report["all_control_bytes_restored"],
                      "effective_limit_restored": report["effective_limit_restored"],
                      "restore_ntstatus": report["restore"]["ntstatus"],
                      "restore_rm_status": report["restore"]["rm_status"],
                      "interval_until_restore_s": report["interval_until_restore_s"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
