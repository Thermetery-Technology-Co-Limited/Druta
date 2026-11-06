# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only SLCG requests with partial source-bound register witnesses.

Each source-qualified native layout selects its own READ32 witnesses.
Invalid registers remain unavailable diagnostics; one family's offsets are
never substituted for another family's unqualified layout. Valid initial and
released values must match the selected source table. Only the public named profiler request writes state; no raw register
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
_LAYOUTS = {  # {**a, **b}, not a | b: the legacy builds run Python 3.8
    **{(0x1B0, implementation): "gb20x-slcg" for implementation in (2, 3, 5, 6, 7)},
    **{(0x170, implementation): "ga10x-slcg" for implementation in (2, 3, 4, 6, 7)},
    **{(0x190, implementation): "ad10x-slcg" for implementation in (2, 3, 4, 6, 7)},
    **{(0x160, implementation): "tu10x-slcg" for implementation in (2, 4, 6, 8, 7)},
}
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
# Native 0x170 and 0x190 qualified implementations independently select
# equivalent three-register tables; LPWR+100 is a no-op on both paths. Each
# adapter still needs valid zero baseline reads and an exact observed transition.
_GA_AD_DISABLED_VALUES = {0x248018: 3, 0x251A18: 3, 0x251818: 3}
_LAYOUT_VALUES = {
    "gb20x-slcg": _DISABLED_VALUES,
    "ga10x-slcg": _GA_AD_DISABLED_VALUES,
    "ad10x-slcg": _GA_AD_DISABLED_VALUES,
    "tu10x-slcg": {0x24A124: 0},
}
_LAYOUT_RELEASE_VALUES = {
    **{layout: dict.fromkeys(values, 0) for layout, values in _LAYOUT_VALUES.items()
       if layout != "tu10x-slcg"},
    "tu10x-slcg": {0x24A124: 1},
}
# The named Turing request also writes these full registers. Their release
# value is 0xffffffff, indistinguishable from a READ32 poison sentinel; their
# prior values and transitions are therefore not verified by this witness.
_TURING_UNVERIFIED_PEERS = (
    0x248018,
    0x246018,
    0x246218,
    0x246418,
    0x246618,
    0x246818,
    0x246A18,
    0x246C18,
    0x246E18,
    0x244018,
    0x244218,
    0x244418,
    0x244618,
    0x244818,
    0x244A18,
)


def _status_classification(status):
    if status & 8:
        return "unsupported_operation"
    if status & (1 | 2 | 4 | 16):
        return "invalid_operation"
    if status & 32:
        return "access_denied"
    return "unknown_operation_status"


