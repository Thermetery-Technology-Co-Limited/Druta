import copy
from types import SimpleNamespace

import pytest

from druta import combined_profiles as combined


def tune(kind='boost', index=None, value=10):
    return {'schema': 2, 'scope': 'tuning_action', 'saved_at': 'now', 'saved_ts': 1,
            'device': {'name': None, 'vbios': None, 'driver': None, 'uuid': None, 'slot': 'a'},
            'incomplete': [], 'tuning_action': {'kind': kind, 'index': index, 'value': value}}


def gate(enabled):
    return {'schema': 2, 'scope': 'voltage_controls', 'incomplete': [],
            'voltage_controls': {'kind': 'gate', 'identity': {'slot': 'a', 'architecture': 1,
                'device_id': 2, 'subsystem_id': 3}, 'rows': [{'enabled': enabled}]}}


def test_compose_copies_and_orders_gate_boost_and_disable():
    state = tune()
    bundle = combined.compose([('boost', state), ('gate', gate(True))])
    state['tuning_action']['value'] = 80
    assert [c['name'] for c in combined.validate(bundle)] == ['gate', 'boost']
    assert bundle['components'][0]['state']['tuning_action']['value'] == 10
    assert [c['name'] for c in combined.validate(combined.compose([('gate', gate(False)), ('boost', tune())]))] == ['boost', 'gate']


def test_rejects_nested_and_overlapping_targets():
    bundle = combined.compose([('boost', tune())])
    with pytest.raises(ValueError, match='nested'):
        combined.compose([('nested', bundle)])
    with pytest.raises(ValueError, match='overlapping'):
        combined.compose([('one', tune()), ('two', tune(value=20))])
    full = {'schema': 2, 'vf_applicable': False, 'volt_boost_pct': 1}
    with pytest.raises(ValueError, match='overlapping'):
        combined.compose([('full', full), ('boost', tune())])


def test_preflight_all_and_stop_at_first_failed_restore(monkeypatch):
    bundle = combined.compose([('one', tune('domain', 1, 1000)), ('two', tune('domain', 2, 2000))])
    calls = []
    monkeypatch.setattr(combined, '_child_preflight', lambda app, child, projected_gate=False: calls.append(('check', child['tuning_action']['index'])) or None)
    monkeypatch.setattr(combined.profiles, 'restore', lambda gpu, state, **kw: calls.append(('write', state['tuning_action']['index'])) or [(False, 'failed')])
    app = SimpleNamespace(gpu=object(), rail=None, i2c_verified=lambda: False)
    result = combined.restore(app, bundle)
    assert calls == [('check', 1), ('check', 2), ('check', 1), ('write', 1)]
    assert result == [(False, 'one: failed')]


def test_undo_capture_is_all_or_nothing(monkeypatch):
    bundle = combined.compose([('one', tune('domain', 1, 1000)), ('two', tune('domain', 2, 2000))])
    monkeypatch.setattr(combined, 'preflight', lambda app, state: None)
    seen = []
    def capture(app, child):
        seen.append(child['tuning_action']['index'])
        if len(seen) == 2:
            raise ValueError('read failed')
        return copy.deepcopy(child)
    monkeypatch.setattr(combined, '_capture', capture)
    with pytest.raises(ValueError, match='read failed'):
        combined.capture_undo(object(), bundle)
    assert seen == [1, 2]


def request(field='REL'):
    return {'schema': 2, 'scope': 'voltage_requests', 'incomplete': [],
            'voltage_requests': {'identity': {'slot': 'a', 'architecture': 1, 'device_id': 2, 'subsystem_id': 3},
                'rails': {'0': {'identity': {'name': 'x', 'policy_type': 1, 'control_type': 1},
                                'offsets_uv': {field: 0}}}}}


def test_full_rail_overlap_maps_legacy_fields_but_allows_vcrit():
    full = {'schema': 2, 'vf_applicable': False, 'rail_limits_uv': {'0': {'reliability': 1}}}
    with pytest.raises(ValueError, match='overlapping'):
        combined.compose([('full', full), ('rel', request())])
    combined.compose([('full', full), ('vcrit', request('VCRIT low'))])


def test_projected_gate_only_for_explicit_enable_and_rechecks_before_write(monkeypatch):
    calls = []
    def check(app, child, projected_gate=False):
        calls.append((child['scope'], projected_gate))
        if child['scope'] == 'voltage_requests' and not projected_gate:
            return 'gate disabled'
        return None
    monkeypatch.setattr(combined, '_child_preflight', check)
    monkeypatch.setattr(combined.profiles, 'restore', lambda *args, **kwargs: [(True, 'ok')])
    app = SimpleNamespace(gpu=object(), rail=None, i2c_verified=lambda: False)
    disabled = combined.compose([('request', request()), ('gate', gate(False))])
    assert 'gate disabled' in combined.preflight(app, disabled)
    enabled = combined.compose([('request', request()), ('gate', gate(True))])
    assert combined.preflight(app, enabled) is None
    result = combined.restore(app, enabled)
    assert result[-1][0] is False and 'gate disabled' in result[-1][1]
    assert calls[-1] == ('voltage_requests', False)


