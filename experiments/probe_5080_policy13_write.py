"""Bounded policy-13 experiment for RTX 5080 / NVIDIA 580.97.

Default: capture and suppress the ordinary power-slider identity SET so its
exact RM wire format can be inspected without sending that SET to the driver.
"""
import argparse
import ctypes as C
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
from probe_5080_power_channels import capture_transport, read
from probe_5080_policy_series import capture as capture_policies

OUT = ROOT / "experiments/power-5080-20260908/policy13-write"
GET_CONTROL, SET_CONTROL = 0x2080a61a, 0x2080e61b
PARAM_BYTES = 4432
FULL_MASK = 0x3ffff
POLICY = 13
RECORD_WORD = (0x14 + POLICY * 0x7c) // 4
LIMIT_WORD = RECORD_WORD + 1
DLL_SHA256 = "4d7a5e8b7d12e8060a204fc48632de5c0003626ef2f9bc130fcd9210f5cb1e03"


def get_control(gpu, transport):
    return read(gpu, transport, GET_CONTROL, PARAM_BYTES, (0, 0, 0, 0, FULL_MASK))


def capture_suppressed_slider_set(gpu):
    """Capture the real NVAPI-created SET, but refuse to forward it."""
    gdi = gpu._legacy_clk_gdi()
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.VirtualProtect.argtypes = [C.c_void_p, C.c_size_t, n.u32, C.POINTER(n.u32)]
    kernel.GetCurrentProcess.restype = C.c_void_p
    kernel.FlushInstructionCache.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
    process = kernel.GetCurrentProcess()
    address = C.cast(gdi.D3DKMTEscape, C.c_void_p).value
    original = C.string_at(address, 14)
    proto = C.WINFUNCTYPE(C.c_long, C.c_void_p)
    records = []
    allowed_reads = {0x20800111, 0x2080a612, 0x2080a618, 0x2080a61a,
                     0x2080a619, 0x2080a613}

    def restore():
        C.memmove(address, original, 14)
        kernel.FlushInstructionCache(process, address, 14)

    def callback(ptr):
        restore()
        escape = n.GPU._Escape.from_address(ptr)
        record = {"suppressed": True}
        if escape.pPrivateDriverData and escape.PrivateDriverDataSize >= 68:
            data = C.cast(escape.pPrivateDriverData, C.POINTER(n.u32))
            record.update(command=data[14], size=data[15], header=list(data[:17]),
                          fields={k: getattr(escape, k) for k in
                                  ("hAdapter", "hDevice", "Type", "Flags", "hContext")},
                          input=list(data[17:escape.PrivateDriverDataSize // 4]))
            if data[14] in allowed_reads:
                record["suppressed"] = False
                rc = proto(address)(ptr)
                record["output"] = list(data[17:escape.PrivateDriverDataSize // 4])
                record["rm_status"] = data[16]
            else:
                # Synthetic failure, explicitly labeled, never a driver result.
                data[16] = 0x56
                rc = -1073741637  # STATUS_NOT_SUPPORTED
                record["synthetic_failure"] = True
        else:
            rc = -1073741637
        record["escape_status"] = rc
        records.append(record)
        C.memmove(address, patch, 14)
        kernel.FlushInstructionCache(process, address, 14)
        return rc

    keep = proto(callback)
    patch = b"\xff\x25\0\0\0\0" + struct.pack("<Q", C.cast(keep, C.c_void_p).value)
    block = n._PwrPolStatus(version=gpu.nvapi.ver(n._PwrPolStatus, 1))
    assert gpu.nvapi.PowerPolStatus(gpu.nvapi.gpu, C.byref(block)) == 0
    assert block.count == 1 and block.entries[0].target_pcm == 125000
    setter = gpu.nvapi._i(0xad95f5ed, n.PTR, n.PTR)
    old = n.u32()
    if not kernel.VirtualProtect(address, 14, 0x40, C.byref(old)):
        raise C.WinError(C.get_last_error())
    try:
        C.memmove(address, patch, 14)
        kernel.FlushInstructionCache(process, address, 14)
        rc = setter(gpu.nvapi.gpu, C.byref(block))
    finally:
        restore()
        unused = n.u32()
        kernel.VirtualProtect(address, 14, old.value, C.byref(unused))
    blocked = [r for r in records if r["suppressed"]]
    assert len(blocked) == 1 and blocked[0]["command"] == SET_CONTROL
    return {"nvapi_status_after_synthetic_failure": rc, "captures": records,
            "hardware_set_forwarded": False}


def save_report(path, report):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def direct_set(gpu, transport, current_control, target):
    """Only identity, 5 A decrease, or restoration of the measured 300 A."""
    assert target in (295000, 300000)
    assert len(current_control) * 4 == PARAM_BYTES
    assert current_control[4] == FULL_MASK
    assert current_control[RECORD_WORD] == 0x12
    assert current_control[LIMIT_WORD] in (295000, 300000)
    header, fields = transport
    params = list(current_control)
    params[4] = 1 << POLICY
    params[LIMIT_WORD] = target
    changed = {i for i, (a, b) in enumerate(zip(params, current_control)) if a != b}
    assert changed <= {4, LIMIT_WORD}
    packet = (n.u32 * (17 + PARAM_BYTES // 4))(*header, *params)
    packet[2], packet[14], packet[15], packet[16] = C.sizeof(packet), SET_CONTROL, PARAM_BYTES, 0
    started = time.time()
    status = gpu._legacy_clk_escape(packet, fields)
    return {"command": hex(SET_CONTROL), "started": started, "duration_s": time.time() - started,
            "selected_mask": hex(params[4]), "requested_limit_ma": target,
            "changed_parameter_words": sorted(changed),
            "ntstatus": status, "rm_status": packet[16],
            "input_params": params, "output_params": list(packet[17:])}


def observations(gpu, count=8):
    result = []
    for _ in range(count):
        sample, _, _ = capture_policies(gpu)
        telemetry = {}
        gpu._read_power(telemetry)
        gpu._read_throttle(telemetry)
        clock = n.u32()
        if gpu.nvml.dll.nvmlDeviceGetClockInfo(gpu.nvml.dev, 0, C.byref(clock)) == 0:
            telemetry["graphics_mhz"] = clock.value
        result.append({"time": sample["unix_time"], "telemetry": telemetry,
                       "policy_13": sample["policies"][13],
                       "policy_14": sample["policies"][14],
                       "board_policy": sample["policies"][8],
                       "current_policy_candidate_khz": sample["policies"][10]["status_prefix"][64:66],
                       "global_candidate_khz": sample["global_clock_words"][3:5]})
        time.sleep(.25)
    return result


def execute_test(gpu, read_transport, captured, baseline):
    # Use transport fields from the exact captured ordinary SET on this GPU's
    # live client; _legacy_clk_escape opens and closes a PCI-matched adapter.
    setter = next(r for r in captured["captures"] if r["suppressed"])
    getter = next(r for r in captured["captures"] if r["command"] == GET_CONTROL)
    assert setter["size"] == PARAM_BYTES
    assert {i for i, (a, b) in enumerate(zip(getter["output"], setter["input"])) if a != b} == {4}
    assert setter["input"][4] == 0x100
    write_transport = (setter["header"], setter["fields"])
    path = OUT / "execution.json"
    if path.exists():
        raise RuntimeError("Refusing to overwrite a previous execution record")
    report = {"identity": gpu.static, "original_control": baseline,
              "experiment": "policy 13: identity 300 A, temporary 295 A, restore 300 A",
              "phases": {}, "writes": []}
    save_report(path, report)
    report["phases"]["baseline"] = observations(gpu)
    save_report(path, report)
    attempted_write = False
    try:
        for phase, target in (("identity", 300000), ("lower", 295000)):
            current = get_control(gpu, read_transport)
            if phase == "identity":
                assert current == baseline
            else:
                assert current == baseline, "Identity SET changed the control record"
            report["pending_write"] = {"phase": phase, "target_ma": target}
            save_report(path, report)
            attempted_write = True
            result = direct_set(gpu, write_transport, current, target)
            result["phase"] = phase
            report["writes"].append(result)
            report.pop("pending_write", None)
            after = get_control(gpu, read_transport)
            result["readback_control"] = after
            result["control_changed_words"] = [i for i, (a, b) in enumerate(zip(baseline, after)) if a != b]
            save_report(path, report)
            assert set(result["control_changed_words"]) <= {LIMIT_WORD}, "Other control field changed"
            report["phases"][phase] = observations(gpu)
            save_report(path, report)
            if result["ntstatus"] != 0 or result["rm_status"] != 0:
                report["outcome"] = f"{phase} rejected"
                break
            if after[LIMIT_WORD] != target:
                report["outcome"] = f"{phase} acknowledged but control did not retain requested value"
                break
            if phase == "lower":
                effective = {s["policy_13"]["limit"] for s in report["phases"][phase]}
                report["outcome"] = ("295 A stored and effective" if effective == {295000}
                                     else f"295 A stored; effective limits were {sorted(effective)}")
    except BaseException as exc:
        report["error"] = repr(exc)
        raise
    finally:
        # A failed GET or an unexpected clamped value must not prevent restore.
        # Replaying the original selected record also restores any unexpected
        # subfield change. The one-bit mask leaves all other policies alone.
        current = None
        try:
            current = get_control(gpu, read_transport)
        except BaseException as exc:
            report["pre_restore_read_error"] = repr(exc)
        if attempted_write and (current is None or current[RECORD_WORD:RECORD_WORD + 31] != baseline[RECORD_WORD:RECORD_WORD + 31]):
            report["pending_write"] = {"phase": "restore", "target_ma": 300000}
            try:
                save_report(path, report)
            except OSError as exc:
                report["pre_restore_log_error"] = repr(exc)
            restored = direct_set(gpu, write_transport, baseline, 300000)
            restored["phase"] = "restore"
            report["writes"].append(restored)
            report.pop("pending_write", None)
        report["final_control"] = get_control(gpu, read_transport)
        report["all_control_bytes_restored"] = report["final_control"] == baseline
        save_report(path, report)
        report["phases"]["restored"] = observations(gpu)
        report["effective_limit_restored"] = all(s["policy_13"]["limit"] == 300000
                                                   for s in report["phases"]["restored"])
        save_report(path, report)
        assert report["all_control_bytes_restored"] and report["effective_limit_restored"], "Restoration did not verify"
    summary = {"outcome": report.get("outcome"), "all_control_bytes_restored": report["all_control_bytes_restored"],
               "effective_limit_restored": report["effective_limit_restored"],
               "writes": [{k: r[k] for k in ("phase", "requested_limit_ma", "ntstatus", "rm_status")} for r in report["writes"]],
               "effective_limits": {phase: sorted({s["policy_13"]["limit"] for s in samples})
                                    for phase, samples in report["phases"].items()}}
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run the bounded 300/295/300 A experiment")
    args = parser.parse_args()
    gpu = n.GPU("0000:01:00.0")
    assert (gpu.static["name"], gpu.static["driver"], gpu.static["vbios"]) == (
        "NVIDIA GeForce RTX 5080", "580.97", "98.03.3b.c0.6f")
    assert hashlib.sha256(Path("C:/Windows/System32/nvapi64.dll").read_bytes()).hexdigest() == DLL_SHA256
    transport = capture_transport(gpu)
    before = get_control(gpu, transport)
    assert before[RECORD_WORD] == 0x12 and before[LIMIT_WORD] == 300000
    OUT.mkdir(exist_ok=True)
    result = capture_suppressed_slider_set(gpu)
    after = get_control(gpu, transport)
    report = {"identity": gpu.static, "before": before, "after": after,
              "unchanged": before == after, "capture": result}
    (OUT / "suppressed-slider-identity.json").write_text(json.dumps(report, indent=2))
    assert before == after
    setter = next(r for r in result["captures"] if r["suppressed"])
    getter = next(r for r in result["captures"] if r["command"] == GET_CONTROL)
    delta = [{"byte_offset": hex(i * 4), "get": a, "set": b}
             for i, (a, b) in enumerate(zip(getter["output"], setter["input"])) if a != b]
    print(json.dumps({"hardware_set_forwarded": False, "unchanged": before == after,
                      "set_param_bytes": setter["size"], "set_input_header": setter["input"][:5],
                      "get_to_set_delta": delta}, indent=2))
    if args.execute:
        execute_test(gpu, transport, result, before)


if __name__ == "__main__":
    main()
