# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose and verify traced PERF limits registry flags.

This module never writes the registry, changes GPU state or restarts a device.
The transaction owner must stage the returned exact registry snapshot, restart
the selected GPU, obtain a fresh-process observation, and verify its contract.
The cache flag controls reuse of limit calculations. The skip-updates flag gates
host performance-limit update requests; firmware enforcement remains
uncharacterized. Evidence is in experiments/registry-overrides/driver-controls.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping

from .perf_diagnostics import LAYOUTS, PROBE_ARCHITECTURES, PerfDiagnosticsReader

REGISTRY_NAME = "RmPerfLimitsOverride"
CACHE_KEY = "perf_limits.cache"
CACHE_FLAG = "limits_control_0x4d"
CACHE_MASK = 0xC
SKIP_KEY = "perf_limits.skip_updates"
SKIP_FLAG = "limits_control_0x4c"
SKIP_MASK = 0x3
_DWORD = 4
_EDITABLE = {
    CACHE_KEY: (CACHE_FLAG, CACHE_MASK, 2, "cache_enabled"),
    SKIP_KEY: (SKIP_FLAG, SKIP_MASK, 0, "skip_updates"),
}
_FIELDS = ((0, SKIP_FLAG), (2, CACHE_FLAG), (6, "limits_info_0x7a88"))
_FLAG_NAMES = frozenset(name for _, name in _FIELDS)
_KNOWN_MASK = 0xCF  # bits5:4 and bits31:8 cannot be reconciled by this reader


def _fail(message):
    # registry_overrides imports this module in its transaction paths.
    from .registry_overrides import OverrideError

    raise OverrideError(message)


def _changes(changes, *, allow_empty=False):
    if not isinstance(changes, Mapping) or (not changes and not allow_empty):
        _fail("No validated registry flag change selected")
    if any(key not in _EDITABLE or type(value) is not bool for key, value in changes.items()):
        _fail("Only Boolean PERF limits cache or skip-updates flags can be changed")
    return dict(changes)


def _entry(registry):
    if not isinstance(registry, Mapping):
        _fail("Registry snapshot must be a mapping")
    names = {}
    for name in registry:
        if not isinstance(name, str) or name.casefold() in names:
            _fail("Registry snapshot has invalid or duplicate value names")
        names[name.casefold()] = name
    name = names.get(REGISTRY_NAME.casefold())
    if name is None:
        return REGISTRY_NAME, None
    entry = registry[name]
    if (
        not isinstance(entry, Mapping)
        or set(entry) != {"type", "value"}
        or type(entry["type"]) is not int
        or entry["type"] != _DWORD
        or type(entry["value"]) is not int
        or not 0 <= entry["value"] <= 0xFFFFFFFF
    ):
        _fail(REGISTRY_NAME + ": existing value is not an understood DWORD; preserve and reconcile it first")
    return name, entry


