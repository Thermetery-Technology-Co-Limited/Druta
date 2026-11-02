# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-owned engine clock-gating requests with independent readback.

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
        self._owned_mode = None
        self._arbitration_verified = False
        self._arbitration_probe = None
        self._request_supported = None

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
            "owned_mode": self._owned_mode,
            "arbitration_verified": self._arbitration_verified,
            "uncertain_outcome": self._uncertain,
            "state": "uncertain" if self._uncertain else "held" if self._owned else "idle",
            "observed_engines": [bit for bit in range(64) if self._observed & (1 << bit)],
            "verification_scope": (
                "partial enabled eligibility and conflicting profiler request rejection; not unique ownership"
                if self._owned_mode == "enabled"
                else "partial independent engine eligibility witness"
            ),
            "last_verification_error": self._last_verification_error,
        }

    def hold(self):
        """Own the default DISABLE request with a positive eligibility transition."""
        return self._hold("disabled")

    def hold_enabled(self):
        """Own ENABLE only from complete all-eligible support, with a conflict probe.

        The callback's reachable indices0..44 fit the complete64-bit bitmap.
        Nonzero supported==eligible establishes zero aggregate reasons for every
        reachable supported index; it does not establish unique ownership or
        physical gating. Release/free remains this object's explicit lifecycle.
        """
        return self._hold("enabled")

    def _check_enabled(self, state):
        if (
            not state["support"]
            or not state["available"]
            or state["supported_engines"] != self._request_supported
            or state["eligible_engines"] != state["supported_engines"]
        ):
            raise EngineClockGatingError(
                "ELCG ENABLE support or eligibility changed from the complete all-eligible inventory"
            )

    def _verify_enable_arbitration(self, enabled):
        self._arbitration_probe = _ProfilerTransport(self.reader, self.identity, feature="ELCG")
        try:
            self._arbitration_probe.allocate()
            self._arbitration_probe.probe_disable_conflict()
        finally:
            self._arbitration_probe.free()
            self._arbitration_probe = None
        after = self._read_locked()
        _same_support(enabled, after)
        self._check_enabled(after)
        self._arbitration_verified = True

    def _hold(self, mode):
        with self.gpu._lock:
            if self._uncertain:
                raise EngineClockGatingError(
                    "Engine clock-gating outcome is uncertain; explicitly release this session"
                )
            before = self._read_locked()
            if self._owned:
                if self._owned_mode != mode:
                    raise EngineClockGatingError("Release the owned ELCG request before changing mode")
                if mode == "enabled":
                    self._check_enabled(before)
                elif not before["support"] or before["eligible_engines"] & self._observed:
                    raise EngineClockGatingError("The owned ELCG request is no longer independently visible")
                return self._snapshot(before)
            if not before["support"]:
                raise EngineClockGatingError("Engine clock-gating metadata is unavailable or unsupported")
            if not before["eligible_engines"]:
                raise EngineClockGatingError("No eligible engine remains to independently verify an ELCG request")
            if mode == "enabled" and before["supported_engines"] != before["eligible_engines"]:
                raise EngineClockGatingError("ELCG ENABLE requires complete nonzero supported==eligible engine bitmaps")
            self._request_supported = before["supported_engines"]
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
                _same_support(before, after)
                if mode == "enabled":
                    self._check_enabled(after)
                    self._observed = 0
                    self._verify_enable_arbitration(after)
                else:
                    if after["eligible_engines"] & ~before["eligible_engines"]:
                        raise EngineClockGatingError("ELCG request unexpectedly added eligible engines")
                    observed = before["eligible_engines"] & ~after["eligible_engines"]
                    if not observed:
                        raise EngineClockGatingError(
                            "ELCG request produced no independent engine eligibility transition"
                        )
                    self._observed = observed
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
                if self._owned_mode == "enabled":
                    self._check_enabled(before)
                if self._profiler.handle is not None and self._profiler.requested:
                    self._profiler.release()
                    self._owned = False
                after = self._read_locked()
                _same_support(before, after)
                if self._owned_mode == "enabled":
                    self._check_enabled(after)
                elif after["eligible_engines"] != before["eligible_engines"] | self._observed:
                    raise EngineClockGatingError("ELCG release did not match the exact expected eligible engine mask")
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
                        self._observed = 0
                        self._owned_mode = None
                        self._arbitration_verified = False
                        self._request_supported = None
            return self._snapshot(after)

    def close(self):
        """Explicitly release/free only this controller's own request and probe."""
        with self.gpu._lock:
            if self._profiler.handle is None and self._arbitration_probe is None:
                return None
            return self.release()
