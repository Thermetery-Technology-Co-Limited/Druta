#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in native request tests, each followed by exact baseline restoration.

No GPU access on import or --help. Default execution is read-only.
The filename identifies the first target, never a compatibility allowlist.
Close other tuning clients: the card mutex only coordinates this process.
These tests establish stored requests, not physical voltage/current enforcement.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import math
from pathlib import Path
import struct
import sys
import time
import traceback

from native_live_smoke_3070ti import (
    Report, RestorationFailed, SkipControl, diagnostics, fan_request, identity,
    integer, observe, paired, plain, snapshot, utc_now,
)

FIELDS = ("reliability", "alt_reliability", "overvoltage", "vmin")
CASES = ("boost", "current", "policies", "rail_limit", "nvvdd", "msvdd")


def value(state, key, *, nullable=False):
    row = state[key]
    if not row["ok"] or (row.get("value") is None and not nullable):
        raise SkipControl(f"{key} has no readable baseline: {row}")
    return row.get("value")


def microvolts(mv):
    if type(mv) not in (float, int) or not math.isfinite(mv):
        raise SkipControl("voltage request is not finite")
    uv = round(mv * 1000)
    if not -(1 << 31) <= uv < (1 << 31) or abs(mv * 1000 - uv) > 1e-6:
        raise SkipControl("voltage request is not an exact signed microvolt value")
    return uv


def clock_blocks(gpu):
    layout = gpu.clkdom_layout()
    if layout is None:
        return None
    domains = gpu.clkdom_domains()
    if not domains:
        return None
    if any(type(d) is not int or not 0 <= d < 32 for d in domains) or len(set(domains)) != len(domains):
        raise RuntimeError("invalid accepted clock-control domain inventory")
    mask = sum(1 << domain for domain in domains)
    status, block = gpu._clkdom_get(mask)
    if status or not gpu._clkdom_valid_block(block, layout, mask):
        raise RuntimeError(f"combined accepted clock-control mask: invalid read, status {status}")
    return {"layout": vars(layout), "domains": domains, "ui_controls": gpu.clkdom_controls_for_ui(),
            "mask": mask, "hex": bytes(block)[:layout.size].hex()}


def capture(gpu):
    from druta import native_rails

    state = snapshot(gpu)
    readers = {
        "boost": lambda: gpu.read_voltage_boost(),
        "current_rows": lambda: gpu.get_current_limits(),
        "policy_rows": lambda: gpu.read_power_policies(),
        "policy_table": lambda: gpu._power_policy_table(),
        "rail_deltas": lambda: native_rails.read_volt_rail_deltas(gpu),
        "rail_limits": lambda: gpu.read_volt_rail_limits(),
        "rail_state": lambda: gpu.read_volt_rail_state(),
        "rail_supported": lambda: gpu.volt_rail_limits_supported(0),
        "rail_fields": lambda: gpu.volt_rail_limit_fields(0),
        "rail_diagnostics": lambda: gpu.volt_rail_diagnostics(),
        "hold": lambda: gpu.hold_headroom_record(),
        "foreign_hold": lambda: gpu._foreign_hold_headroom,
        "clock_blocks": lambda: clock_blocks(gpu),
        "nvvdd": lambda: gpu.rail_offset_capability(0),
        "msvdd": lambda: gpu.rail_offset_capability(1),
        "flags": lambda: {"volt_limits_write_enabled": gpu.volt_limits_write_enabled,
                          "msvdd_write_enabled": gpu.msvdd_write_enabled,
                          "voltage_xoc_enabled": gpu.voltage_xoc_enabled},
    }
    state.update({key: observe(reader) for key, reader in readers.items()})
    return state


