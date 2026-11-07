# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
from unittest.mock import Mock, patch

import pytest

from druta.druta import Druta
from tests.test_tuning_action_profiles import GPU


@pytest.fixture
def app():
    a = Druta.__new__(Druta)
    a.gpu, a.rail = GPU(), None
    a.log, a.show_win, a.refresh_profile_list = Mock(), Mock(), Mock()
    return a


def test_current_memory_capture_has_no_writes_or_unrelated_capture(app):
    app._profile_save_scope, app._profile_save_kind, app._profile_save_index = 'tuning_action', 'memory', None
    with patch('druta.druta.dpg.get_value', return_value='named'), \
         patch('druta.druta.dpg.does_item_exist', return_value=False), \
         patch('druta.druta.profiles.save', return_value='named.json') as save, \
         patch('druta.druta.profiles.capture', side_effect=AssertionError('full capture')), \
         patch('druta.druta.profiles.autosave_state', side_effect=AssertionError('autosave')):
        app.save_profile()
    assert save.call_args.args[1]['tuning_action']['value'] == 17
    assert app.gpu.calls == []


@pytest.mark.parametrize('choice,scope,kind,index', [
    ('Memory offset', 'tuning_action', 'memory', None),
    ('Voltage boost', 'tuning_action', 'boost', None),
    ('Fan policy', 'fan', None, None),
    ('Coupled power/current requests', 'power_action', None, None),
    ('Full tune', None, None, None),
])
def test_selection_and_reset(app, choice, scope, kind, index):
    app.ordinary_profile_contents = Mock(return_value={
        'Full tune': (None, None, None), 'Memory offset': ('tuning_action', 'memory', None),
        'Voltage boost': ('tuning_action', 'boost', None), 'Fan policy': ('fan', None, None),
        'Coupled power/current requests': ('power_action', None, None)})
    with patch('druta.druta.dpg.does_item_exist', return_value=True), \
         patch('druta.druta.dpg.get_value', return_value='name'), \
         patch('druta.druta.dpg.configure_item') as config, \
         patch('druta.druta.dpg.set_value'):
        app.open_save_profile()
        app.select_profile_contents(app_data=choice)
        assert (app._profile_save_scope, app._profile_save_kind, app._profile_save_index) == (scope, kind, index)
        app.open_save_profile(user_data='clock_vf')
        assert app._profile_save_scope == 'clock_vf'
        assert any(c.args[0] == 'prof_save_contents' and not c.kwargs['show'] for c in config.call_args_list if c.args[0] == 'prof_save_contents')
        app.open_save_profile()
        assert (app._profile_save_scope, app._profile_save_kind, app._profile_save_index) == (None, None, None)


def test_failed_or_incomplete_capture_and_busy_never_save(app):
    app._profile_save_scope, app._profile_save_kind, app._profile_save_index = 'fan', None, None
    with patch('druta.druta.dpg.get_value', return_value='named'), \
         patch('druta.druta.dpg.does_item_exist', return_value=False), \
         patch('druta.druta.profiles.capture_fan', return_value={'scope': 'fan', 'incomplete': ['failed read']}) as capture, \
         patch('druta.druta.profiles.save') as save:
        app.save_profile()
        save.assert_not_called()
        capture.assert_called_once()
        app._i2c_busy = True
        app.save_profile()
        capture.assert_called_once()


def test_failed_tuning_read_never_saves(app):
    app.gpu.boost = None
    app._profile_save_scope, app._profile_save_kind, app._profile_save_index = 'tuning_action', 'boost', None
    with patch('druta.druta.dpg.get_value', return_value='named'), \
         patch('druta.druta.profiles.save') as save:
        app.save_profile()
    save.assert_not_called()


@pytest.mark.parametrize('entry', ['begin_profile_load', 'finish_profile_load'])
def test_fan_automatic_load_refused(app, entry):
    app.profile_failure = Mock()
    getattr(app, entry)('named', {'scope': 'fan'}, automatic=True)
    app.profile_failure.assert_called_once()



def test_domains_use_live_capability_and_label(app):
    app.gpu.read = Mock(return_value={'clk_domains': [{'domain': 5}]})
    app.gpu.clkdom_controls_for_ui = Mock(return_value=[1, 3])
    app.gpu.clkdom_domains = Mock(return_value=[1, 3])
    app.gpu.clkdom_offset_slider_offered = Mock(side_effect=lambda i: i == 3)
    app.domain_knob_label = Mock(return_value=('Named domain offset', None, 5))
    choices = app.ordinary_profile_contents()
    assert choices['Named domain offset'] == ('tuning_action', 'domain', 3)
    assert not any(v == ('tuning_action', 'domain', 1) for v in choices.values())
    app.gpu.clkdom_controls_for_ui.assert_called_once_with([{'domain': 5}])


@pytest.mark.parametrize('scope,kind,index,helper,args', [
    ('tuning_action', 'domain', 7, 'capture_tuning_action', ('domain', 7)),
    ('tuning_action', 'boost', None, 'capture_tuning_action', ('boost', None)),
    ('power_action', None, None, 'capture_power_action', ()),
    ('fan', None, None, 'capture_fan', ()),
])
def test_scoped_capture_routing(app, scope, kind, index, helper, args):
    app._profile_save_scope, app._profile_save_kind, app._profile_save_index = scope, kind, index
    snapshot = {'scope': scope}
    with patch('druta.druta.dpg.get_value', return_value='named'), \
         patch('druta.druta.dpg.does_item_exist', return_value=False), \
         patch('druta.druta.profiles.' + helper, return_value=snapshot) as capture, \
         patch('druta.druta.profiles.incomplete', return_value=[]), \
         patch('druta.druta.profiles.summarize', return_value='selected'), \
         patch('druta.druta.profiles.capture', side_effect=AssertionError('full capture')), \
         patch('druta.druta.profiles.save', return_value='named.json') as save:
        app.save_profile()
    capture.assert_called_once_with(app.gpu, *args)
    save.assert_called_once_with('named', snapshot)


def test_nvvdd_named_choice_and_saved_value(app):
    from tests.test_tuning_action_profiles import RailGPU
    app.gpu = RailGPU()
    assert app.ordinary_profile_contents()['NVVDD offset'] == ('tuning_action', 'rail_offset', None)
    app._profile_save_scope, app._profile_save_kind, app._profile_save_index = 'tuning_action', 'rail_offset', None
    with patch('druta.druta.dpg.get_value', return_value='named'), \
         patch('druta.druta.dpg.does_item_exist', return_value=False), \
         patch('druta.druta.profiles.save', return_value='named.json') as save:
        app.save_profile()
    assert save.call_args.args[1]['tuning_action']['value'] == 12501
    assert not app.gpu.calls
    app.gpu.available = False
    assert 'NVVDD offset' not in app.ordinary_profile_contents()


@pytest.mark.parametrize('entry', ['begin_profile_load', 'finish_profile_load'])
def test_nvvdd_automatic_load_refused(app, entry):
    app.profile_failure = Mock()
    getattr(app, entry)('named', {'scope': 'tuning_action', 'tuning_action': {'kind': 'rail_offset'}}, automatic=True)
    app.profile_failure.assert_called_once()
