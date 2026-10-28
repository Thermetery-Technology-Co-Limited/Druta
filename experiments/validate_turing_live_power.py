# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reversible idle qualification of the smaller client/native power ABI.

Default is read-only. --execute tests 349/351/400 W only when those points
bracket THIS adapter's reported bounds. No workload, voltage, registry or
firmware operation. Raw undo state stays in the supplied private directory;
the report contains decoded values and buffer hashes only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from druta import client_power_runtime as cp
from druta.nvbackend import GPU


def save(path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def summary(state):
    size, stride = cp._small_native_geometry(state["binding"])
    info, control = bytes.fromhex(state["native_info_hex"]), bytes.fromhex(state["native_control_hex"])
    assert len(control) == size
    mask = cp._u32(info, 4)
    policies = []
    for index in range(32):
        if mask >> index & 1:
            at, ctl = 88 + index * 228, 20 + index * stride
            policies.append({"index": index, "type": info[at + 4], "channel": info[at + 5],
                             "unit": info[at + 6], "requested": cp._u32(control, ctl + 4),
                             "minimum": cp._u32(info, at + 8), "rated": cp._u32(info, at + 12),
                             "maximum": cp._u32(info, at + 16)})
    return {"binding": state["binding"], "requested_mw": state["requested_mw"], "policies": policies,
            "buffers": {k: {"bytes": len(bytes.fromhex(v)), "sha256": hashlib.sha256(bytes.fromhex(v)).hexdigest()}
                        for k, v in state.items() if k.endswith("_hex")}}


def idle(gpu):
    live = gpu.read()
    result = {k: live.get(k) for k in ("pstate", "util_gpu", "power_w", "temp_edge")}
    assert result["pstate"] == 0, "P0 hold is required for the memory-modified qualification board"
    assert result["util_gpu"] is not None and result["util_gpu"] <= 5, result
    assert result["power_w"] is not None and result["power_w"] <= 80, result
    assert result["temp_edge"] is not None and result["temp_edge"] <= 55, result
    return result


def native_status(gpu, baseline):
    with gpu._lock:
        layout = gpu._current_limit_abi()
        mask = cp._u32(bytes.fromhex(baseline["native_info_hex"]), 4)
        words = gpu._current_limit_rm(cp.NATIVE_STATUS, policy_mask=mask)
    assert words[1] == mask
    return {str(index): {"type": words[(layout["status"][0] + index * layout["status"][1]) // 4],
                         "effective": words[(layout["status"][0] + index * layout["status"][1]) // 4 + 1]}
            for index in range(32) if mask >> index & 1}


def restore_baseline(controller, state):
    # A new watchdog must establish the live ABI and target before replaying
    # saved bytes. Never treat a captured PCI slot as a permanent identity.
    fresh = controller.read()
    for key in ("schema", "stable_reads", "identity", "binding", "info_hex", "role_info_hex", "native_info_hex"):
        if fresh[key] != state[key]:
            raise RuntimeError(f"Restoration target or definitions changed: {key}")
    with controller.gpu._lock:
        return controller._rollback(state)


def watchdog(args):
    state = json.loads(args.undo.read_text(encoding="utf-8"))
    while time.time() < args.deadline:
        if args.disarm.exists():
            return
        time.sleep(0.2)
    gpu = GPU(args.slot)
    result = restore_baseline(cp.ClientPowerController(gpu), state)
    save(args.undo.with_suffix(".watchdog-result.json"),
         {k: v for k, v in result.items() if k != "readback"})
    if not result["verified"]:
        raise RuntimeError(result["error"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--private-dir", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--watchdog", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--undo", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--disarm", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--deadline", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.watchdog:
        return watchdog(args)
    if args.output is None:
        parser.error("--output is required")
    gpu = GPU(args.slot)
    hold, hold_error = gpu.read_vf_lock_status()
    assert not hold_error and hold and hold["lockMode"] == 3, "Establish and retain a P0 V/F hold before this experiment"
    idle(gpu)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    assert before["identity"]["architecture"] == GPU.ARCH_TURING
    assert before["binding"].get("native_layout") == "policy124"
    report = {"date": "2026-10-28", "identity": {k: gpu.static.get(k) for k in ("name", "driver", "vbios", "slot")},
              "existing_p0_hold": hold, "board_note": "Owner reports imperfect 16 GB VRAM modification; P5/P8 must be avoided",
              "pci_identity": before["identity"], "baseline": summary(before), "trials": [],
              "scope": "Idle software request acceptance and restoration; physical enforcement unmeasured"}
    save(args.output, report)
    if not args.execute:
        return
    if args.private_dir is None:
        parser.error("--execute requires --private-dir for durable undo records")
    args.private_dir.mkdir(parents=True, exist_ok=True)
    maximum = before["binding"]["maximum_mw"]
    assert before["binding"]["minimum_mw"] < 349000 <= maximum == 350000
    for label, target in (("client_in_range", 349000), ("client_above_maximum", 351000),
                          ("client_400w", 400000), ("production_backend_with_current_pin", 351000)):
        assert cp._state(controller.read()) == cp._state(before)
        trial = {"label": label, "target_mw": target, "idle_before": idle(gpu)}
        report["trials"].append(trial)
        undo = args.private_dir / f"undo-{target}.json"
        disarm = args.private_dir / f"disarm-{target}-{time.time_ns()}"
        save(undo, before)
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watchdog", "--slot", args.slot,
                                  "--undo", str(undo), "--disarm", str(disarm), "--deadline", str(time.time() + 30)],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            save(args.output, report)
            if label == "production_backend_with_current_pin":
                gpu.voltage_xoc_enabled = True
                ok, message = gpu.set_power_limit_mw(target)
                assert ok, message
                trial["production_message"] = message
            else:
                controller.set_request(target)
            trial["after"] = summary(controller.read())
            trial["samples"] = []
            for _ in range(5):
                trial["samples"].append({"at": time.time(), "configured_mw": gpu.read_power_limit_mw(),
                                         "native_status": native_status(gpu, before),
                                         "idle": idle(gpu)})
                time.sleep(0.25)
            trial["current_limits"] = gpu.get_current_limits()
            trial["current_error"] = getattr(gpu, "_current_limit_error", None)
            if label == "production_backend_with_current_pin":
                assert trial["current_limits"], trial["current_error"]
                ok, message = gpu.set_current_limit_ma(13, 350000)
                assert ok, message
                trial["current_pin_ma"] = 350000
                ok, message = gpu.set_power_limit_mw(target)
                assert ok, message
                trial["after_repeated_board_request"] = summary(controller.read())
                trial["pinned_current_readback"] = gpu.get_current_limits()
                assert trial["pinned_current_readback"][0]["requested_ma"] == 350000
                pinned = controller.read()
                ok, message = gpu.set_current_limit_ma(13, 360001)
                assert not ok
                trial["above_maximum_current_rejected"] = message
                assert cp._state(controller.read()) == cp._state(pinned)
        finally:
            restored = restore_baseline(controller, before)
            trial["restoration"] = {k: v for k, v in restored.items() if k != "readback"}
            save(args.output, report)
            if restored["verified"]:
                disarm.touch()
                child.wait(timeout=5)
            assert restored["verified"], restored["error"]
            gpu._power_policy_pins = {}
            gpu.voltage_xoc_enabled = False
    report["final"] = summary(controller.read())
    report["final_p0_hold"] = gpu.read_vf_lock_status()[0]
    assert report["final_p0_hold"] == hold
    report["full_baseline_restored"] = cp._state(controller.read()) == cp._state(before)
    save(args.output, report)
    print(json.dumps({"targets": [t["target_mw"] for t in report["trials"]],
                      "restored": report["full_baseline_restored"]}))


if __name__ == "__main__":
    main()
