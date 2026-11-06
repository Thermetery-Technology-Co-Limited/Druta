# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only idle thresholds with independently read raw peers."""

from __future__ import annotations

import ctypes
import struct

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

_MAX = 0xFFFFFFFF
_IMPLEMENTATIONS = frozenset({2, 3, 5, 6, 7})
_SET = 0x2080E802
_RAW = 0x20801706
CONTROLLERS = {0: "Graphics (GR)", 4: "Memory subsystem (MS)",
               10: "MS DIFR SW-ASR", 11: "MS DIFR CG",
               12: "NVDEC", 13: "NVENC", 14: "OFA", 15: "NVJPG"}


class GrIdleThresholdError(RuntimeError):
    """The requested threshold or its exact readback could not be verified."""


def _scale(raw, scaled):
    low, high = 1, _MAX
    for r, s in zip(raw, scaled, strict=True):
        low = max(low, r * 1000 // (s + 1) + 1)
        if s:
            high = min(high, r * 1000 // s)
    if low > high:
        raise GrIdleThresholdError("Raw and scaled threshold values are inconsistent")
    return low if low == high else None


class IdleThresholdController:
    """Control one source-qualified idle request, in requested microseconds."""

    def __init__(self, gpu, index=0):
        if type(index) is not int or index not in CONTROLLERS:
            raise ValueError("Unsupported idle threshold controller index")
        self.index = index
        self.gpu = gpu
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)

    def _attribute(self, attribute):
        request = lpwr._request(attribute, (self.index,))
        response = lpwr._get(self.reader, self.identity, lpwr.ATTRIBUTE_COMMAND, lpwr.ATTRIBUTE_SIZE, request)
        return lpwr._attributes(response, attribute, (self.index,))[str(self.index)]

    def _raw_pair(self, sentinel):
        storage, entries = clock._guarded(24)
        for i in range(2):
            struct.pack_into("<III", entries, i * 12, i + 1, self.index, sentinel)
        pointer = ctypes.addressof(entries)
        request = struct.pack("<I4xQ", 2, pointer)
        try:
            response = lpwr._get(self.reader, self.identity, _RAW, 16, request)
        finally:
            clock._guards(storage)
        if response != request:
            raise GrIdleThresholdError("Raw threshold GET changed its pointer or header")
        values = []
        for i in range(2):
            selector, index, value = struct.unpack_from("<III", entries, i * 12)
            if selector != i + 1 or index != self.index:
                raise GrIdleThresholdError("Raw threshold GET changed its entry routing")
            values.append(value)
        return values

    def _once(self, sentinel):
        raw_arch = lpwr._get(self.reader, self.identity, 0x20801701, 16, bytes(16))
        architecture, implementation, revision, auxiliary = struct.unpack("<4I", raw_arch)
        arch = {"architecture": architecture, "implementation": implementation,
                "revision": revision, "raw_auxiliary": auxiliary}
        state = {"index": self.index, "name": CONTROLLERS[self.index],
                 "available": False, "status": "unsupported_layout", "architecture": arch,
                 "raw_pair": None, "scaled_pair": None, "scale_factor": None,
                 "minimum_us": 1, "maximum_us": None, "requested_us": None,
                 "applied_us": None, "changed": False}
        if architecture != 0x1B0 or implementation not in _IMPLEMENTATIONS:
            return state
        available, supported = self._attribute(0x39)
        if not available or supported != 1:
            state["status"] = "unsupported" if available else "unavailable"
            return state
        values = [self._attribute(a) for a in (0x3A, 0x3B, 0x4C)]
        if not all(available for available, _ in values):
            state["status"] = "unavailable"
            return state
        pair = self._raw_pair(sentinel)
        scaled = [values[0][1], values[1][1]]
        if pair[0] != values[2][1]:
            raise GrIdleThresholdError("Raw first threshold does not match independent readback")
        factor = _scale(pair, scaled)
        state.update(available=True, status="available" if factor is not None else "ambiguous_scale",
                     raw_pair=pair, scaled_pair=scaled, scale_factor=factor,
                     maximum_us=min(_MAX, (2**32 * 1000 - 1) // factor) if factor else None,
                     applied_us=scaled[0])
        return state

    def _read_locked(self):
        for attempt in range(2):
            try:
                a, b = self._once(0xA5A5A5A5), self._once(0x5A5A5A5A)
                if a != b:
                    raise GrIdleThresholdError("Threshold readback was unstable or an entry was skipped")
                return b
            except PerfDiagnosticError as exc:
                if attempt or exc.kind != "transient":
                    raise
        raise AssertionError("unreachable")

    def read(self):
        with self.gpu._lock:
            return self._read_locked()

    def apply(self, value_us):
        if type(value_us) is not int or not 1 <= value_us <= _MAX:
            raise ValueError("Threshold must be an integer in the valid microsecond range")
        with self.gpu._lock:
            before = self._read_locked()
            factor = before["scale_factor"]
            if not before["available"] or factor is None:
                raise GrIdleThresholdError("Threshold or unique scale is unavailable")
            if value_us > before["maximum_us"]:
                raise ValueError("Threshold exceeds the current scale's 32-bit range")
            expected = max(1, value_us * factor // 1000)
            if expected == before["raw_pair"][0]:
                return {**before, "requested_us": value_us, "changed": False}
            request = bytearray(lpwr.ATTRIBUTE_SIZE)
            struct.pack_into("<IHHHHII", request, 0, 1, 3, self.index, 0x3A, 0, value_us, 0)
            response = lpwr._get(self.reader, self.identity, _SET, lpwr.ATTRIBUTE_SIZE, bytes(request))
            struct.pack_into("<H", request, 10, 1)
            if response != bytes(request):
                raise GrIdleThresholdError("Threshold SET acknowledgement did not match the request")
            after = self._read_locked()
            expected_scaled = [expected * 1000 // factor, before["scaled_pair"][1]]
            if (not after["available"] or after["architecture"] != before["architecture"]
                    or after["scale_factor"] != factor
                    or after["raw_pair"] != [expected, before["raw_pair"][1]]
                    or after["scaled_pair"] != expected_scaled):
                raise GrIdleThresholdError("Threshold SET was not verified against the exact raw pair")
            return {**after, "requested_us": value_us, "changed": True}


# Retain the existing GR-only entry point and default controller.
GrIdleThresholdController = IdleThresholdController
