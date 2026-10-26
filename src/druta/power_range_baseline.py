# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only original BIOS range for styling the existing power control.

The current driver range may already contain a Druta override. Recover the
original from the existing receipt chain, never from the rated/default value.
Failure here affects this baseline only, not power-control availability.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import asdict
from pathlib import Path

from . import client_power_runtime as client
from . import registry_overrides as overrides
from . import vbios_fields

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_BOUNDS = ("minimum", "default", "maximum")


def _unknown(reason):
    return {"minimum_mw": None, "maximum_mw": None, "source": "unavailable", "reason": str(reason)}


def _integer(value, lower, upper, name):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"Original power range: invalid {name}")
    return value


def _packet(observation, name, size):
    value = observation.get(name)
    if not isinstance(value, str) or len(value) != size * 2:
        raise ValueError(f"Original power range: missing {name}")
    raw = bytes.fromhex(value)
    if len(raw) != size:
        raise ValueError(f"Original power range: invalid {name}")
    return raw


def _live(observation, target):
    """Bind the supplied typed role1 capture to one native INFO policy."""
    if not isinstance(observation, dict) or type(observation.get("schema")) is not int or observation["schema"] != 1:
        raise ValueError("Original power range: client-power metadata is unavailable")
    if type(observation.get("stable_reads")) is not int or observation["stable_reads"] != 2:
        raise ValueError("Original power range: client-power metadata is not stable")
    identity, binding = observation["identity"], observation["binding"]
    device = _integer(identity["device_id"], 0, 0xFFFF, "device identity")
    subsystem = _integer(identity["subsystem_id"], 0, 0xFFFFFFFF, "subsystem identity")
    _integer(identity["architecture"], 0, 0xFFFFFFFF, "architecture")
    hardware = target.instance_id.split("\\", 2)[1].upper()
    if (
        identity["slot"] != target.slot
        or f"DEV_{device:04X}" not in hardware
        or f"SUBSYS_{subsystem:08X}" not in hardware
    ):
        raise ValueError("Original power range: client-power target differs from the selected GPU")
    index = _integer(binding["native_index"], 0, client.NATIVE_CAPACITY - 1, "native policy index")
    selected = _integer(binding["client_index"], 0, client.CAPACITY - 1, "client policy index")
    if (
        _integer(binding["semantic_role"], 0, 255, "semantic role") != client.ROLE
        or _integer(binding["client"], 0, 255, "client") != client.CLIENT
        or binding["units"] != "mW"
    ):
        raise ValueError("Original power range: no understood milliwatt role1 binding")
    info = _packet(observation, "native_info_hex", client.SIZES[client.NATIVE_INFO])
    role = _packet(observation, "role_info_hex", client.SIZES[client.ROLE_INFO])
    public = _packet(observation, "info_hex", client.SIZES[client.INFO])
    mask = struct.unpack_from("<I", info, 4)[0]
    row_at = client.NATIVE_INFO_BASE + index * client.NATIVE_INFO_STRIDE
    row = info[row_at : row_at + client.NATIVE_INFO_STRIDE]
    matches = [
        i
        for i in range(client.NATIVE_CAPACITY)
        if mask >> i & 1
        and info[
            client.NATIVE_INFO_BASE + i * client.NATIVE_INFO_STRIDE : client.NATIVE_INFO_BASE
            + (i + 1) * client.NATIVE_INFO_STRIDE
        ]
        == role[4:]
    ]
    if role[:4] != struct.pack("<I", client.ROLE) or matches != [index] or row[0] != 0x40 or row[6] != 0:
        raise ValueError("Original power range: native role1 INFO binding differs")
    public_mask = int.from_bytes(public[4 : client.INFO_BASE], "little")
    candidates = [
        i
        for i in range(client.CAPACITY)
        if public_mask >> i & 1 and public[client.INFO_BASE + i * client.INFO_STRIDE + 2] == client.ROLE
    ]
    at = client.INFO_BASE + selected * client.INFO_STRIDE
    if (
        public_mask >> client.CAPACITY
        or candidates != [selected]
        or public[at] != public[at + 1]
        or public[at + 3] != 0
    ):
        raise ValueError("Original power range: client INFO binding differs")
    limits = tuple(_integer(binding[key], 0, 0xFFFFFFFF, key) for key in ("minimum_mw", "rated_mw", "maximum_mw"))
    if (
        limits != tuple(sorted(limits))
        or struct.unpack_from("<III", row, 8) != limits
        or struct.unpack_from("<III", public, at + 4) != limits
    ):
        raise ValueError("Original power range: client/native limits disagree")
    return index, row[4], row[5], device, limits


