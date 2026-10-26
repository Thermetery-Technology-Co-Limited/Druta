# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and compose the registry gate exposing non-P0 OC editability.

There are no clock/voltage setters, registry writes or restarts here. The
transaction owner supplies exact registry undo and fresh-process activation
readback. Reported editability does not establish that a state-specific clock
setter changes hardware. Evidence: driver-controls/all-pstates-mechanism-20261023.json.
"""

from __future__ import annotations

import copy
import ctypes
import struct
from collections.abc import Mapping
from contextlib import nullcontext

from .perf_diagnostics import PROBE_ARCHITECTURES, PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

KEY = "pstates.all_editable"
REGISTRY_NAME = "RMEnableOverclockingAllPstates"
_PUBLIC = {0x11C94: 7316, 0x21CF8: 7416, 0x31CF8: 7416}
_EXTENDED_VERSION, _EXTENDED_SIZE = 0x23D98, 0x3D98
# Public GET chooses this map version before projecting native clock domains
# (nvapi64_impl 1D6AAC..1D6ACF). Map V1 explicitly suppresses public domain8.
_MAP_VERSIONS = {0x11C94: 0x10188, 0x21CF8: 0x10188, 0x31CF8: 0x20188}
_MAP_SIZE = 0x188


def _fail(message):
    from .registry_overrides import OverrideError

    raise OverrideError(message)


def _u32(raw, offset):
    return struct.unpack_from("<I", raw, offset)[0]


def _delta(raw, offset):
    value, minimum, maximum = struct.unpack_from("<3i", raw, offset)
    return {"value": value, "minimum": minimum, "maximum": maximum}


def _decode_table(raw, *, extended):
    version = _u32(raw, 0) if len(raw) >= 20 else 0
    size = (
        _EXTENDED_SIZE if extended and version == _EXTENDED_VERSION else _PUBLIC.get(version) if not extended else None
    )
    if size is None or len(raw) != size:
        raise ValueError("Pstates20 packet version/size is not understood")
    states, clocks, voltages = struct.unpack_from("<3I", raw, 8)
    clock_cap, voltage_cap = (16, 8) if extended else (8, 4)
    if not 1 <= states <= 16 or not 1 <= clocks <= clock_cap or voltages > voltage_cap:
        raise ValueError("Pstates20 counts exceed packet geometry")
    stride, voltage_offset, voltage_stride = (968, 712, 32) if extended else (456, 360, 24)
    rows, seen = [], set()
    for index in range(states):
        pos = 20 + index * stride
        state, flags = struct.unpack_from("<2I", raw, pos)
        if not 0 <= state <= 15 or state in seen:
            raise ValueError("Pstates20 has duplicate/invalid state IDs")
        seen.add(state)
        clock_rows, domains = [], set()
        for c in range(clocks):
            cp = pos + 8 + c * 44
            domain, kind, cf = struct.unpack_from("<3I", raw, cp)
            if not 0 <= domain < 32 or domain in domains or kind not in ((0, 1, 2, 3) if extended else (0, 1)):
                raise ValueError("Pstates20 has duplicate/invalid clock domains or kinds")
            domains.add(domain)
            clock_rows.append(
                {
                    "domain": domain,
                    "kind": kind,
                    "flags": cf,
                    "delta": _delta(raw, cp + 12),
                    "data_raw": list(struct.unpack_from("<5I", raw, cp + 24)),
                    "offset": cp,
                }
            )
        voltage_rows, domains = [], set()
        for v in range(voltages):
            vp = pos + voltage_offset + v * voltage_stride
            domain = _u32(raw, vp)
            if domain in domains or not 0 <= domain < 16:
                raise ValueError("Pstates20 has duplicate/invalid voltage domains")
            domains.add(domain)
            voltage_rows.append(
                {
                    "domain": domain,
                    "flags": _u32(raw, vp + (8 if extended else 4)),
                    "voltage_uv": _u32(raw, vp + (12 if extended else 8)),
                    "delta": _delta(raw, vp + (16 if extended else 12)),
                    "raw_hex": raw[vp : vp + voltage_stride].hex(),
                    "offset": vp,
                }
            )
        rows.append({"pstate": state, "flags": flags, "clocks": clock_rows, "voltages": voltage_rows, "offset": pos})
    ov_offset = 0x3C94 if extended else 0x1C94
    count = _u32(raw, ov_offset) if len(raw) > ov_offset else 0
    if count > voltage_cap:
        raise ValueError("Pstates20 overvoltage count exceeds geometry")
    return {
        "version": version,
        "bytes": size,
        "flags": _u32(raw, 4),
        "num_pstates": states,
        "num_clocks": clocks,
        "num_voltages": voltages,
        "pstates": rows,
        "overvoltage_count": count,
    }


def _decode_map(raw, public_version):
    version = _MAP_VERSIONS.get(public_version)
    if version is None or len(raw) != _MAP_SIZE or _u32(raw, 0) != version:
        raise ValueError("Pstates20 domain-map version/size differs from its public layout")
    rows = []
    for public_domain in range(32):
        native, flags, scale = struct.unpack_from("<3I", raw, 8 + public_domain * 12)
        if native > 32 or not 1 <= scale <= 0x7FFFFFFF:
            raise ValueError("Pstates20 domain mapping or scale is invalid")
        if version == 0x10188 and public_domain == 8 and (native, flags, scale) != (32, 0, 100):
            raise ValueError("Pstates20 V1 domain map did not suppress public domain8")
        rows.append({"public_domain": public_domain, "native_domain": native, "flags": flags, "scale": scale})
    return rows


def _raw(observation, key):
    value = observation.get(key)
    if not isinstance(value, str) or len(value) % 2 or len(value) > 2 * _EXTENDED_SIZE:
        raise ValueError("Missing/invalid Pstates20 raw payload")
    result = bytes.fromhex(value)
    if result.hex() != value:
        raise ValueError("Pstates20 raw payload is not canonical hex")
    return result


def _identity(observation):
    identity = observation.get("identity")
    if not isinstance(identity, dict) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        raise ValueError("Pstates20 needs an exact paired adapter identity")
    from .nvbackend import parse_slot

    address = parse_slot(identity["slot"]) if isinstance(identity["slot"], str) else None
    if address is None or address[0] != 0 or address[1] > 255 or address[2] > 31 or address[3] > 7:
        raise ValueError("Pstates20 adapter PCI address is invalid")
    for key, maximum in (("architecture", 0xFFFFFFFF), ("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        if type(identity[key]) is not int or not 0 <= identity[key] <= maximum:
            raise ValueError("Pstates20 adapter identity is invalid")
    if identity["architecture"] not in PROBE_ARCHITECTURES:
        raise ValueError("The understood all-state editability path requires Pascal or later")
    return copy.deepcopy(identity)


def _scaled(value, scale):
    product = value * scale
    if not -(1 << 31) <= product < 1 << 31:
        raise ValueError("Pstates20 scale would overflow the traced signed conversion")
    return (abs(product) // 100) * (-1 if product < 0 else 1)


def _tables(observation):
    identity = _identity(observation)
    raw = {key: _raw(observation, key) for key in ("public_hex", "extended_hex", "map_hex")}
    public = _decode_table(raw["public_hex"], extended=False)
    native = _decode_table(raw["extended_hex"], extended=True)
    mapping = _decode_map(raw["map_hex"], public["version"])
    if [s["pstate"] for s in public["pstates"]] != [s["pstate"] for s in native["pstates"]]:
        raise ValueError("Public and native Pstate inventories disagree")
    for ps, ns in zip(public["pstates"], native["pstates"], strict=True):
        if ps["flags"] != ns["flags"] or ps["flags"] not in (0, 1):
            raise ValueError("Public/native Pstate flags disagree")
        by_domain = {c["domain"]: c for c in ns["clocks"]}
        if any(c["flags"] not in (0, 1) for c in ns["clocks"]):
            raise ValueError("Native clock flags are not the understood Boolean encoding")
        expected_domains = [m["public_domain"] for m in mapping if m["native_domain"] != 32]
        if [c["domain"] for c in ps["clocks"]] != expected_domains:
            raise ValueError("Public clock inventory disagrees with domain mapping")
        for pc in ps["clocks"]:
            m = mapping[pc["domain"]]
            nc = by_domain.get(m["native_domain"])
            if nc is None:
                raise ValueError("Public clock has no mapped native row")
            editable = nc["flags"] & m["flags"] & 1
            triple = {k: _scaled(v, m["scale"]) if editable else 0 for k, v in nc["delta"].items()}
            if pc["flags"] != editable or pc["delta"] != triple or pc["kind"] != int(nc["kind"] >= 2):
                raise ValueError("Public clock flags/kind/delta differ from native projection")
        # The voltage path uses the same P0-or-PERF+2F1 gate. Domain IDs are
        # unchanged by native-to-public conversion; unsupported sentinel16 is
        # outside active voltage inventory. Preserve all payload bytes.
        nv = {v["domain"]: v for v in ns["voltages"]}
        if any(v["flags"] not in (0, 1) for v in nv.values()):
            raise ValueError("Native voltage flags are not Boolean")
        for pv in ps["voltages"]:
            v = nv.get(pv["domain"])
            if v is None or pv["flags"] != v["flags"]:
                raise ValueError("Public and native voltage inventories/flags disagree")
            expected_delta = {k: n if v["flags"] else 0 for k, n in v["delta"].items()}
            if pv["voltage_uv"] != v["voltage_uv"] or pv["delta"] != expected_delta:
                raise ValueError("Public voltage data differs from native projection")
    return identity, raw, public, native, mapping


def _editability(native):
    p0 = next((s for s in native["pstates"] if s["pstate"] == 0), None)
    if p0 is None:
        raise ValueError("No P0 baseline establishes the editable domain masks")
    clock_domains = {c["domain"] for c in p0["clocks"] if c["flags"] & 1}
    voltage_domains = {v["domain"] for v in p0["voltages"] if v["flags"] & 1}
    seen, targets = set(), []
    for state in native["pstates"]:
        if state["pstate"] == 0:
            continue
        for group, domains in (("clocks", clock_domains), ("voltages", voltage_domains)):
            found = {r["domain"] for r in state[group]}
            if not domains <= found:
                raise ValueError("Non-P0 inventory lacks a P0 editable domain")
            for row in state[group]:
                flag = row["flags"]
                if row["domain"] in domains:
                    seen.add(bool(flag & 1))
                    targets.append({"pstate": state["pstate"], "group": group, "domain": row["domain"]})
                elif flag & 1:
                    raise ValueError("Non-P0 editable domain has no matching P0 capability")
        if state["flags"] != int(any(r["flags"] & 1 for g in ("clocks", "voltages") for r in state[g])):
            raise ValueError("Non-P0 aggregate editability disagrees with its domains")
    if not targets or len(seen) != 1:
        raise ValueError("Non-P0 editability is empty or mixed; the registry gate cannot be inferred")
    return seen.pop(), targets


def _shape(observation, *, writable=False):
    if not isinstance(observation, Mapping):
        _fail("All-state editability observation is unavailable")
    try:
        identity, raw, public, native, mapping = _tables(observation)
        enabled, targets = _editability(native)
        if "enabled" in observation and observation["enabled"] is not enabled:
            raise ValueError("Derived editability disagrees with raw Pstates20 flags")
        if writable and (type(observation.get("stable_reads")) is not int or observation["stable_reads"] != 2):
            raise ValueError("Two stable fresh Pstates20 reads are required")
        return identity, raw, public, native, mapping, enabled, targets
    except (ValueError, KeyError, TypeError, struct.error) as exc:
        _fail(str(exc))


def _expected(observation, enabled):
    identity, raw, public, native, mapping, _, targets = _shape(observation, writable=True)
    extended, projected = bytearray(raw["extended_hex"]), bytearray(raw["public_hex"])
    target_set = {(t["pstate"], t["group"], t["domain"]) for t in targets}
    for state in native["pstates"]:
        if state["pstate"] == 0:
            continue
        for group in ("clocks", "voltages"):
            for row in state[group]:
                if (state["pstate"], group, row["domain"]) in target_set:
                    struct.pack_into("<I", extended, row["offset"] + 8, int(enabled))
        struct.pack_into("<I", extended, state["offset"] + 4, int(enabled))
    changed = _decode_table(bytes(extended), extended=True)
    for ps, ns in zip(public["pstates"], changed["pstates"], strict=True):
        if ps["pstate"] == 0:
            continue
        struct.pack_into("<I", projected, ps["offset"] + 4, ns["flags"])
        native_clocks = {r["domain"]: r for r in ns["clocks"]}
        for clock in ps["clocks"]:
            m = mapping[clock["domain"]]
            row = native_clocks[m["native_domain"]]
            flag = row["flags"] & m["flags"] & 1
            struct.pack_into("<I", projected, clock["offset"] + 8, flag)
            struct.pack_into(
                "<3i",
                projected,
                clock["offset"] + 12,
                *(_scaled(row["delta"][k], m["scale"]) if flag else 0 for k in ("value", "minimum", "maximum")),
            )
        native_voltages = {r["domain"]: r for r in ns["voltages"]}
        for voltage in ps["voltages"]:
            row = native_voltages[voltage["domain"]]
            flag = row["flags"]
            struct.pack_into("<I", projected, voltage["offset"] + 4, flag)
            struct.pack_into("<I", projected, voltage["offset"] + 8, row["voltage_uv"])
            struct.pack_into(
                "<3i",
                projected,
                voltage["offset"] + 12,
                *(row["delta"][k] if flag else 0 for k in ("value", "minimum", "maximum")),
            )
    return {
        "identity": identity,
        "public_hex": bytes(projected).hex(),
        "extended_hex": bytes(extended).hex(),
        "map_hex": raw["map_hex"].hex(),
    }


def _observe(identity, public, extended, mapping):
    observation = {
        "identity": identity,
        "public_hex": public.hex(),
        "extended_hex": extended.hex(),
        "map_hex": mapping.hex(),
    }
    _, _, pub, native, domain_map = _tables(observation)
    observation.update(
        {"public": pub, "extended": native, "domain_map": domain_map, "enabled": None, "targets": [], "issues": []}
    )
    try:
        observation["enabled"], observation["targets"] = _editability(native)
    except ValueError as exc:
        observation["issues"].append(str(exc))
    return observation


def _binding(reader):
    api, slot, architecture = reader._binding()
    return api, {
        "slot": slot,
        "architecture": architecture,
        "device_id": api.selected.get("devid"),
        "subsystem_id": api.selected.get("subsys"),
    }


def _get(api, query_id, version, size):
    getter = api._i(query_id, ctypes.c_void_p, ctypes.c_void_p)
    if not getter:
        raise PerfDiagnosticError("unsupported", f"Pstates20 GET export {query_id:08X} is unavailable")
    packet = ctypes.create_string_buffer(size + 64)
    struct.pack_into("<I", packet, 0, version)
    ctypes.memset(ctypes.addressof(packet) + size, 0xA5, 64)
    code = getter(api.gpu, ctypes.byref(packet))
    if code:
        kind = "unsupported" if code in (-3, -9, -104, -175) else "transient"
        raise PerfDiagnosticError(kind, f"Pstates20 GET {query_id:08X} returned {code}", code)
    if packet.raw[size:] != bytes([0xA5]) * 64:
        raise PerfDiagnosticError("malformed", "Pstates20 GET wrote outside its declared layout")
    return packet.raw[:size]


def _once(gpu, reader):
    api, identity = _binding(reader)
    public, error = gpu._read_pstates20()
    if public is None:
        raise PerfDiagnosticError("transient", error or "Public Pstates20 GET failed")
    try:
        public = bytes(public)
        public_version = _decode_table(public, extended=False)["version"]
    except (ValueError, TypeError, struct.error) as exc:
        raise PerfDiagnosticError("malformed", str(exc)) from exc
    mapping = _get(api, 0x1B46D4CC, _MAP_VERSIONS[public_version], _MAP_SIZE)
    extended = _get(api, 0xC5DDF56E, _EXTENDED_VERSION, _EXTENDED_SIZE)
    _, after = _binding(reader)
    if identity != after:
        raise PerfDiagnosticError("identity", "Paired adapter identity changed during Pstates20 GET")
    try:
        return _observe(identity, public, extended, mapping)
    except (ValueError, struct.error) as exc:
        raise PerfDiagnosticError("malformed", str(exc)) from exc


def read(gpu):
    """Fresh paired public/native/map GETs, independent bounded transient retry.

    Readable but mixed/empty editability remains diagnostic with enabled=None.
    No historical observation can satisfy an activation contract.
    """
    error = "Pstates20 read failed"
    for _ in range(3):
        try:
            reader = PerfDiagnosticsReader(gpu)
            with getattr(gpu, "_lock", nullcontext()):
                first, second = _once(gpu, reader), _once(gpu, reader)
            keys = ("identity", "public_hex", "extended_hex", "map_hex")
            if not all(_same_state(first[k], second[k]) for k in keys):
                raise PerfDiagnosticError("transient", "Pstates20 metadata changed between reads; retry")
            second["stable_reads"] = 2
            return second, None
        except PerfDiagnosticError as exc:
            error = str(exc)
            if exc.kind != "transient":
                break
        except Exception as exc:
            error = str(exc)
    return None, error


def _changes(changes, *, empty=False):
    if (
        not isinstance(changes, Mapping)
        or (not changes and not empty)
        or any(k != KEY or type(v) is not bool for k, v in changes.items())
    ):
        _fail("Only a Boolean all-state OC editability change is supported")
    return dict(changes)


def _entry(registry):
    if not isinstance(registry, Mapping):
        _fail("Registry snapshot must be a mapping")
    names = {}
    for name in registry:
        if not isinstance(name, str) or name.casefold() in names:
            _fail("Registry snapshot has invalid/duplicate names")
        names[name.casefold()] = name
    name = names.get(REGISTRY_NAME.casefold(), REGISTRY_NAME)
    entry = registry.get(name)
    if name in registry and (
        not isinstance(entry, Mapping)
        or set(entry) != {"type", "value"}
        or type(entry["type"]) is not int
        or entry["type"] != 4
        or type(entry["value"]) is not int
        or not 0 <= entry["value"] <= 0xFFFFFFFF
    ):
        _fail(REGISTRY_NAME + ": existing entry is not an understood DWORD; preserve and reconcile it")
    return name, entry


def validate_existing(before_registry, observation):
    _, entry = _entry(before_registry)
    if entry is None or entry["value"] == 0:
        return
    enabled = _shape(observation)[5]
    if enabled is not bool(entry["value"]):
        _fail(REGISTRY_NAME + ": registry and active editability disagree; reconcile pending settings")


def compose(before_registry, changes, observation):
    changes = _changes(changes)
    enabled = _shape(observation, writable=True)[5]
    validate_existing(before_registry, observation)
    name, entry = _entry(before_registry)
    result = copy.deepcopy(dict(before_registry))
    if changes[KEY] is enabled:
        return result
    if changes[KEY]:
        result[name] = {"type": 4, "value": 1}
    elif entry is None or entry["value"] == 0:
        _fail("Editability is already enabled by the driver default; removing an override cannot disable it")
    else:
        # The initializer only forces true. False restores the driver default;
        # a default that stays true must fail readback and trigger exact undo.
        result.pop(name)
    return result


def contracts(changes, observation):
    changes = _changes(changes, empty=True)
    if not changes:
        return {}
    *_, before, _ = _shape(observation, writable=True)
    if changes[KEY] is before:
        return {}
    before_state = _expected(observation, before)
    # This roundtrip also rejects incoherent public hidden values/projections.
    if any(before_state[k] != observation[k] for k in before_state):
        _fail("Pstates20 baseline is not an exact projection of native state")
    return {
        KEY: {
            "schema": 1,
            "registry_name": REGISTRY_NAME,
            "before": before,
            "after": changes[KEY],
            "before_state": before_state,
            "after_state": _expected(observation, changes[KEY]),
        }
    }


def verify(observation, saved_contracts, *, restore=False):
    if not isinstance(saved_contracts, Mapping) or any(k != KEY for k in saved_contracts):
        _fail("Unrecognized all-state editability contract")
    if not saved_contracts:
        return []
    contract = saved_contracts[KEY]
    expected_keys = {"schema", "registry_name", "before", "after", "before_state", "after_state"}
    if (
        not isinstance(contract, Mapping)
        or set(contract) != expected_keys
        or type(contract["schema"]) is not int
        or contract["schema"] != 1
        or contract["registry_name"] != REGISTRY_NAME
        or type(contract["before"]) is not bool
        or type(contract["after"]) is not bool
    ):
        _fail("Malformed all-state editability contract")
    for phase in ("before", "after"):
        state = contract[phase + "_state"]
        if type(state) is not dict or set(state) != {"identity", "public_hex", "extended_hex", "map_hex"}:
            _fail("Malformed saved Pstates20 state")
        restored = {**state, "stable_reads": 2, "enabled": contract[phase]}
        _shape(restored, writable=True)
    source = {**contract["before_state"], "stable_reads": 2, "enabled": contract["before"]}
    if not _same_state(_expected(source, contract["after"]), contract["after_state"]):
        _fail("All-state contract contains unrelated metadata changes")
    identity, raw, _, _, _, enabled, _ = _shape(observation, writable=True)
    state = {"identity": identity, **{k: v.hex() for k, v in raw.items()}}
    phase = "before" if restore else "after"
    if enabled is not contract[phase] or not _same_state(state, contract[phase + "_state"]):
        _fail("All-state editability or preserved adapter/state metadata differs from expected readback")
    return [KEY]
