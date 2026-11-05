# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Counted DIFR disable requests; predicates do not measure residency."""

import struct

from . import clock_bios_runtime as clock
from . import lpwr_runtime as lpwr
from .perf_diagnostics import PerfDiagnosticsReader


class DifrControlError(RuntimeError):
    """The request or its readback could not be established."""


class DifrController:
    """Own at most one reason-zero request for this GPU instance."""

    def __init__(self, gpu):
        self.gpu = gpu
        self.reader = PerfDiagnosticsReader(gpu)
        self.identity = clock._binding(self.reader)
        self._owned = False
        self._uncertain = False
        self._owned_bank = None

    def _control(self, value):
        result = lpwr._get(self.reader, self.identity, 0x20802801, 4, struct.pack('<I', value))
        observed = struct.unpack('<I', result)[0]
        if value in (1, 2) and observed != value:
            raise DifrControlError('DIFR control response changed the request')
        if value == 3 and observed not in (1, 2):
            raise DifrControlError('DIFR support response is malformed')
        return observed

    def _once(self):
        support = self._control(3) == 1
        values = {}
        for attr in (0x37, 0x39, 0x48, 0x49):
            raw = lpwr._get(self.reader, self.identity, lpwr.ATTRIBUTE_COMMAND,
                            lpwr.ATTRIBUTE_SIZE, lpwr._request(attr, (9,)))
            available, value = lpwr._attributes(raw, attr, (9,))['9']
            if not available or (attr in (0x37, 0x39) and value not in (0, 1)):
                raise DifrControlError('DIFR controller attribute is unavailable or malformed')
            values[attr] = value
        return {'support': support, 'enabled_predicate': bool(values[0x37]),
                'present': bool(values[0x39]),
                'reason_masks': {0x48: values[0x48], 0x49: values[0x49]}}

    def _read_locked(self):
        first, second = self._once(), self._once()
        if first != second:
            raise DifrControlError('DIFR readbacks were not stable')
        return first

    def _snapshot(self, state):
        return {**state, 'owned_request': self._owned, 'uncertain_outcome': self._uncertain}

    def read(self):
        with self.gpu._lock:
            return self._snapshot(self._read_locked())

    def hold(self):
        with self.gpu._lock:
            if self._uncertain:
                raise DifrControlError('A dispatched DIFR request has an uncertain outcome')
            before = self._read_locked()
            if self._owned:
                self._check_owned(before)
                return self._snapshot(before)
            if not before['support'] or not before['present']:
                raise DifrControlError('DIFR request control is unavailable')
            if any(value & 1 for value in before['reason_masks'].values()):
                raise DifrControlError('A reason-zero request already exists')
            self._uncertain = True
            self._control(1)
            after = self._read_locked()
            changes = [key for key in (0x48, 0x49)
                       if after['reason_masks'][key] != before['reason_masks'][key]]
            if (not after['support'] or not after['present'] or len(changes) != 1
                    or after['reason_masks'][changes[0]] != before['reason_masks'][changes[0]] | 1):
                raise DifrControlError('DIFR request did not produce the expected mask readback')
            self._owned_bank = changes[0]
            self._owned = True
            self._uncertain = False
            return self._snapshot(after)

    def _check_owned(self, state):
        masks = state['reason_masks']
        other = 0x49 if self._owned_bank == 0x48 else 0x48
        if (not state['support'] or not state['present']
                or not masks[self._owned_bank] & 1 or masks[other] & 1):
            raise DifrControlError('DIFR reason-zero masks no longer match the owned request')

    def release(self):
        with self.gpu._lock:
            if self._uncertain:
                raise DifrControlError('A dispatched DIFR request has an uncertain outcome')
            if not self._owned:
                raise DifrControlError('This controller does not own a DIFR request')
            before = self._read_locked()
            if not before['support'] or not before['present']:
                raise DifrControlError('DIFR request control is unavailable')
            self._check_owned(before)
            expected = before['reason_masks'].copy()
            expected[self._owned_bank] &= ~1
            self._uncertain = True
            self._control(2)
            after = self._read_locked()
            if not after['support'] or not after['present'] or after['reason_masks'] != expected:
                raise DifrControlError('DIFR release did not match the expected current reason masks')
            self._owned = False
            self._owned_bank = None
            self._uncertain = False
            return self._snapshot(after)
