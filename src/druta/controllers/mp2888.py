# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""MP2888A discovery and session-only PMBus voltage control.

The address register is not a model ID. These checks establish a candidate
register layout, not a unique controller identity; the UI must still Verify
its bounded voltage response before enabling Apply. VOUT_COMMAND is a nominal
VID, not a calibrated physical voltage. Only 0x21 and E1 bit 4 are written;
coarse/fine offsets, sense configuration, protection and buffer-update commands
are never changed.

MPS MP2888A Rev.1.11: pp.30/86 address, p.43 offset/limit, pp.71-72
telemetry. Linux drivers/hwmon/pmbus/mp2888.c documents 0x44 bit3 for
current resolution. No PAGE selection or vendor/device-ID gates.
"""
import ctypes
import math
import threading
import time
from copy import deepcopy
from statistics import median

from ..railctl import _V3, I2C_READ_EX, I2C_WRITE_EX, PTR, Profile, Rail, _normalize_routes, _signed

DISCOVERY_PORTS = tuple(range(8))
DISCOVERY_ADDRESSES = (0x20,) + tuple(a for a in range(0x08, 0x78) if a != 0x20)
FINGERPRINT_VERSION = 1
CONTROL_KIND = 'mp2888-pmbus-v1'
NORMAL_MAX_MV = 1200.0
XOC_MAX_MV = 2000.0
CONTROL_REGISTERS = {
    'page': (0x00, 1), 'address': (0xBE, 1),
    'command': (0x21, 2), 'mode_word': (0xE1, 2),
    'operation': (0x01, 1), 'pwm_config': (0x45, 2),
    'offset_word': (0x23, 2), 'sense_config': (0x2A, 2),
    'fine_trim': (0x2D, 1), 'vout_min': (0x1F, 2), 'vout_max': (0x24, 2),
}


class _VoltageProfile(Profile):
    """The inherited recipe describes offsets; the live UI uses nominal VID."""

    @property
    def hw_min_mv(self):
        return self.env_min

    @property
    def hw_max_mv(self):
        return self.hardware_max_mv


def _bound_profile(profile, port, addr7):
    """Copy the register recipe; keep the legacy saved-profile identity at home."""
    data = deepcopy(profile.src)
    original = (port == profile.port and addr7 == profile.addr7)
    if not original:
        data['bus'] = {'port': port, 'addr7': addr7}
        data['match'] = {}
        data['identity'] = [{'reg': 0xBE, 'bytes': 1, 'mask': 0x7F,
                             'value': addr7, 'fingerprint': True}]
        data['discovery'] = {'controller': 'MP2888A candidate',
                             'fingerprint_version': FINGERPRINT_VERSION}
    p = _VoltageProfile(data, profile.path)
    p.profile_name = profile.name
    p.profile_rail = p.rail  # Saved profiles used the original recipe's NVVDD label.
    p.rail = 'Controller output'
    p.name = (f'MP2888A candidate (port {port}, 0x{addr7:02X}; '
              'physical rail unassigned)')
    p.port, p.addr7, p.addrs = port, addr7, [addr7]
    p.pci_device = p.pci_subsys = set()
    p.weak_id = True
    p.env_min, p.env_max, p.hardware_max_mv = 300.0, NORMAL_MAX_MV, NORMAL_MAX_MV
    # Runtime presentation/decoding can evolve without invalidating old saved
    # offsets on the original connection. present() never trusts that old ID.
    p.telemetry_specs = deepcopy(p.telemetry_specs)
    for spec in p.telemetry_specs:
        if spec.get('key') == 'iout_a':
            spec.update(encoding='uint', bits='11:0', scale=0.25)
    return p


class MP2888Candidate(Rail):
    """A location-bound candidate; every write rechecks the read-only layout."""

    requires_verification = True
    absolute_voltage = True
    requires_voltage_clamp = True
    session_only_voltage = True
    controller_reported_vout = True
    vout_readback_label = 'controller-reported VOUT'
    vout_readback_note = ' Physical voltage requires an independent measurement.'

    def __init__(self, profile, nvapi, port, addr7):
        if (type(port) is not int or port not in DISCOVERY_PORTS
                or type(addr7) is not int or addr7 not in DISCOVERY_ADDRESSES):
            raise ValueError('MP2888A discovery requires port 0..7 and unicast 0x08..0x77')
        # The private EX entry points use this documented V3 layout on x64.
        offsets = (0, 4, 8, 9, 16, 24, 32, 40, 44, 48, 52, 56)
        if (ctypes.sizeof(_V3) != 64
                or tuple(getattr(_V3, name).offset for name, *_ in _V3._fields_) != offsets):
            raise ValueError('MP2888A NVAPI V3 packet geometry is unsupported')
        super().__init__(_bound_profile(profile, port, addr7), nvapi, addr7)
        self._bound_handle = self._gpu_handle()
        self._bound_selection = deepcopy(getattr(nvapi, 'selected', None))
        self._mutex = threading.RLock()
        self.discovery_telemetry = {}
        self.discovery_diagnostics = {}
        self.last_written_control = None
        self.last_control_restore_ok = True
        self.last_control_restore_error = ''
        self._pending_recovery_states = []
        self._verification_write_attempted = False
        self._verification_restore_ok = True
        self._verification_restore_error = ''

    def _gpu_handle(self):
        value = getattr(self.nvapi, 'gpu', None)
        return int(getattr(value, 'value', value) or 0)

    def _binding(self):
        if (self.nvapi is None or not getattr(self.nvapi, 'ok', False)
                or not self._bound_handle or self._gpu_handle() != self._bound_handle
                or getattr(self.nvapi, 'selected', None) != self._bound_selection):
            raise ValueError('selected GPU/handle changed or NVAPI is unavailable')
        selected = self._bound_selection
        if isinstance(selected, dict) and 'handle' in selected:
            handle = selected['handle']
            if int(getattr(handle, 'value', handle) or 0) != self._bound_handle:
                raise ValueError('selected GPU metadata does not match its handle')

    def _transfer(self, cmd, width, value=None):
        self._binding()
        if type(cmd) is not int or not 0 <= cmd <= 255 or width not in (1, 2):
            raise ValueError('unsupported MP2888A register transaction')
        writing = value is not None
        fn = self.nvapi._i(I2C_WRITE_EX if writing else I2C_READ_EX, PTR, PTR, PTR)
        if fn is None:
            return None
        payload = value.to_bytes(width, 'little') if writing else bytes([0xA5] * width)
        buf = (ctypes.c_uint8 * width).from_buffer_copy(payload)
        packet, prefix = self._mk(cmd, buf, width)
        geometry = bytes(packet)
        extra = (ctypes.c_uint32 * 2)()
        status = fn(self.nvapi.gpu, ctypes.byref(packet), ctypes.byref(extra))
        self._binding()
        if bytes(packet) != geometry or bytes(prefix) != bytes([cmd]):
            raise ValueError('NVAPI changed MP2888A transaction geometry')
        if writing and bytes(buf) != payload:
            raise ValueError('NVAPI changed the MP2888A write payload')
        return (True if writing else int.from_bytes(bytes(buf), 'little')) if status == 0 else None

    def read(self, cmd, n):
        return self._transfer(cmd, n)

    def _read_retry(self, cmd, width):
        for attempt in range(3):
            try:
                value = self.read(cmd, width)
            except OSError:
                value = None
            if value is not None:
                return value
            if attempt < 2:
                time.sleep(.01)
        return None

    def _raw_write(self, cmd, value, nbytes):
        if (cmd not in (0x21, 0xE1) or nbytes != 2 or type(value) is not int
                or not 0 <= value <= 0xFFFF):
            raise ValueError('MP2888A permits only word writes to 0x21 and E1 bit 4')
        if cmd == 0x21 and value & ~0x1FF:
            raise ValueError('MP2888A VOUT_COMMAND has reserved bits set')
        page, address = self._read_retry(0x00, 1), self._read_retry(0xBE, 1)
        if page != 0 or address is None or address & 0x7F != self.addr7:
            raise ValueError('MP2888A PAGE/address changed before write')
        if cmd == 0xE1:
            current = self._read_retry(cmd, 2)
            if current is None or (current ^ value) & ~0x10:
                raise ValueError('MP2888A E1 non-mode bits changed or are unavailable')
        return self._transfer(cmd, nbytes, value) is True

    def _fingerprint_sample(self):
        # Test address first so absent devices cost just one read.
        addr = self.read(0xBE, 1)
        if addr is None or addr & 0x7F != self.addr7:
            return None
        offset, limit = self.read(0x23, 2), self.read(0x24, 2)
        if (offset is None or limit is None or offset & 0xFF00 or limit & 0xFE00
                or not -111 <= _signed(offset, 8) <= 112
                or not 300 <= limit * 6.25 <= 3193.75):
            return None
        vout = self.read(0x8B, 2)
        current = self.read(0x8C, 2)
        temp = self.read(0x8D, 2)
        config = self.read(0x44, 2)
        if any(value is None for value in (vout, current, temp, config)):
            return None
        if (vout & 0xF000 or current & 0xF000 != 0xE000 or temp & 0xF800
                or not 300 <= vout <= 2000 or not 0 <= temp <= 1500):
            return None
        scale = 0.5 if config & 8 else 0.25
        return {'vout_mv': float(vout), 'iout_a': (current & 0xFFF) * scale,
                'vrm_temp_c': temp * 0.1, 'vout_max_mv': limit * 6.25,
                'offset_raw': offset, 'offset_mv': _signed(offset, 8) * 6.25,
                'current_lsb_a': scale, 'address_register': addr}

    def present(self):
        self.discovery_telemetry = {}
        if self.nvapi is None or not getattr(self.nvapi, 'ok', False):
            return False
        try:
            first = self._fingerprint_sample()
            if first is None:
                return False
            samples = [first, self._fingerprint_sample()]
        except Exception:
            return False
        if any(s is None for s in samples):
            return False
        first, last = samples
        # Config/offset must be repeatable. VOUT can move with the governor;
        # allow ordinary idle/load transitions, while rejecting erratic reads.
        if (any(first[k] != last[k] for k in
                ('offset_raw', 'vout_max_mv', 'current_lsb_a', 'address_register'))
                or abs(first['vout_mv'] - last['vout_mv']) > 500
                or abs(first['vrm_temp_c'] - last['vrm_temp_c']) > 10):
            return False
        self.discovery_telemetry = {k: median(s[k] for s in samples)
                                    for k in first}
        self.discovery_telemetry['offset_raw'] = last['offset_raw']
        return True

    @staticmethod
    def normalize_voltage_mv(mv):
        if isinstance(mv, bool):
            raise ValueError('MP2888A target must be a finite voltage')
        value = float(mv)
        if not math.isfinite(value):
            raise ValueError('MP2888A target must be a finite voltage')
        return math.floor(value / 6.25) * 6.25

    @staticmethod
    def _shape(state):
        if (not isinstance(state, dict)
                or set(state) != {'kind', 'enabled', *CONTROL_REGISTERS}
                or state['kind'] != CONTROL_KIND or type(state['enabled']) is not bool):
            raise ValueError('invalid MP2888A control snapshot')
        for key, (_, width) in CONTROL_REGISTERS.items():
            if type(state[key]) is not int or not 0 <= state[key] < 1 << (8 * width):
                raise ValueError(f'invalid MP2888A {key} encoding')
        if state['page'] != 0:
            raise ValueError('MP2888A PAGE must remain zero')
        if any(state[key] & ~0x1FF for key in ('command', 'vout_min', 'vout_max')):
            raise ValueError('MP2888A VID command/limit has reserved bits set')
        if (state['offset_word'] & 0xFF00
                or not -111 <= _signed(state['offset_word'], 8) <= 112):
            raise ValueError('MP2888A coarse offset encoding is unsupported')
        if not 0 <= state['vout_min'] <= state['vout_max'] <= 511:
            raise ValueError('MP2888A MIN/MAX limits are inconsistent')
        if bool(state['mode_word'] & 0x10) != state['enabled']:
            raise ValueError('MP2888A mode snapshot is inconsistent')

    @staticmethod
    def _context(state):
        context = {key: value for key, value in state.items() if key not in ('enabled', 'command')}
        context['mode_word'] &= ~0x10
        return context

    @staticmethod
    def _desired(before, *, enabled, command=None):
        state = deepcopy(before)
        state['enabled'] = enabled
        state['mode_word'] = (before['mode_word'] | 0x10) if enabled else (before['mode_word'] & ~0x10)
        if command is not None:
            state['command'] = command
        return state

    def _bounds(self, state, *, xoc=False):
        self._shape(state)
        sense, fine = state['sense_config'], state['fine_trim']
        # MPS pp.45/47: DIVIDER scale is 32*VOUT/VOUT_SENSE. Without an
        # understood divider transfer, a nominal VID cannot enforce a ceiling.
        if sense & 0xC000 or ((sense >> 9) & 3) == 3:
            raise ValueError('MP2888A sense/ADC gain encoding is unsupported')
        if sense & 0x1FF != 32:
            raise ValueError('MP2888A external-divider scaling is not understood; manual control refused')
        if sense & 0x800:
            raise ValueError('MP2888A half-gain command scaling is not supported; manual control refused')
        if fine & 0x80 or _signed(fine, 7) == -64:
            raise ValueError('MP2888A fine-trim encoding is unsupported')
        fine_budget = abs(_signed(fine, 7)) * .5
        offset = _signed(state['offset_word'], 8) * 6.25
        hardware_max = min(state['vout_max'] * 6.25, 1600.0)
        ceiling = min(hardware_max, XOC_MAX_MV if xoc else NORMAL_MAX_MV)
        # Coarse trim precedes the VID limiter. Fine trim shifts the sense
        # amplifier, so conservatively retain headroom for either trim sign.
        low = max(0.0, state['vout_min'] * 6.25 - offset + fine_budget)
        high = min(3193.75, ceiling - offset - fine_budget)
        low = math.ceil(low / 6.25) * 6.25
        high = math.floor(high / 6.25) * 6.25
        if low > high:
            raise ValueError('MP2888A live limits and retained trims leave no supported voltage range')
        return low, high

    def capture_control(self):
        with self._mutex:
            self._binding()
            state = {'kind': CONTROL_KIND}
            for key, (reg, width) in CONTROL_REGISTERS.items():
                value = self._read_retry(reg, width)
                if value is None:
                    raise ValueError(f'MP2888A required register 0x{reg:02X} is unavailable')
                state[key] = value
                if key == 'page' and value != 0:
                    raise ValueError('MP2888A PAGE must remain zero')
                if key == 'address' and value & 0x7F != self.addr7:
                    raise ValueError('MP2888A address fingerprint changed')
            state['enabled'] = bool(state['mode_word'] & 0x10)
            self._shape(state)
            if state['address'] & 0x7F != self.addr7:
                raise ValueError('MP2888A address fingerprint changed')
            # Reject a torn command/mode or target-selection snapshot. Required
            # fields use bounded read-only recovery; IOUT/TEMP are not required.
            for key in ('page', 'address', 'mode_word', 'command'):
                reg, width = CONTROL_REGISTERS[key]
                if self._read_retry(reg, width) != state[key]:
                    raise ValueError('MP2888A command/mode or target changed while reading')
            try:
                self.p.env_min, self.p.env_max = self._bounds(state)
                self.p.hardware_max_mv = self._bounds(state, xoc=True)[1]
            except ValueError:
                pass  # Unsupported activation does not erase a readable entry.
            return state

    def validate_control(self, state, xoc=None):
        self._shape(state)
        if state['address'] & 0x7F != self.addr7:
            raise ValueError('MP2888A control belongs to another address')
        if state['enabled']:
            if state['operation'] & 0xF0 != 0x80:
                raise ValueError('MP2888A requires output ON and normal, non-margin OPERATION')
            if state['pwm_config'] & 0x20:
                raise ValueError('MP2888A three-level PWM selection prevents independent PMBus control')
            low, high = self._bounds(state, xoc=self.xoc if xoc is None else xoc)
            if not low <= state['command'] * 6.25 <= high:
                raise ValueError(f'MP2888A nominal target must be {low:g}..{high:g} mV with retained trims')
        return True

    def _activation_status(self):
        status = self._read_retry(0x78, 1)
        if status is None:
            raise ValueError('MP2888A STATUS_BYTE is unavailable; activation refused')
        if status & 0x40:
            raise ValueError('MP2888A reports output OFF; activation refused')
        return status

    @property
    def pending_recovery_states(self):
        return deepcopy(self._pending_recovery_states)

    def recover_control(self, state):
        """Retry our uncertain transaction without adopting a foreign control."""
        with self._mutex:
            try:
                if not self._pending_recovery_states:
                    raise ValueError('MP2888A has no owned pending recovery')
                self._shape(state)
                context = self._context(self._pending_recovery_states[0])
                if self._context(state) != context:
                    raise ValueError('MP2888A recovery target has a different captured context')
                current = self.capture_control()
                if current not in self._pending_recovery_states:
                    raise ValueError('MP2888A current state is outside the owned recovery candidates')
                ok, message = self.restore_control(state, recovery=True, expected=current)
                if ok:
                    self._verification_restore_ok = True
                    self._verification_restore_error = ''
                return ok, message
            except (ValueError, TypeError) as exc:
                return False, str(exc)

    def _apply_control(self, target, before, observed):
        if self._context(target) != self._context(before):
            raise ValueError('MP2888A captured context changed; refusing to overwrite configuration')
        expected = deepcopy(before)

        def write(key):
            nonlocal expected
            if target[key] == expected[key]:
                return
            if self.capture_control() != expected:
                raise ValueError('MP2888A control changed before write; another client may own it')
            after = deepcopy(expected)
            after[key] = target[key]
            if key == 'mode_word':
                after['enabled'] = bool(after[key] & 0x10)
            # A failed driver call may still have reached the device. Remember
            # both possible states, then inspect without retrying the write.
            observed.append(deepcopy(after))
            self._control_dispatched = True
            reg, width = CONTROL_REGISTERS[key]
            accepted = self._raw_write(reg, target[key], width)
            current = self.capture_control()
            if not accepted or current != after:
                raise ValueError(f'MP2888A 0x{reg:02X} exact write/readback failed')
            expected = after

        if target['enabled']:
            write('command')
            write('mode_word')
        else:
            # Select GPU PWM VID before putting an inactive/off command back.
            write('mode_word')
            write('command')
        if self.capture_control() != target:
            raise ValueError('MP2888A final command/mode readback mismatch')

    def restore_control(self, state, *, recovery=False, expected=None):
        with self._mutex:
            self._control_dispatched = False
            before, observed = None, []
            try:
                self._shape(state)
                if not recovery:
                    self.validate_control(state)
                    if state['enabled']:
                        self._activation_status()
                before = self.capture_control()
                if expected is not None and before != expected:
                    raise ValueError('MP2888A control changed; refusing another client\'s state')
                observed.append(deepcopy(before))
                self._apply_control(state, before, observed)
            except Exception as exc:
                if not self._control_dispatched:
                    return False, str(exc)
                try:
                    current = self.capture_control()
                    if current not in observed:
                        raise ValueError('current state is neither the captured entry nor an attempted write; ownership is uncertain')
                    self._apply_control(before, current, [])
                    if self.capture_control() != before:
                        raise ValueError('captured command/mode was not restored')
                except Exception as rollback:
                    self.last_control_restore_ok = False
                    self.last_control_restore_error = str(rollback)
                    self._verification_restore_ok = False
                    self._verification_restore_error = str(rollback)
                    self._pending_recovery_states = deepcopy(observed)
                    return False, f'{exc}; RESTORE FAILED: {rollback}'
                self.last_control_restore_ok = True
                self.last_control_restore_error = ''
                return False, f'{exc}; captured command/mode restored exactly'
            self.last_written_control = deepcopy(state)
            self._pending_recovery_states = []
            self.last_control_restore_ok = True
            self.last_control_restore_error = ''
            if recovery:
                self._verification_restore_ok = True
                self._verification_restore_error = ''
            return True, (f'MP2888A nominal PMBus target {state["command"] * 6.25:g} mV read back exactly'
                          if state['enabled'] else 'MP2888A Auto: GPU PWM VID selected; captured command retained')

    def plan(self, mv):
        try:
            if self._pending_recovery_states:
                raise ValueError('MP2888A owned recovery is pending; restore the captured entry before another request')
            state = self.capture_control()
            low, high = self._bounds(state, xoc=self.xoc)
            value = float(mv)
            target = self.normalize_voltage_mv(mv)
            if not low <= value <= high:
                raise ValueError(f'MP2888A nominal target must be {low:g}..{high:g} mV with retained trims')
            desired = self._desired(state, enabled=True, command=int(target / 6.25))
            self.validate_control(desired)
            self._activation_status()
            return True, f'MP2888A nominal PMBus target {target:g} mV; captured coarse/fine trim retained'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, str(exc)

    def set_voltage_mv(self, mv, *, acknowledged=False, expected=None):
        if not acknowledged:
            return False, 'I2C voltage writes require acknowledgment'
        with self._mutex:
            try:
                if self._pending_recovery_states:
                    raise ValueError('MP2888A owned recovery is pending; restore the captured entry before another request')
                before = self.capture_control()
                if expected is not None and before != expected:
                    raise ValueError('MP2888A control changed; refusing another client\'s state')
                low, high = self._bounds(before, xoc=self.xoc)
                target = self.normalize_voltage_mv(mv)
                if not low <= float(mv) <= high:
                    raise ValueError(f'MP2888A nominal target must be {low:g}..{high:g} mV with retained trims')
                desired = self._desired(before, enabled=True, command=int(target / 6.25))
                return self.restore_control(desired, expected=before)
            except (ValueError, TypeError, OverflowError) as exc:
                return False, str(exc)

    def reset(self, *, expected=None):
        with self._mutex:
            try:
                before = self.capture_control()
                if expected is not None and before != expected:
                    raise ValueError('MP2888A control changed; refusing another client\'s state')
                return self.restore_control(self._desired(before, enabled=False), recovery=True, expected=before)
            except (ValueError, TypeError) as exc:
                return False, str(exc)

    def set_offset_mv(self, mv, *, acknowledged=False):
        return False, 'MP2888A legacy offset control is disabled; use explicit PMBus voltage control'

    def _restore_word(self, *args, **kwargs):
        return False, 'MP2888A legacy offset restoration is disabled; use captured PMBus control'

    def read_vout(self):
        value = self._read_retry(0x8B, 2)
        return float(value) if type(value) is int and 300 <= value <= 2000 else None

    def telemetry(self):
        # Discovery remains useful on a partial read-only layout. Once a full
        # control is present, IOUT/TEMP loss must not remove the voltage control.
        result = {}
        try:
            state = self.capture_control()
            offset = _signed(state['offset_word'], 8) * 6.25
            result.update(control=state, target_mv=state['command'] * 6.25 if state['enabled'] else None,
                          coarse_offset_mv=offset, offset_mv=None,
                          effective_vid_mv=state['command'] * 6.25 + offset if state['enabled'] else None)
        except ValueError:
            if not self.present():
                return {}
            result = dict(self.discovery_telemetry)
        result['vout_mv'] = self.read_vout()
        current, config = self._read_retry(0x8C, 2), self._read_retry(0x44, 2)
        temp = self._read_retry(0x8D, 2)
        result['iout_a'] = ((current & 0xFFF) * (.5 if config & 8 else .25)
                            if current is not None and current & 0xF000 == 0xE000
                            and config is not None else None)
        result['vrm_temp_c'] = temp * .1 if temp is not None and 0 <= temp <= 1500 else None
        return result

    def verify(self, *, acknowledged=False, ref=None, log=None, cancelled=None,
               operating_point=None):
        """Prove two PMBus commands and reversal at a caller-owned clock clamp.

        READ_VOUT is the controller's report, not independent physical voltage.
        The callback must confirm a stable P-state/core/memory clock triple;
        P-state and memory readings may be consistently unavailable (None).
        Unlike load-based offset verification this does not require P0.
        """
        if self._pending_recovery_states:
            return False, 'MP2888A owned recovery is pending; restore the captured entry before Verify', []
        self._verification_write_attempted = False
        self._verification_restore_ok = True
        self._verification_restore_error = ''
        if not acknowledged:
            return False, 'MP2888A verification requires acknowledgment; nothing written', []
        if not callable(operating_point):
            return False, 'MP2888A verification requires a stable low-clock operating-point callback; nothing written', []
        cancelled = cancelled or (lambda: False)
        with self._mutex:
            rows, point = [], None
            entry_xoc = bool(self.xoc)
            original = expected = None
            entry_status = None

            def check():
                nonlocal point
                if cancelled():
                    raise ValueError('verification cancelled')
                if bool(self.xoc) != entry_xoc:
                    raise ValueError('XOC mode changed during verification')
                current = operating_point()
                if (not isinstance(current, (tuple, list)) or len(current) != 3
                        or type(current[1]) not in (int, float) or not math.isfinite(current[1])
                        or current[1] <= 0
                        or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v))
                               for v in (current[0], current[2]))
                        or (current[2] is not None and current[2] <= 0)):
                    raise ValueError('GPU observed core clock or optional operating-point readings are invalid')
                current = tuple(current)
                if point is None:
                    point = current
                elif point != current:
                    raise ValueError('GPU P-state or core/memory clocks changed during verification')

            def sample(control):
                values = []
                for _ in range(15):
                    check()
                    if self.capture_control() != control:
                        raise ValueError('MP2888A control/context changed during sampling')
                    status = self._activation_status()
                    if entry_status is not None and (status & 0x3C) & ~(entry_status & 0x3C):
                        raise ValueError('MP2888A reported a new output/input/current/temperature fault during verification')
                    value = self.read_vout()
                    if value is None:
                        raise ValueError('MP2888A controller-reported VOUT is unavailable')
                    if value > NORMAL_MAX_MV:
                        raise ValueError('controller-reported VOUT exceeded the normal 1200 mV verification ceiling')
                    values.append(value)
                    check()
                    time.sleep(.04)
                return {'samples_mv': values, 'median_mv': median(values),
                        'noise_mv': max(values) - min(values)}

            try:
                check()
                original = self.capture_control()
                expected = deepcopy(original)
                low, high = self._bounds(original)
                first = max(900.0, low)
                if first + 25.0 > high:
                    first = self.normalize_voltage_mv(high - 25.0)
                if first < low or first + 25.0 > high:
                    raise ValueError('live limits leave no room for two separated normal-mode commands')
                self.validate_control(self._desired(original, enabled=True, command=int(first / 6.25)), xoc=False)
                entry_status = self._activation_status()
                entry = sample(original)
                if entry['noise_mv'] > 6.25:
                    raise ValueError('entry controller-reported VOUT is too noisy for a bounded verification')
            except Exception as exc:
                return False, f'MP2888A INCONCLUSIVE - {exc}; nothing written', rows

            failure = None
            try:
                # A -> B -> A in PMBus mode rejects a controller that changes
                # once when enabled but then ignores VOUT_COMMAND entirely.
                for stage, target in (('A', first), ('B', first + 25.0), ('A-return', first)):
                    check()
                    if self.capture_control() != expected:
                        raise ValueError('MP2888A control changed before the next trial')
                    self._control_dispatched = False
                    ok, message = self.set_voltage_mv(target, acknowledged=True, expected=expected)
                    self._verification_write_attempted |= self._control_dispatched
                    if not ok:
                        raise ValueError(message)
                    expected = deepcopy(self.last_written_control)
                    self._verification_restore_ok = False
                    time.sleep(.15)
                    row = {'stage': stage, 'target_mv': target, **sample(expected)}
                    rows.append(row)
                    if log:
                        log(f'MP2888A {stage}: nominal PMBus {target:g} mV; controller-reported '
                            f'VOUT {row["median_mv"]:.1f} mV; noise {row["noise_mv"]:.1f} mV')
                a, b, returned = rows
                noise = max(row['noise_mv'] for row in rows)
                delta = min(b['median_mv'] - a['median_mv'], b['median_mv'] - returned['median_mv'])
                for row in rows:
                    row.update(delta_mv=delta, response_noise_mv=noise)
                if noise > 6.25 or abs(returned['median_mv'] - a['median_mv']) > 7.0:
                    raise ValueError('PMBus A-B-A response is noisy or did not reverse repeatably')
                if delta < 6.25 or delta <= noise:
                    raise ValueError('two PMBus commands produced no clear positive controller-reported response')
            except Exception as exc:
                failure = str(exc)
            finally:
                if self._verification_write_attempted:
                    try:
                        current = self.capture_control()
                        if current != original:
                            if current != expected:
                                if current not in self._pending_recovery_states:
                                    raise ValueError('control/context changed outside the confirmed trial; ownership is uncertain')
                                ok, message = self.recover_control(original)
                            else:
                                ok, message = self.restore_control(original, recovery=True, expected=expected)
                            if not ok:
                                raise ValueError(message)
                        if self.capture_control() != original:
                            raise ValueError('captured entry command/mode is not restored exactly')
                        self._verification_restore_ok = True
                        self._verification_restore_error = ''
                    except Exception as exc:
                        self._verification_restore_ok = False
                        self._verification_restore_error = str(exc)

            if not self._verification_restore_ok:
                return False, f'MP2888A INCONCLUSIVE - {failure or "trial finished"}; RESTORE FAILED: {self._verification_restore_error}', rows
            if failure:
                return False, f'MP2888A INCONCLUSIVE - {failure}; captured entry restored exactly', rows
            # Also prove return to the entry operating voltage, independently of
            # the command reversal. Cancellation here cannot undo restoration.
            try:
                time.sleep(.15)
                restored = sample(original)
                if (restored['noise_mv'] > 6.25
                        or abs(restored['median_mv'] - entry['median_mv']) > max(7.0, entry['noise_mv'])):
                    raise ValueError('controller-reported VOUT did not return to the entry range')
            except Exception as exc:
                failure = str(exc)
            try:
                if self.capture_control() != original:
                    raise ValueError('control/context changed during the final entry reading; current restoration is unconfirmed')
            except Exception as exc:
                self._verification_restore_ok = False
                self._verification_restore_error = str(exc)
                return False, f'MP2888A INCONCLUSIVE - RESTORE FAILED: {exc}', rows
            if failure:
                return False, f'MP2888A INCONCLUSIVE - {failure}; captured entry restored exactly', rows
            return True, ('MP2888A PMBus command response and reversal verified from controller-reported VOUT; '
                          'captured entry restored exactly. Physical voltage requires an independent measurement.'), rows


def discover(nvapi, profile, log=None, *, progress=None, cancelled=None, routes=None):
    """Return every candidate on this GPU, with no writes or arbitrary choice."""
    selected_routes = _normalize_routes(routes)
    if nvapi is None or not getattr(nvapi, 'ok', False):
        return []
    candidates = []
    cancelled = cancelled or (lambda: False)
    # Preferred address on every port first; then the other unicast addresses.
    for addr7 in DISCOVERY_ADDRESSES:
        for port in DISCOVERY_PORTS:
            if selected_routes is not None and (port, addr7) not in selected_routes:
                continue
            if cancelled():
                return candidates
            candidate = MP2888Candidate(profile, nvapi, port, addr7)
            if not candidate.present():
                if progress:
                    progress(f"MP2888A port {port}, 0x{addr7:02X}")
                continue
            # These IDs are user-programmable, so diagnostics cannot gate
            # discovery on the datasheet's example/default values (25h/88h).
            for key, reg in (('vendor_id_user', 0x27), ('product_id_user', 0x28),
                             ('product_rev_user', 0x29)):
                try:
                    candidate.discovery_diagnostics[key] = candidate.read(reg, 1)
                except Exception:
                    candidate.discovery_diagnostics[key] = None
            candidates.append(candidate)
            if log:
                t = candidate.discovery_telemetry
                log(f'{candidate.p.name}: read-only fingerprint passed; '
                    f'{t["vout_mv"]:.0f} mV, {t["iout_a"]:.2f} A, '
                    f'{t["vrm_temp_c"]:.1f} C. Verify required before Apply.', True)
            if progress:
                progress(f"MP2888A port {port}, 0x{addr7:02X}")
    return candidates
