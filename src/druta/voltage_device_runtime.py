# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Loaded PWM voltage-device switch-delay requests with exact live undo.

B205 INFO and B207 CONTROL bind a device ordinal to its voltage domain and
operation. F208 updates one switchDelayus DWORD. Microseconds are the request
units; physical switching latency and scheduler behavior are not measured.
These are separate from the power-up/down registry delay words. No registry,
voltage amplitude, restart or persistence operation occurs here.
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass

from . import clock_bios_runtime as clock
from .perf_diagnostics import PerfDiagnosticsReader
from .registry_flags import _same_state

_INFO, _CONTROL, _SET = 0x2080B205, 0x2080B207, 0x2080F208
_INFO_SIZE, _CONTROL_SIZE = 0x708, 0x188
_HEADER, _CAPACITY, _INFO_STRIDE, _CONTROL_STRIDE = 8, 32, 56, 12
# NVIDIA ctrl2080volt.h NV2080_CTRL_VOLT_VOLT_DOMAIN_* and
# NV2080_CTRL_VOLT_VOLT_DEVICE_OPERATION_TYPE_*; see the research source copy.
DOMAIN_LABELS = {1: "LOGIC", 2: "SRAM", 3: "MSVDD"}
OPERATION_LABELS = {1: "DEFAULT", 2: "LPWR_STEADY_STATE", 3: "LPWR_SLEEP_STATE", 4: "IPC_VMIN"}


class VoltageDeviceError(RuntimeError):
    """Unavailable requests remain errors; dispatched errors include undo."""

    def __init__(self, kind, message, status=None, *, rollback=None):
        super().__init__(message)
        self.kind, self.status, self.rollback = kind, status, rollback


@dataclass
class _Write:
    dispatched: bool = False


def _error(exc, kind="malformed"):
    if isinstance(exc, VoltageDeviceError):
        return exc
    return VoltageDeviceError(getattr(exc, "kind", kind), str(exc), getattr(exc, "status", None))


def _u32(raw, offset):
    return struct.unpack_from("<I", raw, offset)[0]


def _decode(info, control):
    if len(info) != _INFO_SIZE or len(control) != _CONTROL_SIZE:
        raise VoltageDeviceError("malformed", "Voltage-device packet geometry changed")
    if info[0] != control[0] or info[4:8] != control[4:8]:
        raise VoltageDeviceError("malformed", "Voltage-device INFO/CONTROL group or inventory disagrees")
    mask, rows = _u32(info, 4), []
    for index in range(_CAPACITY):
        if not mask & (1 << index):
            continue
        raw = info[_HEADER + index * _INFO_STRIDE : _HEADER + (index + 1) * _INFO_STRIDE]
        ctl = control[_HEADER + index * _CONTROL_STRIDE : _HEADER + (index + 1) * _CONTROL_STRIDE]
        row = {
            "index": index,
            "type": raw[0],
            "info_duplicate_type": raw[1],
            "control_type": ctl[0],
            "control_duplicate_type": ctl[1],
            "info_hex": raw.hex(),
            "control_hex": ctl.hex(),
            "status": "unsupported",
            "issues": [],
            "switch_delay_us": None,
        }
        if len({raw[0], raw[1], ctl[0], ctl[1]}) != 1:
            row["status"] = "malformed"
            row["issues"].append("Device INFO/CONTROL type and duplicate type disagree")
        elif raw[0] != 3:
            row["issues"].append("Only the understood PWM type3 has a qualified switch-delay request")
        else:
            row.update(
                voltage_domain=raw[2],
                i2c_device_index=raw[3],
                operation_type=raw[4],
                minimum_uv=_u32(raw, 8),
                maximum_uv=_u32(raw, 12),
                first_table_interval_uv=_u32(raw, 16),
                switch_delay_us=_u32(ctl, 4),
            )
            if raw[2] not in DOMAIN_LABELS or raw[4] not in OPERATION_LABELS:
                row["issues"].append("PWM voltage domain or operation is not understood")
            elif row["minimum_uv"] > row["maximum_uv"]:
                row["status"] = "malformed"
                row["issues"].append("PWM voltage bounds are reversed")
            elif row["switch_delay_us"] == 0:
                row["status"] = "malformed"
                row["issues"].append("Loaded zero switch delay is rejected by the device initialization validator")
            else:
                row["status"] = "ready"
        rows.append(row)
    return {"mask": mask, "info_hex": info.hex(), "control_hex": control.hex(), "rows": rows}