def native(kind, rail, equation, role=None):
    row = ({'rail': rail, 'equation': equation, 'upper_uv': 900000} if kind == 'rel_bounds'
           else {'rail': rail, 'role': role, 'equation': equation, 'value_uv': 900000})
    return {'schema': 2, 'scope': 'voltage_controls', 'incomplete': [],
            'voltage_controls': {'kind': kind, 'identity': {'slot': 'a', 'architecture': 1,
                'device_id': 2, 'subsystem_id': 3}, 'rows': [row]}}


def test_native_equation_alias_and_vf_curve_overlap():
    with pytest.raises(ValueError, match='overlapping'):
        combined.compose([('a', native('constants', 0, 7, 'OV')),
                          ('b', native('constants', 1, 7, 'VMIN'))])
    combined.compose([('a', native('constants', 0, 7, 'OV')),
                      ('b', native('constants', 1, 8, 'VMIN'))])
    full = {'schema': 2, 'vf_applicable': True, 'vf_deltas': {'1': 1000}}
    with pytest.raises(ValueError, match='overlapping'):
        combined.compose([('curve', full), ('domain', tune('domain', 0, 1000))])


def test_full_undo_preserves_coupled_power_and_excludes_unselected_curve(monkeypatch):
    current = {'schema': 2, 'vf_applicable': False, 'vf_deltas': {'1': 50},
               'power_limit_mw': 100, 'current_limits_ma': {'1': 30},
               'power_policies': {'1': {'value': 3}}, 'power_policy_pins': {'1': {'value': 3}}}
    monkeypatch.setattr(combined.profiles, 'capture', lambda gpu, rail: copy.deepcopy(current))
    app = SimpleNamespace(gpu=object(), rail=None)
    target = {'schema': 2, 'vf_applicable': False, 'power_limit_mw': 200}
    undo = combined._capture(app, target)
    assert undo['vf_deltas'] is None
    assert undo['power_limit_mw'] == current['power_limit_mw']
    assert undo['power_policies'] == current['power_policies']
    assert undo['power_policy_pins'] == current['power_policy_pins']
    assert undo['current_limits_ma'] == current['current_limits_ma']


def test_undo_gate_disable_runs_after_request_when_gate_is_enabled(monkeypatch):
    state = {'enabled': True}
    events = []
    def check(app, child, projected_gate=False):
        if child['scope'] == 'voltage_requests' and not (state['enabled'] or projected_gate):
            return 'gate disabled'
        return None
    def restore(gpu, child, **kwargs):
        if child['scope'] == 'voltage_controls':
            state['enabled'] = child['voltage_controls']['rows'][0]['enabled']
            events.append('disable')
        else:
            assert state['enabled']
            events.append('request')
        return [(True, 'ok')]
    monkeypatch.setattr(combined, '_child_preflight', check)
    monkeypatch.setattr(combined.profiles, 'restore', restore)
    app = SimpleNamespace(gpu=object(), rail=None, i2c_verified=lambda: False)
    bundle = combined.compose([('gate', gate(False)), ('request', request())])
    assert all(ok for ok, _ in combined.restore(app, bundle))
    assert events == ['request', 'disable'] and not state['enabled']
    events.clear()
    assert combined.preflight(app, bundle) == 'request: gate disabled'
    assert events == []


def test_generic_profile_routes_describe_combined_but_cannot_replay_without_owner():
    bundle = combined.compose([('boost', tune())])
    assert combined.profiles.incomplete(bundle) == []
    assert 'boost' in combined.profiles.summarize(bundle)
    result = combined.profiles.restore(object(), bundle)
    assert len(result) == 1 and result[0][0] is False
    assert 'owning application' in result[0][1]


def test_unavailable_later_component_prevents_all_writes(monkeypatch):
    bundle = combined.compose([('one', tune('domain', 1, 1000)), ('two', tune('domain', 2, 2000))])
    monkeypatch.setattr(combined, '_child_preflight', lambda app, child, projected_gate=False:
                        'unavailable' if child['tuning_action']['index'] == 2 else None)
    writes = []
    monkeypatch.setattr(combined.profiles, 'restore', lambda *a, **kw: writes.append(a))
    result = combined.restore(SimpleNamespace(), bundle)
    assert not writes
    assert result == [(False, 'combined profile not applied: two: unavailable')]


def test_full_nvvdd_and_scoped_offset_overlap():
    full = {'schema': 2, 'vf_applicable': False, 'nvvdd_offset_mv': 12.5}
    with pytest.raises(ValueError, match='overlapping'):
        combined.compose([('full', full), ('NVVDD', tune('rail_offset', None, 12500))])
    combined.compose([('full', {'schema': 2, 'vf_applicable': False}),
                      ('NVVDD', tune('rail_offset', None, 12500))])
