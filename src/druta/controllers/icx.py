# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""EVGA RTX 3090 Ti Kingpin iCX voltage requests, user-mode NVAPI I2C.

Classified talks to the iCX MCU at GPU I2C port 1, 7-bit address 0x2D. It does
not load a private driver. The packets below are the bytes that shifted through
that port's hardware master while one Classified control moved at a time.

NVVDD, command 0xC8: 09 01 VV, where VV = millivolts / 5.
Confirmed: 900 mV = 0xB4, 950 mV = 0xBE, 1000 mV = 0xC8.

FBVDD, same command: count high, count low, 01, where the 16-bit count is
millivolts / 5. Confirmed: 1300 mV = 01 04 01, 1380 mV = 01 14 01.

The NVIDIA driver does not read either setpoint back. This module does not
send the OpenRGB init write 0xB2, the 0x90 save write, the frequency selects
0x98 and 0xF8, the OCP selects 0x90 and 0xF0, or the loadline select 0x88.
"""
import ctypes
import math
import threading
from types import SimpleNamespace

from ..railctl import Rail, I2C_READ_EX, I2C_WRITE_EX, PTR, u8, u32

REGULATOR = "EVGA iCX"
PORT = 1
ADDR7 = 0x2D
DEVICE_ID = 0x2203
SUBSYSTEM_ID = 0x49983842
COMMAND = 0xC8
FIRMWARE_REG = 0xB1
FIRMWARE = bytes((0x05, 0xFC, 0x2D, 0x13, 0x01, 0x0E))

NVVDD_MIN_MV = 750
NVVDD_MAX_MV = 1150
FBVDD_MIN_MV = 1250
FBVDD_MAX_MV = 1450
STEP_MV = 5


def _millivolts(value, low, high, rail):
    if type(value) is bool or type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{rail} must be a finite number of millivolts")
    if not math.isclose(value, round(value), rel_tol=0, abs_tol=1e-9):
        raise ValueError(f"{rail} must be a whole number of millivolts")
    mv = int(round(value))
    if mv % STEP_MV:
        raise ValueError(f"{rail} steps by {STEP_MV} mV")
    if not low <= mv <= high:
        raise ValueError(f"{rail} must be {low}..{high} mV")
    return mv


def nvvdd_payload(mv):
    """Three data bytes for a manual NVVDD request. The command byte is separate."""
    value = _millivolts(mv, NVVDD_MIN_MV, NVVDD_MAX_MV, "NVVDD")
    return bytes((0x09, 0x01, value // STEP_MV))


# Classified Auto / the second Default click. The middle byte is 0x00, not the
# manual 0x01, and 0x96 is the 750 mV caption that tool shows for Auto. A
# manual Apply of 750 mV is 09 01 96 and is a different packet. The 0E 01
# trailer is the next transaction on the same command, two bytes, not a third
# data byte of the Auto packet.
NVVDD_AUTO_PAYLOAD = bytes((0x09, 0x00, 0x96))
NVVDD_AUTO_TRAILER = bytes((0x0E, 0x01))


def fbvdd_payload(mv):
    """Three data bytes for an FBVDD request. The command byte is separate."""
    value = _millivolts(mv, FBVDD_MIN_MV, FBVDD_MAX_MV, "FBVDD")
    count = value // STEP_MV
    return bytes((count >> 8, count & 0xFF, 0x01))


class EVGAICx(Rail):
    """One Kingpin iCX MCU. Identity is the firmware read plus this board's PCI IDs."""

    icx_mcu = True

    def __init__(self, nvapi, *, port=PORT, addr7=ADDR7):
        from ..railctl import _V3
        if ctypes.sizeof(_V3) != 64:
            raise ValueError("EVGA iCX requires the understood 64-byte NVAPI I2C V3 layout")
        if port != PORT or addr7 != ADDR7:
            raise ValueError("EVGA iCX voltage is only known at port 1 address 0x2D")
        src = {"controller": REGULATOR, "contract": 1, "port": PORT, "addr7": ADDR7,
               "device_id": DEVICE_ID, "subsystem_id": SUBSYSTEM_ID,
               "firmware": FIRMWARE.hex(), "command": COMMAND}
        profile = SimpleNamespace(
            name="EVGA RTX 3090 Ti Kingpin iCX", regulator=REGULATOR,
            rail="NVVDD/FBVDD", port=PORT, addr7=ADDR7, src=src, read_only=False,
            weak_id=False, provenance={"source": "Classified 3.1.9.0 port-1 captures"})
        super().__init__(profile, nvapi, addr7=ADDR7)
        self._mutex = threading.RLock()

    def _board_selected(self):
        selected = getattr(self.nvapi, "selected", None) or {}
        return (selected.get("devid") == DEVICE_ID
                and selected.get("subsys") == SUBSYSTEM_ID)

    def _firmware(self):
        fn = self.nvapi._i(I2C_READ_EX, PTR, PTR, PTR)
        if fn is None:
            return None
        buf, extra = (u8 * len(FIRMWARE))(), (u32 * 2)()
        packet, pointer = self._mk(FIRMWARE_REG, buf, len(FIRMWARE))
        status = fn(self.nvapi.gpu, ctypes.byref(packet), ctypes.byref(extra))
        _keep = (pointer, buf, extra)
        if status != 0 or packet.cbSize != len(FIRMWARE):
            return None
        return bytes(buf)

    def present(self):
        try:
            if not getattr(self.nvapi, "ok", False) or not self._board_selected():
                return False
            return self._firmware() == FIRMWARE
        except Exception:
            return False

    def telemetry(self):
        """Identity only. The MCU does not return the programmed voltage."""
        return {"firmware_ok": self.present()}

    def plan(self, mv):
        return False, "iCX voltage is not an offset register; nothing was written"

    def set_offset_mv(self, mv, *, acknowledged=False, dry_run=False, _xoc=None):
        return False, "iCX voltage is not an offset register; nothing was written"

    def reset(self):
        return False, "iCX stock is the NVVDD Auto packet, not this reset path; nothing was written"

    def verify(self, *, acknowledged=False, log=None, ref=None,
               allow_idle=False, cancelled=None, operating_point=None):
        return False, "iCX voltage has no driver readback; nothing was written", []

    def _write_payload(self, payload):
        if COMMAND != 0xC8 or (len(payload) != 3 and payload != NVVDD_AUTO_TRAILER):
            raise ValueError("refusing an iCX write outside the captured command 0xC8 payloads")
        fn = self.nvapi._i(I2C_WRITE_EX, PTR, PTR, PTR)
        if fn is None:
            return False
        buf, extra = (u8 * len(payload))(*payload), (u32 * 2)()
        packet, pointer = self._mk(COMMAND, buf, len(payload))
        status = fn(self.nvapi.gpu, ctypes.byref(packet), ctypes.byref(extra))
        _keep = (pointer, buf, extra)
        return status == 0

    def _send(self, mv, payload, rail):
        with self._mutex:
            if not self.present():
                return False, "EVGA iCX identity no longer matches this 3090 Ti Kingpin"
            if not self._write_payload(payload):
                return False, f"EVGA iCX {rail} write was not acknowledged"
            return True, (f"iCX {rail} request {mv} mV sent "
                          f"(command 0x{COMMAND:02X}, {payload.hex(' ')}). "
                          "The NVIDIA driver does not read this setpoint back.")

    def set_nvvdd_auto(self, *, acknowledged=False):
        """Send the Auto packet, then the trailer. Not a manual 750 mV request."""
        if not acknowledged:
            return False, "I2C voltage writes require acknowledgment"
        with self._mutex:
            if not self.present():
                return False, "EVGA iCX identity no longer matches this 3090 Ti Kingpin"
            if not self._write_payload(NVVDD_AUTO_PAYLOAD):
                return False, "EVGA iCX NVVDD Auto write was not acknowledged"
            if not self._write_payload(NVVDD_AUTO_TRAILER):
                return False, (
                    "iCX NVVDD Auto packet was sent, but the 0E 01 trailer "
                    "was not acknowledged")
            return True, (
                "iCX NVVDD Auto packet sent, then the trailer "
                f"(command 0x{COMMAND:02X}, {NVVDD_AUTO_PAYLOAD.hex(' ')}, "
                f"then {NVVDD_AUTO_TRAILER.hex(' ')}). This is not a manual "
                "750 mV write. The NVIDIA driver does not read this setpoint back.")

    def set_nvvdd_mv(self, mv, *, acknowledged=False):
        if not acknowledged:
            return False, "I2C voltage writes require acknowledgment"
        try:
            payload = nvvdd_payload(mv)
        except ValueError as exc:
            return False, str(exc)
        return self._send(int(round(mv)), payload, "NVVDD")

    def set_fbvdd_mv(self, mv, *, acknowledged=False):
        if not acknowledged:
            return False, "I2C voltage writes require acknowledgment"
        try:
            payload = fbvdd_payload(mv)
        except ValueError as exc:
            return False, str(exc)
        return self._send(int(round(mv)), payload, "FBVDD")
