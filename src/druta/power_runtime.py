# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bind BIOS power fields to independently observed driver policy semantics.

No setters or registry access. The transaction owner pairs adapters and performs
fresh-process observation around restart. Evidence for the GB20x reconstruction
is in experiments/registry-overrides/vbios-fields/rated-max-transform-contract.json.
"""

import copy
import ctypes
import re
from collections.abc import Mapping

from .vbios_fields import POWER_V40_RUNTIME_TYPES

_KEY = re.compile(r"power\.(\d+)\.(minimum|default|maximum)")
_BOUNDS = ("minimum", "default", "maximum")
_IDENTITY = ("type", "channel", "unit")
_GB20X_IMPL = frozenset((2, 3, 5, 6, 7))
_EVIDENCE = "vbios-fields/rated-max-transform-contract.json"


class PowerRuntimeError(RuntimeError):
    pass


def _selected(changes):
    return [(key, int(match[1]), match[2]) for key in changes if (match := _KEY.fullmatch(key))]


def _rows(rows):
    result = {}
    for row in rows or ():
        if not isinstance(row, Mapping) or type(row.get("policy")) is not int or row["policy"] < 0:
            raise PowerRuntimeError("Power policy metadata contains an invalid index")
        index = row["policy"]
        if index in result:
            raise PowerRuntimeError("Power policy metadata contains a duplicate index")
        result[index] = row
    return result


def _bounds(values, description):
    if not isinstance(values, Mapping) or any(
        type(values.get(name)) is not int or not 0 <= values[name] <= 0x7FFFFFFF for name in _BOUNDS
    ):
        raise PowerRuntimeError(description + ": missing or invalid power bounds")
    result = {name: values[name] for name in _BOUNDS}
    if not result["minimum"] <= result["default"] <= result["maximum"]:
        raise PowerRuntimeError(description + ": inconsistent power bounds")
    return result


def _architecture(gpu):
    """Distinguish GB20x from other Blackwell families through public NVAPI."""
    try:
        generation = gpu.arch()
    except Exception as exc:
        raise PowerRuntimeError("GPU architecture could not be read; refresh and retry") from exc
    if generation is None:
        raise PowerRuntimeError("GPU architecture could not be read; refresh and retry")
    if generation != 10:
        return {"generation": generation}, False
    api = getattr(gpu, "nvapi", None)
    reader = getattr(api, "GetArchInfo", None)
    if not api or not api.ok or reader is None:
        raise PowerRuntimeError("Blackwell implementation metadata is unavailable; refresh and retry")
    from .nvbackend import NvAPI, _GpuArchInfo

    for _ in range(2):
        for revision in (2, 1):
            expected_version = NvAPI.ver(_GpuArchInfo, revision)
            info = _GpuArchInfo(version=expected_version)
            try:
                status = reader(api.gpu, ctypes.byref(info))
            except Exception:
                break
            if status == -9:
                continue
            if status != 0 or info.version != expected_version or not info.architecture:
                break
            identity = {
                "generation": generation,
                "architecture": int(info.architecture),
                "implementation": int(info.implementation),
            }
            return identity, info.architecture == 0x1B0 and info.implementation in _GB20X_IMPL
    raise PowerRuntimeError("Blackwell implementation metadata could not be read; refresh and retry")


def _transformed_layout(inspection, fields, index):
    tables = [table for table in inspection.get("tables", ()) if table.get("name") == "power"]
    if len(tables) != 1:
        raise PowerRuntimeError("The transformed power policy needs one understood BIOS power table")
    table = tables[0]
    if (
        (table.get("version"), table.get("header_size"), table.get("stride"))
        not in ((0x40, 0x38, 0x66), (0x40, 0x3D, 0x66))
        or type(table.get("count")) is not int
        or not 0 <= index < table["count"] <= 255
    ):
        raise PowerRuntimeError("The transformed power policy BIOS layout is not understood")
    for field in fields:
        binding = field.get("runtime_binding", {})
        if (
            field.get("policy_type") not in (6, 7)
            or binding.get("vbios_type") != field["policy_type"]
            or binding.get("policy_index") != index
            or binding.get("channel") != field.get("channel")
            or binding.get("units") != field.get("units")
            or POWER_V40_RUNTIME_TYPES.get(field["policy_type"]) != binding.get("runtime_type")
        ):
            raise PowerRuntimeError("The transformed policy class, index or BIOS field binding differs")


def _project(bounds, mode):
    result = dict(bounds)
    if mode == "rated-maximum":
        result["maximum"] = result["default"]
    elif mode != "native":
        raise PowerRuntimeError("Unknown power policy contract mode")
    return result


def contracts(gpu, inspection, changes):
    """Capture all before/after bounds for selected, typed BIOS power fields."""
    selected = _selected(changes)
    if not selected:
        return {}
    if getattr(gpu, "pairing_error", None):
        raise PowerRuntimeError("A paired power policy adapter is unavailable")
    try:
        rows, error = gpu.read_power_policies()
    except Exception as exc:
        raise PowerRuntimeError("Power policy metadata is unavailable: " + str(exc)) from exc
    if error:
        raise PowerRuntimeError("Power policy metadata is unavailable: " + str(error))
    rows = _rows(rows)
    fields = {}
    for field in inspection.get("fields", ()):
        if field.get("key") in fields:
            raise PowerRuntimeError("BIOS fields contain a duplicate binding")
        fields[field.get("key")] = field
    result = {}
    by_policy = {}
    architecture = None
    for key, index, role in selected:
        if index not in by_policy:
            row = rows.get(index)
            peers = [fields.get(f"power.{index}.{bound}") for bound in _BOUNDS]
            if not row or not row.get("consistent") or any(peer is None for peer in peers):
                raise PowerRuntimeError(f"{key}: runtime policy or BIOS bounds are unavailable")
            for peer in peers:
                runtime_type = peer.get("runtime_binding", {}).get("runtime_type")
                if runtime_type is None or row.get("type") != runtime_type:
                    raise PowerRuntimeError(f"{key}: BIOS-to-runtime policy class mapping is unavailable or differs")
                if (
                    peer.get("policy") != index
                    or peer.get("channel") != row.get("channel")
                    or peer.get("units") != row.get("unit")
                    or row.get("unit") not in ("mW", "mA")
                ):
                    raise PowerRuntimeError(f"{key}: runtime policy identity differs from the BIOS field")
            before_rom = _bounds({bound: peer["current"] for bound, peer in zip(_BOUNDS, peers, strict=True)}, key)
            after_rom = dict(before_rom)
            for bound in _BOUNDS:
                if (peer_key := f"power.{index}.{bound}") in changes:
                    after_rom[bound] = changes[peer_key]
            after_rom = _bounds(after_rom, key)
            mode = "native"
            arch_identity = None
            if row["type"] in (17, 18):
                if architecture is None:
                    architecture = _architecture(gpu)
                arch_identity, transformed = architecture
                if transformed:
                    _transformed_layout(inspection, peers, index)
                    mode = "rated-maximum"
                    if f"power.{index}.maximum" in changes:
                        raise PowerRuntimeError(
                            f"power.{index}.maximum: the driver rebuilds this maximum from the rated/default limit; "
                            f"edit power.{index}.default to change the active ceiling and leave BIOS maximum unchanged"
                        )
            before_runtime = _bounds(row, key)
            if before_runtime != _project(before_rom, mode):
                raise PowerRuntimeError(f"{key}: driver adapts the BIOS limits outside the understood mapping")
            by_policy[index] = {
                "schema": 2,
                "policy": index,
                "mode": mode,
                **{name: row[name] for name in _IDENTITY},
                "before_bounds": before_runtime,
                "after_bounds": _project(after_rom, mode),
                "rom_before_bounds": before_rom,
                "rom_after_bounds": after_rom,
                "architecture": arch_identity,
                "evidence": _EVIDENCE if mode == "rated-maximum" else None,
            }
        contract = copy.deepcopy(by_policy[index])
        contract["before"] = contract["before_bounds"][role]
        result[key] = contract
    preserved = {}
    for index, row in rows.items():
        if (
            index in by_policy
            or not row.get("consistent")
            or row.get("type") not in POWER_V40_RUNTIME_TYPES.values()
            or row.get("unit") not in ("mW", "mA")
            or type(row.get("channel")) is not int
        ):
            continue
        try:
            bounds = _bounds(row, f"policy {index}")
        except PowerRuntimeError:
            continue
        preserved[str(index)] = {"policy": index, **{name: row[name] for name in _IDENTITY}, "bounds": bounds}
    for contract in result.values():
        contract["preserved_policies"] = copy.deepcopy(preserved)
    return result


def verify(snapshot, changes, contracts=None, restore=False):
    """Check complete captured bounds; retain old receipt selected-field semantics."""
    selected = _selected(changes)
    if not selected:
        return []
    if snapshot.get("policy_error"):
        raise PowerRuntimeError("Power policy readback is unavailable: " + str(snapshot["policy_error"]))
    rows = _rows(snapshot.get("policies", ()))
    checked = []
    for key, index, role in selected:
        row = rows.get(index)
        if not row or not row.get("consistent"):
            raise PowerRuntimeError(f"{key}: runtime policy readback is unavailable")
        contract = (contracts or {}).get(key)
        if contract and any(row.get(name) != contract.get(name) for name in _IDENTITY):
            raise PowerRuntimeError(f"{key}: runtime policy identity changed")
        if contract and "schema" in contract:
            if contract["schema"] != 2 or contract.get("policy") != index:
                raise PowerRuntimeError(f"{key}: power policy contract identity is invalid")
            before_rom = _bounds(contract.get("rom_before_bounds"), key)
            after_rom = _bounds(contract.get("rom_after_bounds"), key)
            mode = contract.get("mode")
            if mode == "rated-maximum":
                arch = contract.get("architecture") or {}
                if (
                    contract["type"] not in (17, 18)
                    or arch.get("generation") != 10
                    or arch.get("architecture") != 0x1B0
                    or arch.get("implementation") not in _GB20X_IMPL
                ):
                    raise PowerRuntimeError(f"{key}: transformed policy architecture or class is invalid")
            if _bounds(contract.get("before_bounds"), key) != _project(before_rom, mode) or _bounds(
                contract.get("after_bounds"), key
            ) != _project(after_rom, mode):
                raise PowerRuntimeError(f"{key}: power policy contract projection is inconsistent")
            if not restore and after_rom[role] != changes[key]:
                raise PowerRuntimeError(f"{key}: requested value differs from the prepared power policy contract")
            expected = contract["before_bounds" if restore else "after_bounds"]
            actual = _bounds(row, key)
            if actual != expected:
                details = ", ".join(f"{bound} {actual[bound]} (expected {expected[bound]})" for bound in _BOUNDS)
                raise PowerRuntimeError(f"{key}: runtime policy reports different bounds: {details}")
            for peer in contract.get("preserved_policies", {}).values():
                observed = rows.get(peer["policy"])
                if (
                    not observed
                    or not observed.get("consistent")
                    or any(observed.get(name) != peer.get(name) for name in _IDENTITY)
                    or _bounds(observed, key) != _bounds(peer.get("bounds"), key)
                ):
                    raise PowerRuntimeError(f"{key}: unselected policy {peer['policy']} metadata changed")
        else:
            # Old receipts only captured the selected bound and row identity.
            wanted = contract["before"] if restore and contract else changes[key]
            if row.get(role) != wanted:
                raise PowerRuntimeError(
                    f"{key}: BIOS value {wanted} loaded, but runtime policy reports {row.get(role)}"
                )
        checked.append(key)
    return checked
