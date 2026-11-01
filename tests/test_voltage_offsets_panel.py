"""Offline GUI callbacks for the app-owned policy offset controls."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import dearpygui.dearpygui as dpg
import pytest

from druta import voltage_offsets_cli
from druta import voltage_offsets_panel as panel


@pytest.fixture
def ui(monkeypatch, tmp_path):
    dpg.create_context()
    app = SimpleNamespace(gpu=SimpleNamespace(slot=lambda: '0000:01:00.0'), s=lambda value: value,
                          guard=Mock(return_value=True), log=Mock(), _closing=False, _stop=threading.Event(),
                          pick_file_native=Mock())
    instance = panel.VoltageOffsetsPanel(app)
    with dpg.window():
        instance.build()
    result = {'slot': '0000:01:00.0', 'values': {'voltage.0.REL.offset': -5000},
              'unsupported': ['voltage.1.OV.offset'],
              'policy': {'rails': {'0': {'policy_uv': {'reliability': 1040000, 'alt_reliability': 1060000,
                                                       'overvoltage': 1200000, 'vmin': 800000,
                                                       'effective': 1040000}}}}, 'saved_values': {}}
    read = Mock(return_value=result)
    write = Mock()
    monkeypatch.setattr(panel.offsets, 'read', read)
    monkeypatch.setattr(panel.offsets, 'set_offsets', write)
    monkeypatch.setattr(panel.paths, 'profile_dir', lambda: tmp_path)
    yield instance, app, read, write
    dpg.destroy_context()


def test_read_and_apply_signed_precise_value_for_selected_adapter(ui):
    instance, app, read, write = ui
    instance.refresh()
    assert dpg.get_value('voltage_offsets_value') == '-5'
    assert dpg.get_value('voltage_offsets_select') == 'Rail 0 REL'
    assert 'Rail 1 OV: unavailable' in dpg.get_value('voltage_offsets_inventory')
    assert 'Rail 0 reported REL: 1040 mV' in dpg.get_value('voltage_offsets_inventory')
    assert 'Rail 0 reported ALT: 1060 mV' in dpg.get_value('voltage_offsets_inventory')
    assert 'Rail 0 reported effective: 1040 mV' in dpg.get_value('voltage_offsets_inventory')
    dpg.set_value('voltage_offsets_value', '-1.234')
    instance.apply()
    write.assert_called_once_with('0000:01:00.0', {'voltage.0.REL.offset': -1234})
    assert read.call_count == 2
    dpg.set_value('voltage_offsets_value', '0.0001')
    instance.apply()
    assert write.call_count == 1
    assert 'whole-microvolt' in dpg.get_value('voltage_offsets_status')
    app.gpu = SimpleNamespace(slot=lambda: '0000:02:00.0')
    instance.apply()
    assert write.call_count == 1
    assert 'selected adapter' in dpg.get_value('voltage_offsets_status')


def test_enable_launches_helper_then_closes_gui(ui, monkeypatch):
    instance, app, _, _ = ui
    launch, stop = Mock(), Mock()
    monkeypatch.setattr(panel.devicereset, 'is_admin', lambda: True)
    monkeypatch.setattr(voltage_offsets_cli, 'launch_enable', launch)
    monkeypatch.setattr(dpg, 'stop_dearpygui', stop)
    instance.enable()
    launch.assert_called_once_with('0000:01:00.0')
    assert app._closing and app._stop.is_set()
    stop.assert_called_once()


def test_failed_helper_leaves_gui_open(ui, monkeypatch):
    instance, app, _, _ = ui
    monkeypatch.setattr(panel.devicereset, 'is_admin', lambda: True)
    monkeypatch.setattr(voltage_offsets_cli, 'launch_enable', Mock(side_effect=RuntimeError('launch failed')))
    stop = Mock()
    monkeypatch.setattr(dpg, 'stop_dearpygui', stop)
    instance.enable()
    assert not app._closing and not app._stop.is_set()
    assert not stop.called
    assert 'launch failed' in dpg.get_value('voltage_offsets_status')


def test_save_and_explicit_load_use_native_picker(ui, monkeypatch, tmp_path):
    instance, app, _, _ = ui
    path = tmp_path / 'offsets.json'
    app.pick_file_native.side_effect = [(str(path), None), (str(path), None)]
    capture = Mock(return_value={'voltage.0.REL.offset': -5000})
    replay = Mock()
    monkeypatch.setattr(panel.offsets, 'capture', capture)
    monkeypatch.setattr(panel.offsets, 'replay', replay)
    instance.save_profile()
    assert json.loads(path.read_text()) == {'schema': 1, 'slot': '0000:01:00.0',
                                             'values': {'voltage.0.REL.offset': -5000}}
    assert not replay.called
    instance.load_profile()
    replay.assert_called_once_with('0000:01:00.0', {'voltage.0.REL.offset': -5000})
    assert app.pick_file_native.call_count == 2


def test_profile_for_other_adapter_is_not_applied(ui, monkeypatch, tmp_path):
    instance, app, _, _ = ui
    path = tmp_path / 'wrong.json'
    path.write_text(json.dumps({'schema': 1, 'slot': '0000:02:00.0', 'values': {'voltage.0.REL.offset': 5000}}))
    app.pick_file_native.return_value = (str(path), None)
    replay = Mock()
    monkeypatch.setattr(panel.offsets, 'replay', replay)
    instance.load_profile()
    assert not replay.called
    assert 'selected adapter' in dpg.get_value('voltage_offsets_status')
