from types import SimpleNamespace

from druta import combined_profiles, profiles
from druta.druta import Druta


def app_stub(events):
    app = SimpleNamespace(gpu=object(), rail=None, _i2c_busy=False, _pending_load=None,
                          _held_startup_request=None, guard=lambda: True,
                          set_pending_load=lambda value: None,
                          profile_failure=lambda reason, *args: events.append(('failure', reason)),
                          log=lambda *args: None)
    app.finish_profile_load = lambda *args: events.append(('restore', args))
    return app


def test_combined_manual_load_captures_one_undo_before_restore(monkeypatch):
    events = []
    app = app_stub(events)
    state = {'scope': 'combined'}
    monkeypatch.setattr(combined_profiles, 'validate', lambda state: [{'state': {'scope': 'fan'}}])
    monkeypatch.setattr(combined_profiles, 'preflight', lambda *args: events.append(('preflight',)) or None)
    monkeypatch.setattr(combined_profiles, 'capture_undo', lambda *args: events.append(('capture',)) or {'scope': 'combined'})
    monkeypatch.setattr(profiles, 'autosave_state', lambda *args: events.append(('save',)) or ('undo', '', []))
    Druta.begin_combined_profile_load(app, 'test', state)
    assert [event[0] for event in events] == ['preflight', 'capture', 'save', 'restore']


def test_combined_preflight_failure_and_automatic_never_write(monkeypatch):
    events = []
    app = app_stub(events)
    state = {'scope': 'combined'}
    monkeypatch.setattr(combined_profiles, 'validate', lambda state: [{'state': {'scope': 'fan'}}])
    monkeypatch.setattr(combined_profiles, 'preflight', lambda *args: 'incompatible')
    monkeypatch.setattr(combined_profiles, 'capture_undo', lambda *args: events.append(('capture',)))
    Druta.begin_combined_profile_load(app, 'test', state)
    Druta.begin_combined_profile_load(app, 'test', state, automatic=True)
    assert len(events) == 2 and all(event[0] == 'failure' for event in events)
    assert 'manual' in events[1][1]


def test_combined_full_profile_waits_for_i2c_with_envelope(monkeypatch):
    events = []
    app = app_stub(events)
    full = {'scope': None, 'schema': 2, 'i2c': {'id': 1}}
    state = {'scope': 'combined'}
    rail = object()
    app.rail = rail
    app.rail_for_profile = lambda child: rail
    app._finish_manual_voltage = lambda reason: (True, '')
    app.sync_risk_ui = lambda: None
    app.i2c_verified = lambda: False
    app.verify_i2c_rail = lambda: setattr(app, '_i2c_busy', True)
    app.sync_lock_ui = lambda: None
    monkeypatch.setattr(combined_profiles, 'validate', lambda state: [{'state': full}])
    monkeypatch.setattr(combined_profiles, 'preflight', lambda *args: None)
    monkeypatch.setattr(combined_profiles, 'capture_undo', lambda *args: {})
    monkeypatch.setattr(profiles, 'autosave_state', lambda *args: ('undo', '', []))
    monkeypatch.setattr(profiles, 'device_mismatch', lambda *args: None)
    monkeypatch.setattr(profiles, 'i2c_replay_needed', lambda *args: True)
    import druta.druta as ui
    monkeypatch.setattr(ui.dpg, 'set_value', lambda *args: None)
    monkeypatch.setattr(ui.dpg, 'configure_item', lambda *args, **kwargs: None)
    Druta.begin_combined_profile_load(app, 'test', state)
    assert app._profile_pending == ('test', state, False)
    assert not events


def test_save_combined_uses_only_selected_snapshots(monkeypatch):
    import druta.druta as ui
    events = []
    app = SimpleNamespace(_combine_states=[('one', {'scope': 'fan'}), ('two', {'scope': 'fan'})],
                          log=lambda *args: None, refresh_profile_list=lambda: None)
    values = {'combine_name': 'bundle', 'combine_choice_0': True, 'combine_choice_1': False}
    monkeypatch.setattr(ui.dpg, 'get_value', values.get)
    monkeypatch.setattr(ui.dpg, 'configure_item', lambda *args, **kwargs: None)
    monkeypatch.setattr(combined_profiles, 'compose', lambda selected: events.append(selected) or {'scope': 'combined'})
    monkeypatch.setattr(profiles, 'save', lambda name, state: events.append((name, state)) or 'bundle.json')
    Druta.save_combined_profile(app)
    assert events == [[('one', {'scope': 'fan'})], ('bundle', {'scope': 'combined'})]


