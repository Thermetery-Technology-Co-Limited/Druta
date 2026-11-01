# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-owned GR ELPG disable requests and independent driver predicates.

The public B2CC profiler interface owns one request on one allocated object.
A successful request response alone is insufficient: Windows may implement a
successful stub. A801 predicates verify a bounded observable mask transition;
they do not measure physical gating residency, clocks, or power consumption.
This object never rebinds, persists, or reapplies a request after device reset.
"""

from __future__ import annotations

import ctypes
import secrets

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

_CLASS = 0xB2CC
_REQUEST, _RELEASE = 0xB0CC0301, 0xB0CC0302
_ELPG_DISABLE, _ELPG_SELECTOR = 0x40, 0xC0
_INDICES = tuple(range(22))


class GrGatingError(RuntimeError):
    """A session request or its independent readback was not established."""


class _ProfilerTransport:
    """Only allocate, control, and free our own B2CC object under this parent."""

    def __init__(self, reader, identity):
        self.reader, self.identity = reader, identity
        self.header, self.fields = self._capture()
        self.handle = None
        self.requested = False

    def _capture(self):
        if clock._binding(self.reader) != self.identity:
            raise GrGatingError("GR gating target changed; create a fresh session")
        header, fields = self.reader._transport()
        if (
            len(header) != 17
            or any(header[i] != value for i, value in clock._ENVELOPE.items())
            or not header[12]
            or not header[13]
            or any(type(word) is not int or not 0 <= word <= 0xFFFFFFFF for word in header)
            or any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in fields.values())
        ):
            raise GrGatingError("GR gating requires the understood paired RM transport")
        return tuple(header), dict(fields)

    def check(self):
        header, fields = self._capture()
        # Getter-specific output fields may change; the borrowed root, parent,
        # opaque routing fields, and escape target must remain the same.
        if fields != self.fields or any(header[i] != self.header[i] for i in (*range(2), 3, 4, *range(6, 11), 12, 13)):
            raise GrGatingError("GR gating transport changed; requests cannot be replayed")

    def _send(self, words, status_index, mutable=()):
        self.check()
        storage, buffer = clock._guarded(4 * len(words))
        packet = (ctypes.c_uint32 * len(words)).from_buffer(buffer)
        packet[:] = words
        status = self.reader.gpu._legacy_clk_escape(packet, dict(self.fields))
        # Record successful ownership before guard, target, or geometry checks
        # can throw. Cleanup must never free the borrowed root/client/parent.
        if status_index == 19 and status == 0 and packet[11] == 1 and packet[19] == 0:
            self.handle = words[14]
        clock._guards(storage)
        self.check()
        if any(
            packet[i] != value for i, value in enumerate(words) if i not in (5, 11, status_index, *mutable)
        ) or packet[5] not in (0, 1):
            raise GrGatingError("GR gating response changed routing, opaque fields, or geometry")
        if type(status) is not int or status != 0 or packet[11] != 1 or packet[status_index] != 0:
            raise GrGatingError(f"GR gating RM operation failed (NT={status}, RM={packet[status_index]})")
        return tuple(packet)

    def allocate(self):
        if self.handle is not None:
            raise GrGatingError("This session already owns a profiler object")
        handle = 0x60000000 | secrets.randbits(24)
        words = list(self.header[:12]) + [
            self.header[12],
            self.header[13],
            handle,
            _CLASS,
            8,
            0x77FEF81E,
            0,
            0xFFFFFFFF,
            0,
            0,
        ]
        words[2], words[4], words[5], words[11] = 88, 0x0500002A, 0, 0
        self._send(words, 19)

    def _control(self, command, payload, mutable=()):
        if self.handle is None:
            raise GrGatingError("This session has no owned profiler object")
        words = list(self.header) + list(payload)
        words[2], words[5], words[11], words[13] = 4 * len(words), 0, 0, self.handle
        words[14], words[15], words[16] = command, 4 * len(payload), 0xFFFFFFFF
        return self._send(words, 16, mutable)

    def request(self):
        if self.requested:
            raise GrGatingError("One profiler object may own only one GR request")
        self.check()
        self.requested = True  # A dispatched request can have a lost response.
        result = self._control(_REQUEST, (0xFFFFFFFF, _ELPG_DISABLE, 0xFFFFFFFF), (17, 19))
        if result[17] != 0:
            raise GrGatingError(f"GR gating request global status {result[17]}")
        selected_status = (result[19] >> 6) & 3
        if selected_status:
            label = {1: "rejected", 2: "unsupported", 3: "failed"}[selected_status]
            raise GrGatingError(f"GR ELPG request {label}")

    def release(self):
        self._control(_RELEASE, (_ELPG_SELECTOR,))
        self.requested = False

    def free(self):
        if self.handle is None:
            return
        words = list(self.header[:12]) + [self.header[12], self.header[13], self.handle, 0xFFFFFFFF]
        words[2], words[4], words[5], words[11] = 64, 0x0500000C, 0, 0
        self._send(words, 15)
        self.handle = None
        self.requested = False


def _expected_hold(before, after):
    """Return independently observed reason-zero transitions, preserving peers.

    The supported inventory supplies a partial independent witness, not the
    full membership of the driver's GR groups or an ownership getter. At
    least one transition is mandatory; successful no-ops cannot verify support.
    """
    if before["support"] != after["support"] or before["controllers"].keys() != after["controllers"].keys():
        raise GrGatingError("GR gating support inventory changed")
    changed = []
    for index, old in before["controllers"].items():
        new = after["controllers"][index]
        if any(old[key] != new[key] for key in ("available", "supported")):
            raise GrGatingError("GR gating controller identity changed")
        if not old["supported"]:
            if old != new:
                raise GrGatingError("Unsupported controller changed")
            continue
        masks, observed = old["reason_masks"], new["reason_masks"]
        if observed[0x49] != masks[0x49] or observed[0x48] not in (masks[0x48], masks[0x48] | 1):
            raise GrGatingError("GR gating request changed unrelated reason masks")
        if observed[0x48] != masks[0x48]:
            changed.append(index)
    if not changed:
        raise GrGatingError("GR ELPG request produced no independent reason-mask transition")
    return tuple(changed)


def _expected_release(before, after, selected):
    if before["support"] != after["support"] or before["controllers"].keys() != after["controllers"].keys():
        raise GrGatingError("GR gating support inventory changed during release")
    for index, old in before["controllers"].items():
        new = after["controllers"][index]
        if any(old[key] != new[key] for key in ("available", "supported")):
            raise GrGatingError("GR gating controller identity changed during release")
        if not old["supported"]:
            if old != new:
                raise GrGatingError("Unsupported controller changed during release")
            continue
        expected = dict(old["reason_masks"])
        if index in selected:
            expected[0x48] &= ~1
        if new["reason_masks"] != expected:
            raise GrGatingError("GR ELPG release did not match the exact expected reason masks")


class GrGatingController:
    """Own one explicit session request; release/free is explicit and bounded."""

    def __init__(self, gpu):
        self.gpu = gpu
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)
        self._profiler = _ProfilerTransport(self.reader, self.identity)
        self._owned = False
        self._uncertain = False
        self._selected = ()
        self._last_verification_error = None

    def _once(self):
        self._profiler.check()
        raw = lpwr._get(
            self.reader, self.identity, lpwr.ATTRIBUTE_COMMAND, lpwr.ATTRIBUTE_SIZE, lpwr._request(0x39, _INDICES)
        )
        supports = lpwr._attributes(raw, 0x39, _INDICES)
        indices = tuple(i for i in _INDICES if supports[str(i)] == (True, 1))
        attrs = {}
        for attr in (0x37, 0x48, 0x49):
            if not indices:
                break
            raw = lpwr._get(
                self.reader, self.identity, lpwr.ATTRIBUTE_COMMAND, lpwr.ATTRIBUTE_SIZE, lpwr._request(attr, indices)
            )
            attrs[attr] = lpwr._attributes(raw, attr, indices)
            if any(
                not available or (attr == 0x37 and value not in (0, 1)) for available, value in attrs[attr].values()
            ):
                raise GrGatingError("GR gating controller predicate is unavailable or malformed")
        controllers = {}
        for index in _INDICES:
            key = str(index)
            available, supported = supports[key]
            controllers[key] = {
                "available": available,
                "supported": bool(supported) if available else None,
                "enabled_predicate": bool(attrs[0x37][key][1]) if index in indices else None,
                "reason_masks": {attr: attrs[attr][key][1] for attr in (0x48, 0x49)} if index in indices else {},
            }
        return {"support": bool(indices), "controllers": controllers}

    def _read_locked(self):
        for attempt in range(2):
            try:
                first, second = self._once(), self._once()

                # Enabled predicates can change with current activity even
                # while controller support and reason masks are unchanged.
                # They remain latest diagnostic observations, not a condition
                # for verifying the session request's exact mask transition.
                def stable_fields(state):
                    return state["support"], {
                        index: {key: row[key] for key in ("available", "supported", "reason_masks")}
                        for index, row in state["controllers"].items()
                    }

                if stable_fields(first) != stable_fields(second):
                    raise GrGatingError("GR gating support or reason-mask readbacks were not stable")
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
            "observed_controllers": list(self._selected),
            "verification_scope": "partial independent reason-mask witness",
            "enabled_predicate_scope": "latest observation; stability not required",
            "last_verification_error": self._last_verification_error,
        }

    def read(self):
        with self.gpu._lock:
            return self._snapshot(self._read_locked())

    def hold(self):
        with self.gpu._lock:
            if self._uncertain:
                raise GrGatingError("GR gating outcome is uncertain; explicitly release this session")
            before = self._read_locked()
            if self._owned:
                if any(not before["controllers"][index]["reason_masks"][0x48] & 1 for index in self._selected):
                    raise GrGatingError("The owned GR request is no longer visible")
                return self._snapshot(before)
            if not before["support"]:
                raise GrGatingError("GR gating controller predicates are unsupported")
            try:
                self._profiler.allocate()
                self._uncertain = True
                self._profiler.request()
                self._owned = True
                after = self._read_locked()
                self._selected = _expected_hold(before, after)
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
                _expected_release(before, after, self._selected)
                self._last_verification_error = None
            except Exception as exc:
                self._last_verification_error = str(exc)
                raise
            finally:
                # Explicit close/release frees only this allocated object even
                # if independent verification fails. Target drift still blocks
                # the transport rather than sending a stale handle elsewhere.
                self._profiler.free()
                self._owned = False
                self._selected = ()
                self._uncertain = False
            return self._snapshot(after)

    def close(self):
        """Explicit shutdown cleanup; never runs from a finalizer or background task."""
        with self.gpu._lock:
            if self._profiler.handle is None:
                return None
            return self.release()
