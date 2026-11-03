# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manually applied collections of independently validated profile snapshots."""
import copy
import math
import time

from . import (
    clock_vf_profiles,
    lowpower_profiles,
    power_action_profiles,
    profiles,
    tuning_profiles,
    voltage_control_profiles,
    voltage_offset_profiles,
    voltage_request_profiles,
    voltage_requests_live,
)

_VALIDATORS = {
    'tuning_action': tuning_profiles.validate, 'power_action': power_action_profiles.validate,
    'clock_vf': clock_vf_profiles.validate, 'lowpower_requests': lowpower_profiles.validate,
    'voltage_controls': voltage_control_profiles.validate, 'voltage_offsets': voltage_offset_profiles.validate,
    'voltage_requests': voltage_request_profiles.validate, 'fan': profiles._validate_fan_scope,
}


def compose(named_states):
    now = time.time()
    state = {'schema': 2, 'scope': 'combined', 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
             'saved_ts': now, 'components': [{'name': name, 'state': copy.deepcopy(child)} for name, child in named_states]}
    validate(state)
    return state


def _targets(state):
    scope = state.get('scope')
    data = state.get(scope, {})
    if scope == 'tuning_action':
        return {('tuning', data['kind'], data['index'])}
    if scope == 'power_action':
        return {('power',)}
    if scope == 'fan':
        return {('fan',)}
    if scope == 'clock_vf':
        return {('clock_vf', k) for k in data['policies']}
    if scope == 'voltage_offsets':
        return {('voltage_offsets', k) for k in data['values']}
    if scope == 'voltage_requests':
        return {('rail', rail, field) for rail, row in data['rails'].items() for field in row['offsets_uv']}
    if scope == 'lowpower_requests':
        return {('lowpower', data['kind'], data['index'])}
    if scope == 'voltage_controls':
        kind = data['kind']
        targets = {('voltage_controls', kind, row.get('rail'), row.get('role'), row.get('index'))
                   for row in data['rows']}
        if kind in ('rel_bounds', 'constants'):
            targets.update(('native_equation', row['equation']) for row in data['rows'])
        return targets
    targets = set()
    if state.get('mem_off_units') is not None or state.get('mem_off_true_mhz') is not None:
        targets.add(('tuning', 'memory', None))
    if state.get('core_off_mhz') is not None or state.get('vf_deltas'):
        targets.add(('tuning', 'domain', 0))
    if state.get('volt_boost_pct') is not None:
        targets.add(('tuning', 'boost', None))
    for index in state.get('clock_domain_offsets_mhz', {}):
        targets.add(('tuning', 'domain', int(index)))
    if (state.get('power_limit_mw') is not None or 'power_policies' in state
            or 'power_policy_pins' in state or state.get('current_limits_ma')):
        targets.add(('power',))
    if state.get('fan_control_state') or state.get('fan_manual') is not None or state.get('fan_pct') is not None:
        targets.add(('fan',))
    for rail, fields in (state.get('rail_limits_uv') or state.get('rail_limits_mv') or {}).items():
        mapping = {'reliability': 'REL', 'alt_reliability': 'ALT/OP',
                   'overvoltage': 'OV', 'vmin': 'VMIN'}
        targets.update(('rail', str(rail), mapping.get(field, field)) for field in fields)
    return targets


def _order(state):
    scope = state.get('scope')
    if scope == 'voltage_controls':
        data = state[scope]
        if data['kind'] == 'gate':
            return 0 if data['rows'][0]['enabled'] else 100
        return 10
    return {'voltage_offsets': 10, 'voltage_requests': 30, 'clock_vf': 40,
            None: 50, 'lowpower_requests': 90}.get(scope, 60) if not (
                scope == 'tuning_action' and state[scope]['kind'] == 'boost') else 20