def test_partial_full_tune_preserves_scoped_rail_gate_intent(monkeypatch):
    import druta.druta as ui
    for enabled in (False, True):
        events = []
        app = app_stub(events)
        app.rail_for_profile = lambda child: None
        app._finish_manual_voltage = lambda reason: (True, '')
        values = {'vlim_mode': enabled}
        app.sync_risk_ui = lambda: events.append(('rail-gate', values['vlim_mode']))
        full = {'schema': 2, 'scope': None, 'mem_off_units': 1}
        items = [{'state': full}, {'state': {'scope': 'rail_limits'}}]
        monkeypatch.setattr(combined_profiles, 'validate', lambda state: items)
        monkeypatch.setattr(combined_profiles, 'preflight', lambda *args: None)
        monkeypatch.setattr(combined_profiles, 'capture_undo', lambda *args: {})
        monkeypatch.setattr(profiles, 'autosave_state', lambda *args: ('undo', '', []))
        monkeypatch.setattr(profiles, 'device_mismatch', lambda *args: None)
        monkeypatch.setattr(ui.dpg, 'get_value', values.get)
        monkeypatch.setattr(ui.dpg, 'set_value', values.__setitem__)
        Druta.begin_combined_profile_load(app, 'test', {'scope': 'combined'})
        assert ('rail-gate', enabled) in events
        assert events[-1][0] == 'restore'


def test_combined_restore_refreshes_each_scope_without_replaying(monkeypatch):
    events = []
    children = [{'scope': None}, {'scope': 'lowpower_requests'}, {'scope': 'clock_vf'}]
    app = SimpleNamespace(rail=None, log=lambda *args: None,
                          profile_failure=lambda *args: events.append('failure'),
                          refresh_full_profile=lambda child: events.append(('full', child)),
                          refresh_lowpower_profile=lambda child: events.append(('lowpower', child)),
                          refresh_scoped_profile=lambda child: events.append(('scoped', child)),
                          refresh_profile_list=lambda: None)
    monkeypatch.setattr(combined_profiles, 'restore', lambda *args: events.append('restore') or [(True, 'ok')])
    monkeypatch.setattr(combined_profiles, 'validate', lambda state: [{'state': child} for child in children])
    Druta.finish_combined_profile_load(app, 'test', {'scope': 'combined'})
    assert events == ['restore', ('full', children[0]), ('lowpower', children[1]), ('scoped', children[2])]


def test_composition_dialog_lists_saved_children_and_explains_empty_state(monkeypatch):
    import dearpygui.dearpygui as dpg
    dpg.create_context()
    try:
        with dpg.window(tag='win_combine'):
            dpg.add_input_text(tag='combine_name')
            dpg.add_text('', tag='combine_error')
            with dpg.group(tag='combine_rows'):
                pass
        app = SimpleNamespace(s=lambda x: x, log=lambda *args: None,
                              show_win=lambda **kw: None)
        monkeypatch.setattr(profiles, 'list_profiles', lambda: [('memory', '', '', False), ('bundle', '', '', False)])
        monkeypatch.setattr(profiles, 'load', lambda name: {'scope': 'combined' if name == 'bundle' else 'tuning_action'})
        monkeypatch.setattr(profiles, 'summarize', lambda state: 'captured memory offset')
        Druta.open_combine_profiles(app)
        assert [name for name, _ in app._combine_states] == ['memory']
        assert dpg.does_item_exist('combine_choice_0')
        assert not dpg.does_item_exist('combine_choice_1')
        dpg.set_value('combine_error', 'old failure')
        monkeypatch.setattr(profiles, 'list_profiles', lambda: [])
        Druta.open_combine_profiles(app)
        assert not app._combine_states
        assert dpg.get_value('combine_error') == ''
        rows = dpg.get_item_children('combine_rows', 1)
        assert len(rows) == 1 and 'No saved sections' in dpg.get_value(rows[0])
    finally:
        dpg.destroy_context()
