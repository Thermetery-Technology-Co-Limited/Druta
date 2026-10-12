# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""EVGA iCX Kingpin voltage requests through the observed NVAPI Ex transport.

The paired Classified capture uses I2CWriteEx register 0x10 for NVVDD and
0x18 for FBVDD: 03, mode, then a little-endian 16-bit count in 5 mV units.
Register 0x19 reads the controller's ten-byte requested state; it is not
physical voltage telemetry. Its FBVDD byte 7 has unknown semantics.

B2 gates the observed NVVDD setter: status FC applied the request, while FE
returned NVAPI success but ignored it. Enable only within an acknowledged
setter, verify the gate, send one voltage request, and verify readback.
The evidence covers one powered Kingpin session, not cold-start behavior or
every firmware. Separately captured switching-frequency and OCP controls
read paired state at 0x1C and 0x1B. Loadline reads 0x1A; its other byte is
unknown and must be preserved. No EEPROM save or B2-disable writes are
implemented. Read-only discovery never enables writes.
"""
import ctypes
import math
import threading
import time
from types import SimpleNamespace

from ..railctl import I2C_READ_EX, I2C_WRITE_EX, PTR, Rail, u8, u32

REGULATOR = "EVGA iCX"
ARCH_AMPERE = 7
PORT = 1
ADDR7 = 0x2D
FIRMWARE_REG = 0xB1
GATE_REG = 0xB2
NVVDD_REG = 0x10
FBVDD_REG = 0x18
STATE_REG = 0x19
LOADLINE_STATE_REG = 0x1A
OCP_STATE_REG = 0x1B
FREQUENCY_STATE_REG = 0x1C
FREQUENCY_REGISTERS = {"NVVDD": 0x13, "FBVDD": 0x1F}
OCP_REGISTERS = {"NVVDD": 0x12, "FBVDD": 0x1E}
LOADLINE_REG = 0x11
FREQUENCY_MIN_KHZ = 300
FREQUENCY_MAX_KHZ = 1000
FREQUENCY_STEP_KHZ = 100
FIRMWARE_PREFIX = bytes.fromhex("05 fc 2d 13")
# Recorded evidence, not compatibility allowlists. Revision bytes are diagnostic.
DEVICE_ID = 0x2203
SUBSYSTEM_ID = 0x49983842
FIRMWARE = bytes.fromhex("05 fc 2d 13 01 0e")
GATE_ENABLE_PAYLOAD = bytes.fromhex("04 c6 eb ea 15")
NVVDD_AUTO_PAYLOAD = bytes.fromhex("03 00 00 00")

NVVDD_MIN_MV = 750
NVVDD_MAX_MV = 1150
FBVDD_MIN_MV = 1250
FBVDD_MAX_MV = 1450
NVVDD_XOC_MAX_MV = 1600
STEP_MV = 5
# Incremental delays, not claims about the device's exact settling time.
READ_RETRY_DELAYS = (0.0, 0.05, 0.10)
VERIFY_POLL_DELAYS = (0.0, 0.10, 0.30, 0.60, 0.50)

_ABI_OFFSETS = {
    "version": 0, "displayMask": 4, "bIsDDCPort": 8, "i2cDevAddress": 9,
    "pbI2cRegAddress": 16, "regAddrSize": 24, "pbData": 32, "cbSize": 40,
    "i2cSpeed": 44, "i2cSpeedKhz": 48, "portId": 52, "bIsPortIdSet": 56,
}
_ANCILLARY_REGISTERS = (LOADLINE_STATE_REG, OCP_STATE_REG, FREQUENCY_STATE_REG)
_READ_LENGTHS = {
    FIRMWARE_REG: 6, GATE_REG: 5, STATE_REG: 10,
    LOADLINE_STATE_REG: 3, OCP_STATE_REG: 3, FREQUENCY_STATE_REG: 3,
}


class _IcxError(RuntimeError):
    """An operation cannot be verified without another explicit request."""


class _ReadUnavailableError(_IcxError):
    """A transport read may recover through bounded read-only retries."""


class _InvalidResponseError(_IcxError):
    """A returned packet is not the understood layout or state."""


def _millivolts(value, low, high, rail):
    if type(value) not in (int, float):
        raise ValueError(f"{rail} must be a finite number of millivolts")
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{rail} must be a finite number of millivolts")
        if not math.isclose(value, round(value), rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"{rail} must be a whole number of millivolts")
    mv = int(round(value))
    if mv % STEP_MV:
        raise ValueError(f"{rail} steps by {STEP_MV} mV")
    if not low <= mv <= high:
        raise ValueError(f"{rail} must be {low}..{high} mV")
    return mv


def nvvdd_payload(mv, *, xoc=False):
    """Four data bytes, with a full LE16 count rather than a wrapped byte."""
    high = NVVDD_XOC_MAX_MV if xoc else NVVDD_MAX_MV
    value = _millivolts(mv, NVVDD_MIN_MV, high, "NVVDD")
    return bytes((3, 1)) + (value // STEP_MV).to_bytes(2, "little")


def fbvdd_payload(mv):
    """Four data bytes for the observed manual FBVDD setter."""
    value = _millivolts(mv, FBVDD_MIN_MV, FBVDD_MAX_MV, "FBVDD")
    return bytes((3, 1)) + (value // STEP_MV).to_bytes(2, "little")


def switching_frequency_payload(value):
    """Two data bytes for the captured 300..1000 kHz controls."""
    if type(value) not in (int, float):
        raise ValueError("Switching frequency must be a finite number of kHz")
    if type(value) is float and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError("Switching frequency must be a whole number of kHz")
    value = int(value)
    if not FREQUENCY_MIN_KHZ <= value <= FREQUENCY_MAX_KHZ or value % FREQUENCY_STEP_KHZ:
        raise ValueError("Switching frequency must be 300..1000 kHz in 100 kHz steps")
    return bytes((1, value // FREQUENCY_STEP_KHZ))


def ocp_payload(enabled):
    """Two data bytes for the captured per-rail OCP enable setting."""
    if type(enabled) is not bool:
        raise ValueError("OCP state must be enabled or disabled")
    return bytes((1, int(enabled)))


def loadline_payload(level):
    """Two data bytes for the captured Level 0..15 loadline selector."""
    if type(level) is not int or not 0 <= level <= 15:
        raise ValueError("Loadline level must be an integer from 0 through 15")
    return bytes((1, level))


def _rail_name(rail):
    if not isinstance(rail, str) or rail.upper() not in ("NVVDD", "FBVDD"):
        raise ValueError("The iCX rail must be NVVDD or FBVDD")
    return rail.upper()


def _handle_value(handle):
    try:
        return int(getattr(handle, "value", handle) or 0)
    except (TypeError, ValueError):
        return 0


class EVGAICx(Rail):
    """An Ampere iCX controller with the understood Kingpin voltage interface."""

    icx_mcu = True

    def __init__(self, nvapi, *, architecture=None, port=PORT, addr7=ADDR7):
        from ..railctl import _V3
        if (ctypes.sizeof(_V3) != 64
                or any(getattr(_V3, field).offset != offset
                       for field, offset in _ABI_OFFSETS.items())):
            raise ValueError("EVGA iCX requires the understood 64-byte NVAPI I2C V3 layout")
        if port != PORT or addr7 != ADDR7:
            raise ValueError("EVGA iCX voltage is only understood at port 1 address 0x2D")
        self.architecture = architecture
        self._mutex = threading.RLock()
        self._last_error = ""
        self._firmware_bytes = None
        self._bound_handle = _handle_value(getattr(nvapi, "gpu", None))
        self._bound_gpu = PTR(self._bound_handle)
        selected = getattr(nvapi, "selected", None) or {}
        self._bound_selected = self._selection(selected)
        self._binding_valid = (
            self._bound_handle != 0
            and type(selected.get("devid")) is int
            and type(selected.get("subsys")) is int
            and ("handle" not in selected
                 or _handle_value(selected["handle"]) == self._bound_handle))
        src = {
            "controller": REGULATOR, "contract": 2, "architecture": architecture,
            "port": PORT, "addr7": ADDR7,
            "device_id": selected.get("devid"), "subsystem_id": selected.get("subsys"),
            "firmware_prefix": FIRMWARE_PREFIX.hex(),
            "voltage_registers": [NVVDD_REG, FBVDD_REG],
        }
        profile = SimpleNamespace(
            name="EVGA iCX Kingpin", regulator=REGULATOR,
            rail="NVVDD/FBVDD", port=PORT, addr7=ADDR7, src=src, read_only=False,
            weak_id=False, provenance={"source": "Paired Classified NVAPI capture and B2 gate experiment"})
        super().__init__(profile, nvapi, addr7=addr7)

    @staticmethod
    def _selection(selected):
        return (selected.get("devid"), selected.get("subsys"), selected.get("slot"),
                _handle_value(selected["handle"]) if "handle" in selected else None)

    def _ensure_bound(self):
        selected = getattr(self.nvapi, "selected", None) or {}
        if self.architecture != ARCH_AMPERE:
            raise _IcxError("EVGA iCX voltage layout requires the Ampere architecture")
        if not getattr(self.nvapi, "ok", False):
            raise _ReadUnavailableError("NVAPI is unavailable")
        if (not self._binding_valid
                or _handle_value(getattr(self.nvapi, "gpu", None)) != self._bound_handle
                or self._selection(selected) != self._bound_selected):
            raise _IcxError("EVGA iCX GPU selection changed; detect the controller again")

    def _function(self, identifier):
        self._ensure_bound()
        try:
            fn = self.nvapi._i(identifier, PTR, PTR, PTR)
        except Exception as exc:
            raise _IcxError(f"The NVAPI I2C Ex entry point could not be resolved: {exc}") from exc
        if fn is None:
            raise _IcxError("The required NVAPI I2C Ex entry point is unavailable")
        self._ensure_bound()
        return fn

    def _read_block(self, register):
        """Read one framed byte block; never turn a read into a command prefix."""
        if register not in _READ_LENGTHS:
            raise ValueError("EVGA iCX read is outside the understood register set")
        size = _READ_LENGTHS[register]
        fn = self._function(I2C_READ_EX)
        buf = (u8 * size)(*([0xA5] * size))
        extra = (u32 * 2)()
        packet, regbuf = self._mk(register, buf, size)
        geometry = bytes(packet)
        self._ensure_bound()
        try:
            status = fn(self._bound_gpu, ctypes.byref(packet), ctypes.byref(extra))
        except Exception as exc:
            self._ensure_bound()
            raise _ReadUnavailableError(f"iCX register {register:02X} read failed: {exc}") from exc
        self._ensure_bound()
        if bytes(packet) != geometry or bytes(regbuf) != bytes((register,)):
            raise _InvalidResponseError(f"iCX register {register:02X} returned altered packet geometry")
        if status != 0:
            raise _ReadUnavailableError(f"iCX register {register:02X} read returned status {status}")
        data = bytes(buf)
        if data == bytes((0xA5,)) * size:
            raise _ReadUnavailableError(f"iCX register {register:02X} left the read buffer untouched")
        if data[0] != size - 1:
            raise _InvalidResponseError(f"iCX register {register:02X} returned invalid block framing")
        return data

    @staticmethod
    def _sleep(delay):
        if delay:
            time.sleep(delay)

    def _read_retry(self, register):
        last_error = None
        for delay in READ_RETRY_DELAYS:
            self._sleep(delay)
            try:
                return self._read_block(register)
            except _ReadUnavailableError as exc:
                last_error = exc
        raise last_error or _ReadUnavailableError("iCX read unavailable")

    def _identity(self, *, retry=False):
        data = self._read_retry(FIRMWARE_REG) if retry else self._read_block(FIRMWARE_REG)
        if data[:4] != FIRMWARE_PREFIX:
            raise _InvalidResponseError("EVGA iCX controller identity does not match the understood interface")
        self._firmware_bytes = data
        return data

    def present(self):
        """Read controller identity only; never initialize B2 during discovery."""
        with self._mutex:
            try:
                self._identity()
                self._last_error = ""
                return True
            except _IcxError as exc:
                self._last_error = str(exc)
                return False

    def still_present(self):
        """Retry transient identity reads; do not retry a contradictory identity."""
        with self._mutex:
            try:
                self._identity(retry=True)
                self._last_error = ""
                return True
            except _IcxError as exc:
                self._last_error = str(exc)
                return False

    def read_state(self):
        """Return the framed requested-state bytes, or None after bounded reads."""
        with self._mutex:
            try:
                state = self._read_retry(STATE_REG)
                self._last_error = ""
                return state
            except _IcxError as exc:
                self._last_error = str(exc)
                return None

    def read_ancillary_state(self):
        """Read independent setting groups; an unavailable group stays None."""
        result = {}
        with self._mutex:
            for register in _ANCILLARY_REGISTERS:
                try:
                    result[register] = self._read_retry(register)
                except _IcxError as exc:
                    self._last_error = str(exc)
                    result[register] = None
        return result

    @staticmethod
    def _gate_value(data):
        if data[1] not in (0xFC, 0xFE):
            raise _InvalidResponseError("EVGA iCX returned an unknown B2 write-gate state")
        return data[1] == 0xFC

    def telemetry(self):
        """Requested controller state only; no physical voltage is inferred."""
        result = {
            "firmware_ok": False, "nvvdd_mode": None, "nvvdd_request_mv": None,
            "fbvdd_request_mv": None, "write_enabled": None, "state_available": False,
            "nvvdd_frequency_khz": None, "fbvdd_frequency_khz": None,
            "nvvdd_ocp_enabled": None, "fbvdd_ocp_enabled": None, "loadline_level": None,
            "frequency_state_available": False, "ocp_state_available": False,
            "loadline_state_available": False,
        }
        with self._mutex:
            if not self.still_present():
                return result
            result["firmware_ok"] = True
            state = self.read_state()
            if state is not None:
                result["state_available"] = True
                count_nv = int.from_bytes(state[2:4], "little")
                count_fb = int.from_bytes(state[8:10], "little")
                if state[1] == 0:
                    result["nvvdd_mode"] = "auto"
                elif state[1] == 1 and count_nv:
                    result["nvvdd_mode"] = "manual"
                    result["nvvdd_request_mv"] = count_nv * STEP_MV
                if count_fb:
                    result["fbvdd_request_mv"] = count_fb * STEP_MV
            try:
                result["write_enabled"] = self._gate_value(self._read_retry(GATE_REG))
            except _IcxError as exc:
                self._last_error = str(exc)
            groups = self.read_ancillary_state()
            for register, key in ((FREQUENCY_STATE_REG, "frequency_state_available"),
                                  (OCP_STATE_REG, "ocp_state_available"),
                                  (LOADLINE_STATE_REG, "loadline_state_available")):
                result[key] = groups[register] is not None
            frequency = groups[FREQUENCY_STATE_REG]
            if frequency is not None:
                for index, key in ((1, "nvvdd_frequency_khz"), (2, "fbvdd_frequency_khz")):
                    if 3 <= frequency[index] <= 10:
                        result[key] = frequency[index] * FREQUENCY_STEP_KHZ
            ocp = groups[OCP_STATE_REG]
            if ocp is not None:
                for index, key in ((1, "nvvdd_ocp_enabled"), (2, "fbvdd_ocp_enabled")):
                    if ocp[index] in (0, 1):
                        result[key] = bool(ocp[index])
            loadline = groups[LOADLINE_STATE_REG]
            if loadline is not None and loadline[1] <= 15:
                result["loadline_level"] = loadline[1]
        return result

    @staticmethod
    def _validate_write(register, payload):
        payload = bytes(payload)
        if register == GATE_REG and payload == GATE_ENABLE_PAYLOAD:
            return payload
        if register == NVVDD_REG and payload == NVVDD_AUTO_PAYLOAD:
            return payload
        if (register in FREQUENCY_REGISTERS.values() and len(payload) == 2
                and payload[0] == 1 and 3 <= payload[1] <= 10):
            return payload
        if (register in OCP_REGISTERS.values() and len(payload) == 2
                and payload[0] == 1 and payload[1] in (0, 1)):
            return payload
        if (register == LOADLINE_REG and len(payload) == 2
                and payload[0] == 1 and payload[1] <= 15):
            return payload
        if len(payload) == 4 and payload[:2] == bytes((3, 1)):
            mv = int.from_bytes(payload[2:4], "little") * STEP_MV
            if register == NVVDD_REG:
                _millivolts(mv, NVVDD_MIN_MV, NVVDD_XOC_MAX_MV, "NVVDD")
                return payload
            if register == FBVDD_REG:
                _millivolts(mv, FBVDD_MIN_MV, FBVDD_MAX_MV, "FBVDD")
                return payload
        raise ValueError("Refusing an iCX write outside the understood setting/enable packets")

    def _write_payload(self, register, payload):
        """Issue one guarded WriteEx call. Never retry a potentially applied write."""
        payload = self._validate_write(register, payload)
        fn = self._function(I2C_WRITE_EX)
        buf = (u8 * len(payload))(*payload)
        extra = (u32 * 2)()
        packet, regbuf = self._mk(register, buf, len(payload))
        geometry = bytes(packet)
        self._ensure_bound()
        try:
            status = fn(self._bound_gpu, ctypes.byref(packet), ctypes.byref(extra))
        except Exception as exc:
            self._ensure_bound()
            raise _IcxError(
                f"iCX register {register:02X} write failed; application is unverified: {exc}") from exc
        self._ensure_bound()
        if (bytes(packet) != geometry or bytes(regbuf) != bytes((register,))
                or bytes(buf) != payload):
            raise _InvalidResponseError(
                f"iCX register {register:02X} write returned altered packet geometry; "
                "application is unverified")
        if status != 0:
            raise _IcxError(
                f"iCX register {register:02X} write returned status {status}; "
                "application is unverified")

    def _ensure_write_enabled(self):
        if self._gate_value(self._read_retry(GATE_REG)):
            return
        self._write_payload(GATE_REG, GATE_ENABLE_PAYLOAD)
        last_error = "B2 remained disabled"
        for delay in VERIFY_POLL_DELAYS:
            self._sleep(delay)
            try:
                if self._gate_value(self._read_block(GATE_REG)):
                    return
                last_error = "B2 remained disabled"
            except _ReadUnavailableError as exc:
                last_error = str(exc)
        raise _IcxError(f"iCX write-gate enable could not be verified: {last_error}; no voltage write sent")

    def _verify_state(self, register, payload, baseline):
        last_error = "no matching requested state was returned"
        for delay in VERIFY_POLL_DELAYS:
            self._sleep(delay)
            try:
                state = self._read_block(STATE_REG)
            except _ReadUnavailableError as exc:
                last_error = str(exc)
                continue
            if register == NVVDD_REG:
                if state[4:10] != baseline[4:10]:
                    raise _IcxError("iCX request was sent, but unrelated state changed; no additional write sent")
                matches = state[1] == payload[1]
                if payload[1] == 1:
                    matches = matches and state[2:4] == payload[2:4]
            else:
                # Byte 7 changes in captured FBVDD operations; its meaning is unknown.
                if state[1:7] != baseline[1:7]:
                    raise _IcxError("iCX request was sent, but unrelated state changed; no additional write sent")
                matches = state[8:10] == payload[2:4]
            if matches:
                return
            last_error = f"controller state {state.hex(' ')} does not match the request"
        raise _IcxError(
            f"iCX voltage request was sent but readback was not verified: {last_error}; "
            "no additional write sent")

    def _send(self, register, payload, description):
        with self._mutex:
            write_attempted = False
            try:
                self._identity(retry=True)
                baseline = self._read_retry(STATE_REG)
                if register == NVVDD_REG and (
                        baseline[1] not in (0, 1)
                        or (baseline[1] == 1 and not int.from_bytes(baseline[2:4], "little"))):
                    raise _InvalidResponseError("iCX NVVDD requested state is not understood")
                if register == FBVDD_REG and not int.from_bytes(baseline[8:10], "little"):
                    raise _InvalidResponseError("iCX FBVDD requested state is not understood")
                self._ensure_write_enabled()
                write_attempted = True
                self._write_payload(register, payload)
                self._verify_state(register, payload, baseline)
            except (_IcxError, ValueError) as exc:
                message = str(exc)
                if write_attempted:
                    message = (f"iCX {description} was attempted but could not be verified: "
                               f"{message}. No additional write sent.")
                self._last_error = message
                return False, message
            self._last_error = ""
            return True, (
                f"iCX {description}: controller request verified by readback. "
                "Physical voltage is not measured by this readback.")

    def set_nvvdd_auto(self, *, acknowledged=False):
        if not acknowledged:
            return False, "I2C voltage writes require acknowledgment"
        return self._send(NVVDD_REG, NVVDD_AUTO_PAYLOAD, "NVVDD Auto")

    def set_nvvdd_mv(self, mv, *, acknowledged=False, xoc=False):
        if not acknowledged:
            return False, "I2C voltage writes require acknowledgment"
        try:
            payload = nvvdd_payload(mv, xoc=xoc)
        except ValueError as exc:
            return False, str(exc)
        return self._send(NVVDD_REG, payload, f"NVVDD {int(round(mv))} mV")

    def set_fbvdd_mv(self, mv, *, acknowledged=False):
        if not acknowledged:
            return False, "I2C voltage writes require acknowledgment"
        try:
            payload = fbvdd_payload(mv)
        except ValueError as exc:
            return False, str(exc)
        return self._send(FBVDD_REG, payload, f"FBVDD {int(round(mv))} mV")

    def _send_setting(self, register, payload, state_register, field, description):
        """Change one captured field, checking siblings and readable collateral."""
        with self._mutex:
            write_attempted = False
            unavailable = set()
            try:
                self._identity(retry=True)
                baseline = self._read_retry(state_register)
                understood = {
                    FREQUENCY_STATE_REG: range(3, 11),
                    OCP_STATE_REG: (0, 1),
                    LOADLINE_STATE_REG: range(16),
                }
                if baseline[field] not in understood[state_register]:
                    raise _InvalidResponseError("The selected iCX setting state is not understood")
                collateral = {}
                for other in (STATE_REG,) + _ANCILLARY_REGISTERS:
                    if other == state_register:
                        continue
                    try:
                        collateral[other] = self._read_retry(other)
                    except _ReadUnavailableError:
                        unavailable.add(other)
                self._ensure_write_enabled()
                write_attempted = True
                self._write_payload(register, payload)
                last_error = "no matching requested state was returned"
                for delay in VERIFY_POLL_DELAYS:
                    self._sleep(delay)
                    try:
                        state = self._read_block(state_register)
                    except _ReadUnavailableError as exc:
                        last_error = str(exc)
                        continue
                    if any(value != baseline[index] for index, value in enumerate(state)
                           if index != field):
                        raise _IcxError("iCX sibling or unknown state changed")
                    if state[field] == payload[-1]:
                        break
                    last_error = f"controller state {state.hex(' ')} does not match the request"
                else:
                    raise _IcxError(last_error)
                for other, previous in collateral.items():
                    try:
                        current = self._read_retry(other)
                    except _ReadUnavailableError:
                        unavailable.add(other)
                        continue
                    if current != previous:
                        raise _IcxError(f"iCX unrelated register {other:02X} changed")
                self._ensure_bound()
            except (_IcxError, ValueError) as exc:
                message = str(exc)
                if write_attempted:
                    message = (f"iCX {description} was attempted but could not be verified: "
                               f"{message}. No additional write sent.")
                self._last_error = message
                return False, message
            self._last_error = ""
            message = f"iCX {description}: controller setting verified by readback."
            if unavailable:
                registers = ", ".join(f"{other:02X}" for other in sorted(unavailable))
                message += f" Unrelated readback was unavailable for register(s) {registers}."
            return True, message

    def set_switching_frequency_khz(self, rail, value, *, acknowledged=False):
        if not acknowledged:
            return False, "I2C setting writes require acknowledgment"
        try:
            rail = _rail_name(rail)
            payload = switching_frequency_payload(value)
        except ValueError as exc:
            return False, str(exc)
        field = 1 if rail == "NVVDD" else 2
        return self._send_setting(FREQUENCY_REGISTERS[rail], payload,
                                  FREQUENCY_STATE_REG, field,
                                  f"{rail} switching frequency {int(value)} kHz")

    def set_ocp_enabled(self, rail, enabled, *, acknowledged=False):
        if not acknowledged:
            return False, "I2C setting writes require acknowledgment"
        try:
            rail = _rail_name(rail)
            payload = ocp_payload(enabled)
        except ValueError as exc:
            return False, str(exc)
        field = 1 if rail == "NVVDD" else 2
        return self._send_setting(OCP_REGISTERS[rail], payload, OCP_STATE_REG, field,
                                  f"{rail} OCP {'enabled' if enabled else 'disabled'}")

    def set_loadline(self, level, *, acknowledged=False):
        if not acknowledged:
            return False, "I2C setting writes require acknowledgment"
        try:
            payload = loadline_payload(level)
        except ValueError as exc:
            return False, str(exc)
        return self._send_setting(LOADLINE_REG, payload, LOADLINE_STATE_REG, 1,
                                  f"loadline Level {level}")

    def plan(self, mv):
        return False, "iCX voltage is not an offset register; nothing was written"

    def set_offset_mv(self, mv, *, acknowledged=False, dry_run=False, _xoc=None):
        return False, "iCX voltage is not an offset register; nothing was written"

    def reset(self):
        return False, "iCX NVVDD Auto uses its explicit control; nothing was written"

    def verify(self, *, acknowledged=False, log=None, ref=None,
               allow_idle=False, cancelled=None, operating_point=None):
        return False, (
            "iCX requested state is read-only telemetry; physical voltage verification "
            "requires a meter. Nothing was written."), []
