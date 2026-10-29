# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""VFE definition reads, projections, and a selected type6 upper-bound writer.

The E8192 INFO/CONTROL getters return loaded definitions, not evaluated rail
voltages. BIOS type 1 becomes runtime type 3, and type 7 becomes runtime type 6,
at the same ROM ordinal. Constant, upper-only and two-bound receipt formats
remain separate. Other packet bytes survive apply and undo unchanged.
The caller binds the selected ordinal, terms and dependency graph to the ROM
and checks effective policy separately.
"""

from __future__ import annotations

import copy
import ctypes
import math
import struct
from collections.abc import Mapping
from contextlib import nullcontext

from .perf_diagnostics import PROBE_ARCHITECTURES, PerfDiagnosticError, PerfDiagnosticsReader
from .registry_flags import _same_state

HEADER, CAPACITY, MASK_BYTES = 0x408, 8192, 1024
INFO_SIZE, CONTROL_SIZE = 0xE0408, 0x100408
INFO_STRIDE, CONTROL_STRIDE = 0x70, 0x80
LAYOUTS = {
    "info": (0x2080A0B5, INFO_SIZE, INFO_STRIDE, 0x8D49471C),
    "control": (0x2080A0B6, CONTROL_SIZE, CONTROL_STRIDE, 0x4C75C9FE),
}
_LAYOUT = {
    "info_command": "0x2080A0B5",
    "control_command": "0x2080A0B6",
    "info_bytes": INFO_SIZE,
    "control_bytes": CONTROL_SIZE,
    "header_bytes": HEADER,
    "info_row_bytes": INFO_STRIDE,
    "control_row_bytes": CONTROL_STRIDE,
    "capacity": CAPACITY,
    "mask_bytes": MASK_BYTES,
}
_ENVELOPE = {0: 0x4E564441, 1: 0x10002, 3: 0x4E562A2A, 4: 0x0500002B}
_ESCAPE_FIELDS = {"hAdapter", "hDevice", "Type", "Flags", "hContext"}
_GUARD, _SENTINEL = 64, 0xA5
_SELECTION_FIELDS = {"index", "before_uv", "after_uv", "next_index", "variable", "output_type"}


class VfeEquationError(RuntimeError):
    pass


def _integer(value, low, high, message):
    if type(value) is not int or not low <= value <= high:
        raise VfeEquationError(message)
    return value


def _identity(value):
    from .nvbackend import parse_slot

    if not isinstance(value, dict) or set(value) != {"slot", "architecture", "device_id", "subsystem_id"}:
        raise VfeEquationError("VFE equations need an exact paired adapter identity")
    address = parse_slot(value["slot"]) if isinstance(value["slot"], str) else None
    if address is None or address[0] != 0 or address[1] > 255 or address[2] > 31 or address[3] > 7:
        raise VfeEquationError("Invalid VFE adapter PCI address")
    for key, high in (("architecture", 0xFFFFFFFF), ("device_id", 0xFFFF), ("subsystem_id", 0xFFFFFFFF)):
        _integer(value[key], 0, high, "Invalid VFE adapter identity")
    if value["architecture"] not in PROBE_ARCHITECTURES:
        raise VfeEquationError("VFE equation GET needs a supported modern architecture")
    return dict(value)


def _binding(reader):
    api, slot, architecture = reader._binding()
    return _identity(
        {
            "slot": slot,
            "architecture": architecture,
            "device_id": api.selected.get("devid"),
            "subsystem_id": api.selected.get("subsys"),
        }
    )


def _hex(value, size):
    if not isinstance(value, str) or len(value) != size * 2:
        raise VfeEquationError("VFE equation packet has incorrect geometry")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise VfeEquationError("VFE equation packet is not hexadecimal") from exc
    if len(raw) != size or raw.hex() != value:
        raise VfeEquationError("VFE equation packet needs canonical bounded hexadecimal")
    return raw


def _packets(info, control):
    if len(info) != INFO_SIZE or len(control) != CONTROL_SIZE:
        raise VfeEquationError("VFE equation INFO/CONTROL geometry is not understood")
    if info[0] != control[0] or info[4 : 4 + MASK_BYTES] != control[4 : 4 + MASK_BYTES]:
        raise VfeEquationError("VFE equation group or INFO/CONTROL inventories disagree")
    return int.from_bytes(info[4 : 4 + MASK_BYTES], "little")


def _shape(observation):
    keys = {"schema", "identity", "stable_reads", "info_hex", "control_hex"}
    if not isinstance(observation, dict) or set(observation) != keys:
        raise VfeEquationError("Malformed VFE equation observation")
    if type(observation["schema"]) is not int or observation["schema"] != 1:
        raise VfeEquationError("Unknown VFE equation observation schema")
    if type(observation["stable_reads"]) is not int or observation["stable_reads"] != 2:
        raise VfeEquationError("Two stable fresh VFE equation reads are required")
    _identity(observation["identity"])
    info, control = _hex(observation["info_hex"], INFO_SIZE), _hex(observation["control_hex"], CONTROL_SIZE)
    return info, control, _packets(info, control)


def validate_observation(observation):
    """Validate the pinned schema/geometry and return an independent copy."""
    _shape(observation)
    return copy.deepcopy(observation)


def _guarded(size):
    storage = (ctypes.c_ubyte * (size + 2 * _GUARD))()
    ctypes.memset(ctypes.addressof(storage), _SENTINEL, len(storage))
    data = (ctypes.c_ubyte * size).from_buffer(storage, _GUARD)
    ctypes.memset(ctypes.addressof(data), 0, size)
    return storage, data


def _guards(storage):
    if any(v != _SENTINEL for v in storage[:_GUARD]) or any(v != _SENTINEL for v in storage[-_GUARD:]):
        raise PerfDiagnosticError("malformed", "VFE equation GET exceeded its guarded allocation")


def _get(gpu, reader, name, mask=None):
    if name not in LAYOUTS:
        raise VfeEquationError("Only understood VFE equation GETs are allowed")
    command, size, stride, export = LAYOUTS[name]
    if size != HEADER + CAPACITY * stride:
        raise VfeEquationError("Unknown VFE equation parameter geometry")
    if name == "control":
        if not isinstance(mask, bytes) or len(mask) != MASK_BYTES:
            raise VfeEquationError("VFE CONTROL GET needs the exact INFO inventory")
    elif mask is not None:
        raise VfeEquationError("VFE INFO GET does not accept a request mask")
    identity = _binding(reader)
    if gpu.nvapi._i(export, ctypes.c_void_p, ctypes.c_void_p) is None:
        raise PerfDiagnosticError("unsupported", "VFE equation GET export is unavailable")
    header, fields = reader._transport()
    if (
        not isinstance(header, (tuple, list))
        or len(header) != 17
        or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in header)
        or any(header[i] != v for i, v in _ENVELOPE.items())
        or not isinstance(fields, dict)
        or set(fields) != _ESCAPE_FIELDS
        or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values())
    ):
        raise PerfDiagnosticError("malformed", "VFE equations need the understood paired RM envelope")
    storage, data = _guarded(68 + size)
    packet = (ctypes.c_uint32 * ((68 + size) // 4)).from_buffer(data)
    for i, word in enumerate(header):
        packet[i] = word
    packet[2], packet[14], packet[15], packet[16] = 68 + size, command, size, 0
    packet[5] = packet[11] = 0
    expected = tuple(packet[:16])
    if mask is not None:
        data[72 : 72 + MASK_BYTES] = mask
    try:
        status = gpu._legacy_clk_escape(packet, dict(fields))
    finally:
        _guards(storage)
    if _binding(reader) != identity:
        raise PerfDiagnosticError("identity", "VFE equation target changed during GET")
    if type(status) is not int or status != 0:
        raise PerfDiagnosticError("transient", f"VFE equation transport failed: {status}")
    if any(packet[i] != v for i, v in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
        raise PerfDiagnosticError("malformed", "VFE equation GET changed its envelope or geometry")
    if packet[11] != 1:
        raise PerfDiagnosticError("transient", "VFE equation GET has no completed response")
    if packet[16]:
        raise PerfDiagnosticError(
            "unsupported" if packet[16] in (0x1F, 0x56) else "transient",
            f"VFE equation GET returned RM status {packet[16]}",
        )
    return bytes(data)[68:]


def _once(gpu, reader):
    identity = _binding(reader)
    info = _get(gpu, reader, "info")
    control = _get(gpu, reader, "control", info[4 : 4 + MASK_BYTES])
    _packets(info, control)
    if _binding(reader) != identity:
        raise PerfDiagnosticError("identity", "VFE equation target changed between getters")
    return {"identity": identity, "info_hex": info.hex(), "control_hex": control.hex()}


def read(gpu):
    """Return two identical complete native GET pairs, with one transient retry.

    Unknown or unavailable individual equation types remain opaque. They do not
    erase an independently understood selected constant. Empty inventories are
    readable diagnostics; no constant selection can bind to an absent row.
    """
    reader, error = PerfDiagnosticsReader(gpu), None
    for attempt in range(2):
        try:
            with getattr(gpu, "_lock", nullcontext()):
                first, second = _once(gpu, reader), _once(gpu, reader)
                if not _same_state(first, second):
                    raise PerfDiagnosticError("transient", "VFE equation definitions changed between fresh reads")
                observation = {"schema": 1, "stable_reads": 2, **second}
                _shape(observation)
                return observation, None
        except PerfDiagnosticError as exc:
            error = str(exc)
            if exc.kind != "transient":
                break
        except VfeEquationError as exc:
            error = str(exc)
            break
        except Exception as exc:
            error = f"VFE equation read failed ({type(exc).__name__}): {exc}"
        if attempt == 0:
            for name in ("_current_limit_transport", "_current_limit_layout_choice"):
                try:
                    setattr(gpu, name, None)
                except Exception:
                    pass
    return None, error or "VFE equation definitions are unavailable"


def _constant(info, control, mask, index):
    _integer(index, 0, CAPACITY - 1, "VFE equation index is outside the understood inventory")
    if not mask & (1 << index):
        raise VfeEquationError("Selected VFE equation is absent from the current inventory")
    at, ct = HEADER + index * INFO_STRIDE, HEADER + index * CONTROL_STRIDE
    if (info[at], info[at + 0x6C], control[ct], control[ct + 0x7C]) != (3, 3, 3, 3):
        raise VfeEquationError("Selected VFE equation is not an understood type3 constant")
    if info[at + 0x6E] != 2:
        raise VfeEquationError("Selected VFE equation output is not microvolts")
    lo, hi = struct.unpack_from("<ff", control, ct + 4)
    c0, c1, c2 = struct.unpack_from("<fff", control, ct + 0x10)
    if not all(math.isfinite(v) for v in (lo, hi, c0, c1, c2)) or lo != hi or lo != c0 or c1 != 0 or c2 != 0:
        raise VfeEquationError("Selected VFE equation does not have equal constant bounds and zero higher coefficients")
    if not c0.is_integer():
        raise VfeEquationError("Selected VFE constant is not whole microvolts")
    value = _uv(int(c0))
    return {
        "index": index,
        "value_uv": value,
        "next_index": struct.unpack_from("<H", info, at + 2)[0],
        "variable": info[at + 0x6D],
        "output_type": info[at + 0x6E],
    }


def constant_row(observation, index):
    """Return the typed constant and its immutable ROM-binding metadata."""
    return _constant(*_shape(observation), index)


def _uv(value):
    _integer(value, -(2**31), 2**31 - 1, "VFE constant requires whole signed-32-bit microvolts")
    if struct.unpack("<f", struct.pack("<f", value))[0] != value:
        raise VfeEquationError("VFE constant is not exactly representable as float32 microvolts")
    return value


def _selections(selections):
    if not isinstance(selections, Mapping):
        raise VfeEquationError("Malformed selected VFE constants")
    result, seen = {}, set()
    for key, row in selections.items():
        if not isinstance(key, str) or not key or not isinstance(row, dict) or set(row) != _SELECTION_FIELDS:
            raise VfeEquationError("Malformed selected VFE constant binding")
        index = _integer(row["index"], 0, CAPACITY - 1, "Invalid selected VFE equation index")
        if index in seen:
            raise VfeEquationError("Multiple selected keys alias the same VFE equation")
        seen.add(index)
        _uv(row["before_uv"])
        _uv(row["after_uv"])
        nxt = _integer(row["next_index"], 0, 0xFFFF, "Invalid selected VFE next-equation index")
        if nxt >= CAPACITY and nxt != 0xFFFF:
            raise VfeEquationError("Selected VFE next-equation index exceeds capacity")
        _integer(row["variable"], 0, 255, "Invalid selected VFE variable index")
        if type(row["output_type"]) is not int or row["output_type"] != 2:
            raise VfeEquationError("Selected VFE output must be microvolts")
        result[key] = dict(row)
    return result


def project_constants(observation, selections):
    """Project only selected lo/hi/c0 float words; preserve every other byte."""
    selected = _selections(selections)
    info, control, mask = _shape(observation)
    after = bytearray(control)
    for row in selected.values():
        actual = _constant(info, control, mask, row["index"])
        expected = {
            "index": row["index"],
            "value_uv": row["before_uv"],
            **{k: row[k] for k in ("next_index", "variable", "output_type")},
        }
        if not _same_state(actual, expected):
            raise VfeEquationError("Selected VFE constant does not match its current ROM binding")
        if row["next_index"] != 0xFFFF and not mask & (1 << row["next_index"]):
            raise VfeEquationError("Selected VFE constant links to an absent equation")
        for offset in (4, 8, 0x10):
            struct.pack_into("<f", after, HEADER + row["index"] * CONTROL_STRIDE + offset, row["after_uv"])
    result = copy.deepcopy(observation)
    result["control_hex"] = after.hex()
    return result


def contracts(observation, selections):
    """Save exact definitions before/after a caller-bound ROM constant edit."""
    selected = _selections(selections)
    if not selected:
        return {}
    after = project_constants(observation, selected)
    return {"schema": 1, "selected": selected, "before": copy.deepcopy(observation), "after": after}


def validate_contracts(selections, saved):
    """Pure structural and projection validation, including before recovery."""
    selected = _selections(selections)
    if not selected and isinstance(saved, dict) and not saved:
        return
    if not isinstance(saved, dict) or set(saved) != {"schema", "selected", "before", "after"}:
        raise VfeEquationError("Malformed VFE constant contract")
    if type(saved["schema"]) is not int or saved["schema"] != 1 or not selected:
        raise VfeEquationError("Unknown or empty VFE constant contract")
    if not _same_state(selected, _selections(saved["selected"])):
        raise VfeEquationError("Selected VFE constants disagree with the saved contract")
    expected = project_constants(saved["before"], selected)
    _shape(saved["after"])
    if not _same_state(expected, saved["after"]):
        raise VfeEquationError("Saved VFE projection does not preserve all unselected definition bytes")


def verify(observation, saved, *, restore=False):
    """Verify complete native definitions and identity, never hardware voltage."""
    if isinstance(saved, dict) and not saved:
        return []
    if not isinstance(saved, dict):
        raise VfeEquationError("Malformed VFE constant contract")
    validate_contracts(saved.get("selected"), saved)
    _shape(observation)
    if not _same_state(observation, saved["before" if restore else "after"]):
        raise VfeEquationError("Loaded VFE definitions or preserved peers differ from the exact request")
    return list(saved["selected"])


_CLAMP_SELECTION_FIELDS = {
    "index",
    "lower_uv",
    "before_uv",
    "after_uv",
    "next_index",
    "variable",
    "output_type",
    "variables",
    "coefficients",
}


def _clamp_uv(value, *, positive=False):
    _integer(value, 1 if positive else 0, 2**31 - 1, "VFE voltage clamp requires whole nonnegative microvolts")
    if struct.unpack("<f", struct.pack("<f", value))[0] != value:
        raise VfeEquationError("VFE voltage clamp is not exactly representable as float32 microvolts")
    return value


def _clamp_term(term):
    if not isinstance(term, dict) or set(term) != {"type", "value"}:
        raise VfeEquationError("Malformed VFE polynomial descriptor")
    kind = _integer(term["type"], 1, 3, "Unrecognized VFE polynomial descriptor type")
    value = _integer(
        term["value"],
        0,
        {1: 0xFFFFFFFF, 2: 254, 3: CAPACITY - 1}[kind],
        "VFE polynomial descriptor value exceeds its understood range",
    )
    if kind == 1 and not math.isfinite(struct.unpack("<f", struct.pack("<I", value))[0]):
        raise VfeEquationError("VFE polynomial literal is not finite")
    return {"type": kind, "value": value}


def _clamp_terms(variables, coefficients):
    if (
        not isinstance(variables, list)
        or not isinstance(coefficients, list)
        or len(variables) not in (1, 2, 3)
        or len(coefficients) != {1: 3, 2: 6, 3: 10}[len(variables)]
    ):
        raise VfeEquationError("VFE polynomial variable/coefficient counts are not understood")
    return ([_clamp_term(t) for t in variables], [_clamp_term(t) for t in coefficients])


def _upper_clamp(info, control, mask, index):
    _integer(index, 0, CAPACITY - 1, "VFE equation index is outside the understood inventory")
    if not mask & (1 << index):
        raise VfeEquationError("Selected VFE equation is absent from the current inventory")
    at, ct = HEADER + index * INFO_STRIDE, HEADER + index * CONTROL_STRIDE
    if (info[at], info[at + 0x6C], control[ct], control[ct + 0x7C]) != (6, 6, 6, 6):
        raise VfeEquationError("Selected VFE equation is not an understood type6 upper clamp")
    if info[at + 0x6E] != 2:
        raise VfeEquationError("Selected VFE clamp output is not microvolts")
    lower, upper = struct.unpack_from("<ff", control, ct + 4)
    if not all(math.isfinite(v) and v.is_integer() for v in (lower, upper)):
        raise VfeEquationError("VFE voltage clamp bounds must be finite whole microvolts")
    lower, upper = _clamp_uv(int(lower)), _clamp_uv(int(upper), positive=True)
    if lower > upper:
        raise VfeEquationError("VFE upper clamp is below its lower bound")
    variables, coefficients = control[ct + 0x78], control[ct + 0x79]
    if variables not in (1, 2, 3) or coefficients != {1: 3, 2: 6, 3: 10}[variables]:
        raise VfeEquationError("VFE polynomial variable/coefficient counts are not understood")

    def terms(start, count):
        result = []
        for i in range(count):
            pos = ct + start + 8 * i
            kind = control[pos]
            if kind not in (1, 2, 3):
                raise VfeEquationError("Unrecognized VFE polynomial descriptor type")
            value = struct.unpack_from({1: "<I", 2: "<B", 3: "<H"}[kind], control, pos + 4)[0]
            term = _clamp_term({"type": kind, "value": value})
            if kind == 3 and (value == index or not mask & (1 << value)):
                raise VfeEquationError("VFE polynomial term references its own or an absent equation")
            result.append(term)
        return result

    next_index = struct.unpack_from("<H", info, at + 2)[0]
    if next_index != 0xFFFF and (next_index >= CAPACITY or next_index == index or not mask & (1 << next_index)):
        raise VfeEquationError("VFE clamp links to its own or an absent equation")
    return {
        "index": index,
        "lower_uv": lower,
        "upper_uv": upper,
        "next_index": next_index,
        "variable": info[at + 0x6D],
        "output_type": info[at + 0x6E],
        "variables": terms(0x10, variables),
        "coefficients": terms(0x28, coefficients),
    }


def upper_clamp_row(observation, index):
    """Read a type6 voltage clamp and typed polynomial descriptors.

    Values are loaded definitions. This function does not evaluate the
    polynomial or infer the order of clamping and next-list accumulation.
    """
    return _upper_clamp(*_shape(observation), index)


def _clamp_selections(selections):
    if not isinstance(selections, Mapping):
        raise VfeEquationError("Malformed selected VFE upper clamps")
    result, seen = {}, set()
    for key, row in selections.items():
        if not isinstance(key, str) or not key or not isinstance(row, dict) or set(row) != _CLAMP_SELECTION_FIELDS:
            raise VfeEquationError("Malformed selected VFE upper-clamp binding")
        index = _integer(row["index"], 0, CAPACITY - 1, "Invalid selected VFE equation index")
        if index in seen:
            raise VfeEquationError("Multiple selected keys alias the same VFE equation")
        seen.add(index)
        lower = _clamp_uv(row["lower_uv"])
        if min(_clamp_uv(row["before_uv"], positive=True), _clamp_uv(row["after_uv"], positive=True)) < lower:
            raise VfeEquationError("Selected VFE upper clamp is below its preserved lower bound")
        nxt = _integer(row["next_index"], 0, 0xFFFF, "Invalid selected VFE next-equation index")
        if nxt >= CAPACITY and nxt != 0xFFFF:
            raise VfeEquationError("Selected VFE next-equation index exceeds capacity")
        _integer(row["variable"], 0, 255, "Invalid selected VFE variable index")
        if type(row["output_type"]) is not int or row["output_type"] != 2:
            raise VfeEquationError("Selected VFE output must be microvolts")
        variables, coefficients = _clamp_terms(row["variables"], row["coefficients"])
        result[key] = {**row, "variables": variables, "coefficients": coefficients}
    return result


def project_upper_clamps(observation, selections):
    """Change only selected type6 upper float words, preserving all peers.

    The caller must bind the supplied metadata to the current ROM and complete
    understood dependency graph. Unselected unknown row types stay opaque.
    """
    selected = _clamp_selections(selections)
    info, control, mask = _shape(observation)
    after = bytearray(control)
    for row in selected.values():
        actual = _upper_clamp(info, control, mask, row["index"])
        expected = {k: value for k, value in row.items() if k not in ("before_uv", "after_uv")}
        expected["upper_uv"] = row["before_uv"]
        if not _same_state(actual, expected):
            raise VfeEquationError("Selected VFE upper clamp does not match its current ROM binding")
        struct.pack_into("<f", after, HEADER + row["index"] * CONTROL_STRIDE + 8, row["after_uv"])
    result = copy.deepcopy(observation)
    result["control_hex"] = after.hex()
    return result


def clamp_contracts(observation, selections):
    """Save the exact upper-only projection separately from constant receipts."""
    selected = _clamp_selections(selections)
    if not selected:
        return {}
    after = project_upper_clamps(observation, selected)
    return {
        "schema": 1,
        "kind": "upper_clamps",
        "selected": selected,
        "before": copy.deepcopy(observation),
        "after": after,
    }


def validate_clamp_contracts(selections, saved):
    """Pure upper-clamp receipt validation, usable before cleanup/recovery."""
    selected = _clamp_selections(selections)
    if not selected and isinstance(saved, dict) and not saved:
        return
    if not isinstance(saved, dict) or set(saved) != {"schema", "kind", "selected", "before", "after"}:
        raise VfeEquationError("Malformed VFE upper-clamp contract")
    if type(saved["schema"]) is not int or saved["schema"] != 1 or saved["kind"] != "upper_clamps" or not selected:
        raise VfeEquationError("Unknown or empty VFE upper-clamp contract")
    if not _same_state(selected, _clamp_selections(saved["selected"])):
        raise VfeEquationError("Selected VFE upper clamps disagree with the saved contract")
    expected = project_upper_clamps(saved["before"], selected)
    _shape(saved["after"])
    if not _same_state(expected, saved["after"]):
        raise VfeEquationError("Saved VFE upper-clamp projection does not preserve all unselected definition bytes")


def verify_clamps(observation, saved, *, restore=False):
    """Verify full loaded definitions and adapter identity; not physical voltage."""
    if isinstance(saved, dict) and not saved:
        return []
    if not isinstance(saved, dict):
        raise VfeEquationError("Malformed VFE upper-clamp contract")
    validate_clamp_contracts(saved.get("selected"), saved)
    _shape(observation)
    if not _same_state(observation, saved["before" if restore else "after"]):
        raise VfeEquationError("Loaded VFE upper clamp or preserved peers differ from the exact request")
    return list(saved["selected"])


def write_upper_clamps(gpu, saved, *, restore=False, on_attempt=None):
    """Write one saved type6 row and verify the full fresh definition state.

    The caller owns compensation after an attempted write, including failures.
    The readback verifies loaded definitions, not evaluated or physical voltage.
    """
    if not isinstance(saved, dict):
        raise VfeEquationError("Malformed VFE upper-clamp contract")
    validate_clamp_contracts(saved.get("selected"), saved)
    if len(saved.get("selected", {})) != 1 or type(restore) is not bool:
        raise VfeEquationError("A live VFE write requires exactly one upper clamp")
    return _write_selected_equation(gpu, saved, restore=restore, on_attempt=on_attempt)


def write_constants(gpu, saved, *, restore=False, on_attempt=None):
    """Write one saved type3 constant and verify the full loaded state.

    The caller owns compensation after an attempted write. Readback verifies
    definitions, not evaluated or physical voltage.
    """
    if not isinstance(saved, dict):
        raise VfeEquationError("Malformed VFE constant contract")
    validate_contracts(saved.get("selected"), saved)
    if len(saved.get("selected", {})) != 1 or type(restore) is not bool:
        raise VfeEquationError("A live VFE write requires exactly one constant")
    return _write_selected_equation(gpu, saved, restore=restore, on_attempt=on_attempt)


def write_voltage_bounds(gpu, saved, *, restore=False, on_attempt=None):
    """Write one validated type6 two-bound projection and verify loaded state."""
    if not isinstance(saved, dict):
        raise VfeEquationError("Malformed VFE voltage-bound contract")
    validate_voltage_bounds_contracts(saved.get("selected"), saved)
    if len(saved.get("selected", {})) != 1 or type(restore) is not bool:
        raise VfeEquationError("A live VFE write requires exactly one voltage-bound row")
    return _write_selected_equation(gpu, saved, restore=restore, on_attempt=on_attempt)


def _write_selected_equation(gpu, saved, *, restore, on_attempt):
    if on_attempt is not None and not callable(on_attempt):
        raise VfeEquationError("VFE write attempt marker must be callable")
    index = next(iter(saved["selected"].values()))["index"]
    with getattr(gpu, "_lock", nullcontext()):
        current, error = read(gpu)
        if error or current is None:
            raise VfeEquationError(error or "VFE equation preflight failed")
        _shape(current)
        before = saved["before"]
        if current["identity"] != before["identity"] or current["info_hex"] != before["info_hex"]:
            raise VfeEquationError("VFE target or INFO definitions changed before write")
        if not restore and not _same_state(current, before):
            raise VfeEquationError("VFE definitions changed before apply")
        current_control = bytearray.fromhex(current["control_hex"])
        original = bytes.fromhex(before["control_hex"])
        offset = HEADER + index * CONTROL_STRIDE
        expected = copy.deepcopy(current)
        source = original if restore else bytes.fromhex(saved["after"]["control_hex"])
        current_control[offset : offset + CONTROL_STRIDE] = source[offset : offset + CONTROL_STRIDE]
        expected["control_hex"] = current_control.hex()
        payload = bytearray(current_control)
        payload[4 : 4 + MASK_BYTES] = bytes(MASK_BYTES)
        payload[4 + index // 8] = 1 << (index % 8)

        reader = PerfDiagnosticsReader(gpu)
        identity = _binding(reader)
        if identity != before["identity"]:
            raise VfeEquationError("VFE target changed before write")
        header, fields = reader._transport()
        if (
            not isinstance(header, (tuple, list))
            or len(header) != 17
            or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in header)
            or any(header[i] != v for i, v in _ENVELOPE.items())
            or not isinstance(fields, dict)
            or set(fields) != _ESCAPE_FIELDS
            or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values())
        ):
            raise VfeEquationError("VFE write needs the understood paired RM envelope")
        storage, data = _guarded(68 + CONTROL_SIZE)
        packet = (ctypes.c_uint32 * ((68 + CONTROL_SIZE) // 4)).from_buffer(data)
        for i, word in enumerate(header):
            packet[i] = word
        packet[2], packet[14], packet[15], packet[16] = 68 + CONTROL_SIZE, 0x2080E0B7, CONTROL_SIZE, 0
        packet[5] = packet[11] = 0
        routing = tuple(packet[:16])
        data[68:] = payload
        try:
            if on_attempt is not None:
                on_attempt()
            status = gpu._legacy_clk_escape(packet, dict(fields))
        finally:
            _guards(storage)
        if _binding(reader) != identity:
            raise VfeEquationError("VFE target changed during write")
        if type(status) is not int or status != 0:
            raise VfeEquationError(f"VFE equation write transport failed: {status}")
        if any(packet[i] != v for i, v in enumerate(routing) if i not in (5, 11)) or packet[5] not in (0, 1):
            raise VfeEquationError("VFE equation write changed its routing or geometry")
        if packet[11] != 1 or packet[16] != 0:
            raise VfeEquationError(f"VFE equation write did not complete successfully (RM {packet[16]})")
        observed, error = read(gpu)
        if error or observed is None:
            raise VfeEquationError(error or "VFE equation write readback failed")
        if not _same_state(observed, expected):
            raise VfeEquationError("VFE equation write did not match the exact projected state")
        return observed


def _bounds_selections(selections):
    if not isinstance(selections, Mapping):
        raise VfeEquationError("Malformed selected VFE voltage bounds")
    before = {}
    for key, row in selections.items():
        if not isinstance(row, dict) or set(row) != _CLAMP_SELECTION_FIELDS | {"after_lower_uv"}:
            raise VfeEquationError("Malformed selected VFE voltage-bound binding")
        # Reuse the upper-only validator for captured metadata and aliases.
        # Its preserved-lower restriction applies to that API, whereas this
        # operation can move both bounds below the original lower bound.
        before[key] = {k: v for k, v in row.items() if k != "after_lower_uv"}
        before[key]["after_uv"] = row["before_uv"]
    result = _clamp_selections(before)
    for key, row in result.items():
        row["after_lower_uv"] = _clamp_uv(selections[key]["after_lower_uv"])
        row["after_uv"] = _clamp_uv(selections[key]["after_uv"], positive=True)
        if row["after_lower_uv"] > row["after_uv"]:
            raise VfeEquationError("Selected VFE upper bound is below its new lower bound")
    return result


def project_voltage_bounds(observation, selections):
    """Project two selected type6 floats; retain all terms, links and peers.

    Equal target bounds fix the loaded equation range. Evaluation of linked
    equations, offsets and device quantization belongs to the caller's policy
    contract. An upper-only member uses after_lower_uv equal to lower_uv.
    """
    selected = _bounds_selections(selections)
    info, control, mask = _shape(observation)
    after = bytearray(control)
    for row in selected.values():
        actual = _upper_clamp(info, control, mask, row["index"])
        expected = {k: v for k, v in row.items() if k not in ("before_uv", "after_uv", "after_lower_uv")}
        expected["upper_uv"] = row["before_uv"]
        if not _same_state(actual, expected):
            raise VfeEquationError("Selected VFE voltage bounds do not match their current ROM binding")
        struct.pack_into(
            "<ff", after, HEADER + row["index"] * CONTROL_STRIDE + 4, row["after_lower_uv"], row["after_uv"]
        )
    result = copy.deepcopy(observation)
    result["control_hex"] = after.hex()
    return result


def voltage_bounds_contracts(observation, selections):
    """Save a two-bound projection without changing legacy clamp receipts."""
    selected = _bounds_selections(selections)
    if not selected:
        return {}
    after = project_voltage_bounds(observation, selected)
    return {
        "schema": 1,
        "kind": "voltage_bounds",
        "selected": selected,
        "before": copy.deepcopy(observation),
        "after": after,
    }


def validate_voltage_bounds_contracts(selections, saved):
    """Pure exact-projection validation, including before restoration."""
    selected = _bounds_selections(selections)
    if not selected and isinstance(saved, dict) and not saved:
        return
    if not isinstance(saved, dict) or set(saved) != {"schema", "kind", "selected", "before", "after"}:
        raise VfeEquationError("Malformed VFE voltage-bound contract")
    if type(saved["schema"]) is not int or saved["schema"] != 1 or saved["kind"] != "voltage_bounds" or not selected:
        raise VfeEquationError("Unknown or empty VFE voltage-bound contract")
    if not _same_state(selected, _bounds_selections(saved["selected"])):
        raise VfeEquationError("Selected VFE voltage bounds disagree with the saved contract")
    expected = project_voltage_bounds(saved["before"], selected)
    _shape(saved["after"])
    if not _same_state(expected, saved["after"]):
        raise VfeEquationError("Saved VFE voltage-bound projection does not preserve all unselected definition bytes")


def verify_voltage_bounds(observation, saved, *, restore=False):
    """Verify loaded bounds and every peer byte, not physical rail voltage."""
    if isinstance(saved, dict) and not saved:
        return []
    if not isinstance(saved, dict):
        raise VfeEquationError("Malformed VFE voltage-bound contract")
    validate_voltage_bounds_contracts(saved.get("selected"), saved)
    _shape(observation)
    if not _same_state(observation, saved["before" if restore else "after"]):
        raise VfeEquationError("Loaded VFE voltage bounds or preserved peers differ from the exact request")
    return list(saved["selected"])