def requests(state, keys=None):
    """Separate exact configured requests from sampled status and derived VF frequencies."""
    required = ("core_range", "memory_range", "pstates20", "vf_delta_snapshot", "vf_curve", "vf_other_rows",
                "vf_lock", "clock_lock", "owned_clock_lock_request", "power_request_mw",
                "nvml_power_request_mw", "live_power_request", "power_policy_pins", "fan_state",
                "hold", "foreign_hold")
    optional = ("boost", "policy_table", "rail_deltas", "clock_blocks")
    if keys is None:
        keys = list(required) + [k for k in optional if state[k]["ok"] and state[k].get("value") is not None]
    result = {}
    for key in keys:
        item = copy.deepcopy(value(state, key, nullable=key in (
            "owned_clock_lock_request", "hold", "foreign_hold")))
        if key == "fan_state":
            item = fan_request(item)
        elif key == "vf_curve":
            rows, error = item
            if error or not rows:
                raise SkipControl("V/F point geometry is unavailable")
            item = [{k: point[k] for k in ("idx", "volt_mv", "delta_khz")} for point in rows]
        elif key == "policy_table":
            layout, info, control, _sampled_status = item
            if control is None:
                raise SkipControl("policy CONTROL block is unavailable")
            item = {"layout": layout, "info": info, "control": control}
        elif key == "rail_deltas":
            item = {rail: {"type": row["type"], **{k: microvolts(row[k]) for k in FIELDS}}
                    for rail, row in item.items()}
        elif key == "live_power_request":
            observed, error = item
            # Native CONTROL contains every policy, already compared separately.
            # Arbitration/effective limits are sampled status, not our request.
            item = ({k: observed[k] for k in ("identity", "binding", "requested_mw", "control_hex")}
                    if observed is not None and error is None else {"unavailable": error})
        result[key] = item
    return result


def differences(before, after):
    return [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]


def policy_rows(state):
    rows, error = value(state, "policy_rows")
    if error:
        raise SkipControl(f"policy rows unavailable: {error}")
    return rows


def representative_policies(state, count):
    chosen, kinds = [], set()
    for row in policy_rows(state):
        kind = (row["type"], row["unit"])
        if (not row["writable"] or row["board"] or row["named_current"] or row["maybe_board"]
                or row["requested"] != row["limit"] or row["unit"] not in ("mA", "mW")
                or not row["minimum"] <= row["requested"] - 1000 < row["requested"] <= row["maximum"]
                or kind in kinds):
            continue
        chosen.append(row["policy"])
        kinds.add(kind)
        if len(chosen) == count:
            break
    return chosen


def selected_policy_indices(state, explicit, count):
    """Selection only; every index still needs fresh per-case qualification."""
    if explicit is None:
        return representative_policies(state, count)
    if not explicit or any(type(index) is not int or index < 0 for index in explicit):
        raise ValueError("policy indices must be nonnegative integers")
    return list(dict.fromkeys(explicit))


def policy_index_argument(value):
    try:
        index = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("policy index must be a nonnegative integer") from None
    if index < 0:
        raise argparse.ArgumentTypeError("policy index must be a nonnegative integer")
    return index


