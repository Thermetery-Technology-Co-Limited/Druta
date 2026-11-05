# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only shared adaptive-power policy requests."""

from __future__ import annotations

import struct

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

CONTROLLERS = {0: "Graphics (GR)", 2: "Memory subsystem (MS)", 4: "NVDEC",
               5: "NVENC", 6: "NVJPG", 7: "OFA"}
_IMPLEMENTATIONS = frozenset({2, 3, 5, 6, 7})
_SET = 0x2080E802


class AdaptivePowerError(RuntimeError):
    """Adaptive-power request or readback could not be verified."""


class AdaptivePowerController:
    """Change the selected shared reason bit; aggregate state is not ownership."""

    def __init__(self, gpu, index=0):
        if type(index) is not int or index not in CONTROLLERS:
            raise ValueError("Unsupported adaptive-power role")
        self.gpu = gpu
        self.index = index
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)

    def _attribute(self, attribute):
        request = bytearray(lpwr.ATTRIBUTE_SIZE)
        struct.pack_into("<IHHHHII", request, 0, 1, 4, self.index, attribute, 0, 0, 0)
        response = lpwr._get(self.reader, self.identity, lpwr.ATTRIBUTE_COMMAND, lpwr.ATTRIBUTE_SIZE, bytes(request))
        if len(response) != len(request):
            raise AdaptivePowerError("Malformed adaptive-power response size")
        flags, value = struct.unpack_from("<HI", response, 10)
        copy = bytearray(response)
        copy[10:16] = request[10:16]
        if copy != request or flags not in (0, 1) or (not flags and value != 0):
            raise AdaptivePowerError("Adaptive-power response changed routing or opaque fields")
        if flags and value not in (0, 1):
            raise AdaptivePowerError("Adaptive-power response is not Boolean")
        return bool(flags), value

    def _once(self):
        raw = lpwr._get(self.reader, self.identity, 0x20801701, 16, bytes(16))
        if len(raw) != 16:
            raise AdaptivePowerError("Malformed native architecture response")
        architecture, implementation, revision, auxiliary = struct.unpack("<4I", raw)
        arch = {"architecture": architecture, "implementation": implementation,
                "revision": revision, "raw_auxiliary": auxiliary}
        state = {"index": self.index, "name": CONTROLLERS[self.index], "available": False,
                 "status": "unsupported_layout", "architecture": arch, "enabled": None}
        if architecture != 0x1B0 or implementation not in _IMPLEMENTATIONS:
            return state
        valid, supported = self._attribute(9)
        if not valid:
            return {**state, "status": "unavailable"}
        if supported != 1:
            raise AdaptivePowerError("Valid adaptive-power support must be true")
        valid, enabled = self._attribute(10)
        if not valid:
            return {**state, "status": "unavailable"}
        return {**state, "available": True, "status": "available", "enabled": bool(enabled)}

    def _read_locked(self):
        for attempt in range(2):
            try:
                first, second = self._once(), self._once()
                if first != second:
                    raise AdaptivePowerError("Adaptive-power readback was unstable")
                return second
            except PerfDiagnosticError as exc:
                if attempt or exc.kind != "transient":
                    raise
        raise AssertionError("unreachable")

    def read(self):
        with self.gpu._lock:
            return {**self._read_locked(), "requested_allowed": None, "request_accepted": False,
                    "before_enabled": None, "verification": "read_only"}

    def set_allowed(self, allowed):
        if type(allowed) is not bool:
            raise ValueError("Adaptive-power request must be Boolean")
        with self.gpu._lock:
            before = self._read_locked()
            if not before["available"]:
                raise AdaptivePowerError("Selected adaptive-power role is unavailable")
            if allowed and before["enabled"]:
                return {**before, "requested_allowed": True, "request_accepted": False,
                        "before_enabled": True, "verification": "already_allowed"}
            request = bytearray(lpwr.ATTRIBUTE_SIZE)
            struct.pack_into("<IHHHHII", request, 0, 1, 4, self.index, 10, 0, int(allowed), 0)
            response = lpwr._get(self.reader, self.identity, _SET, lpwr.ATTRIBUTE_SIZE, bytes(request))
            struct.pack_into("<H", request, 10, 1)
            if response != bytes(request):
                raise AdaptivePowerError("Adaptive-power SET acknowledgement did not match exactly")
            after = self._read_locked()
            if not after["available"] or after["architecture"] != before["architecture"]:
                raise AdaptivePowerError("Adaptive-power capability or architecture changed after SET")
            if before["enabled"] != after["enabled"] and after["enabled"] == allowed:
                verification = "aggregate_transition"
            else:
                verification = "aggregate_only"
            return {**after, "requested_allowed": allowed, "request_accepted": True,
                    "before_enabled": before["enabled"], "verification": verification}
