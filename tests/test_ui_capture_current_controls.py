"""Current-control capture invokes reads only and captures their displayed results."""
from types import SimpleNamespace
from unittest.mock import Mock

from druta.ui_capture import UiCapture, _CURRENT_CONTROLS
from tests.test_ui_capture import FakeDpg, app


def test_current_control_frames_use_readbacks_and_restore_display(tmp_path):
    dpg, application = FakeDpg(), app(None)
    dpg.values.update(registry_header=False, reg_rel_live_header=False,
                      reg_constants_live_header=False, reg_rel_live_select='',
                      reg_constants_live_select='', voltage_requests_select='original rail',
                      gr_idle_threshold_value='original threshold',
                      voltage_offsets_select='original offset', voltage_offsets_value='-1.234')
    pending_rails = {f'voltage_requests_{index}_value': f'pending {index}' for index in range(6)}
    dpg.values.update(pending_rails)
    panel = SimpleNamespace(rel_live_choices={}, constants_live_choices={},
                            rel_live_stale=False, constants_live_stale=False,
                            refresh_rel_live=Mock(), refresh_constants_live=Mock(),
                            select_rel_live=Mock(), select_constants_live=Mock(),
                            apply_rel_live=Mock(side_effect=AssertionError('writer')),
                            apply_constants_live=Mock(side_effect=AssertionError('writer')))
    for name, header, attr, method, status, readback in _CURRENT_CONTROLS:
        dpg.values.update({header: False, status: 'not read', readback: ''})
        target = panel if attr is None else SimpleNamespace()
        if attr:
            setattr(panel, attr, target)
        def read(*args, _status=status, _readback=readback, _name=name, **kwargs):
            if _name == 'individual':
                index = kwargs['user_data']
                _status = f'individual_power_gating_{index}_status'
                _readback = f'individual_power_gating_{index}_readback'
            dpg.values[_status] = 'unavailable' if _name == 'difr' else 'read: ' + _name
            dpg.values[_readback] = ('VCRIT stored request; effective threshold not verified'
                                     if _name == 'rail-requests' else 'observed: ' + _name)
            if _name == 'rail-requests':
                dpg.values.update({tag: 'driver readback' for tag in pending_rails})
            elif _name == 'request-gate':
                dpg.values['reg_voltage_gate_value'] = False
            elif _name == 'vf-policies':
                dpg.values['reg_vf_live_monotonicity'] = True
                dpg.values['reg_vf_live_smoothening'] = False
            elif _name == 'voltage-offsets':
                dpg.values['voltage_offsets_select'] = 'refreshed offset'
                dpg.values['voltage_offsets_value'] = '5'
        setattr(target, method, Mock(side_effect=read))
        for writer in ('hold', 'enable', 'apply', 'release'):
            setattr(target, writer, Mock(side_effect=AssertionError('writer invoked')))
    for index in (0, 4, 10, 11, 12, 13, 14, 15):
        dpg.values[f'individual_power_gating_{index}_heading'] = f'Controller {index}'
    for index in (4, 10, 11, 12, 13, 14, 15):
        dpg.values[f'individual_power_gating_{index}_status'] = 'not read'
        dpg.values[f'individual_power_gating_{index}_readback'] = ''
    dpg.values['reg_voltage_gate_value'] = True
    dpg.values['reg_vf_live_monotonicity'] = False
    dpg.values['reg_vf_live_smoothening'] = True
    panel.voltage_requests_panel.select = Mock(
        side_effect=lambda: dpg.values.update({tag: 'selected readback' for tag in pending_rails}))
    panel.voltage_requests_panel.choices = {"original rail": 0}
    application._registry_panel = panel
    capture = UiCapture(dpg, application, tmp_path / 'capture')
    anchors = []
    for _ in range(400):
        if capture.live_scroll is not None:
            anchors.append(capture.live_scroll)
        if not capture.active:
            break
        dpg.frame += 1
        capture.tick()
    assert capture.manifest['state'] == 'complete'
    cases = {item['stage']: item for item in capture.manifest['captures'] if item['stage'].startswith('current-')}
    assert len(cases) == 21
    expected = {
        'current-voltage-offsets': ('voltage_offsets_header', 'voltage_offsets_inventory'),
        'current-vf-policies': ('reg_vf_live_header', 'reg_vf_live_monotonicity'),
        'current-rail-requests': ('voltage_requests_header', 'voltage_requests_inventory'),
        'current-request-gate': ('reg_voltage_gate_header', 'reg_voltage_gate_value'),
        'current-elpg': ('reg_gr_gating_live_header', 'gr_gating_live_readback'),
        'current-difr': ('reg_difr_live_header', 'difr_live_readback'),
        'current-elcg': ('reg_engine_clock_gating_header', 'engine_clock_gating_readback'),
        'current-blcg': ('reg_block_clock_gating_header', 'block_clock_gating_readback'),
        'current-slcg': ('reg_second_level_clock_gating_header', 'second_level_clock_gating_readback'),
        'current-idle-thresholds': ('reg_gr_idle_threshold_header', 'gr_idle_threshold_readback'),
        'current-adaptive-policy': ('reg_adaptive_power_header', 'adaptive_power_readback'),
        'current-pcie-speed': ('reg_pcie_speed_header', 'pcie_speed_readback'),
        'current-power-relationships': ('reg_power_arbitration_header', 'reg_relationships'),
    }
    for stage, (header, readback) in expected.items():
        frame = dpg.output_values[[x['stage'] for x in capture.manifest['captures']].index(stage)]
        assert frame[header] is True and frame[readback] == cases[stage]['current']['readback']
    for index in (0, 4, 10, 11, 12, 13, 14, 15):
        stage = f'current-individual-{index}'
        assert f'individual_power_gating_{index}_heading' in anchors
        frame = dpg.output_values[[x['stage'] for x in capture.manifest['captures']].index(stage)]
        assert frame['reg_individual_power_gating_header'] is True
        assert frame[f'individual_power_gating_{index}_readback'] == cases[stage]['current']['readback']
    assert 'VCRIT stored request' in cases['current-rail-requests']['current']['readback']
    assert cases['current-difr']['current']['status'] == 'unavailable'
    for name, header, attr, method, status, readback in _CURRENT_CONTROLS:
        if name != 'individual':
            item = cases['current-' + name]
            frame = dpg.output_values[[x['stage'] for x in capture.manifest['captures']].index(item['stage'])]
            assert frame[header] is True and frame[status] == item['current']['status']
        target = panel if attr is None else getattr(panel, attr)
        assert getattr(target, method).call_count == (8 if name == 'individual' else 1)
        for writer in ('hold', 'enable', 'apply', 'release'):
            getattr(target, writer).assert_not_called()
    panel.apply_rel_live.assert_not_called()
    panel.apply_constants_live.assert_not_called()
    assert dpg.values['voltage_requests_select'] == 'original rail'
    assert dpg.values['gr_idle_threshold_value'] == 'original threshold'
    assert dpg.values['reg_voltage_gate_value'] is True
    assert dpg.values['reg_vf_live_monotonicity'] is False
    assert dpg.values['reg_vf_live_smoothening'] is True
    assert dpg.values['voltage_offsets_select'] == 'original offset'
    assert dpg.values['voltage_offsets_value'] == '-1.234'
    assert {tag: dpg.values[tag] for tag in pending_rails} == pending_rails
    assert all(dpg.values[header] is False for _, header, *_ in _CURRENT_CONTROLS)