def validate(state):
    if (not isinstance(state, dict) or set(state) != {'schema', 'scope', 'saved_at', 'saved_ts', 'components'}
            or type(state['schema']) is not int or state['schema'] != 2 or state['scope'] != 'combined'
            or not isinstance(state['saved_at'], str) or not state['saved_at']
            or type(state['saved_ts']) not in (int, float) or not math.isfinite(state['saved_ts'])):
        raise ValueError('invalid combined profile')
    components = state['components']
    if not isinstance(components, list) or not components:
        raise ValueError('combined profile has no components')
    occupied = set()
    native, offsets, full_count = False, False, 0
    for item in components:
        if (not isinstance(item, dict) or set(item) != {'name', 'state'}
                or not isinstance(item['name'], str) or not item['name'].strip()
                or not isinstance(item['state'], dict)):
            raise ValueError('invalid combined profile component')
        child = item['state']
        scope = child.get('scope')
        full_count += scope is None
        if full_count > 1:
            raise ValueError('only one full tune is allowed')
        if scope == 'combined' or (scope is not None and scope not in _VALIDATORS):
            raise ValueError('unsupported or nested combined profile')
        if type(child.get('schema')) is not int or child['schema'] != 2:
            raise ValueError('combined components require schema 2')
        if scope is not None:
            _VALIDATORS[scope](child)
        if profiles.incomplete(child):
            raise ValueError(f"{item['name']}: incomplete profile")
        if scope == 'voltage_controls' and child[scope]['kind'] in ('rel_bounds', 'constants'):
            equations = [row['equation'] for row in child[scope]['rows']]
            if len(set(equations)) != len(equations):
                raise ValueError(f"{item['name']}: overlapping native equation targets")
        targets = _targets(child)
        if occupied & targets:
            raise ValueError(f"{item['name']}: overlapping profile targets")
        occupied.update(targets)
        native |= scope == 'voltage_controls' and child[scope]['kind'] in ('rel_bounds', 'constants')
        offsets |= scope == 'voltage_offsets'
    if native and offsets:
        raise ValueError('native REL/constants controls cannot be combined with voltage offsets')
    return sorted(components, key=lambda c: _order(c['state']))


def _child_preflight(app, child, projected_gate=False):
    if child.get('scope') == 'lowpower_requests':
        return lowpower_profiles.preflight(app, child)
    if child.get('scope') == 'voltage_requests' and projected_gate:
        try:
            data = voltage_request_profiles.validate(child)
            error = profiles.voltage_requests_device_error(child, app.gpu)
            if error:
                return error
            snapshot = voltage_requests_live.read(app.gpu)
            snapshot = dict(snapshot, gate_enabled=True)
            voltage_request_profiles.check_live(data, snapshot)
            return None
        except Exception as exc:
            return str(exc)
    return profiles.preflight(app.gpu, child, rail=app.rail)


def preflight(app, state):
    try:
        components = validate(state)
        enable = any(c['state'].get('scope') == 'voltage_controls'
                     and c['state']['voltage_controls']['kind'] == 'gate'
                     and c['state']['voltage_controls']['rows'][0]['enabled'] for c in components)
        for item in components:
            error = _child_preflight(app, item['state'], projected_gate=enable)
            if error:
                return f"{item['name']}: {error}"
        return None
    except Exception as exc:
        return str(exc)


