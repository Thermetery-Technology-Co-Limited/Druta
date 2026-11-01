# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only NVIDIA PERF limits and continuous-feedback diagnostics.

Contracts are traced in experiments/registry-overrides/driver-controls. A
generation permits probing; accepted parameter geometry, matching masks and
typed rows establish the understood readback. No board or driver identity is
used as a compatibility gate. Registered limit slots are not necessarily active
constraints, and a readable flag does not prove a registry override works.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import struct
import time
from contextlib import nullcontext
from dataclasses import dataclass


@dataclass(frozen=True)
class GroupLayout:
    info_id: int
    control_id: int
    info_command: int
    control_command: int
    info_size: int
    control_size: int
    capacity: int
    mask_bytes: int
    info_offset: int
    info_stride: int
    control_offset: int
    control_stride: int


LAYOUTS = {
    "limits": GroupLayout(
        0x4D2C0A9C, 0xA59BE705, 0x2080A022, 0x2080A024, 0x7A8C, 0x16850, 512, 64, 0x288, 0x3C, 0x50, 0xB4
    ),
    "cf_controller": GroupLayout(
        0x153DE751, 0x3BD390E8, 0x2080A0C8, 0x2080E0CA, 0x6A0C, 0x588C, 32, 4, 0xC, 0x350, 0xC, 0x2C4
    ),
    "cf_policy": GroupLayout(0x1CAC3865, 0x69DE2C54, 0x2080A0CC, 0x2080A0CE, 0x230, 0x88, 32, 4, 0x30, 0x10, 8, 4),
}
# Modern PERF board-object groups occur in Pascal and later architectures.
# Packet rejection remains an independent capability result for each group.
PROBE_ARCHITECTURES = frozenset((4, 5, 6, 7, 8, 9, 10))
_GET_COMMANDS = frozenset((layout.info_command, layout.info_size) for layout in LAYOUTS.values()) | frozenset(
    (layout.control_command, layout.control_size) for layout in LAYOUTS.values()
)
_ESCAPE_FIELDS = frozenset(("hAdapter", "hDevice", "Type", "Flags", "hContext"))


class PerfDiagnosticError(RuntimeError):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind, self.status = kind, status


def _layout(name: str) -> GroupLayout:
    try:
        return LAYOUTS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown PERF diagnostic group {name!r}") from exc


def _mask(info: bytes, layout: GroupLayout) -> bytes:
    if len(info) != layout.info_size:
        raise PerfDiagnosticError("malformed", "Unexpected PERF INFO parameter geometry")
    return info[4 : 4 + layout.mask_bytes]


