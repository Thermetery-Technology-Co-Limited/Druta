# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reversible idle qualification of the Pascal client/native power ABI.

Default is read-only. --execute requests points derived from THIS adapter's
reported bounds: one inside the ordinary range, one just above its maximum,
and one clearly extended. Nothing is hardcoded to a board. No workload,
voltage, clock, registry or firmware operation is performed.

The point of the extended trials is that a request above the ordinary maximum
is accepted, read back exactly and restored. Whether the board then enforces
it is a separate question this script does not measure.

Raw undo state stays in the supplied private directory; the report contains
decoded values and buffer hashes only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from druta import client_power_runtime as cp
from druta.nvbackend import GPU


def save(path, value):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def summary(state):
    info_base, info_stride, control_base, stride, size = cp._native_geometry(state["binding"])
    info, control = bytes.fromhex(state["native_info_hex"]), bytes.fromhex(state["native_control_hex"])
    assert len(control) == size
    mask = cp._u32(info, 4)
    policies = []
    for index in range(32):
        if mask >> index & 1:
            at, ctl = info_base + index * info_stride, control_base + index * stride
            policies.append({"index": index, "type": info[at + 4], "channel": info[at + 5],
                             "unit": info[at + 6], "requested": cp._u32(control, ctl + 4),
                             "minimum": cp._u32(info, at + 8), "rated": cp._u32(info, at + 12),
                             "maximum": cp._u32(info, at + 16)})
    return {"binding": state["binding"], "requested_mw": state["requested_mw"], "policies": policies,
            "buffers": {k: {"bytes": len(bytes.fromhex(v)), "sha256": hashlib.sha256(bytes.fromhex(v)).hexdigest()}
                        for k, v in state.items() if k.endswith("_hex")}}


def idle(gpu):
    """Settled idle telemetry. No P-state is required: this script does not
    depend on a hold, and a board that needs one is not a general precondition.
    """
    samples = []
    for attempt in range(5):
        live = gpu.read()
        result = {k: live.get(k) for k in ("pstate", "util_gpu", "power_w", "temp_edge",
                                           "pl_requested_mw", "pl_now_mw")}
        samples.append(result)
        if (result["util_gpu"] is not None and result["util_gpu"] <= 10
                and result["temp_edge"] is not None and result["temp_edge"] <= 70):
            return {**result, **({"settling_samples": samples} if attempt else {})}
        time.sleep(0.2)
    raise AssertionError(f"Adapter did not settle at idle: {samples}")


def targets(binding):
    """Request points derived from the adapter, not from a tested board."""
    minimum, rated, maximum = (binding[k] for k in ("minimum_mw", "rated_mw", "maximum_mw"))
    assert minimum < rated <= maximum, binding
    inside = min(maximum, rated + 10_000)
    assert minimum <= inside <= maximum
    return (("client_in_range", inside),
            ("client_above_maximum", maximum + 1_000),
            ("client_extended", maximum + 50_000))