def _shape(observation, *, writable=False, selected=()):
    """Return stable target/layout state; omit dynamic limit request payloads."""
    if not isinstance(observation, Mapping):
        _fail("PERF limits observation is unavailable")
    layout = LAYOUTS["limits"]
    required = {
        "group": "limits",
        "capacity": layout.capacity,
        "info_command": f"0x{layout.info_command:08X}",
        "control_command": f"0x{layout.control_command:08X}",
        "info_bytes": layout.info_size,
        "control_bytes": layout.control_size,
    }
    if any(
        type(observation.get(key)) is not type(value) or observation[key] != value for key, value in required.items()
    ):
        _fail("PERF limits observation has an unrecognized layout")
    identity = observation.get("identity")
    if not isinstance(identity, Mapping) or set(identity) != {"slot", "architecture", "device_id", "subsystem_id"}:
        _fail("PERF limits observation has no exact adapter identity")
    if identity["slot"] != observation.get("slot") or identity["architecture"] != observation.get("architecture"):
        _fail("PERF limits observation disagrees with its adapter identity")
    from .nvbackend import parse_slot

    address = parse_slot(identity["slot"]) if isinstance(identity["slot"], str) else None
    if address is None or address[0] != 0 or address[1] > 255 or address[2] > 31 or address[3] > 7:
        _fail("PERF limits observation has an unrecognized PCI address")
    for key, maximum in (("architecture", 0xFFFFFFFF), ("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        if type(identity[key]) is not int or not 0 <= identity[key] <= maximum:
            _fail("PERF limits observation has an invalid adapter identity")
    if identity["architecture"] not in PROBE_ARCHITECTURES:
        _fail("Modern PERF groups require a Pascal-or-later architecture")
    if type(observation.get("architecture")) is not int:
        _fail("PERF limits observation has an invalid architecture")
    try:
        mask_text = observation["mask_hex"]
        if not isinstance(mask_text, str) or len(mask_text) != 2 * layout.mask_bytes:
            raise ValueError
        mask = bytes.fromhex(mask_text)
    except (KeyError, TypeError, ValueError):
        _fail("PERF limits observation has an invalid inventory mask")
    if len(mask) != layout.mask_bytes:
        _fail("PERF limits observation has an invalid inventory mask")
    mask_bits = int.from_bytes(mask, "little")
    indices = [index for index in range(layout.capacity) if mask_bits & (1 << index)]
    if type(observation.get("present_count")) is not int or observation["present_count"] != len(indices):
        _fail("PERF limits observation count differs from its mask")
    entries = observation.get("entries")
    if not isinstance(entries, list):
        _fail("PERF limits observation has no typed inventory")
    rows = []
    for entry in entries:
        if not isinstance(entry, Mapping) or any(
            type(entry.get(key)) is not int for key in ("index", "type", "control_type")
        ):
            _fail("PERF limits observation contains an invalid inventory row")
        index, info_type, control_type = (entry[key] for key in ("index", "type", "control_type"))
        if not 0 <= index < layout.capacity or not 0 <= info_type <= 255 or not 0 <= control_type <= 255:
            _fail("PERF limits observation contains an invalid inventory row")
        if writable and info_type != control_type:
            _fail("PERF limits INFO and CONTROL row types disagree")
        rows.append([index, info_type, control_type])
    rows.sort()
    if [row[0] for row in rows] != indices:
        _fail("PERF limits typed inventory differs from its mask")
    flags = observation.get("flags")
    if not isinstance(flags, Mapping):
        _fail("PERF limits observation has no flags")
    raw_flags = {}
    for name, entry in flags.items():
        if (
            not isinstance(name, str)
            or not isinstance(entry, Mapping)
            or type(entry.get("raw")) is not int
            or not 0 <= entry["raw"] <= 255
        ):
            _fail("PERF limits observation has malformed flag data")
        raw_flags[name] = entry["raw"]
    if writable:
        if not indices or type(observation.get("stable_reads")) is not int or observation["stable_reads"] != 2:
            _fail("A stable, populated PERF limits inventory is required")
        if not {SKIP_FLAG, CACHE_FLAG} <= raw_flags.keys() or not raw_flags.keys() <= _FLAG_NAMES:
            _fail("PERF limits flag layout is not understood")
        if any(type(value) is not int or value not in (0, 1) for value in raw_flags.values()):
            _fail("PERF limits flags must have Boolean driver readback")
        if observation.get("cache_enabled") is not bool(raw_flags[CACHE_FLAG]):
            _fail("PERF limits cache state disagrees with its raw flag")
        # Cache-only schema1 receipts predate this derived observation field.
        # New skip edits must carry it; whenever supplied it must be exact.
        if SKIP_KEY in selected or "skip_updates" in observation:
            if observation.get("skip_updates") is not bool(raw_flags[SKIP_FLAG]):
                _fail("PERF limits skip-updates state disagrees with its raw flag")
    return {
        "identity": dict(identity),
        "layout": required,
        "mask_hex": mask.hex(),
        "row_types": rows,
        "flags": raw_flags,
    }


def read(gpu):
    """Return (two-read observation, error) without requiring edit eligibility.

    An unsupported/malformed group returns an error. A readable group with
    unknown flags or row types remains available for diagnostics; composition
    separately validates the selected operation. Historical data is never used.
    """
    try:
        reader = PerfDiagnosticsReader(gpu)
        snapshots = []
        for _ in range(2):
            result = reader.read(["limits"])["groups"]["limits"]
            if result["status"] not in ("ready", "empty") or result["snapshot"] is None:
                _fail(result.get("error") or "PERF limits observation is unavailable")
            observation = copy.deepcopy(result["snapshot"])
            selected = gpu.nvapi.selected
            observation["identity"] = {
                "slot": observation["slot"],
                "architecture": observation["architecture"],
                "device_id": selected.get("devid"),
                "subsystem_id": selected.get("subsys"),
            }
            for flag_name, _, _, field in _EDITABLE.values():
                flag = observation.get("flags", {}).get(flag_name, {}).get("raw")
                observation[field] = bool(flag) if type(flag) is int and flag in (0, 1) else None
            snapshots.append(observation)
        if _shape(snapshots[0]) != _shape(snapshots[1]):
            _fail("PERF limits identity, inventory or flags changed between reads; retry")
        snapshots[1]["stable_reads"] = 2
        return snapshots[1], None
    except Exception as exc:
        return None, str(exc)


def validate_existing(before_registry, observation):
    """Refuse pending or undecoded bits that a restart could newly activate."""
    _, entry = _entry(before_registry)
    if entry is None or entry["value"] == 0:
        return
    value = entry["value"]
    if value & ~_KNOWN_MASK:
        _fail(REGISTRY_NAME + ": undecoded bits are set; preserve and reconcile the existing value first")
    for shift, name in _FIELDS:
        encoded = (value >> shift) & 3
        if encoded == 3:
            _fail(REGISTRY_NAME + ": reserved field encoding; preserve and reconcile the existing value first")
        if encoded:
            state = _shape(observation)["flags"].get(name)
            if type(state) is not int or state not in (0, 1) or state != encoded - 1:
                _fail(REGISTRY_NAME + ": registry and active flags disagree; reconcile pending settings first")


def compose(before_registry, flag_changes, observation):
    """Return a deep-copied registry state with only selected bit-pairs replaced."""
    changes = _changes(flag_changes)
    _shape(observation, writable=True, selected=changes)
    validate_existing(before_registry, observation)
    name, entry = _entry(before_registry)
    before = entry["value"] if entry is not None else 0
    after = before
    for key, (_, mask, shift, _) in _EDITABLE.items():
        if key in changes:
            after = (after & ~mask) | ((2 if changes[key] else 1) << shift)
    desired = copy.deepcopy(dict(before_registry))
    desired[name] = {"type": _DWORD, "value": after}
    return desired


def contracts(flag_changes, observation):
    """Save identity, group inventory, untouched flags and exact expected state."""
    changes = _changes(flag_changes, allow_empty=True)
    if not changes:
        return {}
    state = _shape(observation, writable=True, selected=changes)
    saved = {}
    for key, (flag, mask, _, _) in _EDITABLE.items():
        if key not in changes:
            continue
        unchanged = copy.deepcopy(state)
        before = bool(unchanged["flags"].pop(flag))
        saved[key] = {
            "schema": 1,
            "registry_name": REGISTRY_NAME,
            "registry_mask": mask,
            "before": before,
            "after": changes[key],
            "unchanged": unchanged,
        }
    return saved


def verify(observation, saved_contracts, *, restore=False):
    """Verify fresh active state after a GPU restart, or the captured undo state."""
    if not isinstance(saved_contracts, Mapping) or any(key not in _EDITABLE for key in saved_contracts):
        _fail("Unrecognized registry flag contract")
    if not saved_contracts:
        return []
    for key, contract in saved_contracts.items():
        if (
            not isinstance(contract, Mapping)
            or set(contract) != {"schema", "registry_name", "registry_mask", "before", "after", "unchanged"}
            or type(contract["schema"]) is not int
            or contract["schema"] != 1
            or contract["registry_name"] != REGISTRY_NAME
            or type(contract["registry_mask"]) is not int
            or contract["registry_mask"] != _EDITABLE[key][1]
            or type(contract["before"]) is not bool
            or type(contract["after"]) is not bool
            or type(contract["unchanged"]) is not dict
            or type(contract["unchanged"].get("flags")) is not dict
        ):
            _fail("Malformed PERF limits flag contract")
    state = _shape(observation, writable=True, selected=saved_contracts)
    phase = "before" if restore else "after"
    verified = []
    for key, (flag, _, _, _) in _EDITABLE.items():
        if key not in saved_contracts:
            continue
        contract = saved_contracts[key]
        current_state = copy.deepcopy(state)
        current = bool(current_state["flags"].pop(flag))
        expected_state = copy.deepcopy(contract["unchanged"])
        # Schema1 captures all peer flags at baseline. A peer selected in this
        # same transaction has its own exact before/after contract. Reconcile
        # its baseline first, then use its expected value for this phase.
        for peer, peer_contract in saved_contracts.items():
            if peer == key:
                continue
            peer_flag = _EDITABLE[peer][0]
            baseline = expected_state["flags"].get(peer_flag)
            if type(baseline) is not int or baseline != int(peer_contract["before"]):
                _fail("PERF limits flag contracts disagree on the captured baseline")
            expected_state["flags"][peer_flag] = int(peer_contract[phase])
        if not _same_state(current_state, expected_state):
            _fail("PERF limits identity, inventory or untouched flags changed")
        if current is not contract[phase]:
            _fail("PERF limits flag differs from the expected active state")
        verified.append(key)
    return verified


def _same_state(actual, expected):
    """JSON receipt fields must keep their types as well as their values."""
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            _same_state(value, expected[key]) for key, value in actual.items()
        )
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _same_state(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected
