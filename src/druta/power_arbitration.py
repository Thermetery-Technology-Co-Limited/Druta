# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only interpretation of an understood RM power-policy status prefix.

The caller supplies buffers it already read. This module neither imports a GPU
backend nor changes hardware. Unsupported packet geometry affects these extra
diagnostics only; it must not remove an otherwise working power control.

Evidence: experiments/registry-overrides/vbios-fields/policy-layers-contract.json.
Host CAB180 / GSP 1623D04 produce the common status prefix. Host 907630 / GSP
16590B4 define the 0x50-byte arbitration structure. Addresses document one traced
binary, never locate data or select compatibility at runtime.
"""

import struct
from collections.abc import Mapping, Sequence

_INFO = 0x2080A618
_STATUS = 0x2080A619
_CONTROL = 0x2080A61A
_UINT32_MAX = (1 << 32) - 1
_GEOMETRY = {
    "info": (0xCC, 0x104, _INFO, 20512),
    "status": (0x9C, 0x1730, _STATUS, 397048),
    "control": (0x14, 0xC4, _CONTROL, 13876),
}
_METHODS = {0: "minimum", 1: "maximum", 2: "sum"}


class _PacketError(ValueError):
    pass


def _unavailable(reason):
    return {"available": False, "reason": reason}


def _geometry(layout):
    if not isinstance(layout, Mapping) or not isinstance(layout.get("sizes"), Mapping):
        raise _PacketError("The policy packet layout is unavailable.")
    for name, (base, stride, command, size) in _GEOMETRY.items():
        actual = layout.get(name)
        if (
            not isinstance(actual, (tuple, list))
            or len(actual) != 2
            or any(type(item) is not int for item in actual)
            or tuple(actual) != (base, stride)
        ):
            raise _PacketError("This policy packet geometry has not been traced for arbitration diagnostics.")
        # JSON receipts use string keys; the live backend uses integer keys.
        sizes = layout["sizes"]
        actual_size = sizes.get(command, sizes.get(str(command)))
        if type(actual_size) is not int or actual_size != size:
            raise _PacketError("The policy packet sizes do not match the understood arbitration layout.")
    mask_word = layout.get("status_mask_word", 1)
    if type(mask_word) is not int or mask_word != 1:
        raise _PacketError("The policy status mask position is not understood.")
    commands = layout.get("commands", {})
    if not isinstance(commands, Mapping) or any(
        commands.get(command, commands.get(str(command), command)) != command for command in (_INFO, _STATUS, _CONTROL)
    ):
        raise _PacketError("The policy command mapping is not understood.")


def _words(value, name):
    size = _GEOMETRY[name][3]
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray, memoryview))
        or len(value) != size // 4
    ):
        raise _PacketError(f"The {name} packet has an unexpected length.")
    if any(type(word) is not int or not 0 <= word <= _UINT32_MAX for word in value):
        raise _PacketError(f"The {name} packet contains a value outside unsigned 32-bit words.")
    return value


def _u32(row, offset):
    return struct.unpack_from("<I", row, offset)[0]


def _arbitration(row, offset):
    """Decode only the bounded active inputs; leave opaque bytes uninterpreted."""
    vector = row[offset : offset + 0x50]
    if len(vector) != 0x50:
        return _unavailable("The arbitration structure is truncated.")
    method, count = vector[:2]
    if method not in _METHODS:
        return _unavailable(f"Arbitration method {method} is not understood.")
    if count > 8:
        return _unavailable("The arbitration input count exceeds its eight slots.")
    inputs = [{"client": vector[12 + 8 * index], "value": _u32(vector, 16 + 8 * index)} for index in range(count)]
    if len({item["client"] for item in inputs}) != count:
        return _unavailable("The arbitration structure repeats a client identifier.")
    values = [item["value"] for item in inputs]
    computed = None
    if values:
        if method == 0:
            computed = min(values)
        elif method == 1:
            computed = max(values)
        else:
            # Both traced implementations add using 32-bit arithmetic.
            computed = sum(values) & _UINT32_MAX
    output = _u32(vector, 4)
    return {
        "available": True,
        "reason": None,
        "method": _METHODS[method],
        "method_code": method,
        "count": count,
        "output": output,
        "initial_value": _u32(vector, 8),
        "inputs": inputs,
        "computed_output": computed,
        "consistent": None if computed is None else output == computed,
    }


def _row(index, info, control, status):
    meta = (_GEOMETRY["info"][0] + index * _GEOMETRY["info"][1]) // 4
    request = (_GEOMETRY["control"][0] + index * _GEOMETRY["control"][1]) // 4
    state = (_GEOMETRY["status"][0] + index * _GEOMETRY["status"][1]) // 4
    header = info[meta + 1]
    kind, channel, unit = header & 255, (header >> 8) & 255, (header >> 16) & 255
    row = {
        "policy": index,
        "type": kind,
        "channel": channel,
        "unit": {0: "mW", 1: "mA"}.get(unit),
        "unit_code": unit,
    }
    if info[meta] & 255 != 0x40:
        reason = "The policy row does not declare the understood V4 table format."
    elif control[request] != kind or status[state] & 255 != kind:
        reason = "The policy types disagree between INFO, CONTROL and STATUS."
    else:
        reason = None
    if reason:
        return {
            **row,
            **_unavailable(reason),
            "host": _unavailable(reason),
            "pmu": _unavailable(reason),
            "observations": [],
        }
    # The common prefix ends at 0x130; no derived-class payload is decoded.
    prefix = struct.pack("<76I", *status[state : state + 76])
    host, pmu = _arbitration(prefix, 0x40), _arbitration(prefix, 0xE0)
    requested, effective, value = control[request + 1], status[state + 1], status[state + 2]
    delta = struct.unpack_from("<i", prefix, 12)[0]
    observations = []
    if host["available"] and requested != host["output"]:
        observations.append("The host arbitration output differs from the RM client's request.")
    if host["available"] and pmu["available"] and host["output"] != pmu["output"]:
        observations.append("The returned PMU arbitration output differs from the host output.")
    if pmu["available"] and effective != pmu["output"]:
        observations.append("The effective limit differs from the returned PMU arbitration output.")
    if delta:
        observations.append("A signed adjustment is applied when host limits are serialized for the PMU.")
    for label, vector in (("Host", host), ("PMU", pmu)):
        if vector.get("consistent") is False:
            observations.append(f"{label} arbitration output does not match its returned active inputs.")
    return {
        **row,
        "available": True,
        "reason": None,
        "requested": requested,
        "effective": effective,
        "value": value,
        "delta": delta,
        "host": host,
        "pmu": pmu,
        "observations": observations,
    }


def decode(layout, info, control, status):
    """Return independent diagnostics from the backend's existing word arrays.

    The accepted layout is the existing ``GPU._current_limit_abi()`` dictionary:
    info=(0xCC,0x104), status=(0x9C,0x1730), control=(0x14,0xC4), plus command sizes.
    No GPU identity or driver-version allowlist is used. Other packet geometries
    remain available to the existing reader but are undecoded here.

    Client identifiers are numeric. They must not be presented as policy indices
    without a separately established relationship. A PMU arbitration output also
    need not equal STATUS's effective limit: policies can apply further changes.
    """
    result = {"schema": 1, "available": False, "reason": None, "policies": []}
    try:
        _geometry(layout)
        info = _words(info, "info")
        mask = info[1]
        capacity = min(32, *((size - base) // stride for base, stride, _, size in _GEOMETRY.values()))
        if mask & ~((1 << capacity) - 1):
            raise _PacketError("The policy mask exceeds the understood packet capacity.")
        if mask == 0 and control is None and status is None:
            return {**result, "available": True, "mask": 0}
        control, status = _words(control, "control"), _words(status, "status")
        if status[1] != mask or list(control[:5]) != [0, 0, 0, 255, mask]:
            raise _PacketError("The INFO, CONTROL and STATUS occupancy masks or control header disagree.")
        result.update(
            available=True,
            mask=mask,
            policies=[_row(index, info, control, status) for index in range(capacity) if mask >> index & 1],
        )
    except _PacketError as exc:
        result["reason"] = str(exc)
    return result
