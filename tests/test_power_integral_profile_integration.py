"""Profile framework routes and combination rules for integral bounds."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import combined_profiles as combined
from druta import power_integral_profiles as integral
from druta import profiles


@pytest.fixture
def gpu(monkeypatch):
    gpu = SimpleNamespace(_lock=threading.RLock(), static={'name': 'card', 'driver': 'driver',
                          'vbios': 'vbios', 'uuid': 'uuid'}, slot=lambda: 'slot')
    report = {'identity': {'slot': 'slot', 'architecture': 1, 'device_id': 2, 'subsystem_id': 3},
              'policies': [{'policy': 2, 'type': 1, 'channel': 0, 'unit': 'mW', 'available': True,
                            'lower_raw': 3072, 'upper_raw': 4219},
                           {'policy': 4, 'type': 2, 'channel': 1, 'unit': 'mA', 'available': True,
                            'lower_raw': 2000, 'upper_raw': 5000}]}
    controller = SimpleNamespace(read=lambda: copy.deepcopy(report))
    monkeypatch.setattr(integral, 'PowerIntegralController', lambda _: controller)
    return gpu, report


def test_save_load_summary_and_selected_undo(gpu, monkeypatch, tmp_path):
    card, report = gpu
    state = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    monkeypatch.setattr(profiles, 'DIR', str(tmp_path))
    profiles.save('selected', state)
    assert profiles.load('selected') == state
    assert profiles.incomplete(state) == []
    assert '1 power integral bound' in profiles.summarize(state)
    report['policies'][0]['lower_raw'] = 3200
    name, _, missing = profiles.autosave_power_integral(card, 'load-selected', state)
    undo = profiles.load(name)
    assert missing == []
    assert undo['power_integral']['policies'][0]['values'] == {'lower_raw': 3200}
    assert len(undo['power_integral']['policies']) == 1


def test_preflight_failure_blocks_undo_and_restore(gpu, monkeypatch):
    card, report = gpu
    state = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    report['policies'][0]['available'] = False
    save = Mock()
    monkeypatch.setattr(profiles, 'autosave_state', save)
    with pytest.raises(ValueError, match='unavailable'):
        profiles.autosave_power_integral(card, 'undo', state)
    save.assert_not_called()
    assert not profiles.restore(card, state)[0][0]


def test_combined_disjoint_bounds_selected_undo_and_overlap(gpu):
    card, report = gpu
    lower = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    upper = profiles.capture_power_integral(card, {2: ('upper_raw',)})
    bundle = combined.compose([('lower', lower), ('upper', upper)])
    app = SimpleNamespace(gpu=card, rail=None)
    assert combined.preflight(app, bundle) is None
    report['policies'][0].update(lower_raw=3100, upper_raw=4200)
    undo = combined.capture_undo(app, bundle)
    rows = [item['state']['power_integral']['policies'][0]['values'] for item in undo['components']]
    assert rows == [{'lower_raw': 3100}, {'upper_raw': 4200}]
    with pytest.raises(ValueError, match='overlapping'):
        combined.compose([('one', lower), ('two', lower)])
    # Full ordinary power restore preserves integral ratio words.
    full = {'schema': 2, 'vf_applicable': False, 'power_limit_mw': 100000}
    combined.compose([('full power', full), ('integral', lower)])


def test_inverted_disjoint_bounds_refused_before_any_write(gpu, monkeypatch):
    card, _ = gpu
    lower = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    upper = profiles.capture_power_integral(card, {2: ('upper_raw',)})
    lower['power_integral']['policies'][0]['values']['lower_raw'] = 4096
    upper['power_integral']['policies'][0]['values']['upper_raw'] = 3686
    bundle = combined.compose([('lower', lower), ('upper', upper)])
    app = SimpleNamespace(gpu=card, rail=None, i2c_verified=lambda: False)
    writes = Mock()
    monkeypatch.setattr(profiles, 'restore', writes)
    assert 'inverted' in combined.preflight(app, bundle)
    assert combined.restore(app, bundle)[0][0] is False
    writes.assert_not_called()


def test_generic_restore_routes_only_selected_field(gpu, monkeypatch):
    card, report = gpu
    target = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    report['policies'][0]['lower_raw'] = 3200
    calls = []
    def set_lower(policy, value, **kwargs):
        calls.append((policy, value, kwargs))
        report['policies'][0]['lower_raw'] = value
        return copy.deepcopy(report)
    controller = SimpleNamespace(read=lambda: copy.deepcopy(report), set_lower=set_lower,
                                 set_upper=Mock(side_effect=AssertionError('unselected bound')))
    monkeypatch.setattr(integral, 'PowerIntegralController', lambda _: controller)
    assert all(ok for ok, _ in profiles.restore(card, target))
    assert len(calls) == 1 and calls[0][:2] == (2, 3072)
    assert report['policies'][0]['upper_raw'] == 4219
    assert report['policies'][1]['lower_raw'] == 2000
    controller.set_upper.assert_not_called()


@pytest.mark.parametrize('initial,target,order', [
    ((1000, 2000), (2500, 3000), ('upper_raw', 'lower_raw')),
    ((2500, 3000), (1000, 2000), ('lower_raw', 'upper_raw')),
])
@pytest.mark.parametrize('reverse', [False, True])
def test_split_ranges_restore_with_safe_order(gpu, monkeypatch, initial, target, order, reverse):
    card, report = gpu
    row = report['policies'][0]
    row.update(lower_raw=target[0], upper_raw=target[1])
    lower = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    upper = profiles.capture_power_integral(card, {2: ('upper_raw',)})
    parts = [('lower', lower), ('upper', upper)]
    bundle = combined.compose(parts[::-1] if reverse else parts)
    original = copy.deepcopy(bundle)
    row.update(lower_raw=initial[0], upper_raw=initial[1])
    calls = []
    def setter(field):
        def write(policy, value, **kwargs):
            calls.append(field)
            row[field] = value
            assert row['lower_raw'] <= row['upper_raw']
            return copy.deepcopy(report)
        return write
    controller = SimpleNamespace(read=lambda: copy.deepcopy(report),
                                 set_lower=setter('lower_raw'), set_upper=setter('upper_raw'))
    monkeypatch.setattr(integral, 'PowerIntegralController', lambda _: controller)
    app = SimpleNamespace(gpu=card, rail=None, i2c_verified=lambda: False)
    assert combined.preflight(app, bundle) is None
    undo = combined.capture_undo(app, bundle)
    assert len(undo['components']) == 2
    assert all(ok for ok, _ in combined.restore(app, bundle))
    assert calls == list(order)
    assert (row['lower_raw'], row['upper_raw']) == target
    assert bundle == original


def test_split_range_partial_failure_reports_prior_write(gpu, monkeypatch):
    card, report = gpu
    row = report['policies'][0]
    row.update(lower_raw=2500, upper_raw=3000)
    lower = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    upper = profiles.capture_power_integral(card, {2: ('upper_raw',)})
    bundle = combined.compose([('lower', lower), ('upper', upper)])
    row.update(lower_raw=1000, upper_raw=2000)
    def set_upper(policy, value, **kwargs):
        row['upper_raw'] = value
        return copy.deepcopy(report)
    def fail_lower(*args, **kwargs):
        raise ValueError('readback failed')
    controller = SimpleNamespace(read=lambda: copy.deepcopy(report), set_upper=set_upper, set_lower=fail_lower)
    monkeypatch.setattr(integral, 'PowerIntegralController', lambda _: controller)
    app = SimpleNamespace(gpu=card, rail=None, i2c_verified=lambda: False)
    results = combined.restore(app, bundle)
    assert [ok for ok, _ in results] == [True, False]
    assert 'upper_raw' in results[0][1] and 'readback failed' in results[1][1]
    assert (row['lower_raw'], row['upper_raw']) == (1000, 3000)


def test_split_range_metadata_or_identity_mismatch_refused(gpu):
    card, _ = gpu
    lower = profiles.capture_power_integral(card, {2: ('lower_raw',)})
    upper = profiles.capture_power_integral(card, {2: ('upper_raw',)})
    app = SimpleNamespace(gpu=card, rail=None)
    upper['power_integral']['policies'][0]['channel'] += 1
    assert 'metadata differs' in combined.preflight(app, combined.compose([('lower', lower), ('upper', upper)]))
    upper['power_integral']['policies'][0]['channel'] -= 1
    upper['device']['uuid'] = 'other'
    assert 'UUID' in combined.preflight(app, combined.compose([('lower', lower), ('upper', upper)]))
