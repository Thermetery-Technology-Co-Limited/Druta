# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Selected ordinary tuning requests, in their exact native readback units."""
import math
import time


def _slot(gpu):
    slot = gpu.slot()
    if not isinstance(slot, str) or not slot:
        raise ValueError('adapter slot is unavailable')
    return slot


def _selection(kind, index):
    if kind not in ('memory', 'domain', 'boost', 'rail_offset'):
        raise ValueError('unknown tuning action kind')
    if kind == 'domain':
        if type(index) is not int or not 0 <= index < 32:
            raise ValueError('invalid clock control index')
    elif index is not None:
        raise ValueError('index is only valid for a clock domain')


def _integer(value, label, low=-(1 << 31), high=(1 << 31)-1):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'invalid {label}')
    return value


def validate(state):
    allowed = {'schema', 'scope', 'saved_at', 'saved_ts', 'device', 'incomplete', 'tuning_action'}
    if (not isinstance(state, dict) or set(state) - allowed or type(state.get('schema')) is not int
            or state['schema'] != 2 or state.get('scope') != 'tuning_action'):
        raise ValueError('unsupported tuning action profile scope or fields')
    if (not isinstance(state.get('saved_at'), str) or not state['saved_at']
            or type(state.get('saved_ts')) not in (int, float)):
        raise ValueError('invalid tuning action timestamp')
    try:
        if not math.isfinite(state['saved_ts']):
            raise ValueError('invalid tuning action timestamp')
    except OverflowError:
        raise ValueError('invalid tuning action timestamp') from None
    device = state.get('device')
    if (not isinstance(device, dict) or set(device) != {'name', 'vbios', 'driver', 'uuid', 'slot'}
            or not isinstance(device['slot'], str) or not device['slot']
            or any(device[k] is not None and not isinstance(device[k], str) for k in device)):
        raise ValueError('invalid tuning action device metadata')
    missing = state.get('incomplete')
    if not isinstance(missing, list) or any(not isinstance(x, str) for x in missing):
        raise ValueError('invalid completeness metadata')
    data = state.get('tuning_action')
    if not isinstance(data, dict) or set(data) not in ({'kind', 'index', 'value'}, {'kind', 'index', 'value', 'scale'}):
        raise ValueError('invalid tuning action data')
    kind, index = data['kind'], data['index']
    _selection(kind, index)
    if (kind == 'memory') != ('scale' in data):
        raise ValueError('invalid tuning action fields')
    value = _integer(data['value'], 'tuning action value')
    if kind == 'memory':
        scale = data['scale']
        if type(scale) not in (int, float) or not 0 < scale < (1 << 31) or not math.isfinite(scale):
            raise ValueError('invalid memory scale')
        if (value / scale) * scale != value:
            raise ValueError('memory offset cannot be replayed exactly')
    elif kind == 'domain' and value % 1000:
        raise ValueError('clock domain offset cannot be replayed exactly in MHz')
    elif kind == 'boost':
        _integer(value, 'voltage boost request', 0, 100)
    return data


def device_error(gpu, state):
    saved = state['device']
    current = gpu.static.get('uuid')
    if saved['uuid'] and current and saved['uuid'] != current:
        return 'tuning action profile belongs to a different GPU UUID'
    if _slot(gpu) != saved['slot']:
        return 'tuning action profile belongs to a different adapter slot'
    return None


