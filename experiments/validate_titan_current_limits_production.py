"""Reversible production-backend validation for Pascal/Turing core current.

Each trial runs only while the selected GPU is cool and idle, changes policy 13
by 5 A, requires stored and effective readback, then restores the exact entry
value and verifies that the complete control block is byte-for-byte unchanged.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "druta"))
import nvbackend as n


def save(path, report):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def current_state(gpu):
    return gpu._current_limit_state()


def control_hash(control):
    raw = struct.pack("<" + "I" * len(control), *control)
    return hashlib.sha256(raw).hexdigest()


def snapshot(gpu, state=None):
    live = gpu.read()
    rows, control = state or current_state(gpu)
    return {
        "telemetry": {key: live.get(key) for key in
                      ("pstate", "util_gpu", "util_fb", "power_w",
                       "temp_edge", "core", "mem")},
        "current_limits": rows,
        "control_sha256": control_hash(control),
    }


def require_idle(sample):
    telemetry = sample["telemetry"]
    assert telemetry["pstate"] == 8, telemetry
    assert (telemetry["util_gpu"] or 0) <= 5, telemetry
    assert (telemetry["power_w"] or 0) <= 40, telemetry
    assert (telemetry["temp_edge"] or 0) <= 55, telemetry


def stable_idle(gpu):
    samples = []
    deadline = time.monotonic() + 30
    while len(samples) < 6:
        assert time.monotonic() < deadline, "no stable idle window; no write attempted"
        sample = snapshot(gpu)
        try:
            require_idle(sample)
        except AssertionError:
            samples.clear()
        else:
            samples.append(sample["telemetry"])
        time.sleep(0.35)
    return samples


def validate(slot, report, output):
    gpu = n.GPU(slot)
    assert gpu.arch() in (n.GPU.ARCH_PASCAL, n.GPU.ARCH_TURING)
    assert gpu.nvapi.ok and gpu.nvml.ok and not gpu.pairing_error
    baseline_state = current_state(gpu)
    baseline = snapshot(gpu, baseline_state)
    original_control = baseline_state[1]
    rows = baseline["current_limits"]
    assert len(rows) == 1 and rows[0]["policy"] == 13, rows
    row = rows[0]
    assert row["requested_ma"] == row["limit_ma"]
    original_ma = row["limit_ma"]
    targets = (original_ma - 5000, original_ma + 5000)
    assert row["minimum_ma"] <= min(targets) <= max(targets) <= row["maximum_ma"]
    card = {
        "identity": {key: gpu.static.get(key) for key in
                     ("name", "driver", "vbios", "slot")},
        "architecture": gpu.arch_name(),
        "baseline": baseline,
        "idle": stable_idle(gpu),
        "trials": [],
    }
    report["cards"].append(card)
    save(output, report)
    for direction, target in zip(("down", "up"), targets):
        before = snapshot(gpu)
        require_idle(before)
        trial = {"direction": direction, "target_ma": target,
                 "before": before, "started": time.time()}
        card["trials"].append(trial)
        save(output, report)
        try:
            trial["set"] = gpu.set_current_limit_ma(13, target)
            assert trial["set"][0], trial["set"]
            trial["active"] = snapshot(gpu)
            require_idle(trial["active"])
            active = trial["active"]["current_limits"][0]
            assert active["requested_ma"] == active["limit_ma"] == target
        finally:
            trial["restore"] = gpu.set_current_limit_ma(13, original_ma)
            restored_state = current_state(gpu)
            trial["restored"] = snapshot(gpu, restored_state)
            trial["duration_s"] = time.time() - trial["started"]
            trial["full_control_restored"] = (
                restored_state[1] == original_control)
            save(output, report)
            restored = trial["restored"]["current_limits"][0]
            assert trial["restore"][0], trial["restore"]
            assert restored["requested_ma"] == restored["limit_ma"] == original_ma
            assert trial["full_control_restored"]
    final_state = current_state(gpu)
    card["final"] = snapshot(gpu, final_state)
    card["passed"] = final_state[1] == original_control
    save(output, report)
    assert card["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpu", action="append", required=True,
                        help="PCI slot; pass once per Pascal/Turing GPU")
    args = parser.parse_args()
    assert not args.output.exists(), "refusing to overwrite existing evidence"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"read_modify_write": True, "step_ma": 5000, "cards": []}
    save(args.output, report)
    for slot in args.gpu:
        validate(slot, report, args.output)
    report["passed"] = all(card.get("passed") for card in report["cards"])
    save(args.output, report)
    print(json.dumps({
        "passed": report["passed"],
        "cards": [{"name": card["identity"]["name"],
                   "architecture": card["architecture"],
                   "trials": [{key: trial[key] for key in
                               ("direction", "target_ma", "set", "restore",
                                "full_control_restored")}
                              for trial in card["trials"]]}
                  for card in report["cards"]],
    }, indent=2))


if __name__ == "__main__":
    main()