def decode_group(
    name: str, info: bytes, control: bytes | None, *, slot: str = "", architecture: int | None = None
) -> dict:
    """Decode a matched group pair without touching the GPU.

    Unknown row types and malformed rows remain visible with raw bytes and
    issues; supported peers remain readable. A zero INFO mask is an empty
    inventory, not evidence of a usable control. No fixed policy index is used
    to resolve the registry's targets.
    """
    layout = _layout(name)
    info = bytes(info)
    mask = _mask(info, layout)
    bits = int.from_bytes(mask, "little")
    if bits >> layout.capacity:
        raise PerfDiagnosticError("malformed", "PERF INFO mask exceeds group capacity")
    if control is not None:
        control = bytes(control)
        if len(control) != layout.control_size:
            raise PerfDiagnosticError("malformed", "Unexpected PERF CONTROL parameter geometry")
        if mask != control[4 : 4 + layout.mask_bytes] or info[0] != control[0]:
            raise PerfDiagnosticError("malformed", "PERF INFO and CONTROL group identifiers or masks disagree")
    elif bits:
        raise PerfDiagnosticError("malformed", "A populated PERF group needs matching CONTROL readback")

    entries, issues = [], []
    for index in range(layout.capacity):
        if not bits & (1 << index):
            continue
        pos = layout.info_offset + index * layout.info_stride
        ctl = layout.control_offset + index * layout.control_stride
        raw = info[pos : pos + layout.info_stride]
        current = control[ctl : ctl + layout.control_stride]
        row = {
            "index": index,
            "type": raw[0],
            "control_type": current[0],
            "driver_label": None,
            "info_hex": raw.hex(),
            "control_hex": current.hex(),
            "issues": [],
        }
        if raw[0] != current[0]:
            row["issues"].append("INFO and CONTROL row types disagree")
        elif name == "limits":
            if raw[0] == 6:
                label_field = raw[2:32]
                label = label_field.split(b"\0", 1)[0]
                if b"\0" not in label_field or not label or any(b < 32 or b > 126 for b in label):
                    row["issues"].append("Unrecognized driver label encoding")
                else:
                    row["driver_label"] = label.decode("ascii")
            else:
                row["issues"].append("Limit row type has no decoded label layout")
        elif name == "cf_policy":
            if raw[0] == 0:
                row["role_bytes_raw"] = [raw[2], raw[3]]
                # The group inserts this object into its role-index table
                # using object+0x59, serialized at INFO entry+3. Type 0 adds
                # the controller-selection mask from object+0xC0 at entry+C.
                # Names for these roles are not independently established.
                row["role_id"] = raw[3]
                row["controller_mask"] = struct.unpack_from("<I", raw, 0xC)[0]
                if current[2] in (0, 1):
                    row["registry_controlled_flag_raw"] = current[2]
                else:
                    row["issues"].append("Policy flag is not Boolean")
            else:
                row["issues"].append("Policy row type has no decoded flag layout")
        entries.append(row)

    flags, targets = {}, []
    if bits and control is not None:
        offsets = (
            {"limits_control_0x4c": (control, 0x4C), "limits_control_0x4d": (control, 0x4D)}
            if name == "limits"
            else {"controller_control_0x08": (control, 8), "controller_control_0x09": (control, 9)}
            if name == "cf_controller"
            else {}
        )
        # Only the identified subclass adds this INFO field after the array.
        if name == "limits" and info[0x44] == 8 and control[0x44] == 8:
            offsets["limits_info_0x7a88"] = (info, 0x7A88)
        for label, (packet, offset) in offsets.items():
            value = packet[offset]
            flags[label] = {"raw": value, "boolean": bool(value) if value in (0, 1) else None}
            if value not in (0, 1):
                issues.append(f"{label} is not Boolean")
        if name == "cf_policy":
            by_index = {row["index"]: row for row in entries}
            for field, offset, role in (("bit0", 0x18, 7), ("bits2_1", 0x14, 5), ("bit3", 0x20, 11)):
                index = struct.unpack_from("<H", info, offset)[0]
                row = by_index.get(index)
                target = {
                    "registry_name": "RmPerfCfPolicyOverrides",
                    "field": field,
                    "index": None if index == 0xFFFF else index,
                    "present": row is not None,
                    "flag_raw": None,
                    "issue": None,
                }
                if index != 0xFFFF and row is None:
                    target["issue"] = "Target index is outside the valid policy mask"
                elif row is not None:
                    if row.get("role_id") != role:
                        target["issue"] = "Target policy role does not match its INFO role table"
                    else:
                        target["flag_raw"] = row.get("registry_controlled_flag_raw")
                    if target["flag_raw"] is None and target["issue"] is None:
                        target["issue"] = "Target policy flag layout is not understood"
                targets.append(target)

    return {
        "group": name,
        "slot": slot,
        "architecture": architecture,
        "captured_at": time.time(),
        "capacity": layout.capacity,
        "mask_hex": mask.hex(),
        "present_count": len(entries),
        "info_command": f"0x{layout.info_command:08X}",
        "control_command": f"0x{layout.control_command:08X}",
        "info_bytes": layout.info_size,
        "control_bytes": layout.control_size,
        "info_sha256": hashlib.sha256(info).hexdigest(),
        "control_sha256": hashlib.sha256(control).hexdigest() if control is not None else None,
        "entries": entries,
        "flags": flags,
        "registry_targets": targets,
        "issues": issues,
    }


