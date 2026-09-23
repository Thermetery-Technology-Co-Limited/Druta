# SPDX-License-Identifier: GPL-3.0-or-later
"""Nuvoton NCT3933U three-channel current DAC (June 2013 datasheet, Rev A2).

This is not a PMBus voltage monitor. CR01..03 contain sign/magnitude current
commands; board wiring determines their voltage effect. CR05 is observed and
preserved in full. CR04 is never accessed because its watchdog flag clears on
read. All writes are one-byte, volatile output commands, followed by readback.
"""
import ctypes
import math
import threading
from types import SimpleNamespace

from ..railctl import Rail, I2C_READ_EX, I2C_WRITE_EX, PTR, u8, u32

DISCOVERY_PORTS = tuple(range(8))
# Datasheet section 7.9 lists wire addresses 20/22/24/26/28/2A.
DISCOVERY_ADDRESSES = tuple(range(0x10, 0x16))
OUTPUT_REGISTERS = (1, 2, 3)
CONFIG_REGISTER = 5
IDENTITY = ((0x5D, 0x39), (0x5E, 0x33))


def _byte(value):
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("DAC register values must be integer bytes")
    return value


def _channel(channel):
    if type(channel) is not int or channel not in OUTPUT_REGISTERS:
        raise ValueError("DAC channel must be 1, 2 or 3")
    return channel


def current_step_ua(configuration, channel):
    configuration, channel = _byte(configuration), _channel(channel)
    return 20 if configuration & (1 << (2 * (channel - 1))) else 10


def decode_current_ua(raw, configuration, channel):
    """Nominal commanded current: positive source, negative sink; not voltage."""
    raw = _byte(raw)
    magnitude = (raw & 0x7F) * current_step_ua(configuration, channel)
    return magnitude if raw & 0x80 else -magnitude


def encode_current_ua(value, configuration, channel):
    step = current_step_ua(configuration, channel)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("DAC current must be a finite number of microamps")
    steps = abs(value) / step
    if steps > 127 or not math.isclose(steps, round(steps), rel_tol=0, abs_tol=1e-9):
        raise ValueError(f"Current must be a multiple of {step} uA within +/-{127 * step} uA")
    return int(round(steps)) | (0x80 if value > 0 else 0)


def _state(state):
    if not isinstance(state, dict) or set(state) != {"outputs", "configuration"}:
        raise ValueError("DAC state requires exactly outputs and configuration")
    outputs = state["outputs"]
    if not isinstance(outputs, list) or len(outputs) != 3:
        raise ValueError("DAC state requires three output bytes")
    return {"outputs": [_byte(v) for v in outputs],
            "configuration": _byte(state["configuration"])}


