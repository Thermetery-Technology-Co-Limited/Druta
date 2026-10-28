# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded idle clock-offset checks; preserve the existing P0 hold and curve.

Read-only unless --execute is supplied. Writes lower one ordinary offset at
a time, then restores the captured offsets and complete graphics V/F deltas.
Private undo records remain outside the public report. No load or reset.
"""

import argparse
import ctypes
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from druta.nvbackend import GPU  # noqa: E402


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def curve_hash(shape):
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


def sample(gpu):
    live = gpu.read()
    return {key: live.get(key) for key in (
        "pstate", "core", "mem", "core_off", "mem_off", "vcore_mv", "power_w",
        "temp_edge", "util_gpu", "pl_requested_mw", "pl_now_mw", "vboost_pct", "fans")}


def idle(gpu):
    for _ in range(5):
        live = sample(gpu)
        assert live["pstate"] == 0, "P0 must remain held"
        if (live["util_gpu"] is not None and live["util_gpu"] <= 5
                and live["power_w"] is not None and live["power_w"] <= 80
                and live["temp_edge"] is not None and live["temp_edge"] <= 55):
            return live
        time.sleep(0.2)
    raise RuntimeError(f"Desktop did not settle at idle: {live}")


def restore(gpu, offsets, shape):
    messages = []
    for ctype in (0, 2):
        scale, unit = (1, "MHz") if ctype == 0 else gpu.mem_offset_scale()
        units = offsets[ctype][2]
        # Use the same production route and its exact offset readback.
        ok, message = gpu.set_clock_offset(ctype, units / scale)
        messages.append({"ctype": ctype, "ok": ok, "message": message})
        if not ok:
            return False, messages
    changed, note = gpu._keep_vf_shape(shape["deltas"], 0)
    if changed is False:
        return False, messages + [{"curve_error": note}]
    verified = (all(gpu._offset_range(c) == offsets[c] for c in (0, 2))
                and gpu._vf_delta_snapshot() == shape)
    return verified, messages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    gpu = GPU(args.slot)
    assert not gpu.pairing_error and gpu.arch() == GPU.ARCH_TURING
    hold, error = gpu.read_vf_lock_status()
    assert hold and not error and hold["lockMode"] == 3
    offsets = {c: gpu._offset_range(c) for c in (0, 2)}
    shape = gpu._vf_delta_snapshot()
    assert all(offsets.values()) and shape
    report = {
        "date": "2026-10-28", "identity": {k: gpu.static.get(k) for k in ("name", "driver", "vbios", "slot")},
        "scope": "Idle core/memory offset acceptance, readback, curve and hold restoration; no load qualification",
        "baseline": idle(gpu), "hold": hold, "offset_ranges": offsets,
        "curve_sha256": curve_hash(shape), "curve_points": len(shape["deltas"]),
        "fan_capabilities": gpu.fan_capabilities(), "voltage_boost": gpu.read_voltage_boost(),
        "nvml_clock_offset_exports": {name: gpu.nvml.has(name) for name in (
            "nvmlDeviceGetClockOffsets", "nvmlDeviceSetClockOffsets")},
        "nvml_library_sha256": hashlib.sha256(Path(gpu.nvml.dll_path).read_bytes()).hexdigest(),
        "clock_domain_controls": gpu.clkdom_controls_for_ui(),
        "rail_state": gpu.read_volt_rail_state(), "trials": [],
    }
    save(args.output, report)
    if not args.execute:
        return
    if args.private_dir is None:
        parser.error("--execute requires --private-dir")
    args.private_dir.mkdir(parents=True, exist_ok=True)
    save(args.private_dir / "clock-undo.json", {"offsets": offsets, "shape": shape, "hold": hold})
    info, error = gpu._read_pstates20()
    assert info is not None, error
    save(args.private_dir / "pstates-before-private.json", {"raw_hex": bytes(info).hex()})
    for ctype in (0, 2):
        scale, unit = (1, "MHz") if ctype == 0 else gpu.mem_offset_scale()
        change = gpu.clock_step_khz() / 1000 if ctype == 0 else 1
        target = offsets[ctype][2] / scale - change
        assert target * scale >= offsets[ctype][0]
        trial = {"ctype": ctype, "requested": target, "unit": unit, "before": idle(gpu)}
        report["trials"].append(trial)
        try:
            ok, message = gpu.set_clock_offset(ctype, target)
            trial.update(ok=ok, message=message)
            assert ok, message
            trial["readback"] = gpu._offset_range(ctype)
            assert trial["readback"][2] == round(target * scale)
            trial["samples"] = []
            for _ in range(3):
                time.sleep(0.2)
                trial["samples"].append(idle(gpu))
            other = 2 if ctype == 0 else 0
            assert gpu._offset_range(other) == offsets[other]
        finally:
            verified, messages = restore(gpu, offsets, shape)
            trial["restoration"] = {"verified": verified, "messages": messages}
            trial["hold_preserved"] = gpu.read_vf_lock_status() == (hold, None)
            info, error = gpu._read_pstates20()
            if info is not None:
                save(args.private_dir / f"pstates-after-{ctype}-private.json",
                     {"raw_hex": ctypes.string_at(ctypes.byref(info), ctypes.sizeof(info)).hex()})
            save(args.output, report)
            assert verified and trial["hold_preserved"], trial["restoration"]
    # An unchanged zero peer would not reveal a setter that clears the other
    # offset. Keep a nonzero memory request while changing core as well.
    trial = {"label": "core_write_preserves_nonzero_memory_offset", "before": idle(gpu)}
    report["trials"].append(trial)
    try:
        scale, _ = gpu.mem_offset_scale()
        memory_target = offsets[2][2] / scale - 1
        ok, message = gpu.set_clock_offset(2, memory_target)
        assert ok, message
        memory = gpu._offset_range(2)
        core_target = offsets[0][2] - gpu.clock_step_khz() / 1000
        ok, message = gpu.set_clock_offset(0, core_target)
        trial.update(ok=ok, message=message, core_readback=gpu._offset_range(0),
                     memory_readback=gpu._offset_range(2), sample=idle(gpu))
        assert ok and trial["memory_readback"] == memory, trial
    finally:
        verified, messages = restore(gpu, offsets, shape)
        trial["restoration"] = {"verified": verified, "messages": messages}
        trial["hold_preserved"] = gpu.read_vf_lock_status() == (hold, None)
        save(args.output, report)
        assert verified and trial["hold_preserved"]
    fan_state = gpu.read_fan_control_state()
    boost = gpu.read_voltage_boost()
    assert fan_state and boost is not None
    save(args.private_dir / "auxiliary-undo.json", {"fan": fan_state, "voltage_boost": boost})
    report["auxiliary"] = {}
    try:
        ok, message = gpu.set_fan(100)
        report["auxiliary"]["fan_100"] = {"ok": ok, "message": message}
        assert ok, message
        fan_after = gpu.read_fan_control_state()
        assert fan_after and all(f["manual"] and f["level"] == 100 for f in fan_after["fans"])
        report["auxiliary"]["fan_count_verified"] = len(fan_after["fans"])
        target = 1 if boost == 0 else boost - 1
        ok, message = gpu.set_voltage_boost(target)
        report["auxiliary"]["voltage_boost"] = {"target_percent": target, "ok": ok, "message": message}
        assert ok and gpu.read_voltage_boost() == target
        assert gpu.read_vf_lock_status() == (hold, None)
        idle(gpu)
    finally:
        boost_ok, boost_message = gpu.set_voltage_boost(boost)
        fan_ok, fan_message = gpu.restore_fan_control_state(fan_state)
        report["auxiliary"]["restoration"] = {
            "voltage_boost": boost_ok and gpu.read_voltage_boost() == boost,
            "fan": fan_ok, "boost_message": boost_message, "fan_message": fan_message}
        save(args.output, report)
        assert report["auxiliary"]["restoration"]["voltage_boost"] and fan_ok
    report["final"] = idle(gpu)
    report["captured_offsets_and_graphics_curve_restored"] = (
        gpu._vf_delta_snapshot() == shape and all(gpu._offset_range(c) == offsets[c] for c in (0, 2)))
    report["observed_memory_clock_before_after_mhz"] = [report["baseline"]["mem"], report["final"]["mem"]]
    report["observed_memory_clock_matched"] = report["baseline"]["mem"] == report["final"]["mem"]
    save(args.output, report)
    print(json.dumps({"driver": report["identity"]["driver"],
                      "encoded_state_restored": report["captured_offsets_and_graphics_curve_restored"],
                      "observed_memory_clock_matched": report["observed_memory_clock_matched"]}))


if __name__ == "__main__":
    main()