def status_size_discrimination(gpu):
    """Read-only: record which A619 STATUS sizes this adapter accepts.

    The point is that sharing the 8620-byte info and 4432-byte control blocks
    with another transport does NOT imply sharing its status block, so a
    layout copied across geometries is refused rather than silently wrong.
    """
    import ctypes

    from druta.nvbackend import u32

    header, fields = gpu._rm_power_transport()
    results = {}
    for size in (171048, 172080, 190272, 396024, 397048):
        packet = (u32 * (17 + size // 4))(*header, *([0] * (size // 4)))
        packet[2], packet[14], packet[15], packet[16] = ctypes.sizeof(packet), 0x2080A619, size, 0
        nt = gpu._legacy_clk_escape(packet, fields)
        results[str(size)] = ("accepted" if nt == 0 and packet[16] == 0
                              else f"refused (NTSTATUS {nt}, RM {packet[16]:#x})")
    return results


def current_limit_trial(gpu, report):
    """One reversible core-current request, restored and verified."""
    rows = gpu.get_current_limits()
    report["current_limits"] = {
        "rows": rows,
        "error": getattr(gpu, "_current_limit_error", ""),
        "read_error": getattr(gpu, "_current_limit_read_error", ""),
        "selected_layout": {
            "status_bytes": (gpu._current_limit_layout_choice or {}).get("sizes", {}).get(0x2080A619),
            "info": (gpu._current_limit_layout_choice or {}).get("info"),
            "status": (gpu._current_limit_layout_choice or {}).get("status"),
            "control": (gpu._current_limit_layout_choice or {}).get("control"),
        },
        "trials": [],
    }
    row = next((r for r in rows if r["policy"] == 13), None)
    if row is None:
        return
    baseline = row["requested_ma"]
    target = max(row["minimum_ma"], baseline - 800)
    trial = {"policy": 13, "baseline_ma": baseline, "target_ma": target}
    report["current_limits"]["trials"].append(trial)
    try:
        ok, message = gpu.set_current_limit_ma(13, target, pin=False)
        trial["set"] = {"ok": ok, "message": message}
        assert ok, message
        after = next(r for r in gpu.get_current_limits() if r["policy"] == 13)
        trial["after"] = {k: after[k] for k in ("requested_ma", "limit_ma", "value_ma")}
        trial["exact_readback"] = after["requested_ma"] == target
        assert trial["exact_readback"]
        refused_ok, refused_message = gpu.set_current_limit_ma(
            13, row["maximum_ma"] + 1, pin=False)
        trial["above_maximum_refused"] = {"ok": refused_ok, "message": refused_message}
        assert not refused_ok
    finally:
        ok, message = gpu.set_current_limit_ma(13, baseline, pin=False)
        final = next((r for r in gpu.get_current_limits() if r["policy"] == 13), None)
        trial["restoration"] = {
            "ok": ok, "message": message,
            "restored": bool(final and final["requested_ma"] == baseline),
            "effective_ma": final and final["limit_ma"],
        }
        assert trial["restoration"]["restored"], trial["restoration"]


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
    idle(gpu)
    controller = cp.ClientPowerController(gpu)
    before = controller.read()
    assert before["identity"]["architecture"] == GPU.ARCH_PASCAL
    transport = dict(getattr(gpu, "_current_limit_observed_transport", {}) or {})
    native_effective, native_error = gpu.read_native_power_limit_mw()
    report = {
        "date": time.strftime("%Y-%m-%d"),
        "identity": {k: gpu.static.get(k) for k in ("name", "driver", "vbios", "slot")},
        "pci_identity": before["identity"],
        "captured_power_transport": transport,
        "status_size_discrimination": status_size_discrimination(gpu),
        "current_policy_record_layout": {
            "available": bool(gpu.get_current_limits()),
            "error": getattr(gpu, "_current_limit_error", ""),
            "read_error": getattr(gpu, "_current_limit_read_error", ""),
        },
        "native_effective_mw": native_effective,
        "native_effective_error": native_error,
        "normal_bounds_mw": list(gpu._normal_power_bounds_mw()),
        "slider_bounds_mw": {"xoc_off": list(gpu.power_limit_bounds_mw(xoc=False)),
                             "xoc_on": list(gpu.power_limit_bounds_mw(xoc=True))},
        "baseline": summary(before),
        "trials": [],
        "scope": ("Idle software request acceptance, exact readback and restoration on one "
                  "Pascal adapter and driver. Physical enforcement is unmeasured, and the "
                  "native effective limit is unavailable on this driver's record layout."),
    }
    save(args.output, report)
    if not args.execute:
        return
    if args.private_dir is None:
        parser.error("--execute requires --private-dir for durable undo records")
    args.private_dir.mkdir(parents=True, exist_ok=True)
    extended_low, extended_high = gpu.power_limit_bounds_mw(xoc=True)
    for label, target in targets(before["binding"]):
        assert cp._state(controller.read()) == cp._state(before)
        assert extended_low <= target <= extended_high, (target, extended_low, extended_high)
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
            # The production backend, through the same gate the slider uses.
            gpu.voltage_xoc_enabled = target > before["binding"]["maximum_mw"]
            ok, message = gpu.set_power_limit_mw(target)
            assert ok, message
            trial["xoc_required"] = gpu.voltage_xoc_enabled
            trial["production_message"] = message
            trial["after"] = summary(controller.read())
            trial["exact_request_readback"] = controller.read()["requested_mw"] == target
            assert trial["exact_request_readback"]
            trial["samples"] = []
            for _ in range(4):
                effective, effective_error = gpu.read_native_power_limit_mw()
                trial["samples"].append({"at": time.time(),
                                         "configured_mw": gpu.read_power_limit_mw(),
                                         "native_effective_mw": effective,
                                         "native_effective_error": effective_error,
                                         "idle": idle(gpu)})
                time.sleep(0.25)
            # The ordinary slider must survive an unavailable native read.
            trial["slider_bounds_during_request"] = {
                "xoc_off": list(gpu.power_limit_bounds_mw(xoc=False)),
                "xoc_on": list(gpu.power_limit_bounds_mw(xoc=True))}
        finally:
            restored = restore_baseline(controller, before)
            trial["restoration"] = {k: v for k, v in restored.items() if k != "readback"}
            save(args.output, report)
            if restored["verified"]:
                disarm.touch()
                child.wait(timeout=5)
            assert restored["verified"], restored["error"]
            gpu.voltage_xoc_enabled = False
    current_limit_trial(gpu, report)
    report["final"] = summary(controller.read())
    report["final_configured_mw"] = gpu.read_power_limit_mw()
    report["full_baseline_restored"] = cp._state(controller.read()) == cp._state(before)
    assert report["full_baseline_restored"]
    save(args.output, report)
    print(json.dumps({"targets": [t["target_mw"] for t in report["trials"]],
                      "restored": report["full_baseline_restored"]}))


if __name__ == "__main__":
    main()
