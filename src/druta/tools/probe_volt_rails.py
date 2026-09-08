"""Read per-rail NVAPI and RM responses on one explicitly selected GPU.

Default operation is read-only. --identity tests F214 with unchanged records;
--test-limits tests small reversible field changes. --raise-ceiling also
temporarily de-flattens the V/F curve and holds a point under CUDA load.
Writes are scoped to the two measured TITAN boards on driver 580.97.

Copyright (C) 2026 Thermetery Technology Co Limited
SPDX-License-Identifier: GPL-3.0-or-later
"""
import argparse
import ctypes
import json
from pathlib import Path
import struct
import sys
import time

# When run as module, parent package is accessible
from .. import nvbackend
from ..nvbackend import GPU, u32


def transport_ok(result):
    # The substituted setter need not satisfy the getter's output decoder.
    # The transport and RM result are authoritative for the issued command.
    return (result.get("intercepted") is True
            and result.get("escape_status") == 0
            and result.get("rm_status") == 0)


def read_block(gpu, name, version, mask):
    buf = (ctypes.c_ubyte * 8192)()
    p = ctypes.cast(buf, ctypes.POINTER(u32))
    p[0], p[1] = version, mask
    fn = getattr(gpu.nvapi, name)
    rc = fn(gpu.nvapi.gpu, ctypes.byref(buf)) if fn else None
    return {"status": rc, "words": list(p[:(version & 0xffff) // 4])}


def snapshot(gpu, control_version=0x20AC8):
    result = {}
    for name, ver in (("VoltRailsCtlGet", control_version),
                      ("VoltRailsAbs", 0x10AC8)):
        result[name] = {mask: read_block(gpu, name, ver, mask)
                        for mask in (1, 2, 3)}
    result["vcore_mv"] = gpu.read_vcore_mv()
    result["boost_pct"] = gpu.read_voltage_boost()
    return result


def rm_call(gpu, replacement=None, control_version=0x20AC8, invoke=None,
            capture_multiple=False):
    """Capture B213; optionally substitute F214 and exact captured params.

    Run in this isolated process: the temporary hook is not thread safe.
    It restores itself before forwarding the one escape to the real API.
    No unrelated escape is modified. The packet is copied before returning.
    """
    param_words = 162 if control_version == 0x10AC8 else 259
    mask_index = 0 if control_version == 0x10AC8 else 1
    get_command = 0x20803213 if control_version == 0x10AC8 else GPU._ESC_B213
    set_command = 0x20803214 if control_version == 0x10AC8 else GPU._ESC_F214
    packet_size = 68 + param_words * 4
    gdi = ctypes.WinDLL("gdi32.dll")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                     u32, ctypes.POINTER(u32)]
    kernel.FlushInstructionCache.argtypes = [ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_size_t]
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    process = kernel.GetCurrentProcess()
    addr = ctypes.cast(gdi.D3DKMTEscape, ctypes.c_void_p).value
    original = ctypes.string_at(addr, 14)
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    result = {"intercepted": False}
    captures = []

    def restore():
        ctypes.memmove(addr, original, 14)
        kernel.FlushInstructionCache(process, addr, 14)

    def callback(pesc):
        restore()
        e = GPU._Escape.from_address(pesc)
        p = ctypes.cast(e.pPrivateDriverData, ctypes.POINTER(u32))
        result["escape_size"] = int(e.PrivateDriverDataSize)
        if e.pPrivateDriverData and e.PrivateDriverDataSize >= 68:
            result["escape_header"] = list(p[:17])
            result["escape_prefix"] = list(p[:min(40, e.PrivateDriverDataSize // 4)])
            result["escape_input_params"] = list(p[17:e.PrivateDriverDataSize // 4])
        valid = (e.pPrivateDriverData and e.PrivateDriverDataSize == packet_size
                 and p[14] == get_command and p[15] == param_words * 4
                 and p[17 + mask_index] == 1)
        if valid:
            result["intercepted"] = True
            if replacement is not None:
                if (len(replacement) != param_words
                        or replacement[mask_index] != 1):
                    result["error"] = "invalid replacement geometry/mask"
                    return proto(addr)(pesc)
                p[14] = set_command
                for i, value in enumerate(replacement):
                    p[17 + i] = value
            result["input_params"] = list(p[17:17 + param_words])
        rc = proto(addr)(pesc)
        if e.pPrivateDriverData and e.PrivateDriverDataSize >= 68:
            result["escape_output_params"] = list(p[17:e.PrivateDriverDataSize // 4])
            result["escape_rm_status"] = int(p[16])
        if valid:
            result["escape_status"] = rc
            result["rm_status"] = p[16]
            result["output_params"] = list(p[17:17 + param_words])
        if capture_multiple:
            captures.append(dict(result))
            ctypes.memmove(addr, patch, 14)
            kernel.FlushInstructionCache(process, addr, 14)
        return rc

    keep = proto(callback)
    old = u32()
    if not kernel.VirtualProtect(addr, 14, 0x40, ctypes.byref(old)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        patch = (b"\xff\x25\0\0\0\0"
                 + struct.pack("<Q", ctypes.cast(keep, ctypes.c_void_p).value))
        ctypes.memmove(addr, patch, 14)
        kernel.FlushInstructionCache(process, addr, 14)
        buf = (ctypes.c_ubyte * 8192)()
        p = ctypes.cast(buf, ctypes.POINTER(u32))
        p[0], p[1] = control_version, 1
        result["nvapi_status"] = (invoke() if invoke is not None
                                  else gpu.nvapi.VoltRailsCtlGet(
                                      gpu.nvapi.gpu, ctypes.byref(buf)))
    finally:
        restore()
        previous = u32()
        if not kernel.VirtualProtect(addr, 14, old, ctypes.byref(previous)):
            raise ctypes.WinError(ctypes.get_last_error())
    if capture_multiple:
        result["captures"] = captures
    return result


def stable_fields(snap):
    """Compare stored control, all limit fields, and boost; exclude live state."""
    return {"control": snap["VoltRailsCtlGet"][1],
            "limits": snap["VoltRailsAbs"][1]["words"][20:25],
            "boost": snap["boost_pct"]}


def test_raised_ceiling(gpu, report, save, baseline):
    """Demand 1112.5 mV on a de-flattened curve; A/B the 1093.75/1125 caps.

    Preserve the exact caller's curve, point/frequency locks, and rail values.
    The highest permission requested is 1125 mV; overvoltage is untouched.
    """
    from ..gpuload import BandwidthLoad
    from ..nvbackend import _BoostTable, _set_point_masks

    def read_table():
        table = _BoostTable(version=gpu.nvapi.ver(_BoostTable, 1))
        _set_point_masks(table, gpu.vfp_layout().n_entries)
        rc = gpu.nvapi.BoostTableGet(gpu.nvapi.gpu, ctypes.byref(table))
        if rc != 0:
            raise RuntimeError(f"Cannot preserve V/F table: {rc}")
        return table

    points, err = gpu.read_vf_curve()
    if err:
        raise RuntimeError(err)
    table = read_table()
    lock = gpu._vf_lock_read_raw()
    if lock is None:
        raise RuntimeError("Cannot preserve locks")
    changed, old_top, new_top, meta = GPU.compute_deflatten(
        points, 1112.5, step_khz=gpu.clock_step_khz())
    if (not meta.get("unique") or meta.get("boundary_idx") is None
            or new_top - old_top > gpu.clock_step_khz() / 1000 + .001):
        raise RuntimeError("Unexpected de-flatten plan")
    # Planner frequencies are physical MHz; Pascal's raw delta table is in
    # the doubled GPC2CLK domain. A half-bin request stores but moves nothing.
    before_by_idx = {p["idx"]: p for p in points}
    divisor = gpu.vfp_layout().freq_div
    wire_deltas = {c[0]: before_by_idx[c[0]]["delta_khz"]
                   + int(round((c[4] - before_by_idx[c[0]]["delta_khz"])
                               * divisor)) for c in changed}
    run = {"target_mv": 1112.5, "ceiling_mv": 1125,
           "original_table_hex": bytes(table).hex(),
           "original_lock_hex": bytes(lock).hex(),
           "curve_before": points, "plan": changed,
           "wire_deltas": wire_deltas, "trials": []}
    report["raised_ceiling_test"] = run
    save()
    load = BandwidthLoad(max_seconds=35,
                         device=gpu.nvml.selected["nvml_index"])

    def samples():
        rows = []
        for _ in range(8):
            telemetry = gpu.read()
            if telemetry.get("temp_edge", 0) >= 80:
                raise RuntimeError("Temperature stop")
            state = snapshot(gpu)
            rows.append({"live": state["VoltRailsAbs"][1]["words"][18:27],
                         "vcore_mv": state["vcore_mv"],
                         "telemetry": {k: telemetry.get(k) for k in
                                       ("core", "power_w", "temp_edge", "pstate")}})
            time.sleep(.15)
        return rows

    try:
        run["curve_write"] = gpu.apply_vf_deltas(wire_deltas)
        if not run["curve_write"][0]:
            raise RuntimeError(f"V/F write failed: {run['curve_write']}")
        run["curve_after"], err = gpu.read_vf_curve()
        run["resolved_target"] = GPU.resolve_vf_point(run["curve_after"], 1112.5)
        if (err or not run["resolved_target"]
                or run["resolved_target"]["volt_mv"] != 1112.5):
            raise RuntimeError("Evaluated curve did not make the target unique")
        run["lock_write"] = gpu.set_vf_lock(1112500)
        if not run["lock_write"][0]:
            raise RuntimeError(f"Point lock failed: {run['lock_write']}")
        load.start()
        if not load.wait_started() or load.device_name != gpu.static["name"]:
            raise RuntimeError("CUDA load target mismatch")
        time.sleep(.8)
        for raised in (False, True, False, True):
            params = baseline.copy()
            # At the captured 100% boost both ceiling fields read 1093.75.
            params[4] = params[5] = 31250 if raised else 0
            trial = {"raised": raised, "write": rm_call(gpu, params)}
            run["trials"].append(trial)
            if not transport_ok(trial["write"]):
                raise RuntimeError("Ceiling write failed")
            time.sleep(.35)
            trial["samples"] = samples()
            trial["raw"] = snapshot(gpu)
            print(gpu.static["name"], "raised" if raised else "default",
                  [s["vcore_mv"] for s in trial["samples"]], flush=True)
            save()
    finally:
        # Lower the permission before restoring the former curve or lock.
        run["rail_restore"] = rm_call(gpu, baseline)
        run["curve_restore_status"] = gpu.nvapi.BoostTableSet(
            gpu.nvapi.gpu, ctypes.byref(table))
        run["lock_restore_status"] = gpu.nvapi.VfLockSet(
            gpu.nvapi.gpu, ctypes.byref(lock))
        load.stop()
        load.join(3)
        time.sleep(.3)
        run["after"] = snapshot(gpu)
        run["table_restored"] = bytes(read_table()) == bytes(table)
        run["lock_restored"] = bytes(gpu._vf_lock_read_raw()) == bytes(lock)
        run["limits_restored"] = (stable_fields(run["after"])
                                  == stable_fields(report["before"]))
        save()
        if not (transport_ok(run["rail_restore"])
                and run["curve_restore_status"] == 0
                and run["lock_restore_status"] == 0
                and run["table_restored"] and run["lock_restored"]
                and run["limits_restored"]):
            raise RuntimeError("Restoration mismatch; see saved recovery data")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gpu", required=True, help="PCI slot, e.g. 0000:01:00.0")
    ap.add_argument("--identity", action="store_true")
    ap.add_argument("--test-limits", action="store_true",
                    help="After identity, test each field by 12.5 mV and restore")
    ap.add_argument("--raise-ceiling", action="store_true",
                    help="Demand 1112.5 mV with de-flattening and A/B a 1125 mV cap")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    gpu = GPU(slot=args.gpu)
    if not gpu.nvapi.ok or not gpu.nvml.ok or gpu.pairing_error:
        raise RuntimeError("GPU pairing unavailable")
    report = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "static": gpu.static, "before": snapshot(gpu),
              "rm_read": rm_call(gpu), "identity_tests": []}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    save()
    if args.identity or args.test_limits or args.raise_ceiling:
        board = (gpu.nvapi.selected["devid"], gpu.nvapi.selected["subsys"],
                 gpu.static["vbios"].lower())
        if (gpu.static["driver"] != "580.97" or board not in (
                (0x1E02, 312676574, "90.02.1e.00.02"),
                (0x1B02, 299831518, "86.02.3d.00.01"))):
            raise RuntimeError("Identity experiment is scoped to the two validated TITANs")
        native = report["rm_read"].get("output_params")
        if (not native or report["rm_read"].get("rm_status") != 0
                or native[3:11] != [2, 0, 0, 0, 0, 0, 0, 0]
                or report["before"]["VoltRailsCtlGet"][1]["status"] != 0):
            raise RuntimeError("Unexpected native record; refusing experiment")
        blackwell = native.copy()
        blackwell[3], blackwell[10] = 5, 1
        for label, params in (("native_type2", native),
                              ("blackwell_type5_zero_deltas", blackwell)):
            trial = {"kind": label, "write": rm_call(gpu, params)}
            time.sleep(0.15)
            trial["after"] = snapshot(gpu)
            trial["unchanged"] = (stable_fields(trial["after"])
                                   == stable_fields(report["before"]))
            report["identity_tests"].append(trial)
            save()
            if not transport_ok(trial["write"]):
                raise RuntimeError("Identity transport failed")
            if not trial["unchanged"]:
                report["restore"] = rm_call(gpu, blackwell)
                report["restore_native"] = rm_call(gpu, native)
                report["after_restore"] = snapshot(gpu)
                report["restored"] = (stable_fields(report["after_restore"])
                                      == stable_fields(report["before"]))
                save()
                raise RuntimeError("Identity changed limits; stopped, see evidence")
        if args.test_limits:
            report["field_tests"] = []
            for i, field in enumerate(GPU.VOLT_LIMIT_FIELDS):
                params = blackwell.copy()
                # Ceilings only move down. The floor moves up by 12.5 mV,
                # provided this stays below the current operating voltage.
                delta = 12500 if field == "vmin" else -12500
                live = report["before"]["VoltRailsAbs"][1]["words"][19]
                floor = report["before"]["VoltRailsAbs"][1]["words"][24]
                if field == "vmin" and live < floor + 25000:
                    continue
                params[4 + i] = delta & 0xffffffff
                trial = {"field": field, "delta_uv": delta}
                report["field_tests"].append(trial)
                save()
                try:
                    trial["write"] = rm_call(gpu, params)
                    if not transport_ok(trial["write"]):
                        raise RuntimeError("Field write transport failed")
                    time.sleep(0.2)
                    trial["after"] = snapshot(gpu)
                    trial["unchanged"] = (stable_fields(trial["after"])
                                          == stable_fields(report["before"]))
                finally:
                    trial["restore"] = rm_call(gpu, blackwell)
                    time.sleep(0.15)
                    trial["after_restore"] = snapshot(gpu)
                    trial["restored"] = (stable_fields(trial["after_restore"])
                                         == stable_fields(report["before"]))
                    save()
                if not (transport_ok(trial["restore"]) and trial["restored"]):
                    raise RuntimeError("Restore did not match; stopped, see evidence")
        if args.raise_ceiling:
            words = report["before"]["VoltRailsAbs"][1]["words"]
            if words[20:22] != [1093750, 1093750] or native[2] != 100:
                raise RuntimeError("Raised-ceiling experiment requires baseline caps and 100% boost")
            test_raised_ceiling(gpu, report, save, blackwell)
    report["after"] = snapshot(gpu)
    save()
    print(json.dumps({"gpu": gpu.static["name"], "output": str(output),
                      "native_type": report["rm_read"].get("output_params", [0]*4)[3],
                      "tests": [{"kind": t["kind"],
                                 "status": t["write"].get("rm_status"),
                                 "unchanged": t["unchanged"]}
                                for t in report["identity_tests"]],
                      "fields": [{"field": t["field"],
                                  "status": t["write"].get("rm_status"),
                                  "unchanged": t["unchanged"],
                                  "restored": t["restored"]}
                                 for t in report.get("field_tests", [])]}))


if __name__ == "__main__":
    main()
