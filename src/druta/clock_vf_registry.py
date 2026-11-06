# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read, compose and verify named clock VF enforcement registry policies.

No registry write, clock/curve/voltage SET or device restart occurs here.
Disabling either policy also changes a derived internal driver VF mode. The
readback establishes the active host policy; physical firmware/evaluator effect
is unproven. See experiments/registry-overrides/speedo-iddq for independent
dispatch, selector, initialization and root-owned activation/undo evidence.
"""

from __future__ import annotations

import copy
import ctypes
from collections.abc import Mapping

from .perf_diagnostics import PROBE_ARCHITECTURES, PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

REGISTRY_NAME = "RMClkVfOverride"
MONOTONICITY_KEY = "clock_vf.monotonicity"
SMOOTHENING_KEY = "clock_vf.smoothening"
_EDITABLE = {
    MONOTONICITY_KEY: ("vf_monotonicity_enforced", 0xA, 0x3, 0),
    SMOOTHENING_KEY: ("vf_smoothening_enforced", 0xB, 0xC, 2),
}
_COMMAND, _SIZE, _HEADER_SIZE = 0x2080901B, 0x83C, 0x3C
_SENTINEL, _GUARD_SIZE = 0xA5, 64
_ENVELOPE = {0: 0x4E564441, 1: 0x10002, 3: 0x4E562A2A, 4: 0x0500002B}
_LAYOUT = {"group": "clock_domains", "control_command": "0x2080901B", "control_bytes": _SIZE, "request_mask": 0}


def _fail(message):
    from .registry_overrides import OverrideError

    raise OverrideError(message)


def _changes(changes, *, allow_empty=False):
    if not isinstance(changes, Mapping) or (not changes and not allow_empty):
        _fail("No validated clock VF policy change selected")
    if any(key not in _EDITABLE or type(value) is not bool for key, value in changes.items()):
        _fail("Only Boolean clock VF monotonicity or smoothening policies can be changed")
    return dict(changes)


def _entry(registry):
    if not isinstance(registry, Mapping):
        _fail("Registry snapshot must be a mapping")
    names = {}
    for name in registry:
        if not isinstance(name, str) or name.casefold() in names:
            _fail("Registry snapshot has invalid or duplicate value names")
        names[name.casefold()] = name
    name = names.get(REGISTRY_NAME.casefold(), REGISTRY_NAME)
    if name not in registry:
        return name, None
    entry = registry[name]
    if (
        not isinstance(entry, Mapping)
        or set(entry) != {"type", "value"}
        or type(entry["type"]) is not int
        or entry["type"] != 4
        or type(entry["value"]) is not int
        or not 0 <= entry["value"] <= 0xFFFFFFFF
    ):
        _fail(REGISTRY_NAME + ": existing value is not an understood DWORD; preserve and reconcile it first")
    return name, entry


def _identity(identity):
    from .nvbackend import parse_slot

    if not isinstance(identity, Mapping) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        _fail("Clock VF observation needs an exact paired adapter identity")
    address = parse_slot(identity["slot"]) if isinstance(identity["slot"], str) else None
    if address is None or address[0] != 0 or address[1] > 255 or address[2] > 31 or address[3] > 7:
        _fail("Clock VF observation has an invalid PCI address")
    for key, maximum in (("architecture", 0xFFFFFFFF), ("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        if type(identity[key]) is not int or not 0 <= identity[key] <= maximum:
            _fail("Clock VF observation has an invalid adapter identity")
    if identity["architecture"] not in PROBE_ARCHITECTURES:
        _fail("The understood clock VF path requires Pascal or later")
    return dict(identity)


def _header(raw):
    if len(raw) != _HEADER_SIZE:
        raise ValueError("Clock VF header geometry is not understood")
    if raw[0] == _SENTINEL or raw[4:8] != bytes(4):
        raise ValueError("Clock VF group identifier was not overwritten or empty mask changed")
    if any(value not in (0, 1) for value in raw[8:14]) or raw[0x38] != raw[8]:
        raise ValueError("Clock VF header flags are not canonical or the duplicate flag disagrees")
    if any(raw[i] != _SENTINEL for i in (1, 2, 3, 14, 15, 0x39, 0x3A, 0x3B)):
        raise ValueError("Clock VF response changed an unrecognized reserved header field")
    return raw


def _shape(observation, *, writable=False):
    if not isinstance(observation, Mapping):
        _fail("Clock VF observation is unavailable")
    if any(
        type(observation.get(key)) is not type(value) or observation[key] != value for key, value in _LAYOUT.items()
    ):
        _fail("Clock VF observation has an unrecognized layout")
    identity = _identity(observation.get("identity"))
    text = observation.get("header_hex")
    try:
        if not isinstance(text, str) or len(text) != 2 * _HEADER_SIZE:
            raise ValueError("Clock VF observation has no bounded raw header")
        raw = _header(bytes.fromhex(text))
        if raw.hex() != text:
            raise ValueError("Clock VF raw header is not canonical hex")
    except ValueError as exc:
        _fail(str(exc))
    if writable and (type(observation.get("stable_reads")) is not int or observation["stable_reads"] != 2):
        _fail("Two stable fresh clock VF reads are required")
    flags = {}
    preserved = bytearray(raw)
    for field, offset, _, _ in _EDITABLE.values():
        flags[field] = raw[offset]
        if observation.get(field) is not bool(raw[offset]):
            _fail("Clock VF policy disagrees with its raw header")
        preserved[offset] = 0
    return {"identity": identity, "layout": dict(_LAYOUT), "header_hex": preserved.hex(), "flags": flags}


def _binding(reader):
    api, slot, architecture = reader._binding()
    identity = {
        "slot": slot,
        "architecture": architecture,
        "device_id": api.selected.get("devid"),
        "subsystem_id": api.selected.get("subsys"),
    }
    _identity(identity)
    return identity


def _once(gpu, reader, transport):
    identity = _binding(reader)
    header, fields = transport
    if any(header[i] != value for i, value in _ENVELOPE.items()):
        raise PerfDiagnosticError("malformed", "Clock VF needs the understood NVDA envelope")
    if any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in fields.values()):
        raise PerfDiagnosticError("malformed", "Clock VF escape fields have invalid encodings")
    size = 68 + _SIZE
    storage = (ctypes.c_ubyte * (2 * _GUARD_SIZE + size))()
    ctypes.memset(ctypes.addressof(storage), _SENTINEL, ctypes.sizeof(storage))
    packet = (ctypes.c_uint32 * (size // 4)).from_buffer(storage, _GUARD_SIZE)
    for index, word in enumerate(header):
        packet[index] = word
    packet[2], packet[14], packet[15], packet[16] = size, _COMMAND, _SIZE, 0
    packet[5] = packet[11] = 0
    packet[18] = 0  # Explicit empty subset: no per-domain object GET is called.
    expected = tuple(packet[:16])
    try:
        status = gpu._legacy_clk_escape(packet, dict(fields))
    except Exception as exc:
        raise PerfDiagnosticError("transient", f"Clock VF transport failed: {exc}") from exc
    if _binding(reader) != identity:
        raise PerfDiagnosticError("identity", "Clock VF target changed during GET")
    if any(value != _SENTINEL for value in storage[:_GUARD_SIZE]) or any(
        value != _SENTINEL for value in storage[-_GUARD_SIZE:]
    ):
        raise PerfDiagnosticError("malformed", "Clock VF GET exceeded packet bounds")
    if type(status) is not int or status != 0:
        raise PerfDiagnosticError("transient", f"Clock VF GET transport failed (NTSTATUS {status})", status)
    if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)):
        raise PerfDiagnosticError("malformed", "Clock VF response changed its bound envelope or geometry")
    if packet[5] not in (0, 1):
        raise PerfDiagnosticError("malformed", "Clock VF returned an invalid NVDA context flag")
    if packet[11] != 1:
        raise PerfDiagnosticError("transient", f"Clock VF NVDA completion failed (0x{packet[11]:X})", int(packet[11]))
    if packet[16] != 0:
        code = int(packet[16])
        raise PerfDiagnosticError(
            "unsupported" if code in (0x1F, 0x56) else "transient", f"Clock VF GET rejected (RM 0x{code:X})", code
        )
    raw = bytes(packet)[68:]
    if raw[_HEADER_SIZE:] != bytes([_SENTINEL]) * (_SIZE - _HEADER_SIZE):
        raise PerfDiagnosticError("malformed", "Clock VF empty-mask GET changed a per-domain entry")
    try:
        typed = _header(raw[:_HEADER_SIZE])
    except ValueError as exc:
        raise PerfDiagnosticError("malformed", str(exc)) from exc
    return dict(
        _LAYOUT,
        identity=identity,
        header_hex=typed.hex(),
        vf_monotonicity_enforced=bool(typed[0xA]),
        vf_smoothening_enforced=bool(typed[0xB]),
    )


def read(gpu):
    """Return fresh observation/error; failure is distinct from a false flag."""
    reader = PerfDiagnosticsReader(gpu)
    try:
        with gpu._lock:
            for attempt in range(2):
                try:
                    _binding(reader)
                    transport = reader._transport()
                    first = _once(gpu, reader, transport)
                    second = _once(gpu, reader, transport)
                    if not _same_state(first, second):
                        raise PerfDiagnosticError("unstable", "Clock VF header changed between fresh reads")
                    second["stable_reads"] = 2
                    return second, None
                except PerfDiagnosticError as exc:
                    if exc.kind != "transient" or attempt == 1:
                        raise
                    gpu._current_limit_transport = None
                    gpu._current_limit_layout_choice = None
    except Exception as exc:
        return None, str(exc)
    return None, "Clock VF observation is unavailable"


def _reconcile(value, state):
    # These bits also affect unreturned group+180/+181 and PMU boot flags.
    # Leaving them in a composed DWORD would not establish their activation.
    if value & ~0xF:
        _fail(
            REGISTRY_NAME + ": unsupported high-bit consumers are set; preserve and reconcile the existing value first"
        )
    for field, _, _, shift in _EDITABLE.values():
        encoded = (value >> shift) & 3
        # Traced generic initialization sets both policies true. 0 and 2
        # retain that initialization; 1 explicitly enables, 3 disables.
        expected = int(encoded != 3)
        if state["flags"][field] != expected:
            _fail(
                REGISTRY_NAME + ": registry and active policies disagree; reconcile pending or runtime settings first"
            )


def validate_existing(before_registry, observation):
    """Check existing selectors against their reproducible activation state."""
    _, entry = _entry(before_registry)
    if entry is None:
        return
    _reconcile(entry["value"], _shape(observation, writable=True))


def compose(before_registry, flag_changes, observation):
    changes = _changes(flag_changes)
    state = _shape(observation, writable=True)
    name, entry = _entry(before_registry)
    value = entry["value"] if entry is not None else 0
    _reconcile(value, state)
    after = value
    for key, (_, _, mask, shift) in _EDITABLE.items():
        if key in changes:
            after = (after & ~mask) | ((1 if changes[key] else 3) << shift)
    desired = copy.deepcopy(dict(before_registry))
    desired[name] = {"type": 4, "value": after}
    return desired


def contracts(flag_changes, observation):
    changes = _changes(flag_changes, allow_empty=True)
    if not changes:
        return {}
    state = _shape(observation, writable=True)
    result = {}
    for key, (field, _, mask, _) in _EDITABLE.items():
        if key not in changes:
            continue
        unchanged = copy.deepcopy(state)
        before = bool(unchanged["flags"].pop(field))
        result[key] = {
            "schema": 1,
            "registry_name": REGISTRY_NAME,
            "registry_mask": mask,
            "before": before,
            "after": changes[key],
            "unchanged": unchanged,
        }
    return result


def _contract_schema(saved_contracts):
    if not isinstance(saved_contracts, Mapping) or any(key not in _EDITABLE for key in saved_contracts):
        _fail("Unrecognized clock VF contract")
    for key, contract in saved_contracts.items():
        if (
            not isinstance(contract, Mapping)
            or set(contract) != {"schema", "registry_name", "registry_mask", "before", "after", "unchanged"}
            or type(contract["schema"]) is not int
            or contract["schema"] != 1
            or contract["registry_name"] != REGISTRY_NAME
            or type(contract["registry_mask"]) is not int
            or contract["registry_mask"] != _EDITABLE[key][2]
            or type(contract["before"]) is not bool
            or type(contract["after"]) is not bool
            or type(contract["unchanged"]) is not dict
            or type(contract["unchanged"].get("flags")) is not dict
        ):
            _fail("Malformed clock VF contract")


def validate_contracts(flag_changes, saved_contracts):
    """Validate a complete receipt without a GPU read, including before undo.

    Reconstruct the captured observation and require exactly its canonical
    contracts. This binds every selected change, peer baseline and preserved
    header field even when the current adapter cannot yet be queried.
    """
    changes = _changes(flag_changes, allow_empty=True)
    _contract_schema(saved_contracts)
    if set(changes) != set(saved_contracts):
        _fail("Clock VF selected changes and contracts disagree")
    if not changes:
        return
    key = next(iter(saved_contracts))
    captured = saved_contracts[key]
    preserved = captured["unchanged"]
    if set(preserved) != {"identity", "layout", "header_hex", "flags"}:
        _fail("Malformed clock VF preserved state")
    selected_field, selected_offset, _, _ = _EDITABLE[key]
    peer_fields = {entry[0] for entry in _EDITABLE.values()} - {selected_field}
    if set(preserved["flags"]) != peer_fields or any(
        type(value) is not int or value not in (0, 1) for value in preserved["flags"].values()
    ):
        _fail("Malformed clock VF captured peer flags")
    text = preserved["header_hex"]
    try:
        if not isinstance(text, str) or len(text) != 2 * _HEADER_SIZE:
            raise ValueError("Clock VF contract has no bounded raw header")
        raw = bytearray(_header(bytes.fromhex(text)))
        if raw.hex() != text or any(raw[entry[1]] != 0 for entry in _EDITABLE.values()):
            raise ValueError("Clock VF contract header is not canonical preserved state")
    except ValueError as exc:
        _fail(str(exc))
    raw[selected_offset] = int(captured["before"])
    for field, offset, _, _ in _EDITABLE.values():
        if field != selected_field:
            raw[offset] = preserved["flags"][field]
    before = dict(
        _LAYOUT,
        identity=preserved["identity"],
        header_hex=raw.hex(),
        stable_reads=2,
        vf_monotonicity_enforced=bool(raw[0xA]),
        vf_smoothening_enforced=bool(raw[0xB]),
    )
    if not _same_state(contracts(changes, before), saved_contracts):
        _fail("Clock VF contracts do not match the selected changes and captured state")


def verify(observation, saved_contracts, *, restore=False):
    """Verify selected active policies and every untouched header/target field."""
    if type(restore) is not bool:
        _fail("Unrecognized clock VF contract")
    _contract_schema(saved_contracts)
    if not saved_contracts:
        return []
    state = _shape(observation, writable=True)
    phase = "before" if restore else "after"
    verified = []
    for key, (field, _, _, _) in _EDITABLE.items():
        if key not in saved_contracts:
            continue
        contract = saved_contracts[key]
        current = copy.deepcopy(state)
        enabled = bool(current["flags"].pop(field))
        expected = copy.deepcopy(contract["unchanged"])
        for peer, other in saved_contracts.items():
            if peer == key:
                continue
            peer_field = _EDITABLE[peer][0]
            if type(expected["flags"].get(peer_field)) is not int or expected["flags"][peer_field] != int(
                other["before"]
            ):
                _fail("Clock VF contracts disagree about the captured peer baseline")
            expected["flags"][peer_field] = int(other[phase])
        if not _same_state(current, expected):
            _fail("Clock VF target, header or untouched policy changed")
        if enabled is not contract[phase]:
            _fail("Clock VF policy differs from the expected active state")
        verified.append(key)
    return verified