def _read(gpu, kind, index):
    _selection(kind, index)
    if kind == 'memory':
        nv, api = gpu.nvml, gpu.nvapi
        if not ((nv.ok and nv.has('nvmlDeviceSetClockOffsets')) or
                (api.ok and getattr(api, 'Pstates20Set', None))):
            raise ValueError('memory offset setter unavailable')
        bounds = gpu.clock_offset_range(2)
        if not isinstance(bounds, (tuple, list)) or len(bounds) < 3:
            raise ValueError('memory offset readback unavailable')
        value = _integer(bounds[2], 'memory offset')
        scale = gpu.mem_offset_scale()[0]
        return {'kind': kind, 'index': index, 'value': value, 'scale': scale}
    if kind == 'domain':
        if not (gpu.clkdom_ok() and getattr(gpu.nvapi, 'ClkDomCtlSet', None)
                and gpu.clkdom_layout() is not None and index in gpu.clkdom_domains()):
            raise ValueError('selected clock control unavailable')
        rows, error = gpu.read_clk_domain_offsets()
        if error or not isinstance(rows, dict) or index not in rows or not isinstance(rows[index], dict):
            raise ValueError(error or 'selected clock control readback unavailable')
        value = _integer(rows[index].get('freq_khz'), 'clock control offset')
        return {'kind': kind, 'index': index, 'value': value}
    if kind == 'rail_offset':
        if not (gpu.clkdom_ok() and getattr(gpu.nvapi, 'ClkDomCtlSet', None)):
            raise ValueError('NVVDD offset setter unavailable')
        capability = gpu.rail_offset_capability(rail=0, domain=0)
        mv = capability.get('value_mv')
        if not capability.get('available') or type(mv) not in (int, float) or not math.isfinite(mv):
            raise ValueError('NVVDD offset readback unavailable')
        uv = round(mv * 1000)
        if abs(mv * 1000 - uv) > 0.000001:
            raise ValueError('NVVDD offset is not representable in microvolts')
        return {'kind': kind, 'index': index, 'value': _integer(uv, 'NVVDD offset')}
    api = gpu.nvapi
    if not (api.ok and getattr(api, 'VoltCtrlGet', None) and getattr(api, 'VoltCtrlSet', None)):
        raise ValueError('voltage boost control unavailable')
    value = _integer(gpu.read_voltage_boost(), 'voltage boost request', 0, 100)
    return {'kind': kind, 'index': index, 'value': value}


def capture(gpu, kind, index=None):
    with gpu._lock:
        _selection(kind, index)
        now = time.time()
        device = {k: gpu.static.get(k) for k in ('name', 'vbios', 'driver', 'uuid')}
        device['slot'] = _slot(gpu)
        state = {'schema': 2, 'scope': 'tuning_action',
                 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
                 'saved_ts': now, 'device': device, 'incomplete': [],
                 'tuning_action': _read(gpu, kind, index)}
        validate(state)
        error = preflight(gpu, state)
        if error:
            raise ValueError(error)
        return state


def preflight(gpu, state):
    try:
        data = validate(state)
        if state['incomplete']:
            raise ValueError('tuning action snapshot is incomplete')
        error = device_error(gpu, state)
        if error:
            return error
        live = _read(gpu, data['kind'], data['index'])
        # Validate live state as well, including exact replay of the undo point.
        temp = dict(state, tuning_action=live)
        validate(temp)
        if data['kind'] == 'memory':
            scale = live['scale']
            request = data['value'] / scale
            if request * scale != data['value']:
                raise ValueError('saved memory offset cannot be replayed exactly')
            bounds = gpu.clock_offset_range(2)
            if (not isinstance(bounds, (tuple, list)) or len(bounds) < 3
                    or not bounds[0] <= data['value'] <= bounds[1]):
                raise ValueError('saved memory offset is outside the current control range')
            step = gpu.memory_offset_step_units() if callable(getattr(gpu, 'memory_offset_step_units', None)) else 1
            if type(step) is not int or step <= 0 or data['value'] % step:
                raise ValueError('saved memory offset is not representable by this control')
        elif data['kind'] == 'rail_offset':
            current = live['value'] / 1000
            xoc = bool(getattr(gpu, 'voltage_xoc_enabled', False))
            lower = min(-500 if xoc else -100, current)
            upper = max(gpu.RAIL_OFFSET_XOC_MAX_MV if xoc else gpu.RAIL_OFFSET_MAX_MV, current)
            if not lower <= data['value'] / 1000 <= upper:
                raise ValueError('saved NVVDD offset is outside the current control bounds')
        return None
    except Exception as exc:
        return str(exc)


def restore(gpu, state):
    with gpu._lock:
        error = preflight(gpu, state)
        if error:
            return [(False, f'tuning action not applied: {error}')]
        data = validate(state)
        try:
            if data['kind'] == 'memory':
                result = gpu.set_clock_offset(2, data['value'] / gpu.mem_offset_scale()[0])
            elif data['kind'] == 'domain':
                result = gpu.set_clk_domain_offset(data['index'], data['value'] // 1000)
            elif data['kind'] == 'rail_offset':
                result = gpu.set_rail_offset_mv(data['value'] / 1000, 0)
                if result[0] and _read(gpu, 'rail_offset', None)['value'] != data['value']:
                    return [(False, 'NVVDD offset request readback mismatch')]
            else:
                result = gpu.set_voltage_boost(data['value'])
                if result[0]:
                    back = gpu.read_voltage_boost()
                    if type(back) is not int or back != data['value']:
                        return [(False, f"voltage boost request readback mismatch: expected {data['value']}%, got {back!r}")]
            return [result]
        except Exception as exc:
            return [(False, f'tuning action restore failed: {exc}')]
