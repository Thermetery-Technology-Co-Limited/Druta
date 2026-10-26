# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live NVIDIA voltage-request admission flag, with paired readback and undo.

A030 GET (four bytes) and A031 SET (previous-state/new-state DWORDs) are
traced in experiments/registry-overrides/driver-controls. This flag admits
voltage requests; it is not a voltage value, ceiling or enforcement bypass.
No registry entry is written and no device restart is requested.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

_GET = 0x2080A030
_SET = 0x2080A031
_SENTINEL = 0xA5A55A5A
# NVDA escape envelope, distinct from the RM status at word 16. The kernel
# writes a caller/context flag at +0x14 and the escape result at +0x2C.
# See experiments/registry-overrides/speedo-iddq/voltage-gate-envelope.json.
_ENVELOPE = {0: 0x4E564441, 1: 0x10002, 3: 0x4E562A2A, 4: 0x0500002B}
_ENVELOPE_OUTPUTS = (5, 11)


class VoltageRequestGateError(RuntimeError):
    """Unavailable/invalid state is an error, never a disabled-state result.

    rollback is None when no SET was dispatched. Otherwise it describes the
    compensating SET and its independent fresh readback, including failures.
    """

    def __init__(self, kind: str, message: str, status=None, *, rollback=None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.rollback = rollback


@dataclass
class _Write:
    dispatched: bool = False
    previous: bool | None = None


class VoltageRequestGate:
    """Control one sticky PCI-paired GPU binding; recreate after device restart."""

    def __init__(self, gpu):
        self.gpu = gpu
        self._reader = PerfDiagnosticsReader(gpu, attempts=2)

    def _reset_transport(self):
        self.gpu._current_limit_transport = None
        self.gpu._current_limit_layout_choice = None

    def _binding(self):
        try:
            api, slot, architecture = self._reader._binding()
        except PerfDiagnosticError as exc:
            raise VoltageRequestGateError(exc.kind, str(exc), exc.status) from exc
        except Exception as exc:
            raise VoltageRequestGateError("transient", f"GPU binding could not be read: {exc}") from exc
        selected = api.selected
        for name, maximum in (("devid", 0xFFFF), ("subsys", 0xFFFFFFFF)):
            if type(selected.get(name)) is not int or not 0 <= selected[name] <= maximum:
                raise VoltageRequestGateError("identity", "The selected GPU has no complete PCI identity")
        return {
            "slot": slot,
            "architecture": architecture,
            "device_id": selected["devid"],
            "subsystem_id": selected["subsys"],
        }

    def _transport(self):
        self._binding()
        try:
            transport = self._reader._transport()
        except PerfDiagnosticError as exc:
            raise VoltageRequestGateError(exc.kind, str(exc), exc.status) from exc
        except Exception as exc:
            raise VoltageRequestGateError("transient", f"Paired RM transport could not be captured: {exc}") from exc
        self._binding()
        if any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in transport[1].values()):
            raise VoltageRequestGateError("malformed", "Captured escape fields have an invalid encoding")
        if any(transport[0][index] != value for index, value in _ENVELOPE.items()):
            raise VoltageRequestGateError("malformed", "Captured RM transport has an unrecognized NVDA envelope")
        return transport

    def _call(self, command, transport, *, enabled=None, write=None):
        """Exactly one understood command; SET is never retried here."""
        if command == _GET and enabled is None and write is None:
            payload = [_SENTINEL]
        elif command == _SET and type(enabled) is bool and isinstance(write, _Write):
            payload = [0, int(enabled)]
        else:
            raise ValueError("Only a gate GET or Boolean gate SET is allowed")
        self._binding()
        header, fields = transport
        packet = (ctypes.c_uint32 * (17 + len(payload)))(*header, *payload)
        packet[2], packet[14], packet[15], packet[16] = ctypes.sizeof(packet), command, 4 * len(payload), 0
        # NVAPI zero-initializes these outputs before the escape. Do not reuse
        # a cached completion status as evidence of a fresh successful call.
        packet[5] = packet[11] = 0
        expected_header = tuple(packet[:16])
        if write is not None:
            # From here onward even an exception can mean the write happened.
            write.dispatched = True
        try:
            status = self.gpu._legacy_clk_escape(packet, dict(fields))
        except Exception as exc:
            raise VoltageRequestGateError(
                "transient", f"Voltage-request gate transport failed ({type(exc).__name__}): {exc}"
            ) from exc

        # Only a complete successful response makes the previous-state word
        # trustworthy. Retain it before the post-call identity check.
        if type(status) is not int or status != 0:
            raise VoltageRequestGateError("transient", f"Voltage-request gate transport failed (NTSTATUS {status})", status)
        if any(packet[index] != value for index, value in enumerate(expected_header)
               if index not in _ENVELOPE_OUTPUTS):
            raise VoltageRequestGateError("malformed", "Voltage-request gate response changed its transport header")
        if packet[5] not in (0, 1):
            raise VoltageRequestGateError("malformed", "Voltage-request gate returned an invalid NVDA context flag")
        if packet[11] != 1:
            escape_status = int(packet[11])
            raise VoltageRequestGateError(
                "transient", f"Voltage-request gate escape did not succeed (NVDA 0x{escape_status:X})", escape_status
            )
        if packet[16] != 0:
            rm_status = int(packet[16])
            kind = "unsupported" if rm_status in (0x1F, 0x56) else "transient"
            raise VoltageRequestGateError(kind, f"Voltage-request gate rejected (RM 0x{rm_status:X})", rm_status)
        if packet[17] not in (0, 1):
            raise VoltageRequestGateError("malformed", "Voltage-request gate did not return a canonical Boolean")
        if command == _SET and packet[18] != int(enabled):
            raise VoltageRequestGateError("malformed", "Voltage-request gate SET changed its requested state")
        value = bool(packet[17])
        if write is not None:
            write.previous = value
        self._binding()
        return value

    def _read_locked(self):
        for attempt in range(2):
            try:
                identity = self._binding()
                transport = self._transport()
                first = self._call(_GET, transport)
                second = self._call(_GET, transport)
                if first is not second:
                    raise VoltageRequestGateError("unstable", "Voltage-request admission changed between fresh reads")
                if self._binding() != identity:
                    raise VoltageRequestGateError("identity", "GPU identity changed during voltage-request gate read")
                return {"enabled": first, "identity": identity}
            except VoltageRequestGateError as exc:
                if exc.kind != "transient":
                    raise
                self._reset_transport()
                if attempt == 1:
                    raise
            except Exception as exc:
                self._reset_transport()
                if attempt == 1:
                    raise VoltageRequestGateError(
                        "transient", f"Voltage-request gate read failed ({type(exc).__name__}): {exc}"
                    ) from exc
        raise AssertionError("Bounded gate read did not return or fail")

    def read(self):
        """Return two matching fresh GETs, retrying transient reads at most once."""
        with self.gpu._lock:
            return self._read_locked()

    def _rollback(self, enabled):
        write = _Write()
        result = {"attempted": False, "verified": False, "target": enabled, "readback": None, "error": None}
        try:
            # The same reader's sticky identity is retained even after failure.
            # If it no longer matches, no compensating write is dispatched.
            transport = self._transport()
            self._call(_SET, transport, enabled=enabled, write=write)
            snapshot = self._read_locked()
            result["readback"] = snapshot
            if snapshot["enabled"] is not enabled:
                raise VoltageRequestGateError("readback", "Restored admission state did not match fresh readback")
            result["verified"] = True
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["attempted"] = write.dispatched
        return result

    def set_enabled(self, enabled: bool, *, expected: bool | None = None):
        """Set admission and verify it, restoring the atomic prior state on error.

        expected guards a stale UI snapshot. Even an identity write is sent and
        checked. A concurrent change detected in SET's previous-state output
        fails the operation and restores that returned prior state, rather than
        the older pre-read. A failed SET is never retried as the requested write.
        """
        if type(enabled) is not bool:
            raise TypeError("Voltage-request admission must be a Boolean")
        if expected is not None and type(expected) is not bool:
            raise TypeError("Expected voltage-request admission must be a Boolean or None")
        with self.gpu._lock:
            before = self._read_locked()
            if expected is not None and before["enabled"] is not expected:
                raise VoltageRequestGateError("stale", "Voltage-request admission changed since the displayed snapshot")
            write = _Write()
            try:
                transport = self._transport()
                previous = self._call(_SET, transport, enabled=enabled, write=write)
                if previous is not before["enabled"]:
                    raise VoltageRequestGateError("stale", "Another client changed admission before the SET completed")
                after = self._read_locked()
                if after["enabled"] is not enabled:
                    raise VoltageRequestGateError("readback", "Requested admission state did not match fresh readback")
                return {
                    "before": before["enabled"],
                    "after": after["enabled"],
                    "atomic_previous": previous,
                    "readback": after,
                }
            except BaseException as exc:
                if not write.dispatched:
                    raise
                target = before["enabled"] if write.previous is None else write.previous
                # Discard a possibly interrupted transport, but never the binding.
                self._reset_transport()
                rollback = self._rollback(target)
                detail = "Previous admission state restored and verified" if rollback["verified"] else (
                    "Admission rollback was not verified: " + str(rollback["error"])
                )
                if not isinstance(exc, Exception):
                    exc.add_note(detail)
                    exc.rollback = rollback
                    raise
                kind = getattr(exc, "kind", "transient")
                status = getattr(exc, "status", None)
                raise VoltageRequestGateError(kind, f"{exc}. {detail}", status, rollback=rollback) from exc