class PerfDiagnosticsReader:
    """Independent bounded GETs using an existing PCI-paired backend GPU.

    read() returns fresh snapshots separately from last_valid historical data.
    A failed peer never discards another group. Construct a fresh reader and GPU
    after a driver restart; this object never rebinds, restarts, or sets anything.
    """

    def __init__(self, gpu, *, attempts: int = 2):
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError("PERF read attempts must be between 1 and 3")
        self.gpu, self.attempts = gpu, attempts
        self.last_valid: dict[str, dict] = {}
        self.last_errors: dict[str, PerfDiagnosticError] = {}
        self._identity = None

    def _binding(self):
        from .nvbackend import parse_slot

        gpu, api = self.gpu, getattr(self.gpu, "nvapi", None)
        if getattr(gpu, "pairing_error", ""):
            raise PerfDiagnosticError("identity", gpu.pairing_error)
        if api is None or not api.ok or not api.gpu or not api.selected:
            raise PerfDiagnosticError("transient", "A live PCI-paired NVAPI handle is unavailable")
        slot = gpu.slot()
        address, api_address = parse_slot(slot), parse_slot(api.selected.get("slot"))
        if address is None or address[0] != 0 or address != api_address:
            raise PerfDiagnosticError("identity", "NVAPI handle does not identify the selected PCI address")

        def value(handle):
            return getattr(handle, "value", handle)

        handle = value(api.gpu)
        if value(api.selected.get("handle")) != handle:
            raise PerfDiagnosticError("identity", "NVAPI handle changed without its target binding")
        nvml = getattr(gpu, "nvml", None)
        other = getattr(nvml, "selected", None)
        if other:
            if parse_slot(other.get("slot")) != address:
                raise PerfDiagnosticError("identity", "NVML and NVAPI identify different PCI addresses")
            for key in ("devid", "subsys"):
                if other.get(key) is not None and api.selected.get(key) != other[key]:
                    raise PerfDiagnosticError("identity", f"NVML and NVAPI disagree about {key}")
        architecture = gpu.arch()
        if architecture is None:
            raise PerfDiagnosticError("transient", "GPU architecture could not be read")
        if architecture not in PROBE_ARCHITECTURES:
            raise PerfDiagnosticError("unsupported", "Modern PERF groups require a Pascal-or-later architecture")
        identity = (id(api), handle, address, api.selected.get("devid"), api.selected.get("subsys"), architecture)
        if self._identity is not None and self._identity != identity:
            raise PerfDiagnosticError("identity", "GPU binding changed; create a fresh PERF reader")
        self._identity = identity
        return api, slot, architecture

    def _transport(self):
        try:
            self.gpu._current_limit_transport_layouts()
        except ValueError as exc:
            from .nvbackend import CurrentLimitUnsupported

            kind = "unsupported" if isinstance(exc, CurrentLimitUnsupported) else "transient"
            raise PerfDiagnosticError(kind, str(exc)) from exc
        transport = getattr(self.gpu, "_current_limit_transport", None)
        if not isinstance(transport, (tuple, list)) or len(transport) != 2:
            raise PerfDiagnosticError("transient", "No captured paired RM getter transport")
        header, fields = transport
        if (
            not isinstance(header, (tuple, list))
            or len(header) != 17
            or any(type(word) is not int or not 0 <= word <= 0xFFFFFFFF for word in header)
            or header[14] != 0x2080A612
            or header[2] != 68 + header[15]
            or not isinstance(fields, dict)
            or set(fields) != _ESCAPE_FIELDS
        ):
            raise PerfDiagnosticError("unsupported", "PERF diagnostics need the validated modern RM transport")
        return tuple(header), dict(fields)

    def _get(self, command: int, size: int, transport, mask: bytes | None = None) -> bytes:
        if (command, size) not in _GET_COMMANDS:
            raise ValueError("Only understood read-only PERF commands are allowed")
        layout = next(item for item in LAYOUTS.values() if command in (item.info_command, item.control_command))
        if command == layout.control_command:
            if not isinstance(mask, bytes) or len(mask) != layout.mask_bytes:
                raise ValueError("PERF CONTROL GET requires an exact INFO mask")
        elif mask is not None:
            raise ValueError("PERF INFO GET does not accept a request mask")
        self._binding()
        header, fields = transport
        packet = (ctypes.c_uint32 * (17 + size // 4))()
        for index, word in enumerate(header):
            packet[index] = word
        packet[2], packet[14], packet[15], packet[16] = ctypes.sizeof(packet), command, size, 0
        if mask is not None:
            ctypes.memmove(ctypes.addressof(packet) + 72, mask, len(mask))
        status = self.gpu._legacy_clk_escape(packet, fields)
        self._binding()
        if status != 0:
            self.gpu._current_limit_transport = None
            self.gpu._current_limit_layout_choice = None
            raise PerfDiagnosticError("transient", f"PERF GET transport failed (NTSTATUS {status})", status)
        if packet[16] != 0:
            kind = "unsupported" if packet[16] in (0x1F, 0x56) else "transient"
            raise PerfDiagnosticError(kind, f"PERF GET 0x{command:08X} rejected (RM 0x{packet[16]:X})", int(packet[16]))
        if packet[2] != ctypes.sizeof(packet) or packet[14] != command or packet[15] != size:
            raise PerfDiagnosticError("malformed", "PERF response changed its command or parameter geometry")
        return bytes(packet)[68:]

    def read(self, groups=None) -> dict:
        names = tuple(LAYOUTS) if groups is None else tuple(groups)
        if len(set(names)) != len(names):
            raise ValueError("PERF groups must not be duplicated")
        for name in names:
            _layout(name)
        report = {"slot": "", "architecture": None, "captured_at": time.time(), "groups": {}}
        for name in names:
            layout = LAYOUTS[name]
            error, snapshot = None, None
            for _attempt in range(1, self.attempts + 1):
                try:
                    with getattr(self.gpu, "_lock", nullcontext()):
                        api, slot, architecture = self._binding()
                        report["slot"], report["architecture"] = slot, architecture
                        if (
                            api._i(layout.info_id, ctypes.c_void_p, ctypes.c_void_p) is None
                            or api._i(layout.control_id, ctypes.c_void_p, ctypes.c_void_p) is None
                        ):
                            raise PerfDiagnosticError("unsupported", "PERF getter exports are unavailable")
                        transport = self._transport()
                        info = self._get(layout.info_command, layout.info_size, transport)
                        mask = _mask(info, layout)
                        control = (
                            self._get(layout.control_command, layout.control_size, transport, mask)
                            if any(mask)
                            else None
                        )
                        snapshot = decode_group(name, info, control, slot=slot, architecture=architecture)
                    error = None
                    break
                except PerfDiagnosticError as exc:
                    error = exc
                except Exception as exc:
                    # Native binding/capture failures can also arrive as
                    # AttributeError, TypeError or RuntimeError (for example a
                    # missing DLL entry point or an interrupted capture). Keep
                    # this group unavailable, discard the borrowed transport,
                    # and let the next bounded attempt establish it afresh.
                    # An unexpected exception is not evidence of unsupported
                    # hardware. Do not catch process-exit/cancellation signals.
                    self.gpu._current_limit_transport = None
                    self.gpu._current_limit_layout_choice = None
                    error = PerfDiagnosticError("transient", f"PERF read failed ({type(exc).__name__}): {exc}")
                if error.kind != "transient":
                    break
            if snapshot is not None:
                self.last_valid[name] = copy.deepcopy(snapshot)
                self.last_errors.pop(name, None)
                result = {
                    "status": "ready" if snapshot["present_count"] else "empty",
                    "snapshot": snapshot,
                    "last_valid": None,
                    "error": None,
                    "attempts": _attempt,
                }
            else:
                self.last_errors[name] = error
                result = {
                    "status": error.kind,
                    "snapshot": None,
                    "last_valid": copy.deepcopy(self.last_valid.get(name)),
                    "error": str(error),
                    "attempts": _attempt,
                }
            report["groups"][name] = result
        return report
