# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Balanced session-only individual reason0 references through named E802 attr37.

This is a shared count, with no native object or unique client ownership getter.
Only the selected controller is observed. Reason masks do not measure physical
power gating, and other owners can keep reason0 set after our one release.
Callbacks may reevaluate other controllers; this is not a GR-only physical claim.
"""

from __future__ import annotations

import struct

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

_CONTROLLERS = {
    0: "Graphics (GR)",
    4: "Memory subsystem (MS)",
    10: "MS DIFR SW-ASR",
    11: "MS DIFR CG",
    12: "NVDEC",
    13: "NVENC",
    14: "OFA",
    15: "NVJPG",
}
# Known native families; runtime callback selection is separate. A callback
# can fail after changing the count/bit, so acknowledgement and readback remain
# mandatory and a release dispatch is never blindly repeated.
_SUPPORTED_FAMILIES = frozenset({0x160, 0x170, 0x190, 0x1B0})
# Controller 4 has an additional hardware path on Turing. The listed native
# pairs select the source-verified no-op auxiliary path on Ampere and later.
_MS_IMPLEMENTATIONS = {
    0x170: frozenset({2, 3, 4, 6, 7}),
    0x190: frozenset({2, 3, 4, 6, 7}),
    0x1B0: frozenset({2, 3, 5, 6, 7}),
}
_SET_COMMAND = 0x2080E802


class IndividualPowerGatingError(RuntimeError):
    """A selected shared reference or its independent readback was not verified."""


class IndividualPowerGatingController:
    """Acquire/release one selected shared reference; never retry a SET."""

    def __init__(self, gpu, index):
        if type(index) is not int or index not in _CONTROLLERS:
            raise ValueError(
                "Only individual controller indices 0, 4, 10, 11, 12, 13, 14 and 15 are understood"
            )
        self.gpu, self.index = gpu, index
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)
        self._owned = False
        self._uncertain = False
        self._acquire_attempted = False
        self._release_attempted = False
        self._last_verification_error = None

    def _attribute(self, attribute):
        request = lpwr._request(attribute, (self.index,))
        raw = lpwr._get(self.reader, self.identity, lpwr.ATTRIBUTE_COMMAND, lpwr.ATTRIBUTE_SIZE, request)
        return lpwr._attributes(raw, attribute, (self.index,))[str(self.index)]

    def _once(self):
        raw = lpwr._get(self.reader, self.identity, 0x20801701, 16, bytes(16))
        if len(raw) != 16:
            raise IndividualPowerGatingError("Individual native architecture geometry is not understood")
        architecture, implementation, revision, auxiliary = struct.unpack("<4I", raw)
        state = {
            "architecture": {
                "architecture": architecture,
                "implementation": implementation,
                "revision": revision,
                "raw_auxiliary": auxiliary,
            },
            "support": False,
            "available": False,
            "reason_masks": {0x48: None, 0x49: None},
        }
        if architecture not in _SUPPORTED_FAMILIES:
            return {**state, "status": "unsupported_family"}
        if self.index == 4 and implementation not in _MS_IMPLEMENTATIONS.get(architecture, ()):
            return {**state, "status": "unsupported_layout"}
        available, supported = self._attribute(0x39)
        state["available"] = available
        if not available or not supported:
            return {**state, "status": "unsupported" if available else "unavailable"}
        values = {attribute: self._attribute(attribute) for attribute in (0x48, 0x49)}
        state["available"] = all(item[0] for item in values.values())
        state["support"] = state["available"]
        state["reason_masks"] = {attribute: value for attribute, (_, value) in values.items()}
        return {**state, "status": "available" if state["available"] else "unavailable"}

    def _read_locked(self):
        for attempt in range(2):
            try:
                first, second = self._once(), self._once()
                if first != second:
                    raise IndividualPowerGatingError("Selected individual support or reason masks were not stable")
                return second
            except PerfDiagnosticError as exc:
                if attempt or exc.kind != "transient":
                    raise
        raise AssertionError("unreachable")

    def _snapshot(self, state):
        return {
            **state,
            "index": self.index,
            "name": _CONTROLLERS[self.index],
            "owned_request": self._owned,
            "uncertain_outcome": self._uncertain,
            "release_attempted": self._release_attempted,
            "state": "uncertain" if self._uncertain else "held" if self._owned else "idle",
            "verification_scope": "selected shared reason0 reference; no unique ownership or physical power-gating claim",
            "last_verification_error": self._last_verification_error,
        }

    def read(self):
        with self.gpu._lock:
            return self._snapshot(self._read_locked())

    def _set(self, value):
        request = bytearray(lpwr.ATTRIBUTE_SIZE)
        struct.pack_into("<I", request, 0, 1)
        struct.pack_into("<HHHHII", request, 4, 3, self.index, 0x37, 0, value, 0)
        response = lpwr._get(self.reader, self.identity, _SET_COMMAND, lpwr.ATTRIBUTE_SIZE, bytes(request))
        struct.pack_into("<H", request, 10, 1)
        if response != bytes(request):
            raise IndividualPowerGatingError(
                "Individual SET did not acknowledge exact flags1 with all other request bytes preserved"
            )

    @staticmethod
    def _same_support(before, after):
        if not after["support"] or not after["available"] or before["architecture"] != after["architecture"]:
            raise IndividualPowerGatingError("Selected individual support or native architecture changed")

    def hold(self):
        with self.gpu._lock:
            if self._uncertain:
                raise IndividualPowerGatingError(
                    "Individual reference outcome is uncertain; explicitly release this session"
                )
            before = self._read_locked()
            if not before["support"]:
                raise IndividualPowerGatingError("Selected individual controller is unavailable or unsupported")
            if self._owned:
                if not before["reason_masks"][0x48] & 1:
                    raise IndividualPowerGatingError("The pending individual reason0 reference is no longer visible")
                return self._snapshot(before)
            if before["reason_masks"][0x48] & 1:
                raise IndividualPowerGatingError(
                    "Individual hold requires selected reason48 bit0 clear before acquisition"
                )
            self._release_attempted = False
            self._acquire_attempted = True
            self._uncertain = True
            write_error = None
            try:
                self._set(0)
            except Exception as exc:
                write_error = exc
            try:
                after = self._read_locked()
                if after["reason_masks"][0x48] is not None and after["reason_masks"][0x48] & 1:
                    self._owned = True  # Even a failed response can follow an acquired reference.
                self._same_support(before, after)
                expected = {**before["reason_masks"], 0x48: before["reason_masks"][0x48] | 1}
                if after["reason_masks"] != expected:
                    raise IndividualPowerGatingError(
                        "Individual acquire did not match the exact selected reason0 transition and preserved reason masks"
                    )
                if write_error is not None:
                    raise write_error
                self._last_verification_error = None
                self._uncertain = False
                return self._snapshot(after)
            except Exception as exc:
                self._last_verification_error = (
                    f"{write_error}; {exc}" if write_error is not None and exc is not write_error else str(exc)
                )
                raise

    def release(self):
        with self.gpu._lock:
            # A dispatched decrement can succeed even if its reply/readback fails.
            # Further release/close calls therefore observe only; never decrement twice.
            if self._release_attempted or not (self._owned or self._acquire_attempted):
                return self._snapshot(self._read_locked())
            before = self._read_locked()
            if not before["support"]:
                raise IndividualPowerGatingError(
                    "Selected individual support is unavailable; no release was dispatched"
                )
            if not before["reason_masks"][0x48] & 1:
                self._owned = self._acquire_attempted = self._uncertain = False
                return self._snapshot(before)  # No independent pending reference to decrement.
            self._owned = True
            self._uncertain = True
            self._release_attempted = True
            write_error = None
            try:
                self._set(1)
                self._owned = self._acquire_attempted = False
            except Exception as exc:
                write_error = exc
            try:
                after = self._read_locked()
                self._same_support(before, after)
                if not after["reason_masks"][0x48] & 1:
                    self._owned = self._acquire_attempted = False
                expected = dict(before["reason_masks"])
                expected[0x48] &= ~1
                if after["reason_masks"] not in (expected, before["reason_masks"]):
                    raise IndividualPowerGatingError("Individual release changed unrelated selected reason bits")
                if write_error is not None:
                    raise write_error
                self._last_verification_error = None
                self._uncertain = False
                return self._snapshot(after)
            except Exception as exc:
                self._last_verification_error = (
                    f"{write_error}; {exc}" if write_error is not None and exc is not write_error else str(exc)
                )
                raise

    def close(self):
        """Balance this session's pending reference at most once; no native object exists."""
        with self.gpu._lock:
            if not (self._owned or self._acquire_attempted or self._uncertain):
                return None
            return self.release()