def test_missing_current_panels_are_recorded_as_skipped(tmp_path):
    dpg, application = FakeDpg(), app(None)
    dpg.values.update(registry_header=False, reg_rel_live_header=False,
                      reg_constants_live_header=False, reg_rel_live_select='', reg_constants_live_select='')
    application._registry_panel = SimpleNamespace()
    capture = UiCapture(dpg, application, tmp_path / 'missing')
    assert not capture.current_cases
    assert any('rail-requests: panel unavailable' in item for item in capture.manifest['skipped'])
    assert any('adaptive-policy: panel unavailable' in item for item in capture.manifest['skipped'])


def test_read_exception_is_recorded_without_a_writer():
    dpg = FakeDpg()
    dpg.values.update(registry_header=False, reg_block_clock_gating_header=False,
                      block_clock_gating_status='not read')
    target = SimpleNamespace(read=Mock(side_effect=RuntimeError('read failure')),
                             hold=Mock(side_effect=AssertionError('writer')))
    capture = UiCapture.__new__(UiCapture)
    capture.dpg = dpg
    capture.snapshot = {'headers': {'registry_header': False, 'reg_block_clock_gating_header': False}}
    capture.current_cases = {'current-blcg': ('reg_block_clock_gating_header', target, 'read',
                                               'block_clock_gating_status', 'block_clock_gating_readback', None)}
    capture.manifest = {'skipped': []}
    capture._stage_current('current-blcg')
    assert dpg.values['reg_block_clock_gating_header'] is True
    assert capture.live_scroll == 'reg_block_clock_gating_header'
    assert capture.manifest['skipped'] == ['current-blcg read failed: read failure']
    target.hold.assert_not_called()