def _capture(app, child):
    gpu, scope = app.gpu, child.get('scope')
    if scope is None:
        result = profiles.capture(gpu, app.rail)
        captured_board = result.get('power_limit_mw')
        # A full capture can expose controls absent from an older selected tune.
        # Undo only the fields the selected tune can actually write.
        for key in ('core_off_mhz', 'mem_off_units', 'mem_off_true_mhz', 'power_limit_mw',
                    'volt_boost_pct', 'fan_pct', 'fan_manual', 'fan_control_state',
                    'nvvdd_offset_mv', 'i2c'):
            if child.get(key) is None:
                result[key] = None
        if not child.get('vf_deltas'):
            result['vf_deltas'] = None
        # Board-limit writes can move policies and pins, so capture the full
        # coupled power state whenever the selected tune can change it.
        has_power = ('power',) in _targets(child)
        if has_power:
            result['power_limit_mw'] = captured_board
        for key in ('clock_domain_offsets_mhz', 'msvdd_offsets_mv', 'current_limits_ma',
                    'power_policies', 'power_policy_pins'):
            if has_power and key in ('current_limits_ma', 'power_policies', 'power_policy_pins'):
                continue
            if key in result:
                if key not in child and key in ('power_policies', 'power_policy_pins'):
                    result.pop(key)
                else:
                    result[key] = {k: v for k, v in result[key].items() if k in (child.get(key) or {})}
        for key in ('rail_limits_uv', 'rail_limits_mv'):
            if key in result:
                selected = child.get('rail_limits_uv' if 'rail_limits_uv' in child else 'rail_limits_mv') or {}
                result[key] = {rail: {field: value for field, value in fields.items()
                                      if field in selected.get(rail, {})}
                               for rail, fields in result[key].items() if rail in selected}
                result[key] = {rail: fields for rail, fields in result[key].items() if fields}
        return result
    if scope == 'fan':
        return profiles.capture_fan(gpu)
    if scope == 'tuning_action':
        data = tuning_profiles.validate(child)
        return profiles.capture_tuning_action(gpu, data['kind'], data['index'])
    if scope == 'power_action':
        return profiles.capture_power_action(gpu)
    if scope == 'lowpower_requests':
        data = lowpower_profiles.validate(child)
        return lowpower_profiles.capture(app, data['kind'], data['index'])
    if scope == 'clock_vf':
        return profiles.capture_clock_vf(gpu, selection=child[scope]['policies'])
    if scope == 'voltage_offsets':
        return profiles.capture_voltage_offsets(gpu, selection=child[scope]['values'])
    if scope == 'voltage_requests':
        live = profiles.capture_voltage_requests(gpu)
        target = child[scope]
        rows = live[scope]['rails']
        live[scope]['rails'] = {rail: {'identity': rows[rail]['identity'], 'offsets_uv': {
            field: rows[rail]['offsets_uv'][field] for field in row['offsets_uv']}}
            for rail, row in target['rails'].items()}
        return live
    data = voltage_control_profiles.validate(child)
    snapshot = voltage_control_profiles.controller(gpu, data['kind']).read()
    rows = []
    for row in data['rows']:
        kind = data['kind']
        selection = ((row['rail'], 'bounds' if 'lower_uv' in row else 'upper') if kind == 'rel_bounds'
                     else (row['rail'], row['role']) if kind == 'constants'
                     else row['index'] if kind == 'switch_delay' else None)
        rows.extend(voltage_control_profiles.capture(snapshot, kind, selection)['rows'])
    result = profiles.capture_voltage_controls(gpu, data['kind'], snapshot=snapshot, selection=selection)
    result[scope]['rows'] = rows
    return result


def capture_undo(app, state):
    error = preflight(app, state)
    if error:
        raise ValueError(error)
    captured = [(item['name'], _capture(app, item['state'])) for item in validate(state)]
    return compose(captured)


def restore(app, state):
    error = preflight(app, state)
    if error:
        return [(False, f'combined profile not applied: {error}')]
    results = []
    for item in validate(state):
        child = item['state']
        error = _child_preflight(app, child)
        if error:
            results.append((False, f"{item['name']}: {error}; remaining components not applied"))
            break
        try:
            result = (lowpower_profiles.restore(app, child) if child.get('scope') == 'lowpower_requests'
                      else profiles.restore(app.gpu, child, rail=app.rail, i2c_verified=app.i2c_verified()))
        except Exception as exc:
            result = [(False, str(exc))]
        results.extend((ok, f"{item['name']}: {message}") for ok, message in result)
        if not result or any(not ok for ok, _ in result):
            if not result:
                results.append((False, f"{item['name']}: no restore result"))
            break
    return results
