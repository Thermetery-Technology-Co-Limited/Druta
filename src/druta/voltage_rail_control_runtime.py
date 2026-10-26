# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Complete, read-only singleton rail CONTROL packets for peer preservation.

The current policy establishes independently present rails and their control
discriminators. A zero discriminator is valid. The first four signed offsets
must agree with that policy; two more offsets and all remaining returned bytes
are preserved without inferred semantics. V1 is tried only after V2 reports
INCOMPATIBLE_STRUCT_VERSION, never from a driver version or adapter allowlist.
"""

from __future__ import annotations

import copy
import ctypes
import struct
from contextlib import nullcontext

from . import voltage_bios_runtime as voltage
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

EXPORT = 0xA3070DB0
SIZE, ROW_OFFSET, ROW_STRIDE = 0xAC8, 0x48, 0x54
V2, V1 = 0x20AC8, 0x10AC8
_GUARD, _SENTINEL = 64, 0xA5
_UNSUPPORTED = frozenset((-3, -104))


class VoltageRailControlError(RuntimeError):
    pass


def _policy(policy):
    try:
        return voltage.validate_policy(policy)
    except (ValueError, RuntimeError, TypeError) as exc:
        raise VoltageRailControlError(str(exc)) from exc


def _hex(value):
    if not isinstance(value, str) or len(value) != SIZE * 2:
        raise VoltageRailControlError("Rail CONTROL packet has incorrect geometry")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise VoltageRailControlError("Rail CONTROL packet is not hexadecimal") from exc
    if len(raw) != SIZE or raw.hex() != value:
        raise VoltageRailControlError("Rail CONTROL packet needs canonical bounded hexadecimal")
    return raw


def _row(raw, version, index, policy_row):
    if type(version) is not int or version not in (V2, V1):
        raise VoltageRailControlError("Rail CONTROL version is not understood")
    if len(raw) != SIZE or struct.unpack_from("<II", raw, 0) != (version, 1 << int(index)):
        raise VoltageRailControlError("Rail CONTROL changed its version or singleton inventory")
    at = ROW_OFFSET + int(index) * ROW_STRIDE
    if struct.unpack_from("<I", raw, at)[0] != policy_row["control_type"]:
        raise VoltageRailControlError("Rail CONTROL discriminator differs from its independent policy")
    offsets = struct.unpack_from("<iiii", raw, at + 4)
    if offsets != tuple(policy_row["offsets_uv"][key] for key in voltage._OFFSETS):
        raise VoltageRailControlError("Rail CONTROL offsets differ from the captured policy")


def validate_observation(state, policy):
    """Pure exact identity, selected-inventory and raw-packet validation."""
    policy = _policy(policy)
    if not isinstance(state, dict) or set(state) != {"identity", "rails"}:
        raise VoltageRailControlError("Malformed raw rail CONTROL observation")
    if not _same_state(state["identity"], policy["identity"]):
        raise VoltageRailControlError("Raw rail CONTROL and policy identify different adapters")
    if not isinstance(state["rails"], dict) or set(state["rails"]) != set(policy["rails"]):
        raise VoltageRailControlError("Raw rail CONTROL must preserve exactly the independently present policy rails")
    for index, row in state["rails"].items():
        if not isinstance(row, dict) or set(row) != {"version", "payload_hex"}:
            raise VoltageRailControlError("Malformed raw rail CONTROL row")
        _row(_hex(row["payload_hex"]), row["version"], index, policy["rails"][index])
    return copy.deepcopy(state)


def _binding(reader, expected):
    identity = voltage._identity(reader)
    if not _same_state(identity, expected):
        raise PerfDiagnosticError("identity", "Rail CONTROL target differs from the captured policy")
    return identity


def _guarded():
    storage = (ctypes.c_ubyte * (SIZE + 2 * _GUARD))()
    ctypes.memset(ctypes.addressof(storage), _SENTINEL, len(storage))
    data = (ctypes.c_ubyte * SIZE).from_buffer(storage, _GUARD)
    ctypes.memset(ctypes.addressof(data), 0, SIZE)
    return storage, data


def _guards(storage):
    if any(v != _SENTINEL for v in storage[:_GUARD]) or any(v != _SENTINEL for v in storage[-_GUARD:]):
        raise PerfDiagnosticError("malformed", "Rail CONTROL GET exceeded its guarded allocation")


def _get(gpu, reader, index, version, policy):
    if index not in policy["rails"] or type(index) is not str or index not in ("0", "1"):
        raise VoltageRailControlError("Only independently present rails may be requested")
    if type(version) is not int or version not in (V2, V1):
        raise VoltageRailControlError("Only understood rail CONTROL GET versions are allowed")
    identity = _binding(reader, policy["identity"])
    getter = getattr(gpu.nvapi, "VoltRailsCtlGet", None)
    if getter is None:
        raise PerfDiagnosticError("unsupported", "Rail CONTROL GET export is unavailable")
    storage, data = _guarded()
    struct.pack_into("<II", data, 0, version, 1 << int(index))
    try:
        status = getter(gpu.nvapi.gpu, ctypes.byref(data))
    finally:
        _guards(storage)
    if _binding(reader, policy["identity"]) != identity:
        raise PerfDiagnosticError("identity", "Rail CONTROL target changed during GET")
    if type(status) is not int:
        raise PerfDiagnosticError("transient", "Rail CONTROL returned no understood status")
    if status == -9:
        return None
    if status != 0:
        raise PerfDiagnosticError(
            "unsupported" if status in _UNSUPPORTED else "transient",
            f"Rail {index} CONTROL GET failed with NVAPI status {status}",
        )
    raw = bytes(data)
    _row(raw, version, index, policy["rails"][index])
    return {"version": version, "payload_hex": raw.hex()}


def _once(gpu, reader, policy):
    identity = _binding(reader, policy["identity"])
    rails = {}
    for index in sorted(policy["rails"], key=int):
        row = _get(gpu, reader, index, V2, policy)
        if row is None:
            row = _get(gpu, reader, index, V1, policy)
        if row is None:
            raise PerfDiagnosticError("unsupported", f"Rail {index} rejects both understood CONTROL layouts")
        rails[index] = row
    _binding(reader, identity)
    return {"identity": identity, "rails": rails}


def read(gpu, policy):
    """Two stable full rounds, one transient retry; no unrelated rail probes."""
    try:
        policy = _policy(policy)
    except VoltageRailControlError as exc:
        return None, str(exc)
    reader, error = PerfDiagnosticsReader(gpu), None
    for _ in range(2):
        try:
            with getattr(gpu, "_lock", nullcontext()):
                first, second = _once(gpu, reader, policy), _once(gpu, reader, policy)
                if not _same_state(first, second):
                    raise PerfDiagnosticError("transient", "Raw rail CONTROL packets changed between fresh reads")
                return validate_observation(second, policy), None
        except PerfDiagnosticError as exc:
            error = str(exc)
            if exc.kind != "transient":
                break
        except VoltageRailControlError as exc:
            error = str(exc)
            break
        except Exception as exc:
            error = f"Rail CONTROL read failed ({type(exc).__name__}): {exc}"
    return None, error or "Raw rail CONTROL readback is unavailable"