class VoltageDeviceController:
    """One sticky paired adapter; construct a fresh controller after restart."""

    def __init__(self, gpu):
        self.gpu = gpu
        self._reader = PerfDiagnosticsReader(gpu)

    def _binding(self, expected=None):
        try:
            identity = clock._binding(self._reader)
        except Exception as exc:
            raise _error(exc, "identity") from exc
        if expected is not None and identity != expected:
            raise VoltageDeviceError("identity", "Voltage-device target changed")
        return identity

    def _call(self, transport, identity, command, *, mask=None, payload=None, write=None):
        if command == _INFO and mask is None and payload is None and write is None:
            size = _INFO_SIZE
        elif (
            command == _CONTROL and type(mask) is int and 0 <= mask <= 0xFFFFFFFF and payload is None and write is None
        ):
            size = _CONTROL_SIZE
        elif (
            command == _SET
            and isinstance(payload, bytes)
            and len(payload) == _CONTROL_SIZE
            and isinstance(write, _Write)
        ):
            size = _CONTROL_SIZE
            selected = _u32(payload, 4)
            if mask is not None or not selected or selected & (selected - 1):
                raise VoltageDeviceError("invalid", "Switch-delay SET requires one selected device")
        else:
            raise VoltageDeviceError("invalid", "Only the understood voltage-device requests are allowed")
        self._binding(identity)
        header, fields = transport
        if (
            len(header) != 17
            or any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in header)
            or any(header[i] != value for i, value in clock._ENVELOPE.items())
            or any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in fields.values())
        ):
            raise VoltageDeviceError("malformed", "Voltage-device request needs the validated NVDA transport")
        storage, buffer = clock._guarded(68 + size)
        packet = (ctypes.c_uint32 * ((68 + size) // 4)).from_buffer(buffer)
        for index, value in enumerate(header):
            packet[index] = value
        packet[2], packet[14], packet[15], packet[16] = 68 + size, command, size, 0
        packet[5] = packet[11] = 0
        if mask is not None:
            packet[18] = mask
        if payload is not None:
            ctypes.memmove(ctypes.addressof(buffer) + 68, payload, size)
        expected = tuple(packet[:16])
        if write is not None:
            write.dispatched = True
        try:
            status = self.gpu._legacy_clk_escape(packet, dict(fields))
        except Exception as exc:
            raise VoltageDeviceError("transient", f"Voltage-device transport failed: {exc}") from exc
        self._binding(identity)
        try:
            clock._guards(storage)
        except Exception as exc:
            raise VoltageDeviceError("malformed", "Voltage-device request exceeded packet bounds") from exc
        if any(packet[i] != value for i, value in enumerate(expected) if i not in (5, 11)) or packet[5] not in (0, 1):
            raise VoltageDeviceError("malformed", "Voltage-device response changed its bound envelope")
        if type(status) is not int or status != 0:
            raise VoltageDeviceError("transient", f"Voltage-device transport failed (NTSTATUS {status})", status)
        if packet[11] != 1:
            raise VoltageDeviceError(
                "transient", f"Voltage-device completion failed (0x{packet[11]:X})", int(packet[11])
            )
        returned = bytes(buffer)[68:]
        if payload is not None and returned != payload:
            raise VoltageDeviceError("malformed", "Voltage-device SET changed its request packet")
        if packet[16]:
            code = int(packet[16])
            raise VoltageDeviceError(
                "unsupported" if code in (0x1F, 0x56) else "transient",
                f"Voltage-device request rejected (RM 0x{code:X})",
                code,
            )
        if mask is not None and _u32(returned, 4) != mask:
            raise VoltageDeviceError("malformed", "Voltage-device CONTROL changed its requested inventory")
        return returned

    def _read_locked(self, transport=None, identity=None):
        fixed = transport is not None
        for attempt in range(2):
            try:
                bound = self._binding(identity)
                if fixed:
                    current = transport
                else:
                    try:
                        current = self._reader._transport()
                    except Exception as exc:
                        raise _error(exc, "transient") from exc
                samples = []
                for _ in range(2):
                    info = self._call(current, bound, _INFO)
                    control = self._call(current, bound, _CONTROL, mask=_u32(info, 4))
                    samples.append(_decode(info, control))
                if not _same_state(*samples):
                    raise VoltageDeviceError("unstable", "Voltage-device INFO/CONTROL changed between fresh reads")
                self._binding(bound)
                return dict(samples[-1], identity=bound, stable_reads=2), current
            except Exception as exc:
                error = _error(exc)
                if error.kind != "transient" or attempt == 1:
                    raise error from exc
                if not fixed:
                    self.gpu._current_limit_transport = None
                    self.gpu._current_limit_layout_choice = None
        raise AssertionError("Bounded voltage-device read did not return or fail")

    def read(self):
        """Return two stable full captures; unsupported peers remain diagnostics."""
        with self.gpu._lock:
            observation, _ = self._read_locked()
            return observation

    def _rollback(self, before, transport, undo):
        write = _Write()
        result = {"attempted": False, "verified": False, "readback": None, "error": None}
        errors = []
        try:
            self._call(transport, before["identity"], _SET, payload=undo, write=write)
        except BaseException as exc:
            errors.append(str(exc))
        try:
            current, _ = self._read_locked(transport, before["identity"])
            result["readback"] = current
            if not _same_state(current, before):
                errors.append("Original full voltage-device state did not match fresh readback")
        except BaseException as exc:
            errors.append(str(exc))
        result["attempted"], result["verified"] = write.dispatched, not errors
        result["error"] = "; ".join(errors) or None
        return result

    def set(self, index, delay_us, *, voltage_domain, operation_type):
        """Change one positive uint32 request, bound to its current domain/role.

        SET and one possible compensating SET use the same captured transport
        and singleton mask. Verification GETs read the full current inventory.
        There is no SET retry, fallback transport, or automatic restart.
        """
        if type(index) is not int or not 0 <= index < _CAPACITY:
            raise VoltageDeviceError("invalid", "Voltage-device index must be an integer from 0 through 31")
        if type(delay_us) is not int or not 1 <= delay_us <= 0xFFFFFFFF:
            raise VoltageDeviceError("invalid", "Switch delay must be a positive uint32 number of microseconds")
        if type(voltage_domain) is not int or voltage_domain not in DOMAIN_LABELS:
            raise VoltageDeviceError("invalid", "Select an understood voltage domain")
        if type(operation_type) is not int or operation_type not in OPERATION_LABELS:
            raise VoltageDeviceError("invalid", "Select an understood voltage-device operation")
        with self.gpu._lock:
            before, transport = self._read_locked()
            row = next((row for row in before["rows"] if row["index"] == index), None)
            if row is None:
                raise VoltageDeviceError("unsupported", "Selected voltage device is absent from the current inventory")
            if row["status"] != "ready":
                raise VoltageDeviceError(
                    row["status"], "Selected voltage device is unavailable: " + "; ".join(row["issues"])
                )
            if (row["voltage_domain"], row["operation_type"]) != (voltage_domain, operation_type):
                raise VoltageDeviceError("identity", "Selected voltage-device domain or operation changed")
            if row["switch_delay_us"] == delay_us:
                return before
            original = bytes.fromhex(before["control_hex"])
            projected = bytearray(original)
            struct.pack_into("<I", projected, _HEADER + index * _CONTROL_STRIDE + 4, delay_us)
            wanted = dict(
                _decode(bytes.fromhex(before["info_hex"]), bytes(projected)),
                identity=before["identity"],
                stable_reads=2,
            )
            undo, payload = bytearray(original), bytearray(projected)
            struct.pack_into("<I", undo, 4, 1 << index)
            struct.pack_into("<I", payload, 4, 1 << index)
            write = _Write()
            try:
                self._call(transport, before["identity"], _SET, payload=bytes(payload), write=write)
                after, _ = self._read_locked(transport, before["identity"])
                if not _same_state(after, wanted):
                    raise VoltageDeviceError(
                        "readback", "Switch-delay SET did not preserve the exact full device projection"
                    )
                return after
            except BaseException as exc:
                if not write.dispatched:
                    raise
                rollback = self._rollback(before, transport, bytes(undo))
                detail = (
                    "Original voltage-device state restored and verified"
                    if rollback["verified"]
                    else ("Voltage-device rollback was not verified: " + str(rollback["error"]))
                )
                if not isinstance(exc, Exception):
                    exc.add_note(detail)
                    exc.rollback = rollback
                    raise
                error = _error(exc, "transient")
                raise VoltageDeviceError(error.kind, f"{error}. {detail}", error.status, rollback=rollback) from exc
