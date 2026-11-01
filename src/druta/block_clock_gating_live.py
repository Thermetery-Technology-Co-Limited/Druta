# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-owned BLCG requests corroborated by source-bound register READ32s.

The native architecture/implementation selects an understood register layout
from the driver's chip HAL mapping, not a board or driver-version allowlist.
Only the named public profiler control writes state. Register operations here
are READ32/GLOBAL only, and changed bits are a partial independent witness,
not physical gating or residency measurements. This controller saves and
replays no requests across device reset.
"""

from __future__ import annotations

import ctypes
import struct

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .gr_gating_live import GrGatingController, GrGatingError, _ProfilerTransport
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

BlockClockGatingError = GrGatingError
# Native 20801701 arch 1B0, implementations 2/3/5/6/7 select the same
# C3A1F0 provider through chip HAL 41..45. Masks are source production XOR
# disable-zero values, not the observed deltas from the developer's adapter.
_LAYOUTS = {(0x1B0, implementation): "gb20x-blcg" for implementation in (2, 3, 5, 6, 7)}
_ALLOWED_CLEAR = {0x4041F0: 0xC646, 0x409890: 0x7F, 0x405BF0: 0xC044, 0x407000: 0x4000C042}
# GA10x HAL 2B..2F selects a different graphics table. Its IoAperture
# translates relative offsets back to these GLOBAL addresses. Disable writes
# whole zero words; unlike GB20x, subset-clearing is insufficient verification.
_LAYOUTS.update({(0x170, implementation): "ga10x-blcg" for implementation in (2, 3, 4, 6, 7)})
_AMPERE_CLEAR = {0x4041F0: 0xC646, 0x409890: 0x7F, 0x4098B0: 0x7F, 0x405BF0: 0xC444, 0x407000: 0x4000C242}
_MASKS = {"gb20x-blcg": _ALLOWED_CLEAR, "ga10x-blcg": _AMPERE_CLEAR}
_READ32, _GLOBAL, _ROUTE_NONE = 0, 0, 0


def _read_registers(reader, identity, masks=_ALLOWED_CLEAR):
    # Keep both the guarded storage and its exported operation buffer alive
    # until the synchronous RM call returns and all outputs are validated.
    storage, operations = clock._guarded(32 * len(masks))
    for index, address in enumerate(masks):
        struct.pack_into("<BBBBIII", operations, index * 32, _READ32, _GLOBAL, 0, 0, 0, 0, address)
    before = bytes(operations)
    # 48-byte header includes the 16-byte TYPE_NONE route at offset 32.
    params = struct.pack("<6IQIIQ", 0, 0, 0, 0, 0, len(masks), ctypes.addressof(operations), _ROUTE_NONE, 0, 0)
    try:
        returned = lpwr._get(reader, identity, 0x20800122, 48, params)
    finally:
        clock._guards(storage)
    if returned != params:
        raise BlockClockGatingError("BLCG register read changed routing or parameter geometry")
    after, values = bytes(operations), {}
    untouched = bytearray(after)
    for index, address in enumerate(masks):
        at = index * 32
        if after[at + 2] != 0:
            raise BlockClockGatingError(f"BLCG READ32 0x{address:X} rejected (status {after[at + 2]})")
        value = struct.unpack_from("<I", after, at + 20)[0]
        if value == 0xFFFFFFFF or value & 0xFFF00000 == 0xBAD00000:
            raise BlockClockGatingError(f"BLCG READ32 0x{address:X} returned an unusable value")
        values[hex(address)] = value
        untouched[at + 20 : at + 24] = before[at + 20 : at + 24]
    if bytes(untouched) != before:
        raise BlockClockGatingError("BLCG register read changed operation geometry or opaque fields")
    return values


def _same_layout(before, after):
    if not before["support"] or not after["support"] or before["architecture"] != after["architecture"]:
        raise BlockClockGatingError("BLCG native register layout changed during the request")
    if before["registers"].keys() != after["registers"].keys():
        raise BlockClockGatingError("BLCG register inventory changed")


class BlockClockGatingController(GrGatingController):
    """One BLCG profiler request, explicit release/free, no raw register writes."""

    def __init__(self, gpu):
        self.gpu = gpu
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)
        self._profiler = _ProfilerTransport(self.reader, self.identity, feature="BLCG")
        self._owned = False
        self._uncertain = False
        self._observed = {}
        self._initial_state = None
        self._last_verification_error = None

    def _once(self):
        self._profiler.check()
        raw = lpwr._get(self.reader, self.identity, 0x20801701, 16, bytes(16))
        if len(raw) != 16:
            raise BlockClockGatingError("BLCG native architecture geometry is not understood")
        architecture, implementation, revision, raw_auxiliary = struct.unpack("<4I", raw)
        native = {
            "architecture": architecture,
            "implementation": implementation,
            "revision": revision,
            "raw_auxiliary": raw_auxiliary,
        }
        layout = _LAYOUTS.get((architecture, implementation))
        if layout is None:
            return {"support": False, "architecture": native, "layout": None, "registers": {}}
        return {
            "support": True,
            "architecture": native,
            "layout": layout,
            "registers": _read_registers(self.reader, self.identity, _MASKS[layout]),
        }

    def _read_locked(self):
        for attempt in range(2):
            try:
                first, second = self._once(), self._once()
                if first != second:
                    raise BlockClockGatingError("BLCG native layout or register readbacks were not stable")
                return second
            except PerfDiagnosticError as exc:
                if attempt or exc.kind != "transient":
                    raise
        raise AssertionError("unreachable")

    def _snapshot(self, state):
        return {
            **state,
            "allowed_clear_masks": {hex(address): mask for address, mask in _MASKS.get(state["layout"], {}).items()},
            "observed_register_bits": dict(self._observed),
            "owned_request": self._owned,
            "uncertain_outcome": self._uncertain,
            "state": "uncertain" if self._uncertain else "held" if self._owned else "idle",
            "verification_scope": "partial independent source-bound register witness",
            "last_verification_error": self._last_verification_error,
        }

    def hold(self):
        with self.gpu._lock:
            if self._uncertain:
                raise BlockClockGatingError("BLCG outcome is uncertain; explicitly release this session")
            before = self._read_locked()
            if self._owned:
                if self._initial_state and self._initial_state["layout"] == "ga10x-blcg":
                    _same_layout(self._initial_state, before)
                    if any(before["registers"].values()):
                        raise BlockClockGatingError("The owned BLCG request is no longer independently visible")
                    return self._snapshot(before)
                if not before["support"] or any(
                    before["registers"][key] & bits for key, bits in self._observed.items()
                ):
                    raise BlockClockGatingError("The owned BLCG request is no longer independently visible")
                return self._snapshot(before)
            if not before["support"]:
                raise BlockClockGatingError("The native BLCG register layout is not understood")
            masks = _MASKS[before["layout"]]
            if before["layout"] == "ga10x-blcg" and any(
                before["registers"][hex(address)] & ~mask for address, mask in masks.items()
            ):
                raise BlockClockGatingError("BLCG baseline has bits outside the source-defined full-word values")
            if not any(before["registers"][hex(address)] & mask for address, mask in masks.items()):
                raise BlockClockGatingError("No source-defined BLCG bit remains to independently verify a request")
            try:
                self._initial_state = before
                self._profiler.allocate()
                self._uncertain = True
                self._profiler.request()
                self._owned = True
                after = self._read_locked()
                _same_layout(before, after)
                observed = {}
                for address, allowed in masks.items():
                    key = hex(address)
                    old, new = before["registers"][key], after["registers"][key]
                    cleared = old & ~new
                    if new & ~old or cleared & ~allowed:
                        raise BlockClockGatingError("BLCG request changed bits outside the source-defined clear masks")
                    observed[key] = cleared
                if before["layout"] == "ga10x-blcg" and any(after["registers"].values()):
                    raise BlockClockGatingError("BLCG request did not clear every complete register word")
                if not any(observed.values()):
                    raise BlockClockGatingError("BLCG request produced no independent register transition")
                self._observed = observed
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
                _same_layout(before, after)
                if self._initial_state and self._initial_state["layout"] == "ga10x-blcg":
                    _same_layout(self._initial_state, after)
                    expected = self._initial_state["registers"]
                else:
                    expected = {key: value | self._observed.get(key, 0) for key, value in before["registers"].items()}
                if after["registers"] != expected:
                    raise BlockClockGatingError("BLCG release did not match the exact expected register values")
                self._last_verification_error = None
            except Exception as exc:
                self._last_verification_error = str(exc)
                raise
            finally:
                self._profiler.free()
                self._owned = False
                self._observed = {}
                self._initial_state = None
                self._uncertain = False
            return self._snapshot(after)
