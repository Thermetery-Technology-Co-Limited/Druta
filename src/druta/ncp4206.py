# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""NCP4206 absolute VID control, identified on the selected GPU's I2C bus.

Public source: onsemi NCP4206 datasheet, Table 10/11 and Voltage Control Mode.
Only VOUT_COMMAND and bit 3 of the paired VR Config registers are writable.
No calibration, protection, nonvolatile, or phase-control fields are changed.
"""
import math
import statistics
import threading
import time
from types import SimpleNamespace
from .railctl import Rail, _linear11

NORMAL_MAX_MV = 1281
XOC_MAX_MV = 2000
MIN_MV = 600
DISCOVERY_PORTS = (2, 0, 1, 3, 4, 5, 6, 7)
DISCOVERY_ADDRESSES = (0x20,) + tuple(a for a in range(0x08, 0x78) if a != 0x20)
# https://www.onsemi.com/download/data-sheet/pdf/ncp4206-d.pdf Table 11:
# these are read-only IDs, not user-programmable MP2888 identifiers. Keep
# model identity, but do not mistake the sampled revision for an ABI version.
# 0x20 is the documented address; other unicast routes can identify OEM parts.
OEM_IDENTITY = (0x41, 0x3298, 0x01)
DEFAULT_IDENTITY = (0x41, 0x0208, 0x03)
MODEL_IDS = (OEM_IDENTITY[1], DEFAULT_IDENTITY[1])


def decode_vid(code):
    if not isinstance(code, int) or not 2 <= code <= 198:
        return None
    return 1600.0 - (code - 2) * 6.25


def encode_vid(mv):
    if not math.isfinite(mv) or not 375 <= mv <= 1600:
        raise ValueError('NCP4206 VID can encode only 375..1600 mV; XOC does not extend the register')
    # Never round a requested ceiling upward.
    return 2 + math.ceil((1600 - mv) / 6.25)


class NCP4206(Rail):
    absolute_voltage = True

    def __init__(self, nvapi, *, architecture=None, port=2, addr7=0x20):
        if type(port) is not int or port not in DISCOVERY_PORTS:
            raise ValueError('NCP4206 discovery supports only I2C ports 0..7')
        if type(addr7) is not int or addr7 not in DISCOVERY_ADDRESSES:
            raise ValueError('NCP4206 discovery requires unicast 0x08..0x77')
        self.architecture = architecture
        self._identity = None
        self.discovery_diagnostics = {}
        recipe = {'kind': 'ncp4206-absolute-v1', 'port': port, 'addr7': addr7,
                  'identity': None, 'normal_max': NORMAL_MAX_MV,
                  'xoc_max': XOC_MAX_MV, 'vid_max': 1600, 'min_mv': MIN_MV}
        p = SimpleNamespace(name=f'NCP4206 output (port {port}, 0x{addr7:02X}; physical rail unassigned)', regulator='NCP4206',
                            rail='Controller output', port=port, addr7=addr7, src=recipe,
                            read_only=False, env_min=MIN_MV, env_max=NORMAL_MAX_MV,
                            hw_min_mv=MIN_MV, hw_max_mv=XOC_MAX_MV)
        super().__init__(p, nvapi)
        self._mutex = threading.RLock()

    def present(self):
        if not getattr(self.nvapi, 'ok', False):
            return False
        # Port/address ACKs alone never identify the regulator. Stop at the
        # manufacturer mismatch so empty ports need only one driver round trip.
        manufacturer = self.read(0x99, 1)
        if manufacturer != 0x41:
            return False
        identity = (manufacturer, self.read(0x9a, 2), self.read(0x9b, 1))
        self.discovery_diagnostics = dict(zip(('manufacturer', 'model', 'revision'), identity))
        if (identity[1] not in MODEL_IDS or type(identity[2]) is not int
                or not 0 <= identity[2] <= 255 or self.read(0x20, 1) != 32):
            return False
        if self._identity is not None:
            return identity == self._identity
        self._identity = identity
        self.p.src['identity'] = list(identity)
        # A model ID establishes register semantics, not which GPU rail is
        # connected. Preserve only the historical saved-profile identity.
        legacy = self.addr7 == 0x20 and identity in (OEM_IDENTITY, DEFAULT_IDENTITY)
        self.p.profile_name = ('GTX 770 - NVVDD (NCP4206)'
                               if legacy and self.p.port == 2 and identity == OEM_IDENTITY
                               else (f'Kepler - NVVDD (NCP4206, port {self.p.port})'
                                     if legacy else self.p.name))
        if legacy:
            self.p.profile_rail = 'NVVDD'
        return True

    def capture_control(self):
        if not self.present():
            raise ValueError('NCP4206 identity is unavailable')
        command, a, b = self.read(0x21, 2), self.read(0xd2, 1), self.read(0xd3, 1)
        if None in (command, a, b) or not 0 <= command <= 255 or (a & 8) != (b & 8):
            raise ValueError('NCP4206 command/mode read failed or paired VID modes disagree')
        enabled = bool(a & 8)
        if enabled and decode_vid(command) is None:
            raise ValueError('active NCP4206 VID is not a voltage code')
        return {'kind': 'absolute_vid', 'enabled': enabled, 'command': command}

    def telemetry(self):
        try:
            state = self.capture_control()
            raw = self.read(0xd7, 2)
            value = _linear11(raw) * 1000 if raw is not None else None
            if value is not None and not 300 <= value <= 2000:
                value = None
            return {'vout_mv': value, 'target_mv': decode_vid(state['command']) if state['enabled'] else None,
                    'control': state, 'offset_mv': None}
        except ValueError:
            return {}

    def read_vout(self):
        return self.telemetry().get('vout_mv')

    def validate_control(self, state, xoc=None):
        if not isinstance(state, dict) or set(state) != {'kind', 'enabled', 'command'}:
            raise ValueError('invalid NCP4206 profile control')
        if state['kind'] != 'absolute_vid' or type(state['enabled']) is not bool or type(state['command']) is not int:
            raise ValueError('invalid NCP4206 profile types')
        if not 0 <= state['command'] <= 255:
            raise ValueError('invalid NCP4206 command')
        if state['enabled']:
            target = decode_vid(state['command'])
            ceiling = XOC_MAX_MV if (self.xoc if xoc is None else xoc) else NORMAL_MAX_MV
            if target is None or not MIN_MV <= target <= ceiling:
                raise ValueError('NCP4206 target exceeds the saved mode or encoding bounds')
        return True

    def _write_checked(self, reg, value, width):
        if reg not in (0x21, 0xd2, 0xd3):
            raise ValueError('NCP4206 register is not writable')
        if not self._raw_write(reg, value, width) or self.read(reg, width) != value:
            raise ValueError(f'NCP4206 0x{reg:02X} write/readback failed')

    def _apply_control(self, state):
        # Only the command and VID_EN bits come from the profile. All other
        # register bits are retained from the current device, never replayed.
        a, b = self.read(0xd2, 1), self.read(0xd3, 1)
        if a is None or b is None:
            raise ValueError('NCP4206 configuration read failed')
        enabled = state['enabled']
        if enabled:
            self._write_checked(0x21, state['command'], 2)
        self._write_checked(0xd2, (a | 8) if enabled else (a & ~8), 1)
        self._write_checked(0xd3, (b | 8) if enabled else (b & ~8), 1)
        if not enabled:
            # Off codes may be restored only AFTER GPU VID mode is active.
            self._write_checked(0x21, state['command'], 2)
        if self.capture_control() != state:
            raise ValueError('NCP4206 final command/mode mismatch')

    def restore_control(self, state, *, recovery=False):
        with self._mutex:
            try:
                self.validate_control(state, xoc=True if recovery else None)
                before = self.capture_control()
                try:
                    self._apply_control(state)
                except Exception as exc:
                    try:
                        self._apply_control(before)
                    except Exception as rollback:
                        return False, f'{exc}; restoration also failed: {rollback}'
                    return False, f'{exc}; previous command/mode restored'
                return True, ('NCP4206 GPU VID mode restored' if not state['enabled'] else
                              f"NCP4206 target {decode_vid(state['command']):.2f} mV verified")
            except (ValueError, TypeError) as exc:
                return False, str(exc)

    def plan(self, mv):
        try:
            value = float(mv)
            ceiling = XOC_MAX_MV if self.xoc else NORMAL_MAX_MV
            if not math.isfinite(value) or not MIN_MV <= value <= ceiling:
                raise ValueError(f'NCP4206 target must be {MIN_MV}..{ceiling} mV in this mode')
            code = encode_vid(value)
            self.capture_control()
            return True, f'NCP4206 target {decode_vid(code):.2f} mV (VID 0x{code:02X})'
        except (ValueError, TypeError) as exc:
            return False, str(exc)

    def set_voltage_mv(self, mv, *, acknowledged=False):
        if not acknowledged:
            return False, 'I2C voltage writes require acknowledgment'
        with self._mutex:
            ok, msg = self.plan(mv)
            if not ok:
                return ok, msg
            return self.restore_control({'kind': 'absolute_vid', 'enabled': True,
                                         'command': encode_vid(float(mv))})

    def reset(self):
        with self._mutex:
            try:
                self.capture_control()
            except ValueError:
                # A failed write and rollback can leave the paired modes
                # inconsistent. Normal Apply must reject that state, but Auto
                # can still release the override without replaying a command.
                return self._recover_auto()
            return self.restore_control({'kind': 'absolute_vid', 'enabled': False, 'command': 0})

    def _recover_auto(self):
        """Clear only VID_EN after re-identifying an unreadable control state."""
        try:
            if not self.present():
                raise ValueError('NCP4206 identity is unavailable')
            values = [(reg, self.read(reg, 1)) for reg in (0xd2, 0xd3)]
            if any(type(value) is not int or not 0 <= value <= 255
                   for _, value in values):
                raise ValueError('NCP4206 configuration read failed')
            errors = []
            for reg, value in values:
                try:
                    self._write_checked(reg, value & ~8, 1)
                except Exception as exc:
                    # Still attempt to release the other half. Never roll
                    # back by re-enabling a mode whose command is unknown.
                    errors.append(str(exc))
            for reg, value in values:
                if self.read(reg, 1) != (value & ~8):
                    errors.append(f'NCP4206 0x{reg:02X} Auto readback mismatch')
            if errors:
                return False, 'NCP4206 Auto recovery failed: ' + '; '.join(errors)
            return True, 'NCP4206 GPU VID mode restored; stored command left unchanged'
        except Exception as exc:
            return False, 'NCP4206 Auto recovery failed: ' + str(exc)

    def _verification_vmon(self):
        """Read physical VMON and its actual LINEAR11 least-significant bit."""
        raw = self.read(0xd7, 2)
        if type(raw) is not int or not 0 <= raw <= 0xffff:
            raise ValueError('NCP4206 VMON read failed')
        exponent = raw >> 11
        if exponent & 16:
            exponent -= 32
        value = _linear11(raw) * 1000
        if not math.isfinite(value) or not 300 <= value <= 2000:
            raise ValueError('NCP4206 VMON is unavailable or outside its telemetry range')
        return value, (2.0 ** exponent) * 1000

    def verify(self, *, acknowledged=False, ref=None, log=None, cancelled=None,
               operating_point=None):
        # ref is retained for callers of the older interface. A driver voltage
        # or VID is not a reference for a physical controller-voltage response.
        self._verification_write_attempted = False
        self._verification_restore_ok = True
        self._verification_restore_error = ''
        cancelled = cancelled or (lambda: False)
        if cancelled():
            return False, 'NCP4206 verification cancelled; nothing written', []
        if not acknowledged:
            return False, 'I2C verification requires acknowledgment', []
        if not callable(operating_point):
            return False, 'NCP4206 verification requires a held P0 operating-point callback; nothing written', []
        with self._mutex:
            ladder = []
            point = None
            entry_xoc = bool(self.xoc)

            def check():
                nonlocal point
                if cancelled():
                    raise ValueError('verification cancelled')
                if bool(self.xoc) != entry_xoc:
                    raise ValueError('XOC mode changed during verification')
                if operating_point is not None:
                    current = operating_point()
                    if (not isinstance(current, (tuple, list)) or len(current) != 3
                            or any(type(v) not in (int, float) or not math.isfinite(v)
                                   for v in current)):
                        raise ValueError('GPU operating point is unreadable')
                    current = tuple(current)
                    if current[0] != 0:
                        raise ValueError('GPU is not held in P0')
                    if current[1] <= 0 or current[2] <= 0:
                        raise ValueError('GPU core/memory clocks are unavailable or nonpositive')
                    if point is None:
                        point = current
                    elif current != point:
                        raise ValueError('GPU P-state or core/memory clocks changed during verification')

            def sample(expected, *, voltage_ceiling=None):
                values, quanta = [], []
                # Match the offset verifier's complete one-second windows.
                # Never accept a partly sampled window after cancellation.
                for _ in range(25):
                    check()
                    if self.capture_control() != expected:
                        raise ValueError('NCP4206 command/mode changed during sampling')
                    value, quantum = self._verification_vmon()
                    if (type(value) not in (int, float) or not math.isfinite(value)
                            or type(quantum) not in (int, float)
                            or not math.isfinite(quantum) or quantum <= 0):
                        raise ValueError('NCP4206 VMON read failed')
                    if voltage_ceiling is not None and value > voltage_ceiling:
                        raise ValueError(f'VMON exceeded the normal verification ceiling '
                                         f'({voltage_ceiling:g} mV): {value:.2f} mV')
                    values.append(value)
                    quanta.append(quantum)
                    check()
                    time.sleep(.04)
                check()
                return {'samples_mv': values, 'median_mv': statistics.median(values),
                        'noise_mv': max(values) - min(values),
                        'quantum_mv': max(quanta)}

            try:
                original = self.capture_control()
                baseline = sample(original)
                base = baseline['median_mv']
                # These are bounded absolute-command trials, not a calibration
                # assumption about what fraction of the command VMON follows.
                targets = sorted({math.floor((base + step) / 6.25) * 6.25
                                  for step in (25, 37.5, 50)})
                targets = [target for target in targets
                           if MIN_MV <= target <= NORMAL_MAX_MV and target > base]
                if not targets:
                    raise ValueError('rail lacks headroom for the bounded verification staircase')
            except Exception as exc:
                return False, f'NCP4206 INCONCLUSIVE - {exc}; nothing written', ladder

            hit, failure = None, None
            try:
                for target in targets:
                    check()
                    rung = {'baseline_mv': base,
                            'baseline_samples_mv': baseline['samples_mv'],
                            'baseline_quantum_mv': baseline['quantum_mv'],
                            'target_mv': target, 'voltage_ceiling_mv': NORMAL_MAX_MV, 'moved': False}
                    ladder.append(rung)
                    self._verification_write_attempted = True
                    self._verification_restore_ok = False
                    ok, message = self.set_voltage_mv(target, acknowledged=True)
                    if not ok:
                        raise ValueError('verification write refused: ' + message)
                    time.sleep(.15)
                    trial = sample({'kind': 'absolute_vid', 'enabled': True,
                                    'command': encode_vid(target)}, voltage_ceiling=NORMAL_MAX_MV)
                    noise = max(baseline['noise_mv'], trial['noise_mv'])
                    quantum = max(baseline['quantum_mv'], trial['quantum_mv'])
                    # VID is a command, not a calibrated physical-voltage
                    # ceiling. GTX 770 VMON reproducibly reads above the VID
                    # target. Enforce the existing normal voltage envelope per
                    # sample, and judge response/reversal independently of gain.
                    delta = trial['median_mv'] - base
                    threshold = max(6.25, quantum, noise)
                    moved = delta >= max(6.25, quantum) and delta > noise
                    rung.update(trial, delta_mv=delta, threshold_mv=threshold,
                                response_noise_mv=noise, moved=moved)
                    if log:
                        log(f'NCP4206 I2C VMON {base:.2f} -> {trial["median_mv"]:.2f} mV; '
                            f'target {target:.2f} mV; change {delta:.2f} mV; '
                            f'noise {noise:.2f} mV; quantum {quantum:.4f} mV')
                    if moved:
                        hit = rung
                        break
            except Exception as exc:
                failure = str(exc)
            finally:
                errors = []
                try:
                    ok, message = (self.restore_control(original, recovery=True)
                                   if self._verification_write_attempted
                                   else (True, 'nothing written'))
                    if not ok:
                        errors.append(message)
                except Exception as exc:
                    errors.append(str(exc))
                try:
                    if self.capture_control() != original:
                        errors.append('original command/mode independent readback mismatch')
                except Exception as exc:
                    errors.append('original command/mode readback failed: ' + str(exc))
                self._verification_restore_ok = not errors
                self._verification_restore_error = '; '.join(errors)
            if cancelled():
                failure = 'verification cancelled' + ('; ' + failure if failure else '')
            if errors:
                return False, ('NCP4206 verification restoration failed: ' + '; '.join(errors)
                               + ('; ' + failure if failure else '')), ladder
            if failure:
                return False, ('NCP4206 INCONCLUSIVE - ' + failure
                               + '; original control restored'), ladder
            if hit is None:
                return False, ('NCP4206 INCONCLUSIVE - no positive I2C VMON response '
                               'exceeded measured noise and controller resolution; '
                               'original control restored. Apply remains unverified.'), ladder
            try:
                time.sleep(.15)
                restored = sample(original)
                quantum = max(baseline['quantum_mv'], restored['quantum_mv'])
                # One command step may span a nonintegral number of VMON
                # codes; round the band up to complete measured ADC codes.
                tolerance = max(math.ceil(6.25 / quantum) * quantum,
                                baseline['noise_mv'], restored['noise_mv'])
                if abs(restored['median_mv'] - base) > tolerance:
                    raise ValueError('I2C VMON did not return within the baseline noise/resolution band')
                reversal = hit['median_mv'] - restored['median_mv']
                if (reversal < max(6.25, hit['quantum_mv'], restored['quantum_mv'])
                        or reversal <= max(hit['response_noise_mv'], restored['noise_mv'])):
                    raise ValueError('I2C VMON did not show a downward response above noise after restoration')
                hit.update(restored_vout_mv=restored['median_mv'],
                           restored_samples_mv=restored['samples_mv'],
                           restored_quantum_mv=restored['quantum_mv'],
                           reversal_mv=reversal)
            except Exception as exc:
                failure = str(exc)
            finally:
                # The reversal window can itself reveal a controller-state
                # change. Do not publish a pass based on an earlier readback.
                try:
                    if self.capture_control() != original:
                        raise ValueError('original command/mode changed during voltage-restoration observation')
                except Exception as exc:
                    self._verification_restore_ok = False
                    self._verification_restore_error = str(exc)
            if not self._verification_restore_ok:
                return False, ('NCP4206 verification restoration failed: '
                               + self._verification_restore_error
                               + ('; ' + failure if failure else '')), ladder
            if failure:
                return False, (f'NCP4206 INCONCLUSIVE - {failure}; original command/mode restored; '
                               'voltage response remains unverified'), ladder
            return True, ('NCP4206 WRITE PATH CONFIRMED at the tested operating point. '
                          f'I2C VMON rose {hit["delta_mv"]:.2f} mV and returned to '
                          f'{restored["median_mv"]:.2f} mV after exact command/mode restoration. '
                          'This verifies a response, not full-load behavior or a 1:1 voltage gain.'), ladder
