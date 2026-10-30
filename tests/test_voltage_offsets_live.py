"""Hardware-free tests for app-owned voltage offset profile behavior."""
import copy
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from druta import vfe_equation_runtime as eq
from druta import voltage_offset_scaffold as scaffold
from druta import voltage_offsets_live as live
from tests.test_vbios_fields import apply
from tests.test_voltage_offset_scaffold import case, loaded_from


@pytest.fixture
def engine(monkeypatch):
    image, before, variables = case()
    requests = [[0, 'REL'], [0, 'ALT']]
    plan, heads = scaffold.plan(image, before, variables, requests)
    active_image = apply(image, plan)
    observed = {'equations': loaded_from(before, heads), 'policy': {'rails': {'0': {
        'policy_uv': {'reliability': 1000000, 'alt_reliability': 1020000,
                      'overvoltage': 1200000, 'vmin': 800000, 'effective': 1000000}}}},
        'rail_control': {'opaque': 42}, 'boost_percent': 0}
    profile = {'before_equations': before, 'requests': requests, 'heads': heads,
               'values': {h['key']: 0 for h in heads}}
    state = {'observed': observed, 'profile': profile, 'writes': []}

    def recognize(slot, saved):
        _, found = scaffold.verify_loaded(image, before, variables, requests, active_image, state['observed']['equations'])
        return object(), object(), image, variables, active_image, copy.deepcopy(state['observed']), found

    def writer(gpu, contract):
        item = next(iter(contract['selected'].values()))
        role = next(iter(contract['selected'])).split('.')[2]
        state['observed']['equations'] = copy.deepcopy(contract['after'])
        state['observed']['policy']['rails']['0']['policy_uv'][live._POLICY[role]] += item['after_uv'] - item['before_uv']
        state['writes'].append((role, item['after_uv']))

    monkeypatch.setattr(live, '_load', lambda slot: copy.deepcopy(state['profile']))
    monkeypatch.setattr(live, '_save', lambda slot, value: state.update(profile=copy.deepcopy(value)))
    monkeypatch.setattr(live, '_recognize', recognize)
    monkeypatch.setattr(live, '_voltage', lambda gpu: copy.deepcopy(state['observed']))
    monkeypatch.setattr(eq, 'write_constants', writer)
    return state, heads


def test_signed_independent_values_and_profile_replay(engine):
    state, heads = engine
    rel, alt = (h['key'] for h in heads)
    first = live.set_offsets('adapter', {rel: 5000, alt: -5000})
    assert first['values'] == {rel: 5000, alt: -5000}
    assert first['policy']['rails']['0']['policy_uv']['reliability'] == 1005000
    assert first['policy']['rails']['0']['policy_uv']['alt_reliability'] == 1015000
    profile = live.capture('adapter')
    live.set_offsets('adapter', {rel: 0, alt: 0})
    restored = live.replay('adapter', profile)
    assert restored['values'] == profile
    assert state['profile']['values'] == profile
    assert state['writes'] == [('REL', 5000), ('ALT', -5000), ('REL', 0), ('ALT', 0), ('REL', 5000), ('ALT', -5000)]


def test_preflight_refuses_unknown_or_unrepresentable_values_before_any_write(engine):
    state, heads = engine
    with pytest.raises(live.VoltageOffsetsError):
        live.set_offsets('adapter', {heads[0]['key']: 5000, 'voltage.1.REL.offset': 5})
    with pytest.raises(eq.VfeEquationError):
        live.set_offsets('adapter', {heads[0]['key']: 16777217})
    state['observed']['policy']['rails'].clear()
    with pytest.raises(live.VoltageOffsetsError, match='unavailable'):
        live.set_offsets('adapter', {heads[0]['key']: 5000})
    assert state['writes'] == []


def test_unexpected_policy_is_not_saved_as_verified(engine, monkeypatch):
    state, heads = engine
    original = eq.write_constants

    def bad(gpu, contract):
        original(gpu, contract)
        state['observed']['policy']['rails']['0']['policy_uv']['vmin'] += 1

    monkeypatch.setattr(eq, 'write_constants', bad)
    with pytest.raises(live.VoltageOffsetsError, match='Reported policy'):
        live.set_offsets('adapter', {heads[0]['key']: 5000})
    assert state['profile']['values'][heads[0]['key']] == 0


@pytest.mark.parametrize('role', ['REL', 'ALT'])
def test_nonzero_boost_observes_derived_fields_but_checks_direct_limits(engine, monkeypatch, role):
    state, heads = engine
    state['observed']['boost_percent'] = 25
    state['observed']['policy']['rails']['0']['boost_uv'] = 0
    state['observed']['policy']['rails']['1'] = copy.deepcopy(state['observed']['policy']['rails']['0'])
    writer = eq.write_constants

    def boosted(gpu, contract):
        writer(gpu, contract)
        for row in state['observed']['policy']['rails'].values():
            row['boost_uv'] += 100
            row['policy_uv']['reliability'] += 100
            row['policy_uv']['effective'] += 100

    monkeypatch.setattr(eq, 'write_constants', boosted)
    key = next(h['key'] for h in heads if h['role'] == role)
    result = live.set_offsets('adapter', {key: 5000})
    assert result['values'][key] == 5000
    assert result['policy']['rails']['1']['boost_uv'] == 100


