# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Selected live legacy rail requests, using the complete replay contract.

Request readback verifies stored requests only. It does not establish physical
voltage or an effective VCRIT threshold. No gate is enabled by this module.
"""

import struct
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation

from . import voltage_request_replay as replay
from .voltage_request_gate import VoltageRequestGate, VoltageRequestGateError

FIELDS = ("REL", "ALT/OP", "OV", "VMIN", "VCRIT low", "VCRIT high")
VCRIT_NOTE = "stored request; effective threshold unverified"


class VoltageRequestsError(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def millivolts_to_uv(value):
    """Accept exact decimal mV, with signed-int32 whole-uV precision."""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise VoltageRequestsError("value", "Enter an offset in mV with whole-microvolt precision") from exc
    if not number.is_finite():
        raise VoltageRequestsError("value", "Enter a finite offset with whole-microvolt precision")
    if not Decimal("-2147483.648") <= number <= Decimal("2147483.647"):
        raise VoltageRequestsError("value", "Offset must be a signed int32 whole-microvolt request")
    # Inspect exact decimal digits before arithmetic can round away a tiny
    # fractional uV under the process-wide Decimal precision.
    digits, exponent = number.as_tuple().digits, number.as_tuple().exponent
    if exponent < -3 and any(digits[exponent + 3 :]):
        raise VoltageRequestsError("value", "Enter a finite offset with whole-microvolt precision")
    result = int(number * 1000)
    _uv(result)
    return result


def _uv(value):
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise VoltageRequestsError("value", "Offset must be a signed int32 whole-microvolt request")


def _slot(gpu, slot):
    actual = gpu.slot()
    if not actual or (slot is not None and actual != slot):
        raise VoltageRequestsError("identity", "Selected adapter changed; read its requests first")
    return actual


def _values(state):
    return {
        index: dict(
            zip(
                FIELDS,
                struct.unpack_from(
                    "<6i", bytes.fromhex(raw), replay.controls.ROW_OFFSET + int(index) * replay.controls.ROW_STRIDE + 4
                ),
                strict=True,
            )
        )
        for index, raw in state["public"].items()
    }


def _snapshot(state, slot, *, gate=None, gate_error=None):
    return {
        "slot": slot,
        "identity": state["identity"].copy(),
        "request": state,
        "values": _values(state),
        "gate_enabled": gate["enabled"] if gate is not None else None,
        "gate_error": gate_error,
    }


def _gate(gpu, identity):
    state = VoltageRequestGate(gpu).read()
    if state["identity"] != identity:
        raise VoltageRequestsError("identity", "Voltage gate and rail requests identify different adapters")
    return state


def read(gpu, *, slot=None):
    """Read present requests and admission independently; never issue a SET."""
    with getattr(gpu, "_lock", nullcontext()):
        slot = _slot(gpu, slot)
        state = replay.read_checked(gpu)
        if state["identity"]["slot"] != slot:
            raise VoltageRequestsError("identity", "Rail requests identify another selected adapter")
        gate, error = None, None
        try:
            gate = _gate(gpu, state["identity"])
        except VoltageRequestGateError as exc:
            if exc.kind == "identity":
                raise
            error = str(exc)
        _slot(gpu, slot)
        return _snapshot(state, slot, gate=gate, gate_error=error)


def set_request(gpu, rail, field, value_uv, *, expected, slot=None):
    """Project one field from fresh complete requests, then verify one SET.

    The displayed snapshot binds the target and rail identity only. Its offsets,
    boost and opaque packet contents are never used as the write baseline.
    """
    _uv(value_uv)
    if type(rail) is not str or type(field) is not str or field not in FIELDS:
        raise VoltageRequestsError("value", "Select an independently present rail request field")
    if not isinstance(expected, dict) or "request" not in expected:
        raise VoltageRequestsError("identity", "Read requests for the selected adapter first")
    previous = replay.validate(expected["request"])
    with getattr(gpu, "_lock", nullcontext()):
        slot = _slot(gpu, slot)
        current = replay.read_checked(gpu)
        if (
            expected.get("slot") != slot
            or expected.get("identity") != current["identity"]
            or previous["identity"] != current["identity"]
            or previous["rail_identity"] != current["rail_identity"]
            or previous["info_hex"] != current["info_hex"]
        ):
            raise VoltageRequestsError("identity", "Selected adapter or rail identity changed; read requests again")
        if rail not in current["public"]:
            raise VoltageRequestsError("unsupported", "Selected rail request is unavailable")
        if not current["complete"]:
            raise VoltageRequestsError("unsupported", "Complete request preservation is unavailable for an active rail")
        gate = _gate(gpu, current["identity"])
        if not gate["enabled"]:
            raise VoltageRequestsError(
                "suppressed",
                "Voltage-request gate is disabled; requests are suppressed. "
                "Use Allow voltage requests separately before applying",
            )
        values = _values(current)[rail]
        if values[field] == value_uv:
            return {
                "attempted": False,
                "verified": False,
                "changed": False,
                "snapshot": _snapshot(current, slot, gate=gate),
            }
        row = [values[name] for name in FIELDS]
        row[FIELDS.index(field)] = value_uv
        desired = replay.project(current, offsets_uv={rail: row})
        _slot(gpu, slot)
        result = replay.replay(gpu, desired)
        _slot(gpu, slot)
        result["changed"] = True
        result["snapshot"] = _snapshot(result["after"], slot, gate=gate)
        return result
