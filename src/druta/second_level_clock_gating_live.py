# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only SLCG requests with partial source-bound register witnesses.

Source route 260000 contains 14 rows, including one duplicate address. Its
13 unique READ32s are discovered independently on the current GPU: invalid
registers remain unavailable diagnostics. The known table writes zero on
release, so this bounded lifecycle requires every valid initial value to be
zero. Only the public named profiler request writes state; no raw register
writes, saved requests, or reset replay occur here. Configuration readback
does not measure physical clock gating, power, or residency.
"""

from __future__ import annotations

import ctypes
import struct

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .gr_gating_live import GrGatingController, GrGatingError, _ProfilerTransport
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

SecondLevelClockGatingError = GrGatingError
_LAYOUTS = {(0x1B0, implementation): "gb20x-slcg" for implementation in (2, 3, 5, 6, 7)}
# Source selector1 values for the complete unique route table. Selector3
# releases all entries to zero. These are source values, not local readbacks.
_DISABLED_VALUES = {
    0x2B409C: 0x80000000,
    0x2B4090: 0x80000000,
    0x2A0090: 0x80000000,
    0x2AA09C: 0x80000000,
    0x2AA090: 0x80000000,
    0x2A4090: 0x80000000,
    0x2AA49C: 0x80000000,
    0x2AA490: 0x80000000,
    0x2B0844: 1,
    0x2B6C3C: 1,
    0x2B6C7C: 1,
    0x2B6CBC: 1,
    0x2B6CFC: 1,
}


def _status_classification(status):
    if status & 8:
        return "unsupported_operation"
    if status & (1 | 2 | 4 | 16):
        return "invalid_operation"
    if status & 32:
        return "access_denied"
    return "unknown_operation_status"


def _read_registers(reader, identity):
    storage, operations = clock._guarded(32 * len(_DISABLED_VALUES))
    for index, address in enumerate(_DISABLED_VALUES):
        # Public READ32=0, GLOBAL=0, status/quad/group/subgroup zero.
        struct.pack_into("<BBBBIII", operations, index * 32, 0, 0, 0, 0, 0, 0, address)
    before = bytes(operations)
    # The 16-byte route at +32 is TYPE_NONE=0, padding0, route0.
    params = struct.pack("<6IQIIQ", 0, 0, 0, 0, 0, len(_DISABLED_VALUES), ctypes.addressof(operations), 0, 0, 0)
    try:
        returned = lpwr._get(reader, identity, 0x20800122, 48, params)
    finally:
        clock._guards(storage)
    if returned != params:
        raise SecondLevelClockGatingError("SLCG register read changed routing or parameter geometry")
    after, values, unavailable = bytes(operations), {}, {}
    untouched = bytearray(after)
    for index, address in enumerate(_DISABLED_VALUES):
        at = index * 32
        status = after[at + 2]
        value = struct.unpack_from("<I", after, at + 20)[0]
        key = hex(address)
        if status:
            unavailable[key] = {"classification": _status_classification(status), "operation_status": status}
        elif value == 0xFFFFFFFF or value & 0xFFF00000 == 0xBAD00000:
            unavailable[key] = {"classification": "poison", "operation_status": 0, "raw_value": hex(value)}
        else:
            values[key] = value
        untouched[at + 2] = before[at + 2]
        untouched[at + 20 : at + 24] = before[at + 20 : at + 24]
    if bytes(untouched) != before:
        raise SecondLevelClockGatingError("SLCG register read changed operation geometry or opaque fields")
    return values, unavailable


def _inventory(state):
    return (
        state["architecture"],
        state["layout"],
        state["registers"].keys(),
        {
            key: (item["classification"], item["operation_status"])
            for key, item in state["unavailable_registers"].items()
        },
    )


def _same_inventory(before, after):
    if not before["support"] or not after["support"] or _inventory(before) != _inventory(after):
        raise SecondLevelClockGatingError("SLCG native layout or register availability classification changed")


class SecondLevelClockGatingController(GrGatingController):
    """Own one explicitly released SLCG request; retain unavailable peers."""

    def __init__(self, gpu):
        self.gpu = gpu
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)
        self._profiler = _ProfilerTransport(self.reader, self.identity, feature="SLCG")
        self._owned = False
        self._uncertain = False
        self._observed = ()
        self._request_inventory = None
        self._last_verification_error = None

    def _once(self):
        self._profiler.check()
        raw = lpwr._get(self.reader, self.identity, 0x20801701, 16, bytes(16))
        if len(raw) != 16:
            raise SecondLevelClockGatingError("SLCG native architecture geometry is not understood")
        architecture, implementation, revision, raw_auxiliary = struct.unpack("<4I", raw)
        native = {
            "architecture": architecture,
            "implementation": implementation,
            "revision": revision,
            "raw_auxiliary": raw_auxiliary,
        }
        layout = _LAYOUTS.get((architecture, implementation))
        if layout is None:
            return {
                "support": False,
                "status": "unsupported_layout",
                "architecture": native,
                "layout": None,
                "registers": {},
                "unavailable_registers": {},
            }
        registers, unavailable = _read_registers(self.reader, self.identity)
        return {
            "support": bool(registers),
            "status": "partial" if registers and unavailable else "available" if registers else "unavailable",
            "architecture": native,
            "layout": layout,
            "registers": registers,
            "unavailable_registers": unavailable,
        }

    def _read_locked(self):
        for attempt in range(2):
            try:
                first, second = self._once(), self._once()
                if _inventory(first) != _inventory(second) or first["registers"] != second["registers"]:
                    raise SecondLevelClockGatingError(
                        "SLCG native layout, valid registers, or availability were not stable"
                    )
                return second
            except PerfDiagnosticError as exc:
                if attempt or exc.kind != "transient":
                    raise
        raise AssertionError("unreachable")

    def _snapshot(self, state):
        return {
            **state,
            "expected_disabled_values": {hex(address): value for address, value in _DISABLED_VALUES.items()},
            "observed_registers": list(self._observed),
            "owned_request": self._owned,
            "uncertain_outcome": self._uncertain,
            "state": "uncertain" if self._uncertain else "held" if self._owned else "idle",
            "verification_scope": "partial independent source-bound SLCG register witness",
            "last_verification_error": self._last_verification_error,
        }

    def hold(self):
        with self.gpu._lock:
            if self._uncertain:
                raise SecondLevelClockGatingError("SLCG outcome is uncertain; explicitly release this session")
            before = self._read_locked()
            if self._owned:
                if _inventory(before) != self._request_inventory or any(
                    before["registers"][key] != _DISABLED_VALUES[int(key, 16)] for key in self._observed
                ):
                    raise SecondLevelClockGatingError("The owned SLCG request is no longer independently visible")
                return self._snapshot(before)
            if not before["support"]:
                raise SecondLevelClockGatingError("SLCG has no valid register witness on an understood native layout")
            if any(before["registers"].values()):
                raise SecondLevelClockGatingError("SLCG requires an exact zero baseline on every valid register")
            self._request_inventory = _inventory(before)
            try:
                self._profiler.allocate()
                self._uncertain = True
                self._profiler.request()
                self._owned = True
                after = self._read_locked()
                _same_inventory(before, after)
                if any(value != _DISABLED_VALUES[int(key, 16)] for key, value in after["registers"].items()):
                    raise SecondLevelClockGatingError("SLCG request did not match the source disabled register values")
                if before["registers"] == after["registers"]:
                    raise SecondLevelClockGatingError("SLCG request produced no independent register transition")
                self._observed = tuple(after["registers"])
                self._uncertain = False
                return self._snapshot(after)
            except Exception:
                self._uncertain = self._profiler.handle is not None
                raise

    def release(self):
        with self.gpu._lock:
            if self._profiler.handle is None:
                return self._snapshot(self._read_locked())
            self._uncertain = True
            try:
                before = self._read_locked()
                if self._profiler.requested:
                    self._profiler.release()
                    self._owned = False
                after = self._read_locked()
                _same_inventory(before, after)
                if _inventory(after) != self._request_inventory or any(after["registers"].values()):
                    raise SecondLevelClockGatingError(
                        "SLCG release did not match zero values and the original register inventory"
                    )
                self._last_verification_error = None
            except Exception as exc:
                self._last_verification_error = str(exc)
                raise
            finally:
                self._profiler.free()
                self._owned = False
                self._observed = ()
                self._request_inventory = None
                self._uncertain = False
            return self._snapshot(after)
