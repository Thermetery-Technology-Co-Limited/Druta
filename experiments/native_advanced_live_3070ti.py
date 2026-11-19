"""Opt-in native-backed advanced controls: small requests and verified cleanup.

Default is read-only. Root owns hardware execution. Use a reviewed native DLL
through DRUTA_NATIVE_LIB and pass --write only after reviewing this harness.
The filename identifies this qualification run, not a hardware compatibility
gate. Current capability checks, identities and values drive every operation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
from pathlib import Path
import struct
import sys
import traceback

from native_live_smoke_3070ti import (
    Report, SkipControl, diagnostics, fan_request, identity, observe, paired,
    plain, snapshot, utc_now,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from native_advanced_inventory_3070ti import CONTROLLERS


TYPES = {key: (module, name) for key, module, name in CONTROLLERS}
TYPES["individual"] = ("individual_power_gating_live", "IndividualPowerGatingController")
CASES = (
    ("elpg_disable", "elpg", None, "disabled"),
    ("elcg_disable", "elcg", None, "disabled"),
    ("blcg_disable", "blcg", None, "disabled"),
    ("slcg_disable", "slcg", None, "disabled"),
    ("individual_gr", "individual", 0, "disabled"),
    ("individual_ms", "individual", 4, "disabled"),
    ("vf_monotonicity", "vf_policies", "clock_vf.monotonicity", None),
    ("vf_smoothening", "vf_policies", "clock_vf.smoothening", None),
    ("voltage_request_gate", "voltage_request_gate", None, None),
    ("switch_delay_0", "voltage_switch_delay", 0, None),
    ("switch_delay_1", "voltage_switch_delay", 1, None),
    ("elcg_enable", "elcg", None, "enabled"),
    ("slcg_enable", "slcg", None, "enabled"),
)
GATING = {"elpg", "elcg", "blcg", "slcg", "individual"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_once(controller):
    result = observe(controller.read)  # bounded GET retries only
    if not result["ok"]:
        raise RuntimeError("Independent controller read failed: " + str(result["errors"]))
    return result["value"]


def projection(kind, state):
    """Stable configured witnesses; ownership and activity remain in raw evidence."""
    if kind == "elpg":
        return {"support": state["support"], "controllers": {
            str(index): {key: row[key] for key in ("available", "supported", "reason_masks")}
            for index, row in state["controllers"].items()
        }}
    if kind == "elcg":
        return {key: state[key] for key in ("support", "available", "supported_engines", "eligible_engines", "attribute_chunks")}
    if kind in {"blcg", "slcg"}:
        keys = ("support", "architecture", "layout", "registers")
        result = {key: state[key] for key in keys}
        if kind == "slcg":
            result.update({key: state[key] for key in ("status", "unavailable_registers", "unverified_source_peers")})
        return result
    if kind == "individual":
        return {key: state[key] for key in ("architecture", "support", "available", "reason_masks", "status", "index", "name")}
    if kind == "voltage_switch_delay":
        return {key: state[key] for key in ("identity", "mask", "info_hex", "control_hex")}
    # VF includes the complete exact header; gate includes its paired identity.
    return copy.deepcopy(state)


def masks(row):
    return {int(key): value for key, value in row["reason_masks"].items()}


def no_owned_request(controller, state):
    profiler = getattr(controller, "_profiler", None)
    return (state.get("owned_request", False) is False
            and state.get("uncertain_outcome", False) is False
            and getattr(profiler, "handle", None) is None
            and getattr(controller, "_arbitration_probe", None) is None)


def gating_preflight(kind, before, mode):
    if before.get("support") is not True or before.get("owned_request") is not False or before.get("uncertain_outcome") is not False:
        raise SkipControl("Current controller is unsupported, already owned, or uncertain")
    if kind == "individual" and (before.get("available") is not True or masks(before)[0x48] & 1):
        raise SkipControl("Individual acquisition requires an available, clear reason0 baseline")
    if kind == "elcg":
        if not before.get("available") or not before.get("eligible_engines"):
            raise SkipControl("ELCG has no complete positive eligibility witness")
        if mode == "enabled" and before["eligible_engines"] != before["supported_engines"]:
            raise SkipControl("ELCG ENABLE needs supported==eligible before acquisition")
    if kind == "slcg":
        if before.get("unverified_source_peers"):
            raise SkipControl("SLCG has source peers without independent readback")
        # ENABLE release returns source-release values; an exact original-state
        # experiment therefore starts there for either mode.
        if before["registers"] != before["expected_release_values"]:
            raise SkipControl("SLCG experiment requires the exact current source-release baseline")


def verify_gating_active(kind, before, active, mode):
    require(active.get("owned_request") is True and active.get("uncertain_outcome") is False,
            "Controller did not confirm a certain owned request")
    if kind in {"elcg", "slcg"}:
        require(active.get("owned_mode") == mode, "Owned request mode differs")
        if mode == "enabled":
            require(active.get("arbitration_verified") is True, "ENABLE arbitration was not independently verified")
    if kind == "elpg":
        require(before["support"] == active["support"] and before["controllers"].keys() == active["controllers"].keys(), "ELPG support inventory changed")
        changed = []
        for key, old in before["controllers"].items():
            new = active["controllers"][key]
            require(all(old[field] == new[field] for field in ("available", "supported")), "ELPG support changed")
            if not old["supported"]:
                require(old == new, "Unsupported ELPG controller changed")
                continue
            a, b = masks(old), masks(new)
            require(b[0x49] == a[0x49] and b[0x48] in (a[0x48], a[0x48] | 1), "ELPG changed unrelated reason bits")
            if a != b:
                changed.append(str(key))
        require(bool(changed) and sorted(changed) == sorted(map(str, active["observed_controllers"])), "ELPG has no exact independently observed transition")
    elif kind == "elcg":
        require(before["supported_engines"] == active["supported_engines"] and active["available"] is True, "ELCG support changed")
        if mode == "enabled":
            require(active["eligible_engines"] == before["supported_engines"], "ELCG ENABLE eligibility differs")
        else:
            old, new = before["eligible_engines"], active["eligible_engines"]
            require(not new & ~old and bool(old & ~new), "ELCG did not remove a positive eligible subset")
            observed = sum(1 << bit for bit in active["observed_engines"])
            require(observed == old & ~new, "ELCG observed witness differs from eligibility transition")
    elif kind == "blcg":
        require(before["architecture"] == active["architecture"] and before["layout"] == active["layout"] and before["registers"].keys() == active["registers"].keys(), "BLCG inventory changed")
        changed = False
        for key, old in before["registers"].items():
            new = active["registers"][key]
            cleared = old & ~new
            require(not new & ~old and not cleared & ~before["allowed_clear_masks"][key], "BLCG changed unrelated register bits")
            require(active["observed_register_bits"].get(key) == cleared, "BLCG independent cleared-bit witness differs")
            changed |= bool(cleared)
        require(changed, "BLCG request had no positive register transition")
    elif kind == "slcg":
        expected = before["expected_enabled_values" if mode == "enabled" else "expected_disabled_values"]
        require(active["architecture"] == before["architecture"] and active["layout"] == before["layout"] and active["unavailable_registers"] == before["unavailable_registers"], "SLCG inventory changed")
        require(active["registers"] == expected, "SLCG exact requested register values differ")
    else:
        expected = projection(kind, before)
        expected["reason_masks"] = dict(expected["reason_masks"])
        expected["reason_masks"]["72"] = masks(before)[0x48] | 1
        require(projection(kind, active) == expected, "Individual request changed more than selected reason0")


def scalar_plan(kind, before, index, controller):
    expected = projection(kind, before)
    if kind == "vf_policies":
        from druta.clock_vf_registry import _EDITABLE
        field, offset, _, _ = _EDITABLE[index]
        baseline = before[field]
        if type(baseline) is not bool:
            raise SkipControl("VF policy is not a canonical Boolean")
        target = not baseline
        raw = bytearray.fromhex(expected["header_hex"])
        raw[offset] = int(target)
        expected[field], expected["header_hex"] = target, raw.hex()
        write = lambda: controller.set({index: target})
        restore = lambda: controller.set({index: baseline})
    elif kind == "voltage_request_gate":
        baseline = before["enabled"]
        if baseline is not True:
            raise SkipControl("Admission is already disabled; this trial does not enable a pre-existing disabled gate")
        target = False
        expected["enabled"] = target
        write = lambda: controller.set_enabled(target, expected=baseline)
        restore = lambda: controller.set_enabled(baseline, expected=target)
    else:
        row = next((row for row in before["rows"] if row["index"] == index), None)
        if row is None or row["status"] != "ready":
            raise SkipControl("Selected voltage device is absent or not ready")
        baseline = row["switch_delay_us"]
        if type(baseline) is not int or not 1 <= baseline < 0xFFFFFFFF:
            raise SkipControl("Switch delay cannot take a bounded one-microsecond increase")
        target = baseline + 1
        raw = bytearray.fromhex(expected["control_hex"])
        struct.pack_into("<I", raw, 8 + index * 12 + 4, target)
        expected["control_hex"] = raw.hex()
        kwargs = {key: row[key] for key in ("voltage_domain", "operation_type")}
        write = lambda: controller.set(index, target, **kwargs)
        restore = lambda: controller.set(index, baseline, **kwargs)
    return {"baseline": baseline, "target": target, "expected": expected, "write": write, "restore": restore}


def scalar_restore_compatible(kind, before, current, index):
    """Permit compensation of a quantized selected scalar, never unrelated data."""
    original, projected = projection(kind, before), projection(kind, current)
    if kind == "vf_policies":
        from druta.clock_vf_registry import _EDITABLE
        field, offset, _, _ = _EDITABLE[index]
        raw = bytearray.fromhex(projected["header_hex"])
        raw[offset] = bytes.fromhex(original["header_hex"])[offset]
        projected[field], projected["header_hex"] = original[field], raw.hex()
    elif kind == "voltage_request_gate":
        projected["enabled"] = original["enabled"]
    else:
        offset = 8 + index * 12 + 4
        raw = bytearray.fromhex(projected["control_hex"])
        raw[offset:offset + 4] = bytes.fromhex(original["control_hex"])[offset:offset + 4]
        projected["control_hex"] = raw.hex()
    return projected == original


def shared_requests(gpu):
    """Fresh independent configured requests; omit dynamic telemetry/effective limits."""
    captured = snapshot(gpu)
    names = ("core_range", "memory_range", "memory_scale", "pstates20", "vf_delta_snapshot",
             "vf_lock", "clock_lock", "owned_clock_lock_request", "power_request_mw",
             "nvml_power_request_mw", "power_policy_pins")
    result = {}
    for name in names:
        require(captured[name]["ok"], f"Shared request {name} is unreadable")
        result[name] = captured[name]["value"]
    require(captured["fan_state"]["ok"], "Fan request is unreadable")
    result["fan"] = fan_request(captured["fan_state"]["value"])
    rows, error = gpu.read_clk_domain_offsets()
    require(not error and isinstance(rows, dict), "Clock-domain request inventory is unreadable")
    result["clock_domains"] = plain(rows)
    rows, error = gpu.read_power_policies()
    require(not error and isinstance(rows, (list, tuple)), "Policy request inventory is unreadable")
    result["policies"] = [{key: row[key] for key in ("policy", "channel", "type", "unit", "requested")} for row in rows]
    rails = gpu.read_volt_rail_limits()
    require(isinstance(rails, dict), "Rail request inventory is unreadable")
    result["rails"] = {str(rail): {key: row[key] for key in ("type", "reliability", "alt_reliability", "overvoltage", "vmin") if key in row} for rail, row in rails.items()}
    result["voltage_boost"] = gpu.read_voltage_boost()
    result["nvvdd_offset"] = gpu.read_rail_offset_mv(0, 0)
    return plain(result)


def run_case(report, name, kind, index, mode, controller, *, write_enabled, audit, guard):
    """Injectable runner: fake-controller verification never opens a GPU."""
    record = {"name": name, "kind": kind, "index": index, "mode": mode, "status": "preflight", "started_utc": utc_now(), "attempted": False}
    report.data["operations"].append(record)
    cleanup_needed = False
    plan = None
    try:
        guard()
        before = read_once(controller)
        record["before"] = before
        record["before_projection"] = projection(kind, before)
        record["shared_before"] = audit()
        if kind in GATING:
            gating_preflight(kind, before, mode)
            require(no_owned_request(controller, before), "A newly read controller unexpectedly owns a request")
        else:
            plan = scalar_plan(kind, before, index, controller)
            record["request"] = {key: plan[key] for key in ("baseline", "target", "expected")}
        if not write_enabled:
            record["status"] = "read_only"
            return record
        guard()
        # Save the full baseline before any request can be dispatched.
        record["status"] = "dispatching"
        report.flush()
        cleanup_needed = record["attempted"] = True
        if kind in GATING:
            result = controller.hold_enabled() if mode == "enabled" else controller.hold()
        else:
            result = plan["write"]()
        record["returned"] = plain(result)
        record["active"] = read_once(controller)
        record["active_projection"] = projection(kind, record["active"])
        if kind in GATING:
            verify_gating_active(kind, before, record["active"], mode)
        else:
            require(record["active_projection"] == plan["expected"], "Independent full request readback differs from the selected projection")
        record["shared_active"] = audit()
        require(record["shared_active"] == record["shared_before"], "An unrelated configured request changed while active")
        record["active_verified"] = True
        record["status"] = "passed"
    except SkipControl as exc:
        record.update(status="skipped", reason=str(exc))
    except BaseException as exc:
        record.update(status="failed", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
        record["interrupted"] = not isinstance(exc, Exception)
        if hasattr(exc, "rollback"):
            record["controller_rollback"] = plain(exc.rollback)
    finally:
        if cleanup_needed:
            try:
                guard()
                if kind in GATING:
                    # Exactly one explicit lifecycle cleanup. For a counted
                    # individual reference its controller prevents a duplicate
                    # decrement after any uncertain release acknowledgement.
                    record["cleanup_result"] = plain(controller.close())
                else:
                    current = read_once(controller)
                    record["pre_restore"] = current
                    current_projection = projection(kind, current)
                    if current_projection == record["before_projection"]:
                        record["cleanup_result"] = "Original selected state already restored; no SET"
                    else:
                        require(scalar_restore_compatible(kind, before, current, index), "State changed outside the selected scalar; refusing to overwrite concurrent/unknown changes")
                        record["cleanup_result"] = plain(plan["restore"]())
            except BaseException as exc:
                record["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            try:
                record["after"] = read_once(controller)
                record["after_projection"] = projection(kind, record["after"])
                record["shared_after"] = audit()
                restored = (record["after_projection"] == record["before_projection"]
                            and record["shared_after"] == record["shared_before"]
                            and (kind not in GATING or no_owned_request(controller, record["after"])))
                record["restoration_verified"] = restored
                if not restored:
                    record["status"] = "restoration_failed"
                elif "cleanup_error" in record:
                    record["status"] = "failed_restored"
            except BaseException as exc:
                record.update(status="restoration_failed", restoration_verified=False,
                              restoration_error=f"{type(exc).__name__}: {exc}")
        record["finished_utc"] = utc_now()
        report.flush()
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if Path(args.output).exists():
        parser.error("Choose a new output path; previous evidence is not overwritten")
    report = Report(Path(args.output), args)
    report.data["scope"] = "Native-backed advanced runtime controls, exact selected projections and owned-request cleanup"
    report.data["limitations"] = [
        "Read-only unless --write is explicit; no load generator or physical-voltage/gating-residency claim.",
        "Runtime controllers remain Python consumers of the forced-native GPU transport.",
        "No raw replay, registry write, I2C traffic, restart or profile restore is requested.",
        "Only the selected controller's supported methods dispatch tuning requests; all other requests are audited.",
        "The in-process mutex does not exclude other tuning applications; use one writer for this adapter.",
    ]
    gpu = backend = None
    try:
        from druta import gpu_backend as backend, native, native_frontend
        gpu = backend.open_gpu(args.slot, kind="native")
        report.data["identity"] = identity(gpu)
        report.data["surface_missing_members"] = list(native_frontend.missing_members())
        report.data["backend"] = {"description": backend.describe(), "choice": vars(backend.choice())}
        library = native.load()
        report.data["library"] = {"path": str(library.path), "abi": library.dc_abi_version(),
                                  "sha256": hashlib.sha256(Path(library.path).read_bytes()).hexdigest()}
        guard = lambda: paired(gpu, args.slot, native.parse_slot)
        report.data["pairing"] = guard()
        report.data["initial_requests"] = shared_requests(gpu)
        report.flush()
        completed = {}
        for name, kind, index, mode in CASES:
            if mode == "enabled" and args.write and completed.get(kind + "_disable") != "passed":
                report.data["operations"].append({"name": name, "status": "skipped", "reason": "The preceding disable/release trial did not pass"})
                continue
            module, cls = TYPES[kind]
            try:
                constructor = getattr(importlib.import_module("druta." + module), cls)
                controller = constructor(gpu, index) if kind == "individual" else constructor(gpu)
            except Exception as exc:
                report.data["operations"].append({"name": name, "status": "skipped", "reason": f"Controller unavailable: {type(exc).__name__}: {exc}"})
                report.flush()
                continue
            record = run_case(report, name, kind, index, mode, controller,
                              write_enabled=args.write, audit=lambda: shared_requests(gpu), guard=guard)
            completed[name] = record["status"]
            diagnostics(report, backend)
            report.flush()
            print(f"{name}: {record['status']}", flush=True)
            if record["status"] == "restoration_failed" or record.get("interrupted"):
                report.data["stopped_after"] = name
                break
        report.data["final_requests"] = shared_requests(gpu)
        report.data["final_pairing"] = guard()
        report.data["all_requests_restored"] = report.data["final_requests"] == report.data["initial_requests"]
        failure = (any(row["status"] in ("failed", "failed_restored", "restoration_failed") for row in report.data["operations"])
                   or not report.data["all_requests_restored"] or bool(report.data["diagnostics"]))
        report.data["status"] = "completed_with_failures" if failure else "completed"
    except BaseException as exc:
        report.data.update(status="aborted", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        if gpu is not None:
            try:
                gpu.close()
                report.data["close"] = {"ok": True}
            except Exception as exc:
                report.data["close"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if report.data["status"] == "completed":
                    report.data["status"] = "completed_with_failures"
        if backend is not None:
            diagnostics(report, backend)
            if report.data["diagnostics"] and report.data["status"] == "completed":
                report.data["status"] = "completed_with_failures"
        report.data["finished_utc"] = utc_now()
        report.flush()
        print(f"{report.data['status']}: {report.path}", flush=True)
    return 0 if report.data["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
