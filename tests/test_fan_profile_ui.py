# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fan-only profiles preserve unrelated sessions and refresh only fan targets."""
from unittest.mock import Mock, patch

import pytest

from druta.druta import Druta


@pytest.fixture
def app():
    a = Druta.__new__(Druta)
    a.gpu, a.rail, a._i2c_busy = Mock(), Mock(), False
    a._clk_lock = {'kind': Druta.LOCK_P0, 'verified': True}
    a.guard = Mock(return_value=True)
    a.log, a.profile_failure, a.refresh_profile_list = Mock(), Mock(), Mock()
    for name in ('_finish_manual_voltage', 'rail_for_profile', 'i2c_verified',
                 'sync_risk_ui', 'sync_sliders_from_gpu', 'refresh_full_profile',
                 '_current_dac_failure', '_multi_state_offset_failure'):
        setattr(a, name, Mock(side_effect=AssertionError('unrelated ' + name)))
    return a


def test_fan_load_captures_only_fan_before_restore(app):
    state = {'schema': 2, 'scope': 'fan'}
    events = []
    app.refresh_fan_profile = Mock(side_effect=lambda: events.append('refresh'))
    with (patch('druta.druta.profiles.load', return_value=state),
          patch('druta.druta.profiles.preflight', return_value=None) as preflight,
          patch('druta.druta.profiles.autosave', side_effect=lambda *a, **k: (events.append('undo') or ('undo', 'path', []))) as save,
          patch('druta.druta.profiles.capture', side_effect=AssertionError('full capture')),
          patch('druta.druta.profiles.restore', side_effect=lambda *a, **k: (events.append('restore') or [(True, 'ok')])) as restore):
        app.set_pending_load = Mock()
        app.load_profile('fan-undo')
    preflight.assert_called_once_with(app.gpu, state)
    save.assert_called_once_with(app.gpu, 'load-fan-undo', scope='fan')
    restore.assert_called_once_with(app.gpu, state)
    assert events == ['undo', 'restore', 'refresh']
    assert app._clk_lock == {'kind': Druta.LOCK_P0, 'verified': True}
    assert not app._profile_applying


@pytest.mark.parametrize('failure', ['incomplete', 'exception', 'preflight'])
def test_failed_undo_or_preflight_never_restores(app, failure):
    state = {'schema': 2, 'scope': 'fan'}
    with (patch('druta.druta.profiles.preflight', return_value='bad' if failure == 'preflight' else None),
          patch('druta.druta.profiles.autosave', side_effect=OSError('failed') if failure == 'exception' else None,
                return_value=('undo', 'path', ['missing'] if failure == 'incomplete' else [])),
          patch('druta.druta.profiles.restore') as restore):
        app.begin_profile_load('fan', state)
    restore.assert_not_called()
    app.profile_failure.assert_called_once()


def test_fan_refresh_reads_only_requested_target(app):
    app.gpu.read_fan_control_state.return_value = {'fans': [{'manual': True, 'level': 45}, {'manual': True, 'level': 45}]}
    with (patch('druta.druta.dpg.does_item_exist', return_value=True),
          patch('druta.druta.dpg.set_value') as set_value):
        app.refresh_fan_profile()
    assert set_value.call_args_list == [(("sl_fan", 45),), (("in_fan", 45),)]
    app.gpu.read.assert_not_called()


def test_mixed_fan_targets_leave_shared_input_unchanged(app):
    app.gpu.read_fan_control_state.return_value = {'fans': [{'manual': True, 'level': 45}, {'manual': True, 'level': 60}]}
    with (patch('druta.druta.dpg.does_item_exist', return_value=True),
          patch('druta.druta.dpg.set_value') as set_value):
        app.refresh_fan_profile()
    set_value.assert_not_called()
