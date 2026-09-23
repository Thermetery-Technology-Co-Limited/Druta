"""Reversibly probe policy 13 at its advertised maximum and one mA above.

The production backend intentionally refuses values above the generation
ceiling.  This experiment bypasses only that range check while retaining the
same PCI-paired private transport, exact one-policy mask, idle gate, independent
stored/effective reads, and unconditional full-record restoration.
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


POLICY = 13
SET_CONTROL = 0x2080E61B


def save(path, report):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def digest(words):
    raw = struct.pack("<" + "I" * len(words), *words)
    return hashlib.sha256(raw).hexdigest()


def raw_policy_state(gpu):
    info = gpu._current_limit_rm(0x2080A618)
    assert len(info) == 2155
    mask = info[1]
    control = gpu._current_limit_rm(0x2080A61A, policy_mask=mask)
    dynamic = gpu._current_limit_rm(0x2080A619, policy_mask=mask)
    assert len(control) == 1108 and len(dynamic) == 43020
    meta = (0x58 + POLICY * 0xE4) // 4
    record = (0x14 + POLICY * 0x7C) // 4
    state = (0x70 + POLICY * 0x1454) // 4
    return {
        "mask": mask,
        "minimum_ma": info[meta + 2],
        "default_ma": info[meta + 3],
        "maximum_ma": info[meta + 4],
        "stored_ma": control[record + 1],
        "effective_ma": dynamic[state + 1],
        "measured_ma": dynamic[state + 2],
        "control_sha256": digest(control),
    }, control


def telemetry(gpu):
    live = gpu.read()
    return {key: live.get(key) for key in
            ("pstate", "util_gpu", "util_fb", "power_w", "temp_edge")}


def require_idle(sample):
    assert sample["pstate"] == 8, sample
    assert (sample["util_gpu"] or 0) <= 5, sample
    assert (sample["power_w"] or 0) <= 40, sample
    assert (sample["temp_edge"] or 0) <= 55, sample


def stable_idle(gpu):
    samples = []
    deadline = time.monotonic() + 30
    while len(samples) < 6:
        assert time.monotonic() < deadline, "no stable idle window; no write attempted"
        sample = telemetry(gpu)
        try:
            require_idle(sample)
        except AssertionError:
            samples.clear()
        else:
            samples.append(sample)
        time.sleep(0.35)
    return samples


def send(gpu, original, target_ma):
    record = (0x14 + POLICY * 0x7C) // 4
    request = list(original)
    request[4] = 1 << POLICY
    request[record + 1] = target_ma
    gpu._current_limit_rm(SET_CONTROL, request)


def probe(gpu, report, output, execute):
    assert gpu.arch() in (gpu.ARCH_PASCAL, gpu.ARCH_TURING)
    assert gpu.nvapi.ok and gpu.nvml.ok and not gpu.pairing_error
    baseline, original = raw_policy_state(gpu)
    assert baseline["stored_ma"] == baseline["effective_ma"]
    card = {
        "identity": {key: gpu.static.get(key) for key in
                     ("name", "driver", "vbios", "slot")},
        "architecture": gpu.arch_name(),
        "baseline": baseline,
        "targets_ma": [baseline["maximum_ma"], baseline["maximum_ma"] + 1],
        "trials": [],
    }
    report["cards"].append(card)
    if not execute:
        return
    card["idle"] = stable_idle(gpu)
    save(output, report)
    for label, target in (("advertised_maximum", baseline["maximum_ma"]),
                          ("one_ma_above", baseline["maximum_ma"] + 1)):
        before, before_control = raw_policy_state(gpu)
        assert before_control == original
        sample = telemetry(gpu)
        require_idle(sample)
        trial = {"label": label, "target_ma": target, "before": before,
                 "telemetry_before": sample, "started": time.time()}
        card["trials"].append(trial)
        save(output, report)
        try:
            try:
                send(gpu, original, target)
                trial["transport_accepted"] = True
            except Exception as exc:
                trial["transport_accepted"] = False
                trial["transport_error"] = str(exc)
            active, active_control = raw_policy_state(gpu)
            trial["active"] = active
            changed = [i for i, values in enumerate(zip(original, active_control))
                       if values[0] != values[1]]
            trial["changed_control_words"] = changed
            trial["stored_and_effective_at_target"] = (
                active["stored_ma"] == active["effective_ma"] == target)
        finally:
            # Always re-send the exact original policy record, even after an
            # apparent rejection: transport failure is not proof of no write.
            send(gpu, original, baseline["stored_ma"])
            restored, restored_control = raw_policy_state(gpu)
            trial["restored"] = restored
            trial["full_control_restored"] = restored_control == original
            trial["effective_limit_restored"] = (
                restored["stored_ma"] == restored["effective_ma"]
                == baseline["stored_ma"])
            trial["duration_s"] = time.time() - trial["started"]
            save(output, report)
            assert trial["full_control_restored"]
            assert trial["effective_limit_restored"]
    final, final_control = raw_policy_state(gpu)
    card["final"] = final
    card["passed"] = final_control == original
    save(output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", action="append", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        assert args.output is not None, "--execute requires --output"
        assert not args.output.exists(), "refusing to overwrite existing evidence"
        args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"probe": "advertised maximum and maximum plus one mA",
              "read_modify_write": True, "cards": []}
    if args.execute:
        save(args.output, report)
    for slot in args.gpu:
        probe(n.GPU(slot), report, args.output, args.execute)
    report["passed"] = all(card.get("passed") for card in report["cards"])
    if args.execute:
        save(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