class NCT3933U(Rail):
    current_dac = True
    requires_verification = False

    def __init__(self, nvapi, *, port=1, addr7=0x15):
        from ..railctl import _V3
        if ctypes.sizeof(_V3) != 64:
            raise ValueError("NCT3933U requires the understood 64-byte NVAPI I2C V3 layout")
        if type(port) is not int or port not in DISCOVERY_PORTS:
            raise ValueError("Invalid I2C port")
        if type(addr7) is not int or addr7 not in DISCOVERY_ADDRESSES:
            raise ValueError("Address is outside the NCT3933U documented strap options")
        src = {"controller": "NCT3933U", "contract": 1,
               "units": "nominal_microamps", "port": port, "addr7": addr7}
        p = SimpleNamespace(name="NCT3933U - three current offsets", regulator="Nuvoton NCT3933U",
                            rail="OUT1/OUT2/OUT3", port=port, addr7=addr7, src=src,
                            read_only=False, weak_id=False, provenance={
                                "datasheet": "Nuvoton NCT3933U, June 2013 Rev A2, sections 7.9 and 8"})
        super().__init__(p, nvapi, addr7=addr7)
        self._transaction_lock = threading.RLock()
        self._target = self._target_identity()
        self.last_transaction = None
        self._verification_restore_ok = True
        self._verification_restore_error = ""

    def _target_identity(self):
        handle = getattr(self.nvapi, "gpu", None)
        selected = getattr(self.nvapi, "selected", None) or {}
        return (getattr(handle, "value", handle), selected.get("slot"),
                selected.get("devid"), selected.get("subsys"))

    def read(self, cmd, n=1):
        if cmd not in (*OUTPUT_REGISTERS, CONFIG_REGISTER, *(r for r, _ in IDENTITY)) or n != 1:
            raise ValueError("Read outside the understood NCT3933U register contract")
        fn = self.nvapi._i(I2C_READ_EX, PTR, PTR, PTR)
        if fn is None:
            return None
        buf, extra = (u8 * 1)(0xA5), (u32 * 2)()
        packet, pointer = self._mk(cmd, buf, 1)
        status = fn(self.nvapi.gpu, ctypes.byref(packet), ctypes.byref(extra))
        return int(buf[0]) if status == 0 and packet.cbSize == 1 else None

    def _raw_write(self, cmd, value, nbytes=1):
        if cmd not in OUTPUT_REGISTERS or nbytes != 1:
            raise ValueError("Only NCT3933U output registers are writable")
        _byte(value)
        fn = self.nvapi._i(I2C_WRITE_EX, PTR, PTR, PTR)
        if fn is None:
            return False
        buf, extra = (u8 * 1)(value), (u32 * 2)()
        packet, pointer = self._mk(cmd, buf, 1)
        return fn(self.nvapi.gpu, ctypes.byref(packet), ctypes.byref(extra)) == 0

    def present(self):
        try:
            return (bool(getattr(self.nvapi, "ok", False))
                    and self._target_identity() == self._target
                    and all(self.read(reg, 1) == value for reg, value in IDENTITY))
        except Exception:
            return False

    def capture_control(self):
        with self._transaction_lock:
            if not self.present():
                raise ValueError("NCT3933U identity/selected GPU no longer matches")
            configuration = self.read(CONFIG_REGISTER, 1)
            outputs = [self.read(reg, 1) for reg in OUTPUT_REGISTERS]
            state = _state({"outputs": outputs, "configuration": configuration})
            if self.read(CONFIG_REGISTER, 1) != configuration or not self.present():
                raise ValueError("NCT3933U identity/configuration changed during read")
            return state

    def telemetry(self):
        state = self.capture_control()
        return dict(state, currents_ua=[decode_current_ua(v, state["configuration"], i)
                                       for i, v in enumerate(state["outputs"], 1)],
                    outputs_enabled=not bool(state["configuration"] & 0x40))

    def validate_control(self, state, xoc=None):
        state = _state(state)
        current = self.capture_control()
        if current["configuration"] != state["configuration"]:
            raise ValueError("DAC configuration/scaling changed; capture fresh settings")

    def set_outputs(self, outputs, *, acknowledged=False, expected=None):
        """Set exact output bytes, preserving configuration and other channels.

        An unsuccessful transaction can have reached hardware. Attempted
        channels are rolled back only if identity/configuration still match;
        a failed rollback is reported explicitly, never as successful Apply.
        """
        if not acknowledged:
            return False, "DAC writes require Unlock controls and I2C opt-in"
        attempted = []
        entry = None
        with self._transaction_lock:
            self._verification_restore_ok = True
            self._verification_restore_error = ""
            self.last_transaction = {"requested_outputs": outputs, "attempted_registers": attempted}
            try:
                requested = _state({"outputs": outputs, "configuration": 0})["outputs"]
                if expected is not None:
                    expected = _state(expected)
                entry = self.capture_control()
                self.last_transaction["before"] = entry
                if expected is not None and entry != expected:
                    raise ValueError("DAC settings changed since the request was prepared; read again")
                working = {"outputs": list(entry["outputs"]), "configuration": entry["configuration"]}
                for index, raw in enumerate(requested):
                    if raw == working["outputs"][index]:
                        continue
                    if self.capture_control() != working:
                        raise ValueError("DAC state changed before dispatch; another writer may be active")
                    reg = index + 1
                    attempted.append(reg)
                    if not self._raw_write(reg, raw, 1):
                        raise ValueError(f"OUT{reg} write rejected or outcome uncertain")
                    working["outputs"][index] = raw
                    if self.capture_control() != working:
                        raise ValueError(f"OUT{reg} readback or preserved state did not match")
                after = self.capture_control()
                if after != working:
                    raise ValueError("Final DAC readback did not match")
                self.last_transaction["after"] = after
                return True, ("NCT3933U output bytes " + "/".join(f"{v:02X}" for v in after["outputs"])
                              + " read back exactly; configuration unchanged. "
                              + ("Outputs disabled by power saving. " if after["configuration"] & 0x40 else "")
                              + "Physical voltage requires a meter.")
            except Exception as exc:
                message = str(exc)
                if attempted and entry is not None:
                    errors = []
                    for reg in reversed(attempted):
                        try:
                            current = self.capture_control()
                            if current["configuration"] != entry["configuration"]:
                                raise ValueError("configuration changed; restore not dispatched")
                            original = entry["outputs"][reg - 1]
                            if current["outputs"][reg - 1] != original:
                                if not self._raw_write(reg, original, 1):
                                    raise ValueError("restore write rejected")
                            back = self.capture_control()
                            if back["configuration"] != entry["configuration"] or back["outputs"][reg - 1] != original:
                                raise ValueError("restore readback mismatch")
                        except Exception as restore_exc:
                            errors.append(f"OUT{reg}: {restore_exc}")
                    try:
                        if self.capture_control() != entry:
                            errors.append("complete entry state not restored")
                    except Exception as restore_exc:
                        errors.append(str(restore_exc))
                    self._verification_restore_ok = not errors
                    self._verification_restore_error = "; ".join(errors)
                    message += ("; RESTORE FAILED; state uncertain: " + "; ".join(errors)
                                if errors else "; exact entry settings restored")
                self.last_transaction["error"] = message
                return False, message

    def set_current_ua(self, channel, value, *, acknowledged=False, expected=None):
        try:
            channel = _channel(channel)
            if expected is not None:
                expected = _state(expected)
            with self._transaction_lock:
                state = self.capture_control()
                if expected is not None and state != expected:
                    raise ValueError("DAC settings changed since capture; read again before applying")
                outputs = list(state["outputs"])
                outputs[channel - 1] = encode_current_ua(value, state["configuration"], channel)
                return self.set_outputs(outputs, acknowledged=acknowledged, expected=state)
        except Exception as exc:
            return False, str(exc)

    def zero_outputs(self, *, acknowledged=False, expected=None):
        return self.set_outputs([0, 0, 0], acknowledged=acknowledged, expected=expected)

    def reset(self):
        return self.zero_outputs(acknowledged=True)

    def restore_control(self, state, *, recovery=False):
        try:
            state = _state(state)
            with self._transaction_lock:
                current = self.capture_control()
                if current["configuration"] != state["configuration"]:
                    raise ValueError("DAC configuration/scaling changed; restore not dispatched")
                return self.set_outputs(state["outputs"], acknowledged=True, expected=current)
        except Exception as exc:
            return False, str(exc)

    def read_vout(self):
        return None