def _bios(inspection, live):
    index, kind, channel, device, _ = live
    identity = inspection["identity"]
    if identity.get("pci_vendor") != 0x10DE or identity.get("pci_device") != device:
        raise ValueError("Original power range: BIOS adapter identity differs")
    result, shape = [], None
    for name in _BOUNDS:
        matches = [f for f in inspection.get("fields", ()) if f.get("key") == f"power.{index}.{name}"]
        if len(matches) != 1:
            raise ValueError("Original power range: selected BIOS policy bounds are unavailable")
        field = matches[0]
        binding = field.get("runtime_binding", {})
        bios_kind = _integer(binding.get("vbios_type"), 0, 255, "BIOS policy class")
        current_shape = (
            binding.get("policy_index"),
            bios_kind,
            binding.get("runtime_type"),
            binding.get("channel"),
            binding.get("units"),
        )
        if (
            current_shape != (index, bios_kind, kind, channel, "mW")
            or vbios_fields.POWER_V40_RUNTIME_TYPES.get(bios_kind) != kind
            or field.get("policy") != index
            or field.get("policy_type") != bios_kind
            or field.get("channel") != channel
            or field.get("units") != "mW"
            or (shape is not None and shape != current_shape)
        ):
            raise ValueError("Original power range: BIOS/runtime policy binding differs")
        shape = current_shape
        result.append(_integer(field.get("current"), 0, 0xFFFFFFFF, f"BIOS {name}"))
    if result != sorted(result):
        raise ValueError("Original power range: BIOS policy bounds are inconsistent")
    return tuple(result), shape


def _image_overrides(snapshot):
    if not isinstance(snapshot, dict) or any(not isinstance(name, str) for name in snapshot):
        raise ValueError("Original power range: registry snapshot is unavailable")
    return {
        name.casefold(): value
        for name, value in snapshot.items()
        if name.casefold() == "vbios" or overrides._RECORD.fullmatch(name)
    }


def _digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Original power range: receipt image hash is invalid")
    return value


def _original(target, registry, current):
    """Walk existing receipts through image-preserving registry-only entries."""
    receipt = overrides.active_receipt(target)
    if receipt is None:
        if _image_overrides(registry):
            raise ValueError("Original BIOS range is unknown: a BIOS override has no original Druta receipt")
        return current, "live-bios"
    expected_registry, expected_hash = registry, hashlib.sha256(current).hexdigest()
    original, visited = None, set()
    while receipt is not None:
        path = Path(receipt["path"]).resolve()
        if (
            path.parent != overrides.state_directory().resolve()
            or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name)
            or path in visited
        ):
            raise ValueError("Original power range: invalid or cyclic receipt chain")
        visited.add(path)
        if receipt.get("state") != "active" or receipt.get("target") != asdict(target):
            raise ValueError("Original power range: receipt state or target differs")
        before, after = receipt["before"], receipt["after"]
        _image_overrides(before)
        _image_overrides(after)
        if after != expected_registry:
            raise ValueError("Original power range: receipt registry chain differs")
        before_hash, after_hash = receipt.get("before_image_sha256"), receipt.get("after_image_sha256")
        if before_hash is None and after_hash is None:
            if _image_overrides(before) or _image_overrides(after) or receipt.get("changes"):
                raise ValueError("Original power range: a BIOS receipt is missing image hashes")
        else:
            if _digest(after_hash) != expected_hash:
                raise ValueError("Original power range: running image or receipt image chain differs")
            expected_hash = _digest(before_hash)
            backup = path.with_suffix(".rom").resolve()
            if backup.parent != path.parent or not 32 <= backup.stat().st_size <= overrides._IMAGE_CAPACITY:
                raise ValueError("Original power range: original ROM backup is unavailable or invalid")
            original = backup.read_bytes()
            if hashlib.sha256(original).hexdigest() != expected_hash:
                raise ValueError("Original power range: original ROM backup hash differs")
        expected_registry = before
        parent = receipt.get("parent")
        receipt = overrides.load_receipt(parent) if parent is not None else None
    if _image_overrides(expected_registry):
        raise ValueError("Original BIOS range is unknown: the receipt chain began with an external BIOS override")
    return (original, "receipt-original") if original is not None else (current, "live-bios")


def read(gpu, live_binding=None):
    """Return original minimum/maximum milliwatts, or an independent unknown.

    ``live_binding`` may be a ClientPowerController.read_metadata() observation
    or the complete read() observation already held by the UI. Request CONTROL
    and STATUS, voltage, fan and current telemetry are not required.
    """
    try:
        target = overrides.bind_target(gpu)
        observation = live_binding if live_binding is not None else client.ClientPowerController(gpu).read_metadata()
        live = _live(observation, target)
        registry = overrides.WindowsRegistry(target).snapshot()
        current = overrides.read_vbios(gpu)
        inspection = vbios_fields.inspect(current)
        current_bounds, current_shape = _bios(inspection, live)
        if current_bounds != live[4]:
            raise ValueError("Original power range: running BIOS and client limits differ")
        original, source = _original(target, registry, current)
        bounds, shape = _bios(vbios_fields.inspect(original), live)
        if shape != current_shape:
            raise ValueError("Original power range: original and current policy bindings differ")
        if overrides.bind_target(gpu) != target:
            raise ValueError("Original power range: selected GPU changed during capture")
        return {"minimum_mw": bounds[0], "maximum_mw": bounds[2], "source": source, "reason": None}
    except Exception as exc:
        return _unknown(exc)