def test_unknown_boost_allows_rel_alt_without_assuming_zero(engine, monkeypatch):
    state, heads = engine
    state['observed']['boost_percent'] = None
    writer = eq.write_constants

    def derived(gpu, contract):
        writer(gpu, contract)
        state['observed']['policy']['rails']['0']['policy_uv']['effective'] += 100
        state['observed']['policy']['rails']['0']['policy_uv']['reliability'] += 100

    monkeypatch.setattr(eq, 'write_constants', derived)
    values = {h['key']: 5000 for h in heads}
    assert live.set_offsets('adapter', values)['values'] == values


def test_nonzero_boost_still_rejects_other_direct_policy_change(engine, monkeypatch):
    state, heads = engine
    state['observed']['boost_percent'] = 25
    writer = eq.write_constants

    def changed_peer(gpu, contract):
        writer(gpu, contract)
        state['observed']['policy']['rails']['0']['policy_uv']['vmin'] += 1

    monkeypatch.setattr(eq, 'write_constants', changed_peer)
    with pytest.raises(live.VoltageOffsetsError, match='Reported policy'):
        live.set_offsets('adapter', {heads[1]['key']: 5000})


def test_zero_boost_still_requires_exact_rel_for_alt_change(engine, monkeypatch):
    state, heads = engine
    writer = eq.write_constants

    def changed_rel(gpu, contract):
        writer(gpu, contract)
        state['observed']['policy']['rails']['0']['policy_uv']['reliability'] += 1

    monkeypatch.setattr(eq, 'write_constants', changed_rel)
    with pytest.raises(live.VoltageOffsetsError, match='Reported policy'):
        live.set_offsets('adapter', {heads[1]['key']: 5000})


@pytest.mark.parametrize('mode', ['normal', 'partial', 'capacity', 'refuse'])
def test_enable_stages_composed_image_and_checks_fresh_activation(monkeypatch, tmp_path, mode):
    image, before, variables = case(free=1 if mode == 'capacity' else 3)
    requested = [[0, 'REL'], [0, 'OV'], [0, 'VMIN']] if mode == 'partial' else [[0, 'REL']]
    plan, heads = scaffold.plan(image, before, variables, requested)
    active_image = apply(image, plan)
    baseline = {'equations': before, 'policy': {'rails': {'0': {}}}, 'rail_control': {}, 'boost_percent': 0}
    active = copy.deepcopy(baseline)
    active['equations'] = loaded_from(before, heads)
    state = {'registry': {}, 'restarts': 0, 'active': False, 'snapshots': 0}

    @dataclass
    class Target:
        slot: str = 'adapter'
        instance_id: str = 'gpu'
        driver_key: str = 'key'

    target = Target()

    class Registry:
        def __init__(self, _target):
            pass

        def snapshot(self):
            state['snapshots'] += 1
            if mode == 'refuse' and state['snapshots'] > 1:
                return {'changed': 1}
            return copy.deepcopy(state['registry'])

        def replace(self, expected, desired):
            assert expected == state['registry']
            state['registry'] = copy.deepcopy(desired)

    def restart(_target):
        state['restarts'] += 1
        state['active'] = True
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(live.startup, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(live, 'GPU', lambda slot: object())
    monkeypatch.setattr(live.ro, 'bind_target', lambda gpu: target)
    monkeypatch.setattr(live.ro, 'WindowsRegistry', Registry)
    monkeypatch.setattr(live.ro, 'active_receipt', lambda target: None)
    monkeypatch.setattr(live.ro, 'transaction_lock', lambda target: nullcontext())
    monkeypatch.setattr(live.ro, 'read_vbios', lambda gpu: active_image if state['active'] else image)
    monkeypatch.setattr(live.ro, 'compose_records', lambda *args: {'romOverride00': {'type': 3, 'value': 'aa'}})
    monkeypatch.setattr(live, '_variable_info', lambda gpu, target: variables)
    monkeypatch.setattr(live, '_voltage', lambda gpu: copy.deepcopy(active if state['active'] else baseline))
    monkeypatch.setattr(live.ceiling, 'require_restartable_request', lambda obs: None)
    monkeypatch.setattr(live.vb, 'inspect', lambda data: {'identity': {'vbios_version_raw': 1, 'oem_revision': 2}})
    monkeypatch.setattr(live.devicereset, 'resolve_target', lambda slot: SimpleNamespace(instance_id='gpu'))
    monkeypatch.setattr(live.devicereset, 'restart_target', restart)
    real_plan = scaffold.plan

    def selective_plan(image, before, variables, selections):
        if mode == 'partial' and selections[-1] == (0, 'ALT'):
            raise scaffold.VoltageOffsetScaffoldError('unsupported root')
        return real_plan(image, before, variables, selections)

    monkeypatch.setattr(scaffold, 'plan', selective_plan)
    if mode == 'refuse':
        with pytest.raises(live.VoltageOffsetsError, match='changed'):
            live.enable('adapter', requested)
        assert not live._path('adapter').exists()
        assert not state['registry'] and not state['restarts']
        return
    result = live.enable('adapter', None if mode in ('partial', 'capacity') else requested)
    assert result['values'] == {h['key']: 0 for h in heads}
    expected_unsupported = {
        'partial': ['voltage.0.ALT.offset'],
        'capacity': ['voltage.0.ALT.offset', 'voltage.0.OV.offset', 'voltage.0.VMIN.offset'],
    }
    assert result['unsupported'] == expected_unsupported.get(mode, [])
    assert state['restarts'] == 1 and state['registry']
    assert live.capture('adapter') == result['values']
    with pytest.raises(live.VoltageOffsetsError, match='already exists'):
        live.enable('adapter', requested)
