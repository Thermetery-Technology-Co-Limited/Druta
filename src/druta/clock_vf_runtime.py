# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live host Clock VF policies through the understood empty-domain-mask SET.

The two flags do not recompute the startup-derived VF mode. A driver may
reject a change in its current mode. No registry seeding, restart, curve SET,
or voltage SET occurs here. PMU serialization is traced; physical evaluator
behavior is not established by the host readback.
"""

from __future__ import annotations

import copy
import ctypes
from dataclasses import dataclass

from . import clock_vf_registry as vf
from .perf_diagnostics import PerfDiagnosticsReader
from .registry_flags import _same_state

_SET = 0x2080D01C


class ClockVfError(RuntimeError):
    """A rejected/unavailable policy is distinct from a false policy value."""

    def __init__(self, kind, message, status=None, *, rollback=None):
        super().__init__(message)
        self.kind, self.status, self.rollback = kind, status, rollback


@dataclass
class _Write:
    dispatched: bool = False


def _error(exc, kind="malformed"):
    if isinstance(exc, ClockVfError):
        return exc
    return ClockVfError(getattr(exc, "kind", kind), str(exc), getattr(exc, "status", None))


class ClockVfController:
    """One paired adapter; create a new controller after a driver restart."""

    def __init__(self, gpu):
        self.gpu = gpu
        self._reader = PerfDiagnosticsReader(gpu)

    def _binding(self, expected=None):
        try:
            identity = vf._binding(self._reader)
        except Exception as exc:
            raise _error(exc, "identity") from exc
        if expected is not None and identity != expected:
            raise ClockVfError("identity", "Clock VF target changed")
        return identity

    def _read_locked(self, transport=None, identity=None):
        # Only a pre-write read may capture/recover the transport. After a SET,
        # even compensating writes and bounded GET retries keep the same one.
        fixed = transport is not None
        for attempt in range(2):
            try:
                self._binding(identity)
                if fixed:
                    current = transport
                else:
                    try:
                        current = self._reader._transport()
                    except Exception as exc:
                        raise _error(exc, "transient") from exc
                first = vf._once(self.gpu, self._reader, current)
                second = vf._once(self.gpu, self._reader, current)
                if not _same_state(first, second):
                    raise ClockVfError("unstable", "Clock VF policies changed between fresh reads")
                second["stable_reads"] = 2
                vf._shape(second, writable=True)
                self._binding(second["identity"] if identity is None else identity)
                return second, current
            except Exception as exc:
                error = _error(exc)
                if error.kind != "transient" or attempt == 1:
                    raise error from exc
                if not fixed:
                    self.gpu._current_limit_transport = None
                    self.gpu._current_limit_layout_choice = None
        raise AssertionError("Bounded Clock VF read did not return or fail")

    def read(self):
        """Return the existing Clock VF observation shape from two fresh GETs."""
        with self.gpu._lock:
            observation, _ = self._read_locked()
            return observation

    def _set_once(self, transport, identity, raw_header, write):
        """Dispatch at most once; preserve all bytes except selected flags."""
        try:
            vf._header(raw_header)
        except (TypeError, ValueError) as exc:
            raise ClockVfError("malformed", str(exc)) from exc
        self._binding(identity)
        header, fields = transport
        if (
            len(header) != 17
            or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in header)
            or any(header[i] != v for i, v in vf._ENVELOPE.items())
            or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values())
        ):
            raise ClockVfError("malformed", "Clock VF SET needs the validated paired NVDA transport")
        payload = raw_header + bytes([vf._SENTINEL]) * (vf._SIZE - vf._HEADER_SIZE)
        size = 68 + vf._SIZE
        storage = (ctypes.c_ubyte * (size + 2 * vf._GUARD_SIZE))()
        ctypes.memset(ctypes.addressof(storage), vf._SENTINEL, len(storage))
        packet = (ctypes.c_uint32 * (size // 4)).from_buffer(storage, vf._GUARD_SIZE)
        for index, word in enumerate(header):
            packet[index] = word
        packet[2], packet[14], packet[15], packet[16] = size, _SET, vf._SIZE, 0
        packet[5] = packet[11] = 0
        ctypes.memmove(ctypes.addressof(packet) + 68, payload, len(payload))
        expected = tuple(packet[:16])
        write.dispatched = True
        try:
            status = self.gpu._legacy_clk_escape(packet, dict(fields))
        except Exception as exc:
            raise ClockVfError("transient", f"Clock VF SET transport failed: {exc}") from exc
        self._binding(identity)
        if any(v != vf._SENTINEL for v in storage[: vf._GUARD_SIZE]) or any(
            v != vf._SENTINEL for v in storage[-vf._GUARD_SIZE :]
        ):
            raise ClockVfError("malformed", "Clock VF SET exceeded packet bounds")
        if type(status) is not int or status != 0:
            raise ClockVfError("transient", f"Clock VF SET transport failed (NTSTATUS {status})", status)
        if any(packet[i] != v for i, v in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
            raise ClockVfError("malformed", "Clock VF SET changed its bound transport envelope")
        if packet[11] != 1:
            raise ClockVfError("transient", f"Clock VF SET completion failed (0x{packet[11]:X})", int(packet[11]))
        if bytes(packet)[68:] != payload:
            raise ClockVfError("malformed", "Clock VF SET changed its request payload")
        if packet[16]:
            status = int(packet[16])
            kind = "unsupported" if status in (0x1F, 0x56, 0x6C) else "transient"
            message = (
                "Clock VF policy request was refused in the current startup mode"
                if status in (0x56, 0x6C)
                else ("Clock VF SET rejected")
            )
            raise ClockVfError(kind, f"{message} (RM 0x{status:X})", status)

    def _rollback(self, before, transport):
        write = _Write()
        result = {"attempted": False, "verified": False, "readback": None, "error": None}
        errors = []
        try:
            self._set_once(transport, before["identity"], bytes.fromhex(before["header_hex"]), write)
        except BaseException as exc:
            errors.append(str(exc))
        # Also read after a failed compensating SET: failures may follow writes.
        try:
            current, _ = self._read_locked(transport, before["identity"])
            result["readback"] = current
            if not _same_state(current, before):
                errors.append("Original Clock VF header did not match fresh readback")
        except BaseException as exc:
            errors.append(str(exc))
        result["attempted"] = write.dispatched
        result["verified"] = not errors
        result["error"] = "; ".join(errors) or None
        return result

    def set(self, changes):
        """Set named Boolean policies and return exact fresh host readback.

        An identity request only reads. A dispatched error or mismatched
        readback triggers one compensating SET of the full original header.
        No requested or compensating SET is retried, and no transport is
        recaptured after dispatch. Unavailability never becomes false.
        """
        try:
            selected = vf._changes(changes)
        except Exception as exc:
            raise _error(exc, "invalid") from exc
        with self.gpu._lock:
            before, transport = self._read_locked()
            wanted = copy.deepcopy(before)
            raw = bytearray.fromhex(before["header_hex"])
            for key, value in selected.items():
                field, offset, _, _ = vf._EDITABLE[key]
                raw[offset], wanted[field] = int(value), value
            wanted["header_hex"] = raw.hex()
            if _same_state(before, wanted):
                return before
            write = _Write()
            try:
                self._set_once(transport, before["identity"], bytes(raw), write)
                after, _ = self._read_locked(transport, before["identity"])
                if not _same_state(after, wanted):
                    raise ClockVfError("readback", "Clock VF SET did not preserve the exact requested header")
                return after
            except BaseException as exc:
                if not write.dispatched:
                    raise
                rollback = self._rollback(before, transport)
                detail = (
                    "Original Clock VF header restored and verified"
                    if rollback["verified"]
                    else ("Clock VF rollback was not verified: " + str(rollback["error"]))
                )
                if not isinstance(exc, Exception):
                    exc.add_note(detail)
                    exc.rollback = rollback
                    raise
                error = _error(exc, "transient")
                raise ClockVfError(error.kind, f"{error}. {detail}", error.status, rollback=rollback) from exc
