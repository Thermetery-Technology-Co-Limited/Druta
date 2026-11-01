# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-owned engine clock-gating disable requests with independent readback.

A801 type-10 engine bitmaps report supported engines and engines without a
disable reason. They are configured predicates, not physical clock-gating or
power measurements. Cleared bits provide a partial independent witness for
the named ELCG profiler request. This controller never saves or replays
requests after device reset.
"""

from __future__ import annotations

import struct

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .gr_gating_live import GrGatingController, GrGatingError, _ProfilerTransport
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

EngineClockGatingError = GrGatingError
_ROWS = ((1, 0), (1, 1), (0x801, 0), (0x801, 1))


def _request():
    raw = bytearray(lpwr.ATTRIBUTE_SIZE)
    struct.pack_into("<I", raw, 0, len(_ROWS))
    for number, (attribute, chunk) in enumerate(_ROWS):
        struct.pack_into("<HHHHII", raw, 4 + 16 * number, 10, 0, attribute, 0, 0, chunk)
    return bytes(raw)


def _decode(raw):
    request = _request()
    if len(raw) != len(request):
        raise EngineClockGatingError("Engine clock-gating attribute geometry is not understood")
    untouched, items = bytearray(raw), {}
    for number, (attribute, chunk) in enumerate(_ROWS):
        at = 4 + 16 * number
        flags, value = struct.unpack_from("<HI", raw, at + 6)
        if flags not in (0, 1):
            raise EngineClockGatingError("Engine clock-gating attribute availability is malformed")
        items[f"{attribute:x}:{chunk}"] = {"available": bool(flags), "value": value if flags else None}
        untouched[at + 6 : at + 12] = request[at + 6 : at + 12]
    if bytes(untouched) != request:
        raise EngineClockGatingError("Engine clock-gating reply changed routing or opaque bytes")

    def bitmap(attribute):
        chunks = [items[f"{attribute:x}:{chunk}"] for chunk in range(2)]
        if not all(item["available"] for item in chunks):
            return None
        return chunks[0]["value"] | (chunks[1]["value"] << 32)

    supported, eligible = bitmap(1), bitmap(0x801)
    if supported is not None and eligible is not None and eligible & ~supported:
        raise EngineClockGatingError("Engine clock-gating eligible mask exceeds supported engines")
    available = all(item["available"] for item in items.values())
    return {
        "support": available and bool(supported),
        "available": available,
        "supported_engines": supported,
        "eligible_engines": eligible,
        "attribute_chunks": items,
    }


def _same_support(before, after):
    if not before["support"] or not after["support"] or before["supported_engines"] != after["supported_engines"]:
        raise EngineClockGatingError("Engine clock-gating support changed during the request")


class EngineClockGatingController(GrGatingController):
    """One ELCG request on one owned profiler object; explicit release/free."""

    def __init__(self, gpu):
        self.gpu = gpu
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)
        self._profiler = _ProfilerTransport(self.reader, self.identity, feature="ELCG")
        self._owned = False
        self._uncertain = False
        self._observed = 0
        self._last_verification_error = None

    def _once(self):
        self._profiler.check()
        return _decode(lpwr._get(self.reader, self.identity, lpwr.ATTRIBUTE_COMMAND, lpwr.ATTRIBUTE_SIZE, _request()))

    def _read_locked(self):
        for attempt in range(2):
            try:
                first, second = self._once(), self._once()
                if first != second:
                    raise EngineClockGatingError("Engine clock-gating support or eligible masks were not stable")
                return second
            except PerfDiagnosticError as exc:
                if attempt or exc.kind != "transient":
                    raise
        raise AssertionError("unreachable")

    def _snapshot(self, state):
        return {
            **state,
            "owned_request": self._owned,
            "uncertain_outcome": self._uncertain,
            "state": "uncertain" if self._uncertain else "held" if self._owned else "idle",
            "observed_engines": [bit for bit in range(64) if self._observed & (1 << bit)],
            "verification_scope": "partial independent engine eligibility witness",
            "last_verification_error": self._last_verification_error,
        }

    def hold(self):
        with self.gpu._lock:
            if self._uncertain:
                raise EngineClockGatingError(
                    "Engine clock-gating outcome is uncertain; explicitly release this session"
                )
            before = self._read_locked()
            if self._owned:
                if not before["support"] or before["eligible_engines"] & self._observed:
                    raise EngineClockGatingError("The owned ELCG request is no longer independently visible")
                return self._snapshot(before)
            if not before["support"]:
                raise EngineClockGatingError("Engine clock-gating metadata is unavailable or unsupported")
            if not before["eligible_engines"]:
                raise EngineClockGatingError("No eligible engine remains to independently verify an ELCG request")
            try:
                self._profiler.allocate()
                self._uncertain = True
                self._profiler.request()
                self._owned = True
                after = self._read_locked()
                _same_support(before, after)
                if after["eligible_engines"] & ~before["eligible_engines"]:
                    raise EngineClockGatingError("ELCG request unexpectedly added eligible engines")
                observed = before["eligible_engines"] & ~after["eligible_engines"]
                if not observed:
                    raise EngineClockGatingError("ELCG request produced no independent engine eligibility transition")
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
                _same_support(before, after)
                if after["eligible_engines"] != before["eligible_engines"] | self._observed:
                    raise EngineClockGatingError("ELCG release did not match the exact expected eligible engine mask")
                self._last_verification_error = None
            except Exception as exc:
                self._last_verification_error = str(exc)
                raise
            finally:
                self._profiler.free()
                self._owned = False
                self._observed = 0
                self._uncertain = False
            return self._snapshot(after)
