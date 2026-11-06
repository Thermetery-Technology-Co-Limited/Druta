# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware-free contracts for scoped rail capture, replay and composition."""
import copy
import threading
from types import SimpleNamespace

import pytest

from druta import combined_profiles, profiles


class GPU:
    VOLT_LIMIT_MIN_MV = 300
    VOLT_LIMIT_MAX_MV = 1200
    VOLT_LIMIT_XOC_MAX_MV = 1500

    def __init__(self):
        self._lock = threading.RLock()
        self.static = {'name': 'GPU', 'driver': 'one', 'vbios': 'one', 'uuid': 'uuid'}
        self.supported = {0: ('reliability', 'alt_reliability'), 1: ('vmin',)}
        self.rows = {0: {'reliability': 10.125, 'alt_reliability': 20.0, '_base_mv': {'reliability': 1000, 'alt_reliability': 1000}},
                     1: {'vmin': -2.0, '_base_mv': {'vmin': 1000}}}
        self.calls = []
        self.voltage_xoc_enabled = False
        self.volt_limits_write_enabled = True
        self.project = None
        self.fail_rail = None

    def slot(self):
        return '0000:01:00.0'

    def volt_rail_limit_fields(self, rail):
        return self.supported.get(rail, ())

    def read_volt_rail_limits(self):
        return copy.deepcopy(self.rows)

    def user_rail_limits(self, rows):
        if self.project is not None:
            rows[0]['reliability'] = self.project
        return rows

    def abs_limit_mv(self, row, field):
        return row['_base_mv'][field] + row[field]

    def stock_limit_mv(self, rail, field):
        return None

    def set_volt_rail_limits_raw(self, rail, **fields):
        self.calls.append((rail, fields))
        if not self.volt_limits_write_enabled or rail == self.fail_rail:
            return False, 'setter refused'
        for field, value in fields.items():
            self.rows[rail][field] = value / 1000
        return True, 'verified'


def test_capture_selected_projection_without_unrelated_reads_and_gate_off():
    gpu = GPU()
    gpu.volt_limits_write_enabled = False
    gpu.project = 3.125
    state = profiles.capture_rail_limits(gpu, {0: ['reliability']})
    assert state['rail_limits'] == {'0': {'reliability': 3125}}
    assert profiles.incomplete(state) == []
    assert '1 rail limit' in profiles.summarize(state)
    assert not gpu.calls


def test_disabled_gate_blocks_entire_combined_load_before_any_write():
    gpu = GPU()
    saved = profiles.capture_rail_limits(gpu, {0: ['reliability']})
    bundle = combined_profiles.compose([('rail', saved)])
    gpu.volt_limits_write_enabled = False
    assert 'Enable Rail limits' in profiles.preflight(gpu, saved)
    result = combined_profiles.restore(SimpleNamespace(gpu=gpu, rail=None), bundle)
    assert not result[0][0]
    assert not gpu.calls


def test_all_supported_and_missing_or_invalid_selection():
    gpu = GPU()
    assert profiles.capture_rail_limits(gpu)['rail_limits'] == {
        '0': {'reliability': 10125, 'alt_reliability': 20000}, '1': {'vmin': -2000}}
    del gpu.rows[1]
    with pytest.raises(ValueError, match='rail 1 readback'):
        profiles.capture_rail_limits(gpu)
    with pytest.raises(ValueError, match='unavailable'):
        profiles.capture_rail_limits(gpu, {0: ['vmin']})
    assert profiles.capture_rail_limits(gpu, {0: ['reliability']})['rail_limits']['0']['reliability'] == 10125


def test_selected_capture_does_not_probe_other_rail_capability():
    gpu = GPU()
    original = gpu.volt_rail_limit_fields

    def fields(rail):
        if rail == 1:
            raise RuntimeError('unrelated rail unavailable')
        return original(rail)

    gpu.volt_rail_limit_fields = fields
    state = profiles.capture_rail_limits(gpu, {0: ['reliability']})
    assert profiles.preflight(gpu, state) is None


def test_restore_selected_fields_and_manual_identity():
    gpu = GPU()
    saved = profiles.capture_rail_limits(gpu, {0: ['reliability']})
    gpu.rows[0]['reliability'] = 40.0
    gpu.rows[0]['alt_reliability'] = 55.0
    gpu.static.update(driver='different', vbios='different')
    assert profiles.preflight(gpu, saved) is None
    assert profiles.restore(gpu, saved)[0][0]
    assert gpu.rows[0]['reliability'] == 10.125
    assert gpu.rows[0]['alt_reliability'] == 55.0
    gpu.static['uuid'] = 'other'
    assert 'different GPU UUID' in profiles.preflight(gpu, saved)


def test_restore_stops_on_failure_and_checks_readback():
    gpu = GPU()
    saved = profiles.capture_rail_limits(gpu)
    gpu.fail_rail = 0
    assert not profiles.restore(gpu, saved)[0][0]
    assert len(gpu.calls) == 1
    gpu.fail_rail = None
    gpu.calls.clear()
    gpu.set_volt_rail_limits_raw = lambda rail, **fields: (True, 'incorrect success')
    gpu.rows[0]['reliability'] = 5
    result = profiles.restore(gpu, saved)
    assert len(result) == 1 and not result[0][0] and 'mismatch' in result[0][1]


def test_preflight_rejects_out_of_range_and_bad_profile_before_write():
    gpu = GPU()
    saved = profiles.capture_rail_limits(gpu, {0: ['reliability']})
    saved['rail_limits']['0']['reliability'] = 900000
    assert 'bounds' in profiles.preflight(gpu, saved)
    assert not profiles.restore(gpu, saved)[0][0]
    saved['rail_limits']['0']['reliability'] = True
    assert profiles.incomplete(saved)
    assert not gpu.calls


def test_combined_overlap_and_selected_undo():
    gpu = GPU()
    saved = profiles.capture_rail_limits(gpu, {0: ['reliability']})
    full = {'schema': 2, 'vf_applicable': False, 'rail_limits_uv': {'0': {'reliability': 1}}}
    with pytest.raises(ValueError, match='overlapping'):
        combined_profiles.compose([('rail', saved), ('full', full)])
    bundle = combined_profiles.compose([('rail', saved)])
    gpu.rows[0]['reliability'] = 32
    app = SimpleNamespace(gpu=gpu, rail=None)
    undo = combined_profiles.capture_undo(app, bundle)
    assert undo['components'][0]['state']['rail_limits'] == {'0': {'reliability': 32000}}