def prepare(kind, index, gpu, state, baseline):
    """Only build a plan; this function does not issue any write or change opt-in flags."""
    for key in ("hold", "foreign_hold"):
        if value(state, key, nullable=True):
            raise SkipControl(f"an active {key} makes this isolated experiment unsuitable")
    live, live_error = value(state, "live_power_request")
    if live is not None and live_error is None and live.get("requested_mw") != value(state, "nvml_power_request_mw"):
        raise SkipControl("live and NVML configured board-power requests disagree")
    if value(state, "vf_lock") != [None, None] or value(state, "clock_lock") != [None, None]:
        raise SkipControl("release existing clock locks before this isolated voltage/current experiment")
    expected = copy.deepcopy(baseline)
    plan = {"kind": kind, "index": index, "temporary_flags": {}, "physical_effect": "not measured"}
    if kind == "boost":
        original = integer(value(state, "boost"), "boost percentage")
        if not 0 <= original <= 100:
            raise SkipControl("boost is outside its replayable 0–100% range")
        target = original + 1 if original < 100 else original - 1
        expected["boost"] = target
        setter = lambda number: gpu.set_voltage_boost(number)
    elif kind in ("current", "policy"):
        if type(index) is not int or index < 0:
            raise SkipControl("policy index must be a nonnegative integer")
        if kind == "current":
            rows = value(state, "current_rows")
            row = next((r for r in rows if r["policy"] == index), None)
            if row is None:
                raise SkipControl("named current policy is unavailable")
            original = integer(row["requested_ma"], "current request")
            high = row["maximum_ma"] if gpu.voltage_xoc_enabled else min(
                row["maximum_ma"], max(row["normal_maximum_ma"], row["limit_ma"]))
            if original != row["limit_ma"] or not row["minimum_ma"] <= original - 1000 < original <= high:
                raise SkipControl("current baseline/target are not exactly replayable within mode bounds")
            setter = lambda number: gpu.set_current_limit_ma(index, number, pin=True)
            plan["unit"] = "mA"
        else:
            row = next((r for r in policy_rows(state) if r["policy"] == index), None)
            if (row is None or not row["writable"] or row["board"] or row["named_current"]
                    or row["maybe_board"] or row["unit"] not in ("mA", "mW")):
                raise SkipControl("policy is not offered by the existing qualified policy route")
            original = integer(row["requested"], "policy request")
            if original != row["limit"] or not row["minimum"] <= original - 1000 < original <= row["maximum"]:
                raise SkipControl("policy has no independently replayable small decrease")
            setter = lambda number: gpu.set_power_policy(index, number, pin=True)
            plan["unit"] = row["unit"]
        target = original - 1000
        table = expected.get("policy_table")
        if table is None:
            raise SkipControl("full policy CONTROL block is required")
        base, stride = table["layout"]["control"]
        if base % 4 or stride % 4:
            raise SkipControl("policy CONTROL layout is not word aligned")
        word = (base + index * stride) // 4 + 1
        if not 0 <= word < len(table["control"]) or table["control"][word] != original:
            raise SkipControl("selected policy word does not match its named request")
        table["control"][word] = target
        expected["power_policy_pins"] = {**(expected["power_policy_pins"] or {}), str(index): target}
        plan["control_word"] = word
    elif kind == "rail_limit":
        field = "reliability" if index is None else index
        if field not in FIELDS:
            raise SkipControl("only understood REL, ALT-REL, OV and VMIN request fields may be tested")
        if not value(state, "rail_supported") or field not in value(state, "rail_fields"):
            raise SkipControl(f"rail 0 {field} is not qualified by its current-adapter getters")
        row = value(state, "rail_limits").get("0")
        if not row or not row.get("_absolute_ok") or "rail_deltas" not in expected:
            raise SkipControl("rail 0 has no current-card absolute reference and exact raw request")
        original = expected["rail_deltas"]["0"][field]
        target = original - 6250
        base = row["_base_mv"][field]
        low, high = gpu.VOLT_LIMIT_MIN_MV, gpu.VOLT_LIMIT_MAX_MV
        if not low <= base + target / 1000 <= base + original / 1000 <= high:
            raise SkipControl(f"{field} decrease and restoration do not both fit the normal absolute bounds")
        if microvolts(row[field]) != original:
            raise SkipControl("rail limit and raw delta readers disagree")
        expected["rail_deltas"]["0"][field] = target
        setter = lambda number: gpu.set_volt_rail_limits_raw(0, **{field: number})
        plan.update(unit="µV stored delta", temporary_flags={"volt_limits_write_enabled": True},
                    rail=0, field=field,
                    reference="quantized first read estimate, not a factory default")
    elif kind in ("nvvdd", "msvdd"):
        rail = 0 if kind == "nvvdd" else 1
        cap = value(state, kind)
        blockset = expected.get("clock_blocks")
        if not cap["available"] or blockset is None:
            raise SkipControl(f"{kind} request field unavailable: {cap}")
        domain = integer(cap["domain"], "qualified clock domain")
        layout = blockset["layout"]
        field = layout.get(f"{kind}_uv")
        # rail_offset_capability qualifies rail records independently of the
        # frequency-offset sliders: its understood set also contains core 0.
        if type(field) is not int or domain not in blockset["domains"]:
            raise SkipControl("selected rail field/domain is not in the validated accepted control layout")
        original = microvolts(cap["value_mv"])
        target = original + 1000
        if not -100000 <= original < target <= round(gpu.RAIL_OFFSET_MAX_MV * 1000):
            raise SkipControl("offset and restoration must both remain inside normal NVVDD bounds")
        packet = bytearray.fromhex(blockset["hex"])
        offset = layout["header"] + domain * layout["stride"] + field
        if offset % 4 or not 0 <= offset <= len(packet) - 4 or struct.unpack_from("<i", packet, offset)[0] != original:
            raise SkipControl("selected raw signed offset word disagrees with the capability reader")
        struct.pack_into("<i", packet, offset, target)
        blockset["hex"] = packet.hex()
        setter = lambda number: gpu.set_rail_offset_mv(number / 1000, domain=domain, rail=rail)
        plan.update(unit="µV stored request", domain=domain, rail=rail, selected_byte=offset)
        if rail == 1:
            plan["temporary_flags"] = {"msvdd_write_enabled": True}
    else:
        raise ValueError(kind)
    plan.update(original=original, target=target)
    return plan, expected, setter


