# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual snapshots of selected ordinary rail-limit requests."""
import math
import time

from . import tuning_profiles

FIELDS = ('reliability', 'alt_reliability', 'overvoltage', 'vmin')


def _selection(selection):
    if not isinstance(selection, dict) or not selection:
        raise ValueError('rail limit selection is empty or invalid')
    result = {}
    for rail, fields in selection.items():
        if type(rail) is int and rail in (0, 1):
            key = str(rail)
        elif type(rail) is str and rail in ('0', '1'):
            key = rail
        else:
            raise ValueError('invalid rail limit rail')
        if key in result or not isinstance(fields, (tuple, list, set, frozenset)) or not fields:
            raise ValueError('invalid or duplicate rail selection')
        if any(type(field) is not str or field not in FIELDS for field in fields) or len(set(fields)) != len(fields):
            raise ValueError('invalid or duplicate rail limit field')
        result[key] = tuple(field for field in FIELDS if field in fields)
    return result


def validate(state):
    allowed = {'schema', 'scope', 'saved_at', 'saved_ts', 'device', 'incomplete', 'rail_limits'}
    if (not isinstance(state, dict) or set(state) != allowed or type(state['schema']) is not int
            or state['schema'] != 2 or state['scope'] != 'rail_limits'):
        raise ValueError('invalid rail limit profile')
    # Reuse the ordinary manual-profile metadata contract and adapter matching.
    proxy = dict(state, scope='tuning_action', tuning_action={'kind': 'boost', 'index': None, 'value': 0})
    proxy.pop('rail_limits')
    tuning_profiles.validate(proxy)
    data = state['rail_limits']
    if not isinstance(data, dict):
        raise ValueError('invalid rail limit values')
    selected = _selection({rail: list(fields) if isinstance(fields, dict) else fields for rail, fields in data.items()})
    if any(type(rail) is not str for rail in data):
        raise ValueError('rail profile keys must be strings')
    for rail, fields in selected.items():
        if not isinstance(data[rail], dict):
            raise ValueError('invalid rail limit values')
        for field in fields:
            value = data[rail][field]
            if type(value) is not int or not -(1 << 31) <= value < (1 << 31):
                raise ValueError('invalid signed microvolt rail request')
    return data


def _read(gpu, selection=None, *, project=True):
    if not callable(getattr(gpu, 'set_volt_rail_limits_raw', None)):
        raise ValueError('rail limit setter unavailable')
    if selection is None:
        supported = {str(rail): tuple(gpu.volt_rail_limit_fields(rail) or ()) for rail in (0, 1)}
        selected = _selection({rail: fields for rail, fields in supported.items() if fields})
    else:
        selected = _selection(selection)
        supported = {rail: tuple(gpu.volt_rail_limit_fields(int(rail)) or ()) for rail in selected}
    for rail, fields in selected.items():
        if set(fields) - set(supported[rail]):
            raise ValueError(f'rail {rail} selected fields unavailable')
    records = gpu.read_volt_rail_limits()
    if not isinstance(records, dict):
        raise ValueError('rail limit readback unavailable')
    if project and callable(getattr(gpu, 'user_rail_limits', None)):
        records = gpu.user_rail_limits(records)
        if not isinstance(records, dict):
            raise ValueError('user rail limit readback unavailable')
    result = {}
    for rail, fields in selected.items():
        row = records.get(int(rail))
        if not isinstance(row, dict):
            raise ValueError(f'rail {rail} readback unavailable')
        result[rail] = {}
        for field in fields:
            value = row.get(field)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f'rail {rail} {field} readback unavailable')
            uv = value * 1000
            rounded = round(uv)
            if abs(uv - rounded) > 0.000001 or not -(1 << 31) <= rounded < (1 << 31):
                raise ValueError(f'rail {rail} {field} is not a signed microvolt request')
            result[rail][field] = rounded
    return result


def capture(gpu, selection=None):
    with gpu._lock:
        now = time.time()
        device = {k: gpu.static.get(k) for k in ('name', 'vbios', 'driver', 'uuid')}
        device['slot'] = tuning_profiles._slot(gpu)
        state = {'schema': 2, 'scope': 'rail_limits',
                 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
                 'saved_ts': now, 'device': device, 'incomplete': [],
                 'rail_limits': _read(gpu, selection)}
        validate(state)
        return state


def preflight(gpu, state):
    try:
        data = validate(state)
        if state['incomplete']:
            raise ValueError('rail limit snapshot is incomplete')
        if not gpu.volt_limits_write_enabled:
            raise ValueError('Enable Rail limits before loading this profile')
        error = tuning_profiles.device_error(gpu, state)
        if error:
            return error.replace('tuning action', 'rail limit')
        _read(gpu, {rail: tuple(fields) for rail, fields in data.items()})
        current = gpu.read_volt_rail_limits()
        if not isinstance(current, dict):
            raise ValueError('rail limit readback unavailable')
        for rail, fields in data.items():
            row = current.get(int(rail))
            if not isinstance(row, dict):
                raise ValueError(f'rail {rail} readback unavailable')
            for field, value in fields.items():
                current_value = gpu.abs_limit_mv(row, field)
                if type(current_value) not in (int, float) or not math.isfinite(current_value):
                    raise ValueError(f'rail {rail} {field} has no readable reference')
                wanted = current_value - row[field] + value / 1000
                maximum = (gpu.VOLT_LIMIT_XOC_MAX_MV if gpu.voltage_xoc_enabled
                           else gpu.VOLT_LIMIT_MAX_MV)
                maximum = max(maximum, current_value)
                minimum = min(gpu.VOLT_LIMIT_MIN_MV, current_value)
                initial = gpu.stock_limit_mv(int(rail), field)
                if type(initial) in (int, float) and math.isfinite(initial):
                    minimum = min(minimum, initial)
                if not minimum <= wanted <= maximum:
                    raise ValueError(f'rail {rail} {field} is outside current voltage bounds')
        return None
    except Exception as exc:
        return str(exc)


def restore(gpu, state):
    with gpu._lock:
        error = preflight(gpu, state)
        if error:
            return [(False, f'rail limits not applied: {error}')]
        results = []
        for rail, fields in validate(state).items():
            try:
                ok, message = gpu.set_volt_rail_limits_raw(int(rail), **fields)
                if ok:
                    actual = _read(gpu, {rail: tuple(fields)}, project=False)[rail]
                    if actual != fields:
                        ok, message = False, f'rail {rail} selected request readback mismatch: expected {fields}, got {actual}'
            except Exception as exc:
                ok, message = False, f'rail {rail} restore failed: {exc}'
            results.append((ok, message))
            if not ok:
                break
        return results