def _read_registers(reader, identity, disabled_values):
    storage, operations = clock._guarded(32 * len(disabled_values))
    for index, address in enumerate(disabled_values):
        # Public READ32=0, GLOBAL=0, status/quad/group/subgroup zero.
        struct.pack_into("<BBBBIII", operations, index * 32, 0, 0, 0, 0, 0, 0, address)
    before = bytes(operations)
    # The 16-byte route at +32 is TYPE_NONE=0, padding0, route0.
    params = struct.pack("<6IQIIQ", 0, 0, 0, 0, 0, len(disabled_values), ctypes.addressof(operations), 0, 0, 0)
    try:
        returned = lpwr._get(reader, identity, 0x20800122, 48, params)
    finally:
        clock._guards(storage)
    if returned != params:
        raise SecondLevelClockGatingError("SLCG register read changed routing or parameter geometry")
    after, values, unavailable = bytes(operations), {}, {}
    untouched = bytearray(after)
    for index, address in enumerate(disabled_values):
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
        self._owned_mode = None
        self._arbitration_verified = False
        self._arbitration_probe = None

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
        registers, unavailable = _read_registers(self.reader, self.identity, _LAYOUT_VALUES[layout])
        return {
            "support": bool(registers),
            "status": "partial"
            if registers and (unavailable or layout == "tu10x-slcg")
            else "available"
            if registers
            else "unavailable",
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
            "expected_disabled_values": {
                hex(address): value for address, value in _LAYOUT_VALUES.get(state["layout"], {}).items()
            },
            "expected_release_values": {
                hex(address): value for address, value in _LAYOUT_RELEASE_VALUES.get(state["layout"], {}).items()
            },
            "expected_enabled_values": {
                hex(address): value for address, value in _LAYOUT_RELEASE_VALUES.get(state["layout"], {}).items()
            },
            "unverified_source_peers": [hex(address) for address in _TURING_UNVERIFIED_PEERS]
            if state["layout"] == "tu10x-slcg"
            else [],
            "unverified_source_peers_scope": (
                "Named request writes these full registers; prior values and transitions are not verified."
                if state["layout"] == "tu10x-slcg"
                else None
            ),
            "observed_registers": list(self._observed),
            "owned_request": self._owned,
            "owned_mode": self._owned_mode,
            "arbitration_verified": self._arbitration_verified,
            "uncertain_outcome": self._uncertain,
            "state": "uncertain" if self._uncertain else "held" if self._owned else "idle",
            "verification_scope": (
                "partial source enabled values and conflicting profiler request rejection; not unique ownership"
                if self._owned_mode == "enabled"
                else "partial independent source-bound SLCG register witness"
            ),
            "last_verification_error": self._last_verification_error,
        }

    def hold(self):
        """Own the default DISABLE request, requiring a positive register transition."""
        return self._hold("disabled")

    def hold_enabled(self):
        """Own ENABLE with source values and a separately rejected opposing request.

        Arbitration witnesses a conflicting profiler request; it cannot establish
        unique ownership or attribute existing ENABLE requests to this object.
        """
        return self._hold("enabled")

    def _verify_enable_arbitration(self, enabled):
        self._arbitration_probe = _ProfilerTransport(self.reader, self.identity, feature="SLCG")
        try:
            self._arbitration_probe.allocate()
            self._arbitration_probe.probe_disable_conflict()
        finally:
            self._arbitration_probe.free()
            self._arbitration_probe = None
        after = self._read_locked()
        _same_inventory(enabled, after)
        if enabled["registers"] != after["registers"]:
            raise SecondLevelClockGatingError("SLCG enabled values changed during the arbitration probe")
        self._arbitration_verified = True

    def _hold(self, mode):
        with self.gpu._lock:
            if self._uncertain:
                raise SecondLevelClockGatingError("SLCG outcome is uncertain; explicitly release this session")
            before = self._read_locked()
            values = _LAYOUT_RELEASE_VALUES if mode == "enabled" else _LAYOUT_VALUES
            if self._owned:
                if self._owned_mode != mode:
                    raise SecondLevelClockGatingError("Release the owned SLCG request before changing mode")
                if _inventory(before) != self._request_inventory or any(
                    value != values[before["layout"]][int(key, 16)] for key, value in before["registers"].items()
                ):
                    raise SecondLevelClockGatingError("The owned SLCG request is no longer independently visible")
                return self._snapshot(before)
            if not before["support"]:
                raise SecondLevelClockGatingError("SLCG has no valid register witness on an understood native layout")
            if mode == "disabled" and any(
                value != _LAYOUT_RELEASE_VALUES[before["layout"]][int(key, 16)]
                for key, value in before["registers"].items()
            ):
                raise SecondLevelClockGatingError(
                    "SLCG requires an exact source release baseline (exact zero baseline where applicable) on every valid register"
                )
            if mode == "enabled" and any(
                value
                not in (
                    _LAYOUT_RELEASE_VALUES[before["layout"]][int(key, 16)],
                    _LAYOUT_VALUES[before["layout"]][int(key, 16)],
                )
                for key, value in before["registers"].items()
            ):
                raise SecondLevelClockGatingError(
                    "SLCG ENABLE requires known source enabled or disabled baseline values"
                )
            self._request_inventory = _inventory(before)
            try:
                self._profiler.allocate()
                self._uncertain = True
                self._owned_mode = mode
                if mode == "enabled":
                    self._profiler.request(enabled=True)
                else:
                    self._profiler.request()
                self._owned = True
                after = self._read_locked()
                _same_inventory(before, after)
                if any(value != values[after["layout"]][int(key, 16)] for key, value in after["registers"].items()):
                    raise SecondLevelClockGatingError(f"SLCG request did not match the source {mode} register values")
                self._observed = tuple(
                    key for key, value in after["registers"].items() if before["registers"][key] != value
                )
                if mode == "disabled" and not self._observed:
                    raise SecondLevelClockGatingError("SLCG request produced no independent register transition")
                if mode == "enabled":
                    self._verify_enable_arbitration(after)
                self._uncertain = False
                return self._snapshot(after)
            except Exception:
                self._arbitration_verified = False
                self._uncertain = self._profiler.handle is not None or self._arbitration_probe is not None
                raise

    def release(self):
        with self.gpu._lock:
            if self._profiler.handle is None and self._arbitration_probe is None:
                return self._snapshot(self._read_locked())
            self._uncertain = True
            try:
                if self._arbitration_probe is not None:
                    self._arbitration_probe.free()
                    self._arbitration_probe = None
                before = self._read_locked()
                if self._profiler.handle is not None and self._profiler.requested:
                    self._profiler.release()
                    self._owned = False
                after = self._read_locked()
                _same_inventory(before, after)
                if _inventory(after) != self._request_inventory or any(
                    value != _LAYOUT_RELEASE_VALUES[after["layout"]][int(key, 16)]
                    for key, value in after["registers"].items()
                ):
                    raise SecondLevelClockGatingError(
                        "SLCG release did not match source release values (zero values where applicable) and the original register inventory"
                    )
                self._last_verification_error = None
            except Exception as exc:
                self._last_verification_error = str(exc)
                raise
            finally:
                try:
                    self._profiler.free()
                finally:
                    self._owned = self._owned and self._profiler.handle is not None
                    self._uncertain = self._profiler.handle is not None or self._arbitration_probe is not None
                    if not self._uncertain:
                        self._observed = ()
                        self._request_inventory = None
                        self._owned_mode = None
                        self._arbitration_verified = False
            return self._snapshot(after)

    def close(self):
        """Explicitly release/free only this controller's request and probe objects."""
        with self.gpu._lock:
            if self._profiler.handle is None and self._arbitration_probe is None:
                return None
            return self.release()