def success(result):
    return isinstance(result, (tuple, list)) and bool(result) and result[0] is True


def run_case(kind, index, gpu, report, args, backend, parse_slot):
    row = {"kind": kind, "index": index, "started_utc": utc_now(), "status": "preflight"}
    report.data["operations"].append(row)
    attempted = False
    flags, pins = None, None
    restoration_failed = False
    with gpu._lock:
        try:
            row["binding"] = paired(gpu, args.slot, parse_slot)
            row["before"] = capture(gpu)
            baseline = requests(row["before"])
            plan, expected, setter = prepare(kind, index, gpu, row["before"], baseline)
            row.update(plan=plan, expected_requests=expected)
            flags = value(row["before"], "flags")
            pins = copy.deepcopy(gpu._power_policy_pins)
            row["confirmation"] = capture(gpu)
            if requests(row["confirmation"], baseline) != baseline:
                raise SkipControl("configured state changed between the two preflight reads")
            if paired(gpu, args.slot, parse_slot) != row["binding"]:
                raise SkipControl("pairing changed during preflight")
            for key, setting in plan["temporary_flags"].items():
                setattr(gpu, key, setting)
            row["status"] = "journaled_before_write"
            report.flush()
            attempted = True  # A raised/interrupted setter is still an uncertain write.
            row["set_result"] = plain(setter(plan["target"]))
            time.sleep(args.settle_seconds)
            row["readback"] = capture(gpu)
            readback = requests(row["readback"], baseline)
            row["unexpected_request_sections"] = differences(expected, readback)
            row["request_readback_exact"] = expected == readback
            effective_ok = True
            if kind == "current":
                effective_ok = any(r["policy"] == index and r["requested_ma"] == plan["target"]
                                   and r["limit_ma"] == plan["target"]
                                   for r in value(row["readback"], "current_rows"))
                row["effective_current_readback_exact"] = effective_ok
            row["status"] = "passed" if success(row["set_result"]) and expected == readback and effective_ok else "failed"
        except SkipControl as exc:
            row.update(status="failed" if attempted else "skipped", reason=str(exc))
        except BaseException as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            if attempted:
                # Do not perform a fallible journal flush before the mandatory restoration.
                try:
                    row["restore_result"] = plain(setter(plan["original"]))
                except BaseException as exc:
                    row["restore_error"] = f"{type(exc).__name__}: {exc}"
            try:
                if flags is not None:
                    if attempted:
                        gpu._power_policy_pins = pins
                    for key, setting in flags.items():
                        setattr(gpu, key, setting)
            except BaseException as exc:
                row["bookkeeping_restore_error"] = f"{type(exc).__name__}: {exc}"
                restoration_failed = True
            if attempted:
                try:
                    time.sleep(args.settle_seconds)
                    row["restored"] = capture(gpu)
                    restored = requests(row["restored"], baseline)
                    row["remaining_request_sections"] = differences(baseline, restored)
                    row["restoration_verified"] = (success(row.get("restore_result")) and restored == baseline
                                                     and value(row["restored"], "flags") == flags)
                    if kind == "current":
                        row["effective_current_restored"] = any(
                            r["policy"] == index and r["requested_ma"] == plan["original"]
                            and r["limit_ma"] == plan["original"]
                            for r in value(row["restored"], "current_rows"))
                        row["restoration_verified"] &= row["effective_current_restored"]
                except BaseException as exc:
                    row["restoration_verified"] = False
                    row["restoration_check_error"] = f"{type(exc).__name__}: {exc}"
                restoration_failed |= not row["restoration_verified"]
            if restoration_failed:
                row["status"] = "restoration_failed"
            row["finished_utc"] = utc_now()
            diagnostics(report, backend)
            report.flush()
    if restoration_failed:
        raise RestorationFailed(f"{kind}: captured baseline was not fully restored; no further writes")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write", action="store_true", help="Enable bounded request tests and their restoration")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    policy_selection = parser.add_mutually_exclusive_group()
    policy_selection.add_argument("--policy-count", type=int, choices=(1, 3), default=3)
    policy_selection.add_argument("--policy-indices", type=policy_index_argument, nargs="+",
                                  help="Test these independent policies instead of representative selection; each is freshly qualified")
    parser.add_argument("--rail-fields", nargs="+", choices=FIELDS, default=["reliability"],
                        help="Independent rail-0 fields to test with a bounded decrease; default retains the original REL trial")
    parser.add_argument("--settle-seconds", type=float, default=0.25)
    args = parser.parse_args(argv)
    if args.policy_indices is not None and "policies" not in args.cases:
        parser.error("--policy-indices requires policies in --cases")
    if not math.isfinite(args.settle_seconds) or not 0 <= args.settle_seconds <= 5:
        parser.error("settle seconds must be finite and within 0–5")
    report = Report(Path(args.output), args)
    report.data.update(schema=1, scope="Qualified native voltage/current/policy request smoke; no workload.",
                       limitations=["Stored requests do not establish physical voltage/current enforcement.",
                                    "Rail absolute references are this handle's quantized first reads.",
                                    "No hold headroom, gate, registry, profile, reset or persistent change.",
                                    "Other tuning processes are not excluded by the in-process card mutex."],
                       parked=["hold headroom", "live voltage rel/constants", "unknown policy geometry"])
    gpu = backend = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from druta import gpu_backend as backend, native, native_frontend

        gpu = backend.open_gpu(args.slot, kind="native")
        library = native.load()
        report.data.update(identity=identity(gpu), backend=backend.describe(),
                           library={"path": library.path, "expected_abi": native.ABI_VERSION,
                                    "loaded_abi": library.dc_abi_version(),
                                    "sha256": hashlib.sha256(Path(library.path).read_bytes()).hexdigest()},
                           surface_missing_members=list(native_frontend.missing_members()), initial=capture(gpu))
        report.data["write_binding"] = observe(lambda: paired(gpu, args.slot, native.parse_slot))
        report.flush()
        if args.write:
            for kind in args.cases:
                if kind == "current":
                    rows = value(capture(gpu), "current_rows")
                    indices = [r["policy"] for r in rows]
                elif kind == "policies":
                    indices = selected_policy_indices(capture(gpu), args.policy_indices, args.policy_count)
                elif kind == "rail_limit":
                    indices = list(dict.fromkeys(args.rail_fields))
                else:
                    indices = [None]
                if not indices:
                    report.data["operations"].append({"kind": kind, "status": "skipped",
                                                      "reason": "no readable qualifying independent request"})
                for index in indices:
                    run_case("policy" if kind == "policies" else kind, index, gpu, report, args,
                             backend, native.parse_slot)
        diagnostics(report, backend)
        failed = any(r["status"] in ("failed", "restoration_failed") for r in report.data["operations"])
        report.data["status"] = "completed_with_failures" if failed or report.data["diagnostics"] else "completed"
    except BaseException as exc:
        report.data.update(status="aborted", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        if gpu is not None:
            report.data["final"] = capture(gpu)
            diagnostics(report, backend)
            report.data["close"] = observe(lambda: gpu.close())
            if report.data["status"] == "completed" and (report.data["diagnostics"] or not report.data["close"]["ok"]):
                report.data["status"] = "completed_with_failures"
        report.data["finished_utc"] = utc_now()
        report.flush()
        print(f"{report.data['status']}: {report.path}", flush=True)
    return 0 if report.data["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
