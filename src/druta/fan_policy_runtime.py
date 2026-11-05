# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only fan-policy curves/temperatures and registry activation contracts.

Evidence: experiments/registry-overrides/vbios-fields/fan-policy-stop-curve-contract.json.
Mode/coordinate trace: the adjacent fan-mode-stop-contract.json.
Loaded policy metadata, current CONTROL values and explicit policy-to-cooler masks
provide the binding. Physical fan behavior and the winning policy are not inferred.
No setters, registry access, restarts, or board/driver allowlists exist here.
"""

from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import math
import re
import struct
from collections.abc import Mapping
from contextlib import nullcontext

from . import fan_runtime
from .bios_runtime import BiosRuntimeError
from .perf_diagnostics import PROBE_ARCHITECTURES, PerfDiagnosticError, PerfDiagnosticsReader

INFO_COMMAND, INFO_BYTES = 0x20808532, 336
CONTROL_COMMAND, CONTROL_BYTES = 0x20808534, 1556
ROUTING_COMMAND, ROUTING_BYTES = 0x20808547, 332
CAPACITY = 16
SENTINEL, GUARD_SIZE = 0xA5, 64
ENVELOPE = {0: 0x4E564441, 1: 0x10002, 3: 0x4E562A2A, 4: 0x0500002B}
GETS = {
    (INFO_COMMAND, INFO_BYTES),
    (CONTROL_COMMAND, CONTROL_BYTES),
    (ROUTING_COMMAND, ROUTING_BYTES),
}


def u32(raw, offset):
    return struct.unpack_from("<I", raw, offset)[0]


def i32(raw, offset):
    return struct.unpack_from("<i", raw, offset)[0]


def decode(info, control, routing=None):
    if len(info) != INFO_BYTES or len(control) != CONTROL_BYTES:
        raise ValueError("Unexpected fan-policy parameter geometry")
    mask = u32(info, 4)
    if mask >> CAPACITY or mask != u32(control, 4):
        raise ValueError("Fan-policy inventory masks disagree or exceed capacity")
    if info[0] != control[0]:
        raise ValueError("Fan-policy group identities disagree")
    if control[16] != 0:
        raise ValueError("CONTROL GET did not retain the current-state selector")
    route_entries = []
    if routing is not None:
        if len(routing) != ROUTING_BYTES:
            raise ValueError("Unexpected fan-routing parameter geometry")
        route_mask = u32(routing, 4)
        if route_mask >> CAPACITY:
            raise ValueError("Fan-routing inventory exceeds capacity")
        for index in range(CAPACITY):
            if not route_mask & (1 << index):
                continue
            raw = routing[12 + index * 20 : 12 + (index + 1) * 20]
            entry = {"index": index, "type": raw[0], "raw_hex": raw.hex()}
            if raw[0] == 1:
                a, b = u32(raw, 8), u32(raw, 12)
                entry.update(
                    route_kind_raw=raw[1],
                    cooler_index=raw[2],
                    word_04_raw=int.from_bytes(raw[4:6], "little"),
                    policy_mask_a=a,
                    policy_mask_b=b,
                    policy_indices_a=[i for i in range(CAPACITY) if a & (1 << i)],
                    policy_indices_b=[i for i in range(CAPACITY) if b & (1 << i)],
                    routing_valid=raw[1] == 1 and raw[2] < CAPACITY and not ((a | b) & ~mask),
                )
            route_entries.append(entry)
    entries = []
    for index in range(CAPACITY):
        if not mask & (1 << index):
            continue
        raw = info[16 + index * 20 : 16 + (index + 1) * 20]
        ctl = control[20 + index * 96 : 20 + (index + 1) * 96]
        entry = {
            "index": index,
            "type": raw[0],
            "control_type": ctl[0],
            "info_hex": raw.hex(),
            "control_hex": ctl.hex(),
            "issues": [],
            "stop_issues": [],
            "routes": [
                {
                    "route_index": r["index"],
                    "cooler_index": r["cooler_index"],
                    "mask_a": bool(r["policy_mask_a"] & (1 << index)),
                    "mask_b": bool(r["policy_mask_b"] & (1 << index)),
                }
                for r in route_entries
                if r.get("routing_valid") and (r["policy_mask_a"] | r["policy_mask_b"]) & (1 << index)
            ],
        }
        if raw[0] != ctl[0]:
            entry["issues"].append("INFO/CONTROL row class mismatch")
        elif info[12] != 2 or raw[0] != 2:
            entry["issues"].append("Only loaded policy-table v20/runtime class2 is decoded")
        else:
            points = []
            for point in range(3):
                temp, duty, rpm = struct.unpack_from("<iII", ctl, 0x24 + point * 12)
                points.append(
                    {
                        "point": point,
                        "temperature_q8": temp,
                        "temperature_c": temp / 256,
                        "duty_q16": duty,
                        "duty_percent": duty * 100 / 65536,
                        "rpm": rpm,
                    }
                )
            low, high = i32(ctl, 0x4C), i32(ctl, 0x50)
            entry.update(
                policy_selector_raw=raw[1],
                point2_thermal_relative_flag=raw[5],
                flag4_raw=raw[6],
                point2_thermal_offset_q8=i32(raw, 8),
                curve_unit_selector_raw=raw[13],
                stop_support_flag_raw=raw[14],
                stop_default_enabled_raw=raw[15],
                stop_current_enabled_raw=ctl[0x48],
                stop_word_44_raw=int.from_bytes(raw[16:18], "little"),
                stop_selector_46_raw=raw[18],
                stop_low_q8=low,
                stop_high_q8=high,
                stop_low_c=low / 256,
                stop_high_c=high / 256,
                curve=points,
            )
            if any(raw[p] not in (0, 1) for p in (5, 6, 13, 14, 15)) or ctl[0x48] not in (0, 1):
                entry["issues"].append("Boolean metadata is outside the traced encoding")
            if not 0 < low < high <= 110 * 256:
                entry["stop_issues"].append("Stop threshold ordering/range is outside the planner contract")
            if any(p["duty_q16"] > 65536 or p["rpm"] > 65535 for p in points):
                entry["issues"].append("Curve value is outside the BIOS encoding")
            if any(
                a["temperature_q8"] >= b["temperature_q8"] or a["duty_q16"] > b["duty_q16"] or a["rpm"] > b["rpm"]
                for a, b in zip(points, points[1:], strict=False)
            ):
                entry["issues"].append("Curve ordering is invalid")
        entries.append(entry)
    return {
        "group_id": info[0],
        "mask": mask,
        "policy_layout": info[12],
        "auxiliary_mask_raw": u32(info, 8),
        "info_header_hex": info[:16].hex(),
        "control_header_hex": control[:20].hex(),
        "entries": entries,
        "routing_available": routing is not None,
        "routing_header_hex": None if routing is None else routing[:12].hex(),
        "routing_entries": route_entries,
        "scope": "Loaded INFO metadata and current CONTROL configuration; sequential reads, not atomic; no physical fan validation.",
    }


def _capture(reader):
    gpu = reader.gpu
    result = {"status": "transient", "snapshot": None, "attempts": []}
    for attempt in range(1, 3):
        record = {"attempt": attempt, "commands": []}
        result["attempts"].append(record)
        try:
            with getattr(gpu, "_lock", nullcontext()):
                api, slot, architecture = reader._binding()
                header, fields = reader._transport()
                if len(header) != 17 or any(header[i] != value for i, value in ENVELOPE.items()):
                    raise PerfDiagnosticError("malformed", "Fan policy needs the understood NVDA envelope")
                if any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in fields.values()):
                    raise PerfDiagnosticError("malformed", "Escape fields have invalid encodings")

                def get(command, size, mask=None, *, header=header, fields=fields, record=record):
                    if (command, size) not in GETS:
                        raise ValueError("Only the three traced fan-policy GETs are allowed")
                    total = 68 + size
                    storage = (ctypes.c_ubyte * (2 * GUARD_SIZE + total))()
                    ctypes.memset(ctypes.addressof(storage), SENTINEL, ctypes.sizeof(storage))
                    packet = (ctypes.c_uint32 * (total // 4)).from_buffer(storage, GUARD_SIZE)
                    ctypes.memset(ctypes.addressof(packet), 0, total)
                    for i, value in enumerate(header):
                        packet[i] = value
                    packet[2] = ctypes.sizeof(packet)
                    packet[14], packet[15], packet[16] = command, size, 0
                    packet[5] = packet[11] = 0
                    if mask is not None:
                        packet[18] = mask
                    expected = tuple(packet[:16])
                    reader._binding()
                    status = gpu._legacy_clk_escape(packet, fields)
                    reader._binding()
                    item = {
                        "command": f"0x{command:08X}",
                        "ntstatus": status,
                        "rm_status": int(packet[16]),
                        "context_flag": int(packet[5]),
                        "completion": int(packet[11]),
                    }
                    record["commands"].append(item)
                    if any(v != SENTINEL for v in storage[:GUARD_SIZE]) or any(
                        v != SENTINEL for v in storage[-GUARD_SIZE:]
                    ):
                        raise PerfDiagnosticError("malformed", "Fan-policy GET exceeded packet bounds")
                    if type(status) is not int or status != 0:
                        raise PerfDiagnosticError("transient", f"Fan-policy NTSTATUS {status}")
                    if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)):
                        raise PerfDiagnosticError("malformed", "Response changed its bound envelope or geometry")
                    if packet[5] not in (0, 1):
                        raise PerfDiagnosticError("malformed", "Fan-policy returned an invalid NVDA context flag")
                    if packet[11] != 1:
                        raise PerfDiagnosticError("transient", f"Fan-policy NVDA completion failed (0x{packet[11]:X})")
                    if packet[16] != 0:
                        kind = "unsupported" if packet[16] in (0x1F, 0x56) else "transient"
                        raise PerfDiagnosticError(kind, f"Fan-policy RM status 0x{packet[16]:X}")
                    payload = bytes(packet)[68:]
                    item.update(
                        payload_base64=base64.b64encode(payload).decode("ascii"),
                        sha256=hashlib.sha256(payload).hexdigest(),
                    )
                    return payload

                info = get(INFO_COMMAND, INFO_BYTES)
                mask = u32(info, 4)
                if not mask:
                    result.update(status="empty")
                    return result
                if mask >> CAPACITY:
                    raise PerfDiagnosticError("malformed", "Policy inventory exceeds capacity")
                control = get(CONTROL_COMMAND, CONTROL_BYTES, mask)
                routing = None
                try:
                    routing = get(ROUTING_COMMAND, ROUTING_BYTES)
                except PerfDiagnosticError as exc:
                    if exc.kind not in ("unsupported", "transient"):
                        raise
                    record["routing_error"] = {"kind": exc.kind, "error": str(exc)}
                try:
                    snapshot = decode(info, control, routing)
                except ValueError as exc:
                    raise PerfDiagnosticError("malformed", str(exc)) from exc
                snapshot.update(
                    slot=slot,
                    architecture=architecture,
                    identity={
                        "slot": slot,
                        "architecture": architecture,
                        "device_id": api.selected.get("devid"),
                        "subsystem_id": api.selected.get("subsys"),
                    },
                )
                result.update(status="ready", snapshot=snapshot)
                return result
        except PerfDiagnosticError as exc:
            record.update(error=str(exc), kind=exc.kind)
            result["status"] = exc.kind
            if exc.kind != "transient":
                break
        except Exception as exc:
            record.update(error=f"{type(exc).__name__}: {exc}", kind="transient")
            result["status"] = "transient"
        gpu._current_limit_transport = None
        gpu._current_limit_layout_choice = None
    return result


class FanPolicyRuntimeError(BiosRuntimeError):
    pass


_KEY = re.compile(r"(?:fan_stop\.(0|[1-9]\d*)\.(low|high)|fan_curve\.(0|[1-9]\d*)\.([012])\.(temperature|level|rpm))\Z")
_LAYOUT = {
    "group": "fan_policy_v20",
    "capacity": 16,
    "info_command": "0x20808532",
    "control_command": "0x20808534",
    "routing_command": "0x20808547",
    "info_bytes": 336,
    "control_bytes": 1556,
    "routing_bytes": 332,
}
_META = (
    "policy_selector_raw",
    "point2_thermal_relative_flag",
    "flag4_raw",
    "point2_thermal_offset_q8",
    "curve_unit_selector_raw",
    "stop_support_flag_raw",
    "stop_default_enabled_raw",
    "stop_current_enabled_raw",
    "stop_word_44_raw",
    "stop_selector_46_raw",
)


def _fail(message):
    raise FanPolicyRuntimeError(message)


def _same(a, b):
    return fan_runtime._same(a, b)


def _identity(value):
    from .nvbackend import parse_slot

    if not isinstance(value, dict) or set(value) != {"slot", "architecture", "device_id", "subsystem_id"}:
        _fail("Fan policy needs an exact adapter identity")
    slot = parse_slot(value["slot"]) if isinstance(value["slot"], str) else None
    if slot is None or slot[0] != 0 or not 0 <= slot[1] <= 255 or not 0 <= slot[2] <= 31 or not 0 <= slot[3] <= 7:
        _fail("Fan policy has an invalid PCI identity")
    for name, maximum in (("architecture", 0xFFFFFFFF), ("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        if type(value[name]) is not int or not 0 <= value[name] <= maximum:
            _fail("Fan policy has an invalid adapter identity")
    if value["architecture"] not in PROBE_ARCHITECTURES:
        _fail("Fan-policy probing requires a Pascal-or-later architecture")
    return copy.deepcopy(value)


def _bytes(value, size):
    try:
        if not isinstance(value, str) or len(value) != size * 2:
            raise ValueError
        result = bytes.fromhex(value)
        if len(result) != size:
            raise ValueError
        return result
    except (ValueError, TypeError):
        _fail("Fan-policy raw packet metadata has invalid geometry")


def _canonical(observation, *, stable=False):
    """Reconstruct bounded packets; derived display values never decide a write."""
    if not isinstance(observation, Mapping) or not _same(observation.get("layout"), _LAYOUT):
        _fail("Fan-policy observation has an unrecognized layout")
    identity = _identity(observation.get("identity"))
    if not _same(observation.get("slot"), identity["slot"]) or not _same(
        observation.get("architecture"), identity["architecture"]
    ):
        _fail("Fan-policy observation disagrees with its adapter identity")
    if stable and not _same(observation.get("stable_reads"), 2):
        _fail("Fan policy needs two stable fresh reads")
    info, control = bytearray(INFO_BYTES), bytearray(CONTROL_BYTES)
    info[:16] = _bytes(observation.get("info_header_hex"), 16)
    control[:20] = _bytes(observation.get("control_header_hex"), 20)
    if u32(info, 4) >> CAPACITY:
        _fail("Fan-policy inventory exceeds capacity")
    entries = observation.get("entries")
    if not isinstance(entries, list):
        _fail("Fan-policy inventory is unavailable")
    indices = []
    for row in entries:
        if not isinstance(row, Mapping) or type(row.get("index")) is not int or not 0 <= row["index"] < CAPACITY:
            _fail("Fan-policy row identity is invalid")
        i = row["index"]
        indices.append(i)
        info[16 + i * 20 : 36 + i * 20] = _bytes(row.get("info_hex"), 20)
        control[20 + i * 96 : 116 + i * 96] = _bytes(row.get("control_hex"), 96)
    if sorted(indices) != [i for i in range(CAPACITY) if u32(info, 4) & (1 << i)]:
        _fail("Fan-policy rows disagree with inventory")
    routing = None
    if observation.get("routing_available") is True:
        routing = bytearray(ROUTING_BYTES)
        routing[:12] = _bytes(observation.get("routing_header_hex"), 12)
        rows = observation.get("routing_entries")
        if not isinstance(rows, list) or u32(routing, 4) >> CAPACITY:
            _fail("Fan-routing inventory is invalid")
        indices = []
        for row in rows:
            if not isinstance(row, Mapping) or type(row.get("index")) is not int or not 0 <= row["index"] < CAPACITY:
                _fail("Fan-routing row identity is invalid")
            i = row["index"]
            indices.append(i)
            routing[12 + i * 20 : 32 + i * 20] = _bytes(row.get("raw_hex"), 20)
        if sorted(indices) != [i for i in range(CAPACITY) if u32(routing, 4) & (1 << i)]:
            _fail("Fan-routing rows disagree with inventory")
    elif observation.get("routing_available") is not False:
        _fail("Fan-routing availability is invalid")
    try:
        result = decode(info, control, routing)
    except ValueError as exc:
        _fail(str(exc))
    result["identity"] = identity
    return result


def _shape(observation, *, stable=False):
    decoded = _canonical(observation, stable=stable)
    rows, values, readable = [], {}, []
    for row in decoded["entries"]:
        i = row["index"]
        meta = {"index": i, "type": row["type"], "control_type": row["control_type"]}
        if "curve" in row:
            meta.update({key: row[key] for key in _META})
            readable.append(i)
            values[f"fan_stop.{i}.low"] = row["stop_low_q8"]
            values[f"fan_stop.{i}.high"] = row["stop_high_q8"]
            for p in row["curve"]:
                for role, source in (("temperature", "temperature_q8"), ("level", "duty_q16"), ("rpm", "rpm")):
                    values[f"fan_curve.{i}.{p['point']}.{role}"] = p[source]
        rows.append(meta)
    routes = []
    for row in decoded["routing_entries"]:
        meta = {"index": row["index"], "type": row["type"]}
        if row["type"] == 1:
            meta.update(
                {
                    key: row[key]
                    for key in ("route_kind_raw", "cooler_index", "word_04_raw", "policy_mask_a", "policy_mask_b")
                }
            )
        routes.append(meta)
    state = {
        "layout": dict(_LAYOUT),
        "identity": decoded["identity"],
        "group_id": decoded["group_id"],
        "mask": decoded["mask"],
        "policy_layout": decoded["policy_layout"],
        "auxiliary_mask_raw": decoded["auxiliary_mask_raw"],
        "readable_indices": readable,
        "policy_rows": rows,
        "routing_available": decoded["routing_available"],
        "routing_group_id": None
        if not decoded["routing_available"]
        else bytes.fromhex(decoded["routing_header_hex"])[0],
        "routing_mask": None
        if not decoded["routing_available"]
        else u32(bytes.fromhex(decoded["routing_header_hex"]), 4),
        "routing_rows": routes,
    }
    return state, values, decoded


def _cooler_shape(observation):
    try:
        state, bounds = fan_runtime._shape(observation, stable=True)
        pwm = fan_runtime._pwm_shape(observation)
        return {"state": state, "bounds": bounds, "pwm": pwm}
    except BiosRuntimeError as exc:
        _fail("Fan-policy cooler routing is unavailable: " + str(exc))


def read(gpu):
    """Return (fresh observation, error), preserving independently readable policies."""
    try:
        reader = PerfDiagnosticsReader(gpu)
        observations = []
        for _ in range(2):
            attempt = _capture(reader)
            if attempt["status"] != "ready" or attempt["snapshot"] is None:
                detail = next((r["error"] for r in reversed(attempt["attempts"]) if "error" in r), attempt["status"])
                _fail("Fan-policy GET unavailable: " + detail)
            observation = attempt["snapshot"]
            observation["layout"] = dict(_LAYOUT)
            _shape(observation)
            observations.append(observation)
        first, second = observations
        if not _same(_shape(first)[:2], _shape(second)[:2]):
            _fail("Fan-policy metadata changed between reads; retry")
        second["stable_reads"] = 2
        cooler, error = fan_runtime.read(gpu)
        reader._binding()
        second["cooler"] = cooler
        second["cooler_error"] = error
        if cooler is not None and not _same(_cooler_shape(cooler)["state"]["identity"], second["identity"]):
            _fail("Fan-policy and cooler getters identify different adapters")
        return second, None
    except Exception as exc:
        return None, str(exc)


def _selection(changes):
    if not isinstance(changes, Mapping):
        _fail("Fan-policy changes must be a mapping")
    selected = sorted(k for k in changes if isinstance(k, str) and _KEY.fullmatch(k))
    if selected and any(
        isinstance(k, str) and k.startswith(("fan_stop.", "fan_curve.", "fan.")) and k not in selected for k in changes
    ):
        _fail("Fan-policy transactions cannot also change zero-RPM flags or cooler metadata; apply those separately")
    return selected


def _key(key):
    match = _KEY.fullmatch(key)
    if match is None:
        _fail("Unknown fan-policy field")
    return (int(match[1]), None, match[2]) if match[1] is not None else (int(match[3]), int(match[4]), match[5])


def _temperature(value, *, stop=False):
    if type(value) not in (int, float) or not (1 / 32 if stop else 0) <= value <= 110 or not math.isfinite(value):
        _fail("Fan-policy temperature is outside the understood range")
    if not float(value * 32).is_integer():
        _fail("Fan-policy temperature must use steps of 1/32 C")
    return int(value * 256)


def _stored_temperature(value):
    """Decode an unchanged BIOS u16 temperature without imposing edit limits."""
    if type(value) not in (int, float) or not 0 <= value <= 65535 / 32 or not math.isfinite(value):
        _fail("Stored fan-policy temperature is outside the BIOS encoding")
    if not float(value * 32).is_integer():
        _fail("Stored fan-policy temperature must use steps of 1/32 C")
    return int(value * 256)


def _requested(role, value):
    if role not in ("level", "rpm"):
        return _temperature(value, stop=role in ("low", "high"))
    limit = 100 if role == "level" else 32767
    if (
        type(value) not in (int, float)
        or not 0 <= value <= limit
        or not math.isfinite(value)
        or not float(value).is_integer()
    ):
        _fail(f"Fan-policy {role} needs a whole value in 0..{limit}")
    return _bios_value("level_minimum" if role == "level" else "rpm_minimum", value)


def _public_value(role, raw):
    """Invert selected encodings exactly; rounded Q16 is not arbitrary percent."""
    maximum = 65536 if role == "level" else 32767 if role == "rpm" else 110 * 256
    if type(raw) is not int or not 0 <= raw <= maximum:
        _fail("Fan-policy receipt has an invalid raw value")
    value = (raw * 100 + 32768) // 65536 if role == "level" else raw if role == "rpm" else raw / 256
    if _requested(role, value) != raw:
        _fail("Fan-policy receipt value does not match its BIOS encoding")
    return value


def _output_mode(row, role):
    selector = row.get("curve_unit_selector_raw")
    if type(selector) is not int or selector not in (0, 1):
        _fail("Fan-policy output coordinate is unavailable")
    coordinate = "level" if selector else "rpm"
    return {"selector_raw": selector, "coordinate": coordinate, "selected_active": role == coordinate}


def _coordinate_bounds(values, index, role, state, cooler):
    """Validate actual mask-A routes; mask B is not assumed to choose output mode."""
    rows = _indexed(state["policy_rows"], "Receipt policy")
    mode = _output_mode(rows[index], role)
    routes = [
        r
        for r in state["routing_rows"]
        if r.get("type") == 1 and r.get("route_kind_raw") == 1 and r["policy_mask_a"] & (1 << index)
    ]
    if not routes:
        _fail("Selected fan-curve coordinate has no understood output route")
    cooler_rows = cooler["state"].get("row_types")
    bounds = cooler.get("bounds")
    if not isinstance(cooler_rows, list) or not isinstance(bounds, dict):
        _fail("Selected fan-curve coordinate has no cooler bounds")
    expected_mode = 1 if mode["coordinate"] == "level" else 2
    for route in routes:
        # The route constructor uses the first mask-A policy's mode and its
        # validator requires every mask-A policy to agree. An unrelated mask-B
        # reference does not establish an output-mode relationship.
        for peer in range(CAPACITY):
            if not route["policy_mask_a"] & (1 << peer):
                continue
            row = rows.get(peer, {})
            if (
                row.get("type") != 2
                or row.get("control_type") != 2
                or not _same(row.get("curve_unit_selector_raw"), mode["selector_raw"])
            ):
                _fail("Fan-policy output route has an unknown or inconsistent peer mode")
        target = route["cooler_index"]
        matches = [
            r for r in cooler_rows if isinstance(r, list) and len(r) == 4 and type(r[0]) is int and r[0] == target
        ]
        if (
            len(matches) != 1
            or matches[0][1] not in ((2, 3) if expected_mode == 1 else (3,))
            or not _same(matches[0][1], matches[0][2])
            or not _same(matches[0][3], expected_mode)
        ):
            _fail("Fan-policy output mode and routed cooler mode disagree")
        lo = bounds.get(f"fan.{target}.{role}_minimum")
        hi = bounds.get(f"fan.{target}.{role}_maximum")
        if type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi:
            _fail("Selected fan-curve coordinate has unavailable cooler bounds")
        if any(not lo <= values[f"fan_curve.{index}.{p}.{role}"] <= hi for p in range(3)):
            _fail("Fan curve coordinate must remain inside every routed cooler's bounds")
    return mode


def _bios_value(role, value):
    try:
        return fan_runtime._value(role, value)
    except BiosRuntimeError as exc:
        _fail("BIOS fan-policy value is invalid: " + str(exc))


def _peer_keys(indices):
    return {f"fan_stop.{i}.{r}" for i in indices for r in ("low", "high")} | {
        f"fan_curve.{i}.{p}.{r}" for i in indices for p in range(3) for r in ("temperature", "level", "rpm")
    }


def _relationships(values, indices, stop_indices):
    for i in indices:
        if i in stop_indices and not 0 < values[f"fan_stop.{i}.low"] < values[f"fan_stop.{i}.high"] <= 110 * 256:
            _fail("Fan-stop temperatures must remain strictly ordered")
        for p in range(2):
            a, b = f"fan_curve.{i}.{p}.", f"fan_curve.{i}.{p + 1}."
            if (
                values[a + "temperature"] >= values[b + "temperature"]
                or values[a + "level"] > values[b + "level"]
                or values[a + "rpm"] > values[b + "rpm"]
            ):
                _fail("Fan curve temperatures must rise strictly and level/RPM cannot decrease")


def contracts(gpu, inspection, changes):
    selected = _selection(changes)
    if not selected:
        return {}
    observation, error = read(gpu)
    if error or observation is None:
        _fail("Fan-policy metadata is unavailable: " + str(error))
    state, values, decoded = _shape(observation, stable=True)
    cooler = _cooler_shape(observation.get("cooler"))
    if not _same(cooler["state"]["identity"], state["identity"]):
        _fail("Fan-policy and cooler identities disagree")
    binding = inspection.get("fan_policy_binding") if isinstance(inspection, Mapping) else None
    if not isinstance(binding, Mapping) or type(binding.get("version")) is not int or binding["version"] != 32:
        _fail("BIOS fan-policy identity metadata is unavailable")
    if state["policy_layout"] != 2 or not state["routing_available"]:
        _fail("Fan-policy layout or routing is unavailable")
    if not _same(binding.get("client_id_to_index"), cooler["state"]["client_id_to_index"]):
        _fail("BIOS and runtime cooler role maps disagree")
    fields = {}
    for field in inspection.get("fields", []):
        if not isinstance(field, Mapping) or field.get("key") in fields:
            _fail("BIOS inspection fields are invalid or duplicated")
        fields[field.get("key")] = field
    bios_rows = _indexed(binding.get("policies"), "BIOS fan-policy")
    bios_routes = _indexed(binding.get("routes"), "BIOS fan-routing")
    runtime_routes = {r["index"]: r for r in decoded["routing_entries"] if r["type"] == 1}
    if set(bios_routes) != set(runtime_routes):
        _fail("BIOS and runtime fan-routing inventories disagree")
    for i, row in runtime_routes.items():
        wanted = bios_routes[i]
        for left, right in (
            ("route_kind_raw", "kind"),
            ("cooler_index", "cooler_index"),
            ("word_04_raw", "word"),
            ("policy_mask_a", "policy_mask_a"),
            ("policy_mask_b", "policy_mask_b"),
        ):
            if not _same(row[left], wanted.get(right)):
                _fail("BIOS and runtime fan-policy routing disagree")
    rows = {r["index"]: r for r in decoded["entries"]}
    after = dict(values)
    for key in selected:
        i, point, role = _key(key)
        row, bios = rows.get(i), bios_rows.get(i)
        if row is None or bios is None or row["issues"] or "curve" not in row:
            _fail(f"{key}: selected policy has no understood runtime values")
        if point is None and row["stop_issues"]:
            _fail(f"{key}: selected stop thresholds are outside the understood edit contract")
        flags = bios.get("flags")
        if type(flags) is not int or not 0 <= flags <= 255:
            _fail("BIOS fan-policy flags are invalid")
        expected = {
            "policy_selector_raw": bios.get("selector_raw"),
            "point2_thermal_relative_flag": (flags >> 3) & 1,
            "flag4_raw": (flags >> 4) & 1,
            "point2_thermal_offset_q8": bios.get("thermal_offset_q8"),
            "curve_unit_selector_raw": int(not flags & 1),
            "stop_support_flag_raw": (flags >> 1) & 1,
            "stop_default_enabled_raw": (flags >> 2) & 1,
            "stop_current_enabled_raw": (flags >> 2) & 1,
            "stop_word_44_raw": bios.get("stop_word_44"),
            "stop_selector_46_raw": bios.get("stop_selector_46"),
        }
        if any(not _same(row[name], value) for name, value in expected.items()):
            _fail(f"{key}: BIOS and runtime policy identity or mode differ")
        if point is None and not (row["stop_support_flag_raw"] == row["stop_current_enabled_raw"] == 1):
            _fail(f"{key}: stop temperatures need an already supported and enabled policy")
        if point == 2 and role == "temperature" and row["point2_thermal_relative_flag"]:
            _fail(f"{key}: point 2 follows a thermal-table value and offset; its stored temperature is ignored")
        if not row["routes"]:
            _fail(f"{key}: policy has no understood cooler route")
        for route in row["routes"]:
            target = route["cooler_index"]
            if target not in cooler["state"]["client_id_to_index"].values():
                _fail(f"{key}: policy routes to an unmapped cooler")
            if not any(r[0] == target and r[1] in (2, 3) for r in cooler["state"]["row_types"]):
                _fail(f"{key}: policy routes to an unknown cooler class")
        for peer in _peer_keys([i]):
            field = fields.get(peer)
            expected_unit = (
                "C" if peer.endswith((".temperature", ".low", ".high")) else "%" if peer.endswith(".level") else "RPM"
            )
            if not isinstance(field, Mapping) or field.get("units") != expected_unit:
                _fail(f"{key}: BIOS policy values or units are unavailable")
            if expected_unit == "C":
                wanted = _stored_temperature(field.get("current"))
            elif expected_unit == "%":
                wanted = _bios_value("level_minimum", field.get("current"))
            else:
                wanted = _bios_value("rpm_minimum", field.get("current"))
            # A thermal-relative point is preserved from the driver; the ROM
            # base value is not its active temperature and is never asserted.
            if peer == f"fan_curve.{i}.2.temperature" and row["point2_thermal_relative_flag"]:
                continue
            if wanted != values[peer]:
                _fail(f"{key}: driver policy metadata differs from the decoded BIOS value")
        after[key] = _requested(role, changes[key])
    _relationships(
        after,
        {_key(key)[0] for key in selected},
        {_key(key)[0] for key in selected if key.startswith("fan_stop.")},
    )
    unchanged = {"state": state, "values": {k: v for k, v in values.items() if k not in selected}, "cooler": cooler}
    result = {}
    for key in selected:
        i, point, role = _key(key)
        coordinate = role in ("level", "rpm")
        result[key] = {
            "schema": 2 if coordinate else 1,
            "kind": "fan_policy_curve_value" if coordinate else "fan_policy_temperature",
            "index": i,
            "point": point,
            "role": role,
            "unit": "q16_fraction" if role == "level" else "RPM" if role == "rpm" else "q8_c",
            "before": values[key],
            "after": after[key],
            "selected_fields": selected.copy(),
            "unchanged": copy.deepcopy(unchanged),
        }
        if coordinate:
            _coordinate_bounds(values, i, role, state, cooler)
            result[key]["output_mode"] = _coordinate_bounds(after, i, role, state, cooler)
    validate_contracts(changes, result)
    return result


def _indexed(rows, name):
    if not isinstance(rows, list):
        _fail(f"{name} rows are unavailable")
    result = {}
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or type(row.get("index")) is not int
            or not 0 <= row["index"] < CAPACITY
            or row["index"] in result
        ):
            _fail(f"{name} contains invalid or duplicate indices")
        result[row["index"]] = row
    return result


def validate_contracts(changes, saved):
    """Validate durable receipt shape and its exact selected values without GPU access."""
    selected = _selection(changes)
    if not isinstance(saved, Mapping) or set(saved) != set(selected):
        _fail("Fan-policy receipt does not match the selected fields")
    expected_keys = {
        "schema",
        "kind",
        "index",
        "point",
        "role",
        "unit",
        "before",
        "after",
        "selected_fields",
        "unchanged",
    }
    unchanged = None
    for key in selected:
        receipt = saved[key]
        i, point, role = _key(key)
        coordinate = role in ("level", "rpm")
        keys = expected_keys | {"output_mode"} if coordinate else expected_keys
        if not isinstance(receipt, Mapping) or set(receipt) != keys:
            _fail("Malformed fan-policy receipt")
        expected = {
            "schema": 2 if coordinate else 1,
            "kind": "fan_policy_curve_value" if coordinate else "fan_policy_temperature",
            "index": i,
            "point": point,
            "role": role,
            "unit": "q16_fraction" if role == "level" else "RPM" if role == "rpm" else "q8_c",
            "selected_fields": selected,
            "after": _requested(role, changes[key]),
        }
        if any(not _same(receipt[k], v) for k, v in expected.items()):
            _fail("Fan-policy receipt identity or requested value differs")
        _public_value(role, receipt["before"])
        peers = receipt["unchanged"]
        if (
            not isinstance(peers, dict)
            or set(peers) != {"state", "values", "cooler"}
            or not isinstance(peers["values"], dict)
        ):
            _fail("Fan-policy receipt omits its unchanged peers")
        if unchanged is None:
            unchanged = peers
        elif not _same(unchanged, peers):
            _fail("Fan-policy receipts disagree about their peers")
    if not selected:
        return []
    state = unchanged["state"]
    required = {
        "layout",
        "identity",
        "group_id",
        "mask",
        "policy_layout",
        "auxiliary_mask_raw",
        "readable_indices",
        "policy_rows",
        "routing_available",
        "routing_group_id",
        "routing_mask",
        "routing_rows",
    }
    if not isinstance(state, dict) or set(state) != required or not _same(state["layout"], _LAYOUT):
        _fail("Fan-policy receipt has invalid state identity")
    _identity(state["identity"])
    if (
        type(state["mask"]) is not int
        or not 0 < state["mask"] < (1 << CAPACITY)
        or not _same(state["policy_layout"], 2)
        or state["routing_available"] is not True
    ):
        _fail("Fan-policy receipt has an unsupported policy inventory")
    rows = _indexed(state["policy_rows"], "Receipt policy")
    if set(rows) != {i for i in range(CAPACITY) if state["mask"] & (1 << i)}:
        _fail("Fan-policy receipt rows do not match its mask")
    readable = state["readable_indices"]
    if (
        not isinstance(readable, list)
        or any(type(i) is not int for i in readable)
        or readable != sorted({i for i, r in rows.items() if set(r) == {"index", "type", "control_type", *_META}})
    ):
        _fail("Fan-policy receipt readable rows are invalid")
    if set(unchanged["values"]) != _peer_keys(readable) - set(selected):
        _fail("Fan-policy receipt omits understood peer values")
    if any(type(v) is not int or not -(1 << 31) <= v < (1 << 32) for v in unchanged["values"].values()):
        _fail("Fan-policy receipt peer encodings are invalid")
    for key in selected:
        i, point, role = _key(key)
        row = rows.get(i)
        if i not in readable or row["type"] != 2 or row["control_type"] != 2:
            _fail("Fan-policy receipt selects an unknown policy class")
        if point is None and any(
            not _same(row[k], 1)
            for k in ("stop_support_flag_raw", "stop_default_enabled_raw", "stop_current_enabled_raw")
        ):
            _fail("Fan-policy receipt selects an inactive stop policy")
        if point == 2 and role == "temperature" and not _same(row["point2_thermal_relative_flag"], 0):
            _fail("Fan-policy receipt selects a replaced point-2 temperature")
    routes = _indexed(state["routing_rows"], "Receipt route")
    if (
        type(state["routing_mask"]) is not int
        or not 0 < state["routing_mask"] < (1 << CAPACITY)
        or set(routes) != {i for i in range(CAPACITY) if state["routing_mask"] & (1 << i)}
    ):
        _fail("Fan-policy receipt route inventory is invalid")
    for name in ("group_id", "routing_group_id"):
        if type(state[name]) is not int or not 0 <= state[name] <= 255:
            _fail("Fan-policy receipt group identity is invalid")
    if type(state["auxiliary_mask_raw"]) is not int or not 0 <= state["auxiliary_mask_raw"] <= 0xFFFFFFFF:
        _fail("Fan-policy receipt auxiliary mask is invalid")
    for row in rows.values():
        if any(type(row.get(k)) is not int or not 0 <= row[k] <= 255 for k in ("type", "control_type")):
            _fail("Fan-policy receipt row class is invalid")
        if row["index"] not in readable:
            if set(row) != {"index", "type", "control_type"}:
                _fail("Fan-policy receipt unknown row contains unproved metadata")
            continue
        limits = {
            "policy_selector_raw": (0, 255),
            "point2_thermal_offset_q8": (-32768 * 8, 32767 * 8),
            "stop_word_44_raw": (0, 65535),
            "stop_selector_46_raw": (0, 255),
        }
        for name in _META:
            minimum, maximum = limits.get(name, (0, 1))
            if type(row[name]) is not int or not minimum <= row[name] <= maximum:
                _fail("Fan-policy receipt metadata encoding is invalid")
    for route in routes.values():
        if type(route.get("type")) is not int or not 0 <= route["type"] <= 255:
            _fail("Fan-policy receipt route class is invalid")
        if route["type"] != 1:
            if set(route) != {"index", "type"}:
                _fail("Fan-policy receipt unknown route contains unproved metadata")
            continue
        if set(route) != {
            "index",
            "type",
            "route_kind_raw",
            "cooler_index",
            "word_04_raw",
            "policy_mask_a",
            "policy_mask_b",
        }:
            _fail("Fan-policy receipt route metadata is incomplete")
        for name, maximum in (
            ("route_kind_raw", 255),
            ("cooler_index", 15),
            ("word_04_raw", 65535),
            ("policy_mask_a", (1 << CAPACITY) - 1),
            ("policy_mask_b", (1 << CAPACITY) - 1),
        ):
            if type(route[name]) is not int or not 0 <= route[name] <= maximum:
                _fail("Fan-policy receipt route encoding is invalid")
        if route["policy_mask_a"] & ~state["mask"] or route["policy_mask_b"] & ~state["mask"]:
            _fail("Fan-policy receipt route references absent policies")
    for key in selected:
        index = _key(key)[0]
        if not any(
            route["type"] == 1
            and route["route_kind_raw"] == 1
            and (route["policy_mask_a"] | route["policy_mask_b"]) & (1 << index)
            for route in routes.values()
        ):
            _fail("Fan-policy receipt selects an unrouted policy")
    if not isinstance(unchanged["cooler"], dict) or set(unchanged["cooler"]) != {"state", "bounds", "pwm"}:
        _fail("Fan-policy receipt omits cooler metadata")
    cooler_state = unchanged["cooler"].get("state")
    if not isinstance(cooler_state, dict) or not _same(cooler_state.get("identity"), state["identity"]):
        _fail("Fan-policy receipt cooler identity differs")
    for when in ("before", "after"):
        values = {**unchanged["values"], **{key: saved[key][when] for key in selected}}
        _relationships(
            values,
            {_key(key)[0] for key in selected},
            {_key(key)[0] for key in selected if key.startswith("fan_stop.")},
        )
        for key in selected:
            i, _, role = _key(key)
            if role in ("level", "rpm"):
                mode = _coordinate_bounds(values, i, role, state, unchanged["cooler"])
                if not _same(saved[key]["output_mode"], mode):
                    _fail("Fan-policy receipt output-coordinate binding differs")
    return selected


def verify(snapshot_or_observation, saved, *, restore=False):
    """Verify selected values and all captured known peers, including undo."""
    if not isinstance(saved, Mapping):
        _fail("Fan-policy contracts must be a mapping")
    if not saved:
        return []
    changes = {}
    for key, receipt in saved.items():
        if (
            not isinstance(key, str)
            or _KEY.fullmatch(key) is None
            or not isinstance(receipt, Mapping)
            or type(receipt.get("after")) is not int
        ):
            _fail("Malformed fan-policy receipt")
        changes[key] = _public_value(_key(key)[2], receipt["after"])
    selected = validate_contracts(changes, saved)
    observation = snapshot_or_observation
    if isinstance(observation, Mapping):
        if "fan_policies" in observation:
            observation = observation["fan_policies"]
        elif "fan_policy" in observation:
            observation = observation["fan_policy"]
    state, values, _ = _shape(observation, stable=True)
    cooler = _cooler_shape(observation.get("cooler"))
    unchanged = saved[selected[0]]["unchanged"]
    if not _same(state, unchanged["state"]):
        _fail("Fan-policy identity, flags, inventory or routing changed")
    if not _same(cooler, unchanged["cooler"]):
        _fail("Fan-policy referenced cooler metadata changed")
    if not _same({k: v for k, v in values.items() if k not in selected}, unchanged["values"]):
        _fail("Fan-policy untouched peer metadata changed")
    for key in selected:
        if not _same(values.get(key), saved[key]["before" if restore else "after"]):
            label = "temperature" if _key(key)[2] not in ("level", "rpm") else "curve coordinate"
            _fail(f"{key}: runtime {label} does not match {'restored' if restore else 'requested'} value")
    return selected
