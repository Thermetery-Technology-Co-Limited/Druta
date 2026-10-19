# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""uP9512R volatile, positive offsets for all five load-current states.

Source: uPI uP9512R-DS-F0000, November 2018, pp. 17-23. Only 0A/0B,
0C[7:4], and 2A[6] are controlled. The SMBus lock is NEVER written: an
unlock/relock transition cannot be undone without a VCC power cycle.
FB/IMON telemetry is ADC input voltage, not calibrated board rail/current.
"""
import ctypes
import math
import statistics
import threading
import time
from types import SimpleNamespace

from ..railctl import Rail, I2C_READ_EX, I2C_WRITE_EX, PTR, u8, u32

KIND = 'up9512r-offset-v1'
DISCOVERY_PORTS = tuple(range(8))
DISCOVERY_ADDRESSES = (0x25,) + tuple(a for a in range(0x08, 0x78) if a != 0x25)
IDENTITY = ((0x27, 0x00), (0x28, 0x2B))
CONTROL_REGISTERS = (0x0A, 0x0B, 0x0C, 0x2A)
CONTROL_MASKS = (0xFF, 0xFF, 0xF0, 0x40)
READ_REGISTERS = (*CONTROL_REGISTERS, 0x27, 0x28, 0x2C, 0x2D, 0x39)
HARDWARE_MAX_MV = 150
NORMAL_MAX_MV = 50
NORMAL_CEILING_MV = 1200
TYPO_CEILING_MV = 2000
# Bounded read-only recovery: a transient NACK or busy status is not a value.
READ_ATTEMPTS = 3
READ_RETRY_S = .02
RESTORE_ATTEMPTS = 3
RESTORE_RETRY_S = .05
_ADAPTER_LOCK_GUARD = threading.Lock()


class Unreadable(ValueError):
    """A read failed; unlike a mismatch, retrying it may succeed."""


def _offset(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= HARDWARE_MAX_MV or value % 10):
        raise ValueError('uP9512R offsets must be 0..150 mV in exact 10 mV steps')
    return int(value)


def parse_control(state):
    """Validate saved compound state without touching hardware."""
    if (not isinstance(state, dict) or set(state) != {'kind', 'offsets_mv', 'enabled'}
            or state['kind'] != KIND or type(state['enabled']) is not bool
            or not isinstance(state['offsets_mv'], list) or len(state['offsets_mv']) != 5
            or any(type(v) is not int for v in state['offsets_mv'])):
        raise ValueError('Invalid uP9512R compound offset state')
    return {'kind': KIND, 'offsets_mv': [_offset(v) for v in state['offsets_mv']],
            'enabled': state['enabled']}


def _decode(raw):
    a, b, c, misc = raw
    return {'kind': KIND, 'offsets_mv': [10 * v for v in
            (a >> 4, a & 15, b >> 4, b & 15, c >> 4)], 'enabled': bool(misc & 0x40)}


def _encode(state, before):
    v = [mv // 10 for mv in state['offsets_mv']]
    return (v[0] << 4 | v[1], v[2] << 4 | v[3], v[4] << 4 | (before[2] & 15),
            (before[3] & ~0x40) | (0x40 if state['enabled'] else 0))


class UP9512R(Rail):
    requires_verification = True
    multi_state_offset = True
    # Offsets are positive-only, so zeroing and disabling them can only lower
    # the request. Reset therefore needs no proof that a raise reaches the rail.
    reset_only_lowers = True

    def __init__(self, nvapi, *, port=2, addr7=0x25):
        from ..railctl import _V3
        if ctypes.sizeof(_V3) != 64:
            raise ValueError('uP9512R requires the understood 64-byte NVAPI I2C V3 layout')
        if type(port) is not int or port not in DISCOVERY_PORTS:
            raise ValueError('uP9512R requires I2C port 0..7')
        if type(addr7) is not int or addr7 not in DISCOVERY_ADDRESSES:
            raise ValueError('uP9512R requires a unicast address 0x08..0x77')
        p = SimpleNamespace(
            name=f'uP9512R offsets (port {port}, 0x{addr7:02X}; physical rail unassigned)',
            regulator='uPI uP9512R', rail='Controller output', port=port, addr7=addr7,
            src={'kind': KIND, 'port': port, 'addr7': addr7, 'identity': [0, 0x2B]},
            read_only=False, weak_id=False, lsb_mv=10, env_min=0, env_max=NORMAL_MAX_MV,
            hw_min_mv=0, hw_max_mv=HARDWARE_MAX_MV, raw_min=0, raw_max=15,
            ceiling=NORMAL_CEILING_MV, sanity_rail=TYPO_CEILING_MV, rungs=[10, 20, 30, 40, 50],
            provenance={'datasheet': 'uP9512R-DS-F0000, Nov 2018, pp. 17-23',
                        'guard_policy': 'Software offset/voltage guards; not board-rated limits'})
        super().__init__(p, nvapi, addr7=addr7)
        with _ADAPTER_LOCK_GUARD:
            if not hasattr(nvapi, '_up9512r_mutex'):
                nvapi._up9512r_mutex = threading.RLock()
            self._mutex = nvapi._up9512r_mutex
        self._target = self._target_identity()
        self.last_transaction = None
        self._verification_write_attempted = False
        self._verification_restore_ok = True
        self._verification_restore_error = ''
        self._write_route = None
        self._recovery_states = set()

    def _write_transport(self):
        """Select by architecture and API contract, never by board/driver ID.

        Ada's indexed read supports a two-byte prefix followed by one read
        byte. For this controller that prefix commits one register byte. The
        surrounding transaction must independently read back the complete state.
        Keep the selected route for recovery; never replay a failed write through
        a different transport. A failed architecture query remains retryable.
        """
        if self._write_route is not None:
            return self._write_route
        reader = getattr(self.nvapi, 'GetArchInfo', None)
        if reader is None:
            return 'nvapi-write'
        from ..nvbackend import _GpuArchInfo, NvAPI
        for revision in (2, 1):
            info = _GpuArchInfo(version=NvAPI.ver(_GpuArchInfo, revision))
            status = reader(self.nvapi.gpu, ctypes.byref(info))
            if status == -9:
                continue
            if (status != 0 or info.version != NvAPI.ver(_GpuArchInfo, revision)
                    or not info.architecture or not self._bound()):
                raise ValueError('uP9512R write transport architecture query failed; retry after refresh')
            self._write_route = 'nvapi-combined' if info.architecture == 0x190 else 'nvapi-write'
            return self._write_route
        raise ValueError('uP9512R write transport architecture layout is unavailable')

    def _target_identity(self):
        handle = getattr(self.nvapi, 'gpu', None)
        selected = getattr(self.nvapi, 'selected', None) or {}
        return (getattr(handle, 'value', handle), selected.get('slot'),
                selected.get('devid'), selected.get('subsys'), self.p.port, self.addr7)

    def _bound(self):
        return bool(getattr(self.nvapi, 'ok', False)) and self._target_identity() == self._target

    def read(self, cmd, n=1):
        if type(cmd) is not int or cmd not in READ_REGISTERS or type(n) is not int or n != 1:
            raise ValueError('Read outside the understood uP9512R byte-register contract')
        if not self._bound():
            return None
        fn = self.nvapi._i(I2C_READ_EX, PTR, PTR, PTR)
        if fn is None:
            return None
        # A successful status with an untouched output buffer is not data.
        # If the real byte equals A5, a second sentinel disambiguates it.
        for sentinel in (0xA5, 0x5A):
            buf, extra = (u8 * 1)(sentinel), (u32 * 2)()
            packet, pointer = self._mk(cmd, buf, 1)
            geometry = bytes(packet)
            status = fn(self.nvapi.gpu, ctypes.byref(packet), ctypes.byref(extra))
            if (status != 0 or bytes(packet) != geometry or list(pointer) != [cmd]
                    or list(extra) != [0, 0] or not self._bound()):
                return None
            if buf[0] != sentinel:
                return int(buf[0])
        return None

    def _raw_write(self, cmd, value, nbytes=1):
        if (type(cmd) is not int or cmd not in CONTROL_REGISTERS or type(nbytes) is not int or nbytes != 1
                or type(value) is not int or not 0 <= value <= 255):
            raise ValueError('Only uP9512R offset/enable byte registers are writable')
        if not self._bound():
            return False
        combined = self._write_transport() == 'nvapi-combined'
        fn = self.nvapi._i(I2C_READ_EX if combined else I2C_WRITE_EX, PTR, PTR, PTR)
        if fn is None:
            return False
        buf, extra = (u8 * 1)(value ^ 0xFF if combined else value), (u32 * 2)()
        packet, pointer = self._mk(cmd, buf, 1)
        if combined:
            pointer = (u8 * 2)(cmd, value)
            packet.pbI2cRegAddress = ctypes.cast(pointer, ctypes.POINTER(u8))
            packet.regAddrSize = 2
        prefix = list(pointer)
        geometry = bytes(packet)
        # One dispatch only. Even a failed read-direction call may have stored
        # the prefix byte; _transaction owns independent readback and recovery.
        status = fn(self.nvapi.gpu, ctypes.byref(packet), ctypes.byref(extra))
        return (status == 0 and bytes(packet) == geometry and list(pointer) == prefix
                and list(extra) == [0, 0] and int(buf[0]) == value and self._bound())

    def present(self):
        # Single-shot: discovery probes hundreds of empty routes with this.
        # After identification, use still_present(), which retries failed reads.
        try:
            return self._bound() and all(self.read(reg) == value
                                        for _ in range(2) for reg, value in IDENTITY)
        except Exception:
            return False

    def still_present(self):
        """Post-identification check: failed reads get bounded retry, a mismatch none."""
        try:
            self._check_identity()
            return True
        except Exception:
            return False

    def _read_retry(self, reg):
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                time.sleep(READ_RETRY_S)
            value = self.read(reg)
            if value is not None or not self._bound():
                return value
        return None

    def _check_identity(self):
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                time.sleep(READ_RETRY_S)
            try:
                return self._identity_once()
            except Unreadable as exc:
                error = exc
        raise Unreadable(f'{error} after {READ_ATTEMPTS} attempts')

    def _identity_once(self):
        if not self._bound():
            raise ValueError('uP9512R selected GPU no longer matches')
        for _ in range(2):
            for reg, value in IDENTITY:
                got = self.read(reg)
                if got is None:
                    if not self._bound():
                        raise ValueError('uP9512R selected GPU no longer matches')
                    raise Unreadable(f'uP9512R identity register 0x{reg:02X} unreadable')
                if got != value:
                    raise ValueError('uP9512R identity no longer matches')

    def _capture_once(self):
        self._identity_once()
        raw = tuple(self.read(reg) for reg in CONTROL_REGISTERS)
        if any(v is None for v in raw):
            raise Unreadable('uP9512R control read failed')
        if any(type(v) is not int or not 0 <= v <= 255 for v in raw):
            raise ValueError('uP9512R control read outside one byte')
        again = tuple(self.read(reg) for reg in CONTROL_REGISTERS)
        if any(v is None for v in again):
            raise Unreadable('uP9512R control re-read failed')
        if again != raw:
            raise ValueError('uP9512R control changed during capture')
        self._identity_once()
        return raw

    def _capture_registers(self):
        """Complete control bytes; retry only reads that failed, never a mismatch."""
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                time.sleep(READ_RETRY_S)
            try:
                return self._capture_once()
            except Unreadable as exc:
                error = exc
        raise Unreadable(f'{error} after {READ_ATTEMPTS} attempts')

    def capture_control(self):
        with self._mutex:
            return _decode(self._capture_registers())

    def _writable(self, raw):
        lock = self._read_retry(0x39)
        if lock is None:
            raise Unreadable('uP9512R SMBus lock register 0x39 is unreadable; no write issued')
        if lock != 0x94:
            raise ValueError('uP9512R SMBus is locked; Druta never unlocks register 0x39')
        if raw[3] & 0x83:
            raise ValueError('uP9512R internal-test bits are nonzero; unknown control layout')

    def write_locked(self):
        """True/False from register 0x39; None while identity or lock stays
        unreadable. Raises when the identity no longer matches."""
        with self._mutex:
            try:
                self._check_identity()
            except Unreadable:
                return None
            lock = self._read_retry(0x39)
            return None if lock is None else lock != 0x94

    def matches_control(self, state):
        """Read-only: the live, understood control already equals ``state``.

        A locked controller can still match; nothing needs writing then.
        """
        with self._mutex:
            try:
                state = parse_control(state)
                raw = self._capture_registers()
                return not raw[3] & 0x83 and _decode(raw) == state
            except Exception:
                return False

    def telemetry(self):
        try:
            with self._mutex:
                control = self.capture_control()
                fb, imon, lock = (self._read_retry(reg) for reg in (0x2D, 0x2C, 0x39))
                self._check_identity()
                offsets = control['offsets_mv']
                return {'control': control, 'offsets_mv': offsets,
                        'offset_mv': offsets[0] if len(set(offsets)) == 1 else None,
                        'offset_enabled': control['enabled'],
                        'write_locked': None if lock is None else lock != 0x94,
                        'vout_mv': fb * 10 if fb is not None else None,
                        'imon_mv': imon * 10 if imon is not None else None}
        except Exception:
            return {}

    def read_vout(self):
        try:
            self._check_identity()
            value = self._read_retry(0x2D)
            if value is None:
                return None
            self._check_identity()
        except Exception:
            return None
        return value * 10

    def validate_offset_mv(self, mv, *, xoc=False):
        try:
            value = _offset(mv)
            if not xoc and value > NORMAL_MAX_MV:
                raise ValueError('uP9512R normal software offset guard is 0..50 mV')
            return True, 'Within uP9512R hardware and software offset bounds'
        except ValueError as exc:
            return False, str(exc)

    def _guard(self, state, raw, *, xoc):
        self._writable(raw)
        self.validate_control(state, xoc=xoc)
        voltage = self.read_vout()
        ceiling = TYPO_CEILING_MV if xoc else NORMAL_CEILING_MV
        if voltage is None or not 300 <= voltage <= ceiling:
            raise ValueError(f'uP9512R FB ADC unavailable or outside the 300..{ceiling} mV software guard')
        current = _decode(raw)
        old = current['offsets_mv'] if current['enabled'] else [0] * 5
        new = state['offsets_mv'] if state['enabled'] else [0] * 5
        # The active LCS is not observed: use the largest possible increase.
        # This is a request guard, not a claim of calibrated 1:1 board gain.
        increase = max(0, *(b - a for a, b in zip(old, new)))
        if voltage + increase > ceiling:
            raise ValueError(f'uP9512R requested increase exceeds the {ceiling} mV software ceiling')

    def validate_control(self, state, xoc=None):
        state = parse_control(state)
        if (state['enabled'] and not (self.xoc if xoc is None else xoc)
                and max(state['offsets_mv']) > NORMAL_MAX_MV):
            raise ValueError('uP9512R normal software offset guard is 0..50 mV')
        return True

    def plan_control(self, state, *, xoc=None):
        """Read-only replay preflight; must run before other profile writes."""
        with self._mutex:
            try:
                state = parse_control(state)
                raw = self._capture_registers()
                self._guard(state, raw, xoc=self.xoc if xoc is None else xoc)
                return True, 'uP9512R compound state is ready for guarded replay'
            except Exception as exc:
                return False, str(exc)

    def _dispatch(self, target, working, attempted):
        # Disable before restoring staged fields; enable only after all five
        # fields are installed. When already enabled, never inject a disable.
        order = (3, 0, 1, 2) if not target[3] & 0x40 else (0, 1, 2, 3)
        working = list(working)
        for index in order:
            if target[index] == working[index]:
                continue
            if self._capture_registers() != tuple(working):
                raise ValueError('uP9512R state changed before dispatch; another writer may be active')
            self._writable(working)
            reg = CONTROL_REGISTERS[index]
            attempted.append(reg)  # A failure/exception may still have changed hardware.
            self._verification_write_attempted = True
            next_state = list(working)
            next_state[index] = target[index]
            self._recovery_states = {tuple(working), tuple(next_state)}
            if not self._raw_write(reg, target[index], 1):
                raise ValueError(f'uP9512R 0x{reg:02X} write failed or outcome uncertain')
            working[index] = target[index]
            if self._capture_registers() != tuple(working):
                raise ValueError(f'uP9512R 0x{reg:02X} readback/preserved bits mismatch')
            self._recovery_states = {tuple(working)}
        if self._capture_registers() != target:
            raise ValueError('uP9512R complete control readback mismatch')

    def _transaction(self, state, *, recovery=False, expected=None, exact=None,
                     expected_raw_states=None):
        attempted, before = [], None
        self.last_transaction = {'requested': state, 'attempted_registers': attempted}
        self._verification_restore_ok = True
        self._verification_restore_error = ''
        try:
            state = parse_control(state)
            before = self._capture_registers()
            self.last_transaction['before'] = list(before)
            if expected_raw_states is not None and before not in expected_raw_states:
                raise ValueError('uP9512R control changed outside this transaction; recovery not dispatched')
            if expected is not None and _decode(before) != parse_control(expected):
                raise ValueError('uP9512R settings changed since capture; read again')
            self._writable(before)
            if not recovery:
                self._guard(state, before, xoc=self.xoc)
            target = _encode(state, before)
            if exact is not None and target != exact:
                raise ValueError('uP9512R preserved bits changed; exact restoration is unsafe')
            self._dispatch(target, before, attempted)
            self._writable(target)
            if not recovery:
                voltage = self.read_vout()
                ceiling = TYPO_CEILING_MV if self.xoc else NORMAL_CEILING_MV
                if voltage is None or not 300 <= voltage <= ceiling:
                    raise ValueError('uP9512R post-write FB ADC unavailable or exceeded software ceiling')
                if self._capture_registers() != target:
                    raise ValueError('uP9512R control changed during post-write telemetry')
            self.last_transaction['after'] = list(target)
            return True, 'uP9512R all five offset fields and enable read back exactly; other bits preserved'
        except Exception as exc:
            message = str(exc)
            self.last_transaction['cause'] = message
            if recovery and attempted and before is not None:
                # Recovery may already have disabled offsets. Rolling back to
                # its entry state would re-enable the trial after a later field
                # fails. Keep partial recovery, report it, and never raise the
                # rail again merely to undo a failed restoration.
                try:
                    residual = self._capture_registers()
                    self.last_transaction['residual'] = list(residual)
                    restored = residual == target
                except Exception as read_exc:
                    restored = False
                    message += '; independent recovery readback: ' + str(read_exc)
                self._verification_restore_ok = restored
                self._verification_restore_error = '' if restored else message
                message += ('; requested recovery state independently confirmed'
                            if restored else '; RESTORE FAILED; partial recovery retained')
            elif attempted and before is not None:
                errors = []
                try:
                    current = self._capture_registers()
                    if any((a & ~mask) != (b & ~mask)
                           for a, b, mask in zip(current, before, CONTROL_MASKS)):
                        raise ValueError('preserved bits changed; rollback not dispatched')
                    if current not in self._recovery_states:
                        raise ValueError('control changed outside this transaction; rollback not dispatched')
                    self._writable(current)
                    self._dispatch(before, current, [])
                except Exception as restore_exc:
                    errors.append(str(restore_exc))
                try:
                    if self._capture_registers() != before:
                        errors.append('complete entry control not restored')
                except Exception as restore_exc:
                    errors.append('independent restore readback: ' + str(restore_exc))
                self._verification_restore_ok = not errors
                self._verification_restore_error = '; '.join(errors)
                message += ('; RESTORE FAILED; state uncertain: ' + '; '.join(errors)
                            if errors else '; exact entry control restored')
            self.last_transaction['error'] = message
            return False, message

    def restore_control(self, state, *, recovery=False):
        with self._mutex:
            return self._transaction(state, recovery=recovery)

    def plan(self, mv):
        return self.set_offset_mv(mv, acknowledged=True, dry_run=True)

    def set_offset_mv(self, mv, *, acknowledged=False, dry_run=False, expected=None, _xoc=None):
        if not acknowledged:
            return False, 'I2C voltage writes require acknowledgment'
        with self._mutex:
            try:
                value = _offset(mv)
                state = {'kind': KIND, 'offsets_mv': [value] * 5, 'enabled': True}
                raw = self._capture_registers()
                if expected is not None and _decode(raw) != parse_control(expected):
                    raise ValueError('uP9512R settings changed since capture; read again')
                self._guard(state, raw, xoc=self.xoc if _xoc is None else _xoc)
                if dry_run:
                    return True, f'Would set all five uP9512R offsets to +{value} mV and enable them'
                return self._transaction(state, expected=_decode(raw))
            except Exception as exc:
                return False, str(exc)

    def reset(self, *, expected=None):
        with self._mutex:
            # Release/zero is recovery, not a new request to raise voltage.
            return self._transaction({'kind': KIND, 'offsets_mv': [0] * 5, 'enabled': False},
                                     recovery=True, expected=expected)

    def _restore_entry(self, original, original_raw):
        """Bounded recovery to the exact entry bytes; returns the errors left.

        Each attempt re-reads the controller and dispatches only the fields
        that still differ, so a retry never repeats a write that already
        landed. Accepted start states stay limited to this verification's own.
        """
        if not self._verification_write_attempted:
            try:
                if self._capture_registers() == original_raw:
                    return []
                return ['complete original byte state independent readback mismatch']
            except Exception as exc:
                return [str(exc)]
        accepted = set(self._recovery_states) | {original_raw}
        errors = []
        for attempt in range(RESTORE_ATTEMPTS):
            if attempt:
                time.sleep(RESTORE_RETRY_S)
            errors = []
            try:
                ok, message = self._transaction(original, recovery=True, exact=original_raw,
                                                expected_raw_states=accepted)
                if not ok:
                    errors.append(message)
            except Exception as exc:
                errors.append(str(exc))
            # The independent readback, not a transport status, is the verdict:
            # a write can land although its call reported failure.
            try:
                if self._capture_registers() == original_raw:
                    return []
                errors.append('complete original byte state independent readback mismatch')
            except Exception as exc:
                errors.append(str(exc))
            accepted |= set(self._recovery_states)
        return [f'after {RESTORE_ATTEMPTS} attempts: ' + '; '.join(errors)]

    def verify(self, *, acknowledged=False, ref=None, log=None, cancelled=None,
               operating_point=None, allow_idle=False):
        """Bounded response/reversal test; always restore complete entry state.

        Driver VID/reference is deliberately unused. A result establishes an
        FB ADC response at one held operating point, not a calibrated gain.
        """
        self._verification_write_attempted = False
        self._verification_restore_ok = True
        self._verification_restore_error = ''
        cancelled = cancelled or (lambda: False)
        if not acknowledged or not callable(operating_point):
            return False, 'uP9512R verification requires acknowledgment and held P0 callback; nothing written', []
        with self._mutex:
            ladder, point = [], None
            core_min = core_max = None
            memory_min = memory_max = None
            entry_xoc = bool(self.xoc)
            ceiling = TYPO_CEILING_MV if entry_xoc else NORMAL_CEILING_MV

            def check():
                nonlocal point, core_min, core_max, memory_min, memory_max
                if cancelled():
                    raise ValueError('verification cancelled')
                if bool(self.xoc) != entry_xoc:
                    raise ValueError('XOC mode changed during verification')
                current = operating_point()
                if (not isinstance(current, (tuple, list)) or len(current) != 3
                        or type(current[0]) not in (int, float) or current[0] != 0):
                    raise ValueError('GPU is not at a readable held P0 operating point')
                current = tuple(current)
                # Frequency is diagnostic only. Thermal Boost can change it
                # without changing the voltage hold, which the caller checks
                # independently. Missing clock telemetry also cannot gate the
                # measured FB-voltage response and reversal.
                if type(current[1]) in (int, float) and math.isfinite(current[1]) and current[1] > 0:
                    core_min = current[1] if core_min is None else min(core_min, current[1])
                    core_max = current[1] if core_max is None else max(core_max, current[1])
                if type(current[2]) in (int, float) and math.isfinite(current[2]) and current[2] > 0:
                    memory_min = current[2] if memory_min is None else min(memory_min, current[2])
                    memory_max = current[2] if memory_max is None else max(memory_max, current[2])
                point = current

            def sample(expected):
                values = []
                for _ in range(25):
                    check()
                    if self._capture_registers() != expected:
                        raise ValueError('uP9512R control changed during sampling')
                    value = self.read_vout()
                    if value is None or not 300 <= value <= ceiling:
                        raise ValueError('uP9512R FB ADC unavailable or exceeded verification ceiling')
                    values.append(value)
                    check()
                    time.sleep(.04)
                check()
                return {'samples_mv': values, 'median_mv': statistics.median(values),
                        'noise_mv': max(values) - min(values), 'quantum_mv': 10,
                        'observed_core_clock_range_mhz': [core_min, core_max],
                        'observed_memory_clock_range_mhz': [memory_min, memory_max]}

            try:
                check()
                original_raw = self._capture_registers()
                self._writable(original_raw)
                original = _decode(original_raw)
                baseline = sample(original_raw)
                base = baseline['median_mv']
                effective = original['offsets_mv'] if original['enabled'] else [0] * 5
                maximum = HARDWARE_MAX_MV if entry_xoc else NORMAL_MAX_MV
                steps = [step for step in self.p.rungs if max(effective) + step <= maximum
                         and base + step <= ceiling]
                if not steps:
                    raise ValueError('no headroom for the bounded verification staircase; '
                                     'Reset releases the offsets without a prior Verify')
            except Exception as exc:
                return False, f'uP9512R INCONCLUSIVE - {exc}; nothing written', ladder

            hit, failure = None, None
            try:
                expected = original
                for step in steps:
                    check()
                    trial_state = {'kind': KIND, 'offsets_mv': [v + step for v in effective], 'enabled': True}
                    rung = {'offset_increment_mv': step, 'baseline_mv': base,
                            'baseline_samples_mv': baseline['samples_mv'],
                            'baseline_core_clock_range_mhz': baseline['observed_core_clock_range_mhz'],
                            'clock_observations_gate_verification': False,
                            'trial_control': trial_state, 'voltage_ceiling_mv': ceiling, 'moved': False}
                    ladder.append(rung)
                    ok, message = self._transaction(trial_state, expected=expected)
                    if not ok:
                        # The entry restoration below decides the final state;
                        # report only why the trial stopped.
                        raise ValueError('verification write refused: '
                                         + self.last_transaction.get('cause', message))
                    expected = trial_state
                    time.sleep(.15)
                    trial = sample(_encode(trial_state, original_raw))
                    delta = trial['median_mv'] - base
                    noise = max(baseline['noise_mv'], trial['noise_mv'])
                    moved = delta >= 10 and delta > noise
                    rung.update(trial, delta_mv=delta, threshold_mv=max(10, noise),
                                response_noise_mv=noise, moved=moved)
                    if log:
                        log(f'uP9512R FB ADC {base:g} -> {trial["median_mv"]:g} mV; '
                            f'offset increment {step:g} mV; noise {noise:g} mV')
                    if moved:
                        hit = rung
                        break
            except Exception as exc:
                failure = str(exc)
            finally:
                errors = self._restore_entry(original, original_raw)
                self._verification_restore_ok = not errors
                self._verification_restore_error = '; '.join(errors)
            if errors:
                return False, 'uP9512R RESTORE FAILED: ' + '; '.join(errors), ladder
            if failure or hit is None:
                return False, ('uP9512R INCONCLUSIVE - ' + (failure or 'no positive FB ADC response above noise/resolution')
                               + '; complete original control restored'), ladder
            try:
                time.sleep(.15)
                restored = sample(original_raw)
                tolerance = max(10, baseline['noise_mv'], restored['noise_mv'])
                reversal = hit['median_mv'] - restored['median_mv']
                hit.update(restored_vout_mv=restored['median_mv'], restored_samples_mv=restored['samples_mv'],
                           restored_noise_mv=restored['noise_mv'], reversal_mv=reversal,
                           observed_core_clock_range_mhz=restored['observed_core_clock_range_mhz'],
                           observed_memory_clock_range_mhz=restored['observed_memory_clock_range_mhz'])
                if log:
                    log(f'uP9512R FB ADC restored {hit["median_mv"]:g} -> {restored["median_mv"]:g} mV; '
                        f'reversal {reversal:g} mV; baseline {base:g} mV; band {tolerance:g} mV')
                if abs(restored['median_mv'] - base) > tolerance:
                    raise ValueError('FB ADC did not return to the baseline noise/resolution band')
                # Peak-to-peak already gated the rise. A one-count return inside
                # that band is the reversal. Requiring the drop to beat the same
                # spread made a quantized return inconclusive (issue 34).
                if reversal < 10:
                    raise ValueError('FB ADC did not show a downward response of at least one ADC count after restoration')
            except Exception as exc:
                failure = str(exc)
            finally:
                try:
                    if self._capture_registers() != original_raw:
                        raise ValueError('complete original control changed during reversal observation')
                except Exception as exc:
                    self._verification_restore_ok = False
                    self._verification_restore_error = str(exc)
            if not self._verification_restore_ok:
                return False, 'uP9512R RESTORE FAILED: ' + self._verification_restore_error, ladder
            if failure:
                return False, f'uP9512R INCONCLUSIVE - {failure}; original control restored', ladder
            return True, ('uP9512R WRITE PATH CONFIRMED at the tested operating point: FB ADC rose and reversed '
                          'after exact five-state/enable restoration. '
                          'Frequency observations do not gate this voltage test. '
                          'This does not establish calibrated gain or load safety.'), ladder
