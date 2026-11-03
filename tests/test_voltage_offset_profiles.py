"""Hardware-free scoped offset profiles."""
from types import SimpleNamespace

import pytest

from druta import profiles
from druta import voltage_offset_profiles as scoped
from druta import voltage_offsets_live as live
from tests.test_voltage_offsets_live import engine as scaffold_engine

__all__ = ["scaffold_engine"]

REL = 'voltage.0.REL.offset'
ALT = 'voltage.0.ALT.offset'
OV = 'voltage.0.OV.offset'

@pytest.fixture
def gpu():
    return SimpleNamespace(static={'uuid': 'gpu', 'slot': 'adapter'}, slot=lambda: 'adapter')

@pytest.fixture
def engine(monkeypatch):
    values = {REL: 1000, ALT: -5000}
    writes = []
    monkeypatch.setattr(live, 'read', lambda slot: {'slot': slot, 'values': dict(values)})
    def set_offsets(slot, selected):
        writes.append(dict(selected))
        values.update(selected)
    monkeypatch.setattr(live, 'set_offsets', set_offsets)
    return values, writes


def test_capture_selected_restore_and_autosave(gpu, engine, monkeypatch):
    values, writes = engine
    saved = profiles.capture_voltage_offsets(gpu, selection=[REL])
    assert saved['voltage_offsets']['values'] == {REL: 1000}
    values[REL] = 3000
    values[ALT] = 9000
    monkeypatch.setattr(profiles, 'autosave_state', lambda action, state: state)
    undo = profiles.autosave_voltage_offsets(gpu, 'load', saved)
    assert undo['voltage_offsets']['values'] == {REL: 3000}
    assert profiles.restore(gpu, saved)[0][0]
    assert writes == [{REL: 1000}] and values[ALT] == 9000
    assert 'manual apply only' in profiles.summarize(saved)


def test_refuses_wrong_target_and_missing_scaffold(gpu, engine, monkeypatch):
    saved = profiles.capture_voltage_offsets(gpu)
    gpu.static['uuid'] = 'other'
    assert 'UUID' in profiles.preflight(gpu, saved)
    gpu.static['uuid'] = 'gpu'
    saved['voltage_offsets']['slot'] = 'other'
    assert 'slot' in profiles.preflight(gpu, saved)
    saved['voltage_offsets']['slot'] = 'adapter'
    monkeypatch.setattr(live, 'read', lambda slot: (_ for _ in ()).throw(live.VoltageOffsetsError('No scaffold')))
    assert 'No scaffold' in profiles.preflight(gpu, saved)
    assert engine[1] == []


def test_partial_failure_stops_remaining(gpu, engine, monkeypatch):
    engine[0][OV] = 7000
    saved = profiles.capture_voltage_offsets(gpu)
    calls = []
    def fail_second(slot, selected):
        calls.append(selected)
        if ALT in selected:
            raise RuntimeError('write failed')
    monkeypatch.setattr(live, 'set_offsets', fail_second)
    result = profiles.restore(gpu, saved)
    assert [ok for ok, _ in result] == [True, False]
    assert calls == [{REL: 1000}, {ALT: -5000}]


@pytest.mark.parametrize('key,value', [('voltage.00.REL.offset', 0), ('voltage.0.BAD.offset', 0),
                                      (REL, True), (REL, 16777217)])
def test_invalid_profile_values(gpu, key, value):
    with pytest.raises(ValueError):
        profiles.capture_voltage_offsets(gpu, snapshot={'slot': 'adapter', 'values': {key: value}})


def test_driver_and_vbios_are_informational(gpu, engine):
    gpu.static.update(driver='old', vbios='old')
    saved = profiles.capture_voltage_offsets(gpu)
    gpu.static.update(driver='new', vbios='new')
    assert profiles.preflight(gpu, saved) is None


@pytest.mark.parametrize('where', ['outer', 'payload'])
def test_rejects_opaque_fields(gpu, engine, where):
    saved = profiles.capture_voltage_offsets(gpu)
    target = saved if where == 'outer' else saved['voltage_offsets']
    target['packet'] = 'deadbeef'
    with pytest.raises(ValueError):
        scoped.validate(saved)


def test_real_fake_scaffold_profile_roundtrip(gpu, request, monkeypatch, tmp_path):
    state, heads = request.getfixturevalue("scaffold_engine")
    rel, alt = (h['key'] for h in heads)
    monkeypatch.setattr(profiles, 'DIR', str(tmp_path))
    saved = profiles.capture_voltage_offsets(gpu, selection=[rel])
    live.set_offsets('adapter', {rel: 5000, alt: -5000})
    name, _, missing = profiles.autosave_voltage_offsets(gpu, 'restore', saved)
    assert not missing
    undo = profiles.load(name)
    assert undo['voltage_offsets']['values'] == {rel: 5000}
    assert profiles.restore(gpu, saved) == [(True, f'{rel} restored')]
    assert live.read('adapter')['values'] == {rel: 0, alt: -5000}
    assert state['writes'] == [('REL', 5000), ('ALT', -5000), ('REL', 0)]
