# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""NCP4206 absolute VID control, identified on a Kepler GPU's I2C bus.

Public source: onsemi NCP4206 datasheet, Table 10/11 and Voltage Control Mode.
Only VOUT_COMMAND and bit 3 of the paired VR Config registers are writable.
No calibration, protection, nonvolatile, or phase-control fields are changed.
"""
import math
import statistics
import threading
import time
from types import SimpleNamespace
from railctl import Rail, _linear11

NORMAL_MAX_MV = 1281
XOC_MAX_MV = 2000
MIN_MV = 600
DISCOVERY_PORTS = (2, 0, 1, 3, 4, 5, 6, 7)
# onsemi NCP4206 datasheet Table 11 (p.27) default, plus the OEM identity
# measured on GTX 770 and both GTX 690 controllers. Do not accept every
# onsemi manufacturer ID as this controller: model AND revision must match.
OEM_IDENTITY = (0x41, 0x3298, 0x01)
DEFAULT_IDENTITY = (0x41, 0x0208, 0x03)
IDENTITIES = (OEM_IDENTITY, DEFAULT_IDENTITY)


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

    def __init__(self, nvapi, *, architecture=None, port=2):
        if type(port) is not int or port not in DISCOVERY_PORTS:
            raise ValueError('NCP4206 discovery supports only I2C ports 0..7')
        self.architecture = architecture
        self._identity = None
        recipe = {'kind': 'ncp4206-absolute-v1', 'port': port, 'addr7': 32,
                  'identity': None, 'normal_max': NORMAL_MAX_MV,
                  'xoc_max': XOC_MAX_MV, 'vid_max': 1600, 'min_mv': MIN_MV}
        p = SimpleNamespace(name=f'Kepler - NVVDD (NCP4206, port {port})', regulator='NCP4206',
                            rail='NVVDD', port=port, addr7=32, src=recipe,
                            read_only=False, env_min=MIN_MV, env_max=NORMAL_MAX_MV,
                            hw_min_mv=MIN_MV, hw_max_mv=XOC_MAX_MV)
        super().__init__(p, nvapi)
        self._mutex = threading.RLock()

    def present(self):
        if self.architecture != 2 or not getattr(self.nvapi, 'ok', False):
            return False
        # Port/address ACKs alone never identify the regulator. Stop at the
        # manufacturer mismatch so empty ports need only one driver round trip.
        manufacturer = self.read(0x99, 1)
        if manufacturer != 0x41:
            return False
        identity = (manufacturer, self.read(0x9a, 2), self.read(0x9b, 1))
        if identity not in IDENTITIES or self.read(0x20, 1) != 32:
            return False
        if self._identity is not None:
            return identity == self._identity
        self._identity = identity
        self.p.src['identity'] = list(identity)
        # Preserve the saved-profile fingerprint for the original port-2 OEM
        # recipe, while the visible name describes any identified Kepler card.
        self.p.profile_name = ('GTX 770 - NVVDD (NCP4206)'
                               if self.p.port == 2 and identity == OEM_IDENTITY
                               else self.p.name)
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
        return self.restore_control({'kind': 'absolute_vid', 'enabled': False, 'command': 0})

    def verify(self, *, acknowledged=False, ref=None, log=None):
        if not acknowledged:
            return False, 'I2C verification requires acknowledgment', []
        with self._mutex:
            original = self.capture_control()

            def sample(delay):
                values = []
                for _ in range(5):
                    time.sleep(delay)
                    values.append(self.read_vout())
                return values

            def complete(values):
                return all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for v in values)

            baseline_samples = sample(.05)
            if not complete(baseline_samples):
                return False, 'NCP4206 baseline voltage read failed; nothing written', []
            baseline = statistics.median(baseline_samples)
            noise = max(baseline_samples) - min(baseline_samples)
            threshold = max(8.0, 2 * noise)
            targets = sorted({math.floor((baseline + step) / 6.25) * 6.25
                              for step in (25, 37.5, 50)})
            targets = [target for target in targets
                       if MIN_MV <= target <= NORMAL_MAX_MV
                       and target >= baseline + threshold]
            if not 800 <= baseline <= NORMAL_MAX_MV or not targets:
                return False, 'loaded rail lacks headroom for the bounded verification staircase', []
            ladder = []
            result = (False, 'NCP4206 verification did not run', ladder)
            try:
                for target in targets:
                    rung = {'baseline_mv': baseline, 'baseline_samples_mv': baseline_samples,
                            'noise_mv': noise, 'threshold_mv': threshold,
                            'target_mv': target, 'samples_mv': [], 'moved': False}
                    ladder.append(rung)
                    ok, msg = self.set_voltage_mv(target, acknowledged=True)
                    if not ok:
                        rung['refused'] = msg
                        result = (False, 'NCP4206 verification write refused: ' + msg, ladder)
                        break
                    samples = rung['samples_mv'] = sample(.15)
                    if not complete(samples):
                        rung['read_failed'] = True
                        result = (False, 'NCP4206 verification voltage read failed', ladder)
                        break
                    # Loadline drop can leave VMON well below the requested
                    # VID. Require a sustained rise above measured noise, not
                    # closeness below the target. Overshoot still fails closed.
                    overshoot = max(samples) > target + 15
                    moved = not overshoot and min(samples) >= baseline + threshold
                    rung.update(moved=moved, overshoot=overshoot,
                                minimum_rise_mv=min(samples) - baseline)
                    message = (f'NCP4206 {baseline:.2f} -> target {target:.2f} mV; '
                               f'VMON {samples}; minimum rise {min(samples)-baseline:.2f} mV '
                               f'(need {threshold:.2f} mV)')
                    if log:
                        log(message + ('; MOVED' if moved else '; overshoot' if overshoot else '; flat'))
                    result = (moved, message + ('' if moved else
                              '; overshoot rejected' if overshoot else '; no confirmed response'), ladder)
                    if moved or overshoot:
                        break
            except Exception as exc:
                result = (False, f'NCP4206 verification failed: {exc}', ladder)
            finally:
                try:
                    ok, msg = self.restore_control(original, recovery=True)
                except Exception as exc:
                    ok, msg = False, str(exc)
            if not ok:
                return False, 'NCP4206 verification restoration failed: ' + msg, ladder
            return result
