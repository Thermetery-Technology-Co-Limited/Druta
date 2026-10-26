# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live client power requests with exact readback and compensating restoration.

E633 changes an existing FE current-arbitration request. INFO limits describe
the ordinary range; they are not this call's admission bounds. Downstream PMU
votes and effective limits can differ from the accepted request. No registry,
persistence, GPU restart or workload operation is performed here.
"""

from __future__ import annotations

import copy
import ctypes
import struct
from dataclasses import dataclass

from . import clock_bios_runtime as clock
from . import power_arbitration
from .perf_diagnostics import PerfDiagnosticError, PerfDiagnosticsReader

INFO, CONTROL, SET = 0x2080A630, 0x2080A632, 0x2080E633
ROLE_INFO, NATIVE_INFO, NATIVE_CONTROL, NATIVE_STATUS = 0x2080A61C, 0x2080A618, 0x2080A61A, 0x2080A619
SIZES = {
    INFO: 0x924,
    CONTROL: 0x328,
    SET: 0x328,
    ROLE_INFO: 0x108,
    NATIVE_INFO: 20512,
    NATIVE_CONTROL: 13876,
    NATIVE_STATUS: 397048,
}
INFO_BASE, INFO_STRIDE, CONTROL_BASE, CONTROL_STRIDE, CAPACITY = 0x24, 0x24, 0x28, 0xC, 64
NATIVE_INFO_BASE, NATIVE_INFO_STRIDE = 0xCC, 0x104
NATIVE_CONTROL_BASE, NATIVE_CONTROL_STRIDE = 0x14, 0xC4
# The understood native interface exposes one u32 occupancy mask. Buffer tails
# are preserved; their size must not be mistaken for extra addressable rows.
NATIVE_CAPACITY = min(
    32,
    (SIZES[NATIVE_INFO] - NATIVE_INFO_BASE) // NATIVE_INFO_STRIDE,
    (SIZES[NATIVE_CONTROL] - NATIVE_CONTROL_BASE) // NATIVE_CONTROL_STRIDE,
    (SIZES[NATIVE_STATUS] - 0x9C) // 0x1730,
)
ROLE, CLIENT, MISSING = 1, 0xFE, 0xFFFFFFFF
_LAYOUT = {
    "info": (NATIVE_INFO_BASE, NATIVE_INFO_STRIDE),
    "control": (NATIVE_CONTROL_BASE, NATIVE_CONTROL_STRIDE),
    "status": (0x9C, 0x1730),
    "sizes": {k: SIZES[k] for k in (NATIVE_INFO, NATIVE_CONTROL, NATIVE_STATUS)},
}
_STATE_KEYS = (
    "schema",
    "stable_reads",
    "identity",
    "binding",
    "requested_mw",
    "info_hex",
    "control_hex",
    "role_info_hex",
    "native_info_hex",
    "native_control_hex",
)


class ClientPowerError(RuntimeError):
    """A typed failure; rollback is None when no SET was dispatched."""

    def __init__(self, kind, message, rollback=None):
        super().__init__(message)
        self.kind, self.rollback = kind, rollback


@dataclass
class _Write:
    dispatched: bool = False


def _u32(raw, offset):
    return struct.unpack_from("<I", raw, offset)[0]


def _words(raw):
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


def _state(observation):
    return {key: observation[key] for key in _STATE_KEYS}


def _project(before, mw):
    result = copy.deepcopy(_state(before))
    result["requested_mw"] = mw
    for name, base, stride, index in (
        ("control_hex", CONTROL_BASE, CONTROL_STRIDE, before["binding"]["client_index"]),
        ("native_control_hex", NATIVE_CONTROL_BASE, NATIVE_CONTROL_STRIDE, before["binding"]["native_index"]),
    ):
        raw = bytearray.fromhex(before[name])
        struct.pack_into("<I", raw, base + index * stride + 4, mw)
        result[name] = raw.hex()
    return result


class ClientPowerController:
    """One sticky PCI-paired GPU; construct a new controller after restart."""

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
            raise ClientPowerError(exc.kind, str(exc)) from exc
        except Exception as exc:
            raise ClientPowerError("transient", f"GPU binding could not be read: {exc}") from exc
        for key, maximum in (("devid", 0xFFFF), ("subsys", 0xFFFFFFFF)):
            if type(api.selected.get(key)) is not int or not 0 <= api.selected[key] <= maximum:
                raise ClientPowerError("identity", "Client power needs a complete paired PCI identity")
        return {
            "slot": slot,
            "architecture": architecture,
            "device_id": api.selected["devid"],
            "subsystem_id": api.selected["subsys"],
        }

    def _transport(self):
        self._binding()
        try:
            header, fields = self._reader._transport()
        except PerfDiagnosticError as exc:
            raise ClientPowerError(exc.kind, str(exc)) from exc
        except Exception as exc:
            raise ClientPowerError("transient", f"Paired power transport could not be captured: {exc}") from exc
        self._binding()
        if any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF for v in fields.values()) or any(
            header[i] != value for i, value in clock._ENVELOPE.items()
        ):
            raise ClientPowerError("malformed", "Client-power transport envelope is not understood")
        return header, fields

    def _exchange(self, command, payload, *, write=None):
        """One exact command, including one dispatch only for any SET."""
        if command not in SIZES or len(payload) != SIZES[command] or (command == SET) != isinstance(write, _Write):
            raise ClientPowerError("malformed", "Client-power command or packet geometry is not understood")
        identity = self._binding()
        header, fields = self._transport()
        size = SIZES[command]
        storage, buffer = clock._guarded(68 + size)
        packet = (ctypes.c_uint32 * ((68 + size) // 4)).from_buffer(buffer)
        for index, value in enumerate(header):
            packet[index] = value
        packet[2], packet[14], packet[15], packet[16] = 68 + size, command, size, 0
        packet[5] = packet[11] = 0
        buffer[68:] = payload
        expected = tuple(packet[:16])
        if write is not None:
            write.dispatched = True
        try:
            status = self.gpu._legacy_clk_escape(packet, dict(fields))
        except Exception as exc:
            raise ClientPowerError("transient", f"Client-power transport failed: {exc}") from exc
        try:
            clock._guards(storage)
        except PerfDiagnosticError as exc:
            raise ClientPowerError("malformed", str(exc)) from exc
        if self._binding() != identity:
            raise ClientPowerError("identity", "Client-power target changed during the operation")
        if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
            raise ClientPowerError("malformed", "Client-power response changed its transport envelope")
        if type(status) is not int or status != 0 or packet[11] != 1:
            raise ClientPowerError("transient", f"Client-power escape failed (NT {status!r}, completion {packet[11]})")
        if packet[16] != 0:
            kind = "unsupported" if packet[16] in (0x1F, 0x56, 0x40) else "transient"
            raise ClientPowerError(kind, f"Client-power command {command:#x} rejected (RM {packet[16]:#x})")
        result = bytes(buffer)[68:]
        if command == SET and result != payload:
            raise ClientPowerError("malformed", "Client-power SET changed the requested packet")
        return result

    def _metadata_once(self):
        identity = self._binding()
        info = self._exchange(INFO, bytes(SIZES[INFO]))
        mask = int.from_bytes(info[4:INFO_BASE], "little")
        if mask >> CAPACITY:
            raise ClientPowerError("malformed", "Client-power inventory exceeds the understood row capacity")
        candidates = [i for i in range(CAPACITY) if mask >> i & 1 and info[INFO_BASE + i * INFO_STRIDE + 2] == 1]
        if len(candidates) != 1:
            raise ClientPowerError("unsupported", "No unique role1 client-power policy is available")
        index = candidates[0]
        at = INFO_BASE + index * INFO_STRIDE
        row = info[at : at + INFO_STRIDE]
        if row[0] != row[1]:
            raise ClientPowerError("malformed", "Selected client INFO type discriminants disagree")
        if row[3] != 0:
            raise ClientPowerError("unsupported", "Selected client-power policy does not report milliwatts")
        limits = struct.unpack_from("<III", row, 4)
        if not limits[0] <= limits[1] <= limits[2]:
            raise ClientPowerError("malformed", "Client-power ordinary limits are inconsistent")
        role_request = bytes([ROLE]) + bytes(SIZES[ROLE_INFO] - 1)
        role = self._exchange(ROLE_INFO, role_request)
        if role[:4] != role_request[:4]:
            raise ClientPowerError("malformed", "Power-role INFO changed its semantic selector")
        native_info = self._exchange(NATIVE_INFO, bytes(SIZES[NATIVE_INFO]))
        native_mask = _u32(native_info, 4)
        matches = [
            i
            for i in range(NATIVE_CAPACITY)
            if native_mask >> i & 1
            and native_info[NATIVE_INFO_BASE + i * NATIVE_INFO_STRIDE : NATIVE_INFO_BASE + (i + 1) * NATIVE_INFO_STRIDE]
            == role[4:]
        ]
        if len(matches) != 1:
            raise ClientPowerError("malformed", "Role1 INFO does not uniquely bind a current native policy")
        native_index = matches[0]
        source = role[4:]
        if source[0] != 0x40 or source[6] != row[3] or struct.unpack_from("<III", source, 8) != limits:
            raise ClientPowerError("malformed", "Client INFO differs from the bound native policy definition")
        if self._binding() != identity:
            raise ClientPowerError("identity", "Client-power target changed during metadata capture")
        return {
            "schema": 1,
            "stable_reads": 2,
            "identity": identity,
            "binding": {
                "client_index": index,
                "native_index": native_index,
                "semantic_role": ROLE,
                "client": CLIENT,
                "units": "mW",
                "minimum_mw": limits[0],
                "rated_mw": limits[1],
                "maximum_mw": limits[2],
            },
            "info_hex": info.hex(),
            "role_info_hex": role.hex(),
            "native_info_hex": native_info.hex(),
        }

    def _once(self):
        metadata = self._metadata_once()
        info = bytes.fromhex(metadata["info_hex"])
        native_info = bytes.fromhex(metadata["native_info_hex"])
        source = bytes.fromhex(metadata["role_info_hex"])[4:]
        index, native_index = metadata["binding"]["client_index"], metadata["binding"]["native_index"]
        row = info[INFO_BASE + index * INFO_STRIDE : INFO_BASE + (index + 1) * INFO_STRIDE]
        request = bytearray(SIZES[CONTROL])
        request[:INFO_BASE] = info[:INFO_BASE]
        request[4:INFO_BASE] = (1 << index).to_bytes(32, "little")
        cp = CONTROL_BASE + index * CONTROL_STRIDE
        request[cp : cp + 2], request[cp + 8] = row[:2], CLIENT
        control = self._exchange(CONTROL, request)
        if control[0] != info[0] or control[4:INFO_BASE] != request[4:INFO_BASE]:
            raise ClientPowerError("malformed", "Client CONTROL group or selected mask differs")
        if control[cp : cp + 2] != row[:2] or control[cp + 8] != CLIENT:
            raise ClientPowerError("malformed", "Client CONTROL changed the selected row type or client")
        wanted = _u32(control, cp + 4)
        if wanted == MISSING:
            raise ClientPowerError("unsupported", "The selected FE client has no existing restorable request")
        if wanted == 0:
            raise ClientPowerError("unsupported", "A zero FE input has uncharacterized removal semantics")
        native_mask = _u32(native_info, 4)
        request = bytearray(SIZES[NATIVE_CONTROL])
        struct.pack_into("<I", request, 16, native_mask)
        native_control = self._exchange(NATIVE_CONTROL, request)
        if struct.unpack_from("<5I", native_control) != (0, 0, 0, 255, native_mask):
            raise ClientPowerError("malformed", "Native CONTROL header or inventory differs from INFO")
        np = NATIVE_CONTROL_BASE + native_index * NATIVE_CONTROL_STRIDE
        if _u32(native_control, np) != source[4]:
            raise ClientPowerError("malformed", "Bound native policy CONTROL type differs from INFO")
        if _u32(native_control, np + 4) != wanted:
            raise ClientPowerError("unstable", "Client and native current requests disagree")
        if self._binding() != metadata["identity"]:
            raise ClientPowerError("identity", "Client-power target changed during capture")
        return {
            **metadata,
            "requested_mw": wanted,
            "control_hex": control.hex(),
            "native_control_hex": native_control.hex(),
        }

    def _arbitration(self, observation):
        info, control = bytes.fromhex(observation["native_info_hex"]), bytes.fromhex(observation["native_control_hex"])
        request = bytearray(SIZES[NATIVE_STATUS])
        request[4:8] = info[4:8]
        error = None
        for attempt in range(2):
            try:
                status = self._exchange(NATIVE_STATUS, request)
                decoded = power_arbitration.decode(_LAYOUT, _words(info), _words(control), _words(status))
                if not decoded["available"]:
                    return None, decoded["reason"]
                return decoded, None
            except ClientPowerError as exc:
                # An identity change invalidates the binding, even when it was
                # discovered during optional telemetry. Other STATUS failures
                # remain diagnostic and cannot remove the established request.
                if exc.kind == "identity":
                    raise
                error = str(exc)
                if exc.kind != "transient":
                    break
                self._reset_transport()
                if attempt == 1:
                    break
        return None, error

    def _stable_locked(self, capture):
        for attempt in range(2):
            try:
                first, second = capture(), capture()
                if first != second:
                    raise ClientPowerError(
                        "unstable", "Client-power definitions or requests changed between fresh reads"
                    )
                return second
            except ClientPowerError as exc:
                if exc.kind != "transient":
                    raise
                self._reset_transport()
                if attempt == 1:
                    raise
        raise AssertionError("Bounded client-power read did not return or fail")

    def _read_locked(self):
        state = self._stable_locked(self._once)
        arbitration, error = self._arbitration(state)
        return {**state, "arbitration": arbitration, "arbitration_error": error}

    def read_metadata(self):
        """Two stable INFO captures, independent of current-request eligibility."""
        with self.gpu._lock:
            return self._stable_locked(self._metadata_once)

    def read(self):
        """Two stable definition/request captures; STATUS is optional."""
        with self.gpu._lock:
            return self._read_locked()

    def _rollback(self, before):
        write = _Write()
        result = {"attempted": False, "verified": False, "error": None, "readback": None}
        errors = []
        try:
            self._exchange(SET, bytes.fromhex(before["control_hex"]), write=write)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        result["attempted"] = write.dispatched
        if write.dispatched:
            try:
                result["readback"] = self._read_locked()
                if _state(result["readback"]) != _state(before):
                    raise ClientPowerError(
                        "readback", "Original client-power definitions and requests were not restored"
                    )
                result["verified"] = not errors
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        result["error"] = "; ".join(errors) or None
        return result

    def set_request(self, mw):
        """Write once, verify the exact projection, compensate once on failure."""
        if type(mw) is not int or not 0 < mw < MISSING:
            raise ClientPowerError(
                "invalid", "Power request must be positive whole uint32 milliwatts below the missing-client sentinel"
            )
        with self.gpu._lock:
            before = self._read_locked()
            expected = _project(before, mw)
            write = _Write()
            try:
                self._exchange(SET, bytes.fromhex(expected["control_hex"]), write=write)
                after = self._read_locked()
                if _state(after) != expected:
                    raise ClientPowerError(
                        "readback", "Client-power request or preserved definitions did not read back exactly"
                    )
                return {"before": before, "after": after}
            except BaseException as exc:
                if not write.dispatched:
                    raise
                self._reset_transport()
                rollback = self._rollback(before)
                detail = (
                    "Original request restored and verified"
                    if rollback["verified"]
                    else "Original request restoration was not verified"
                )
                if not isinstance(exc, Exception):
                    exc.add_note(detail)
                    exc.rollback = rollback
                    raise
                raise ClientPowerError(getattr(exc, "kind", "transient"), f"{exc}. {detail}", rollback) from exc
