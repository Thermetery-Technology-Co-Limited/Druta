"""Fixed idle integration test of the shipped current-limit backend."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import nvbackend as n
from probe_5080_policy13_write import capture_transport, get_control, save_report
from probe_5080_policy14_write import snapshot, require_idle

OUT = ROOT / "experiments/power-5080-20260908/production-current-limits.json"


def main():
    assert not OUT.exists()
    gpu = n.GPU("0000:01:00.0")
    assert gpu.static["driver"] == "580.97" and gpu.static["vbios"] == "98.03.3b.c0.6f"
    baseline = gpu.get_current_limits()
    assert {r["policy"]: r["limit_ma"] for r in baseline} == {13: 300000, 14: 120000}
    stable = []
    deadline = time.monotonic() + 30
    while len(stable) < 6:
        assert time.monotonic() < deadline, "No stable P8; no writes attempted"
        sample = snapshot(gpu)
        try:
            require_idle(sample)
        except AssertionError:
            stable.clear()
        else:
            stable.append(sample)
        time.sleep(.35)
    transport = capture_transport(gpu)
    original = get_control(gpu, transport)
    report = {"baseline": baseline, "idle": stable, "original_control": original,
              "mode_rejections": [], "trials": []}
    save_report(OUT, report)
    for policy, invalid in ((13, 500001), (14, 200001)):
        result = gpu.set_current_limit_ma(policy, invalid)
        report["mode_rejections"].append({"policy": policy, "ma": invalid, "result": result})
        assert not result[0] and get_control(gpu, transport) == original
    for policy, target, default in ((13, 305000, 300000), (14, 125000, 120000)):
        trial = {"policy": policy, "target_ma": target, "before": snapshot(gpu)}
        require_idle(trial["before"])
        report["trials"].append(trial)
        save_report(OUT, report)
        started = time.monotonic()
        try:
            trial["set"] = gpu.set_current_limit_ma(policy, target)
            assert trial["set"][0], trial["set"]
            trial["active"] = snapshot(gpu)
            require_idle(trial["active"])
            assert trial["active"][f"policy_{policy}"]["limit"] == target
            trial["production_readback"] = gpu.get_current_limits()
        finally:
            trial["restore"] = gpu.set_current_limit_ma(policy, default)
            trial["interval_s"] = time.monotonic() - started
            trial["restored"] = snapshot(gpu)
            trial["final_control"] = get_control(gpu, transport)
            trial["all_control_bytes_restored"] = trial["final_control"] == original
            save_report(OUT, report)
            assert trial["restore"][0] and trial["all_control_bytes_restored"]
            assert trial["restored"][f"policy_{policy}"]["limit"] == default
    report["final"] = gpu.get_current_limits()
    save_report(OUT, report)
    print(json.dumps({"mode_rejections": report["mode_rejections"],
                      "trials": [{k: t[k] for k in ("policy", "target_ma", "set", "restore", "interval_s",
                                                    "all_control_bytes_restored")} for t in report["trials"]],
                      "final": report["final"]}, indent=2))


if __name__ == "__main__":
    main()
