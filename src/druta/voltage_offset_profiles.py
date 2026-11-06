# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed, explicitly applied snapshots of installed policy offsets."""
import re

from . import vfe_equation_runtime as equations
from . import voltage_offsets_live as live

_KEY = re.compile(r"voltage\.(?:0|[1-9][0-9]*)\.(?:REL|ALT|OV|VMIN)\.offset\Z")


def capture(snapshot, selection=None):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('slot'), str) or not snapshot['slot']:
        raise ValueError('invalid voltage offset adapter slot')
    values = snapshot.get('values')
    if not isinstance(values, dict):
        raise ValueError('invalid voltage offset values')
    if selection is None:
        selected = list(values)
    else:
        if isinstance(selection, (str, bytes)):
            raise ValueError('invalid voltage offset selection')
        selected = list(selection)
        if len(set(selected)) != len(selected) or any(k not in values for k in selected):
            raise ValueError('selected voltage offset unavailable')
    data = {'slot': snapshot['slot'], 'values': {k: values[k] for k in selected}}
    _data(data)
    return data


def _data(data):
    if (not isinstance(data, dict) or set(data) != {'slot', 'values'}
            or not isinstance(data['slot'], str) or not data['slot']
            or not isinstance(data['values'], dict) or not data['values']):
        raise ValueError('invalid voltage offset profile')
    for key, value in data['values'].items():
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError('invalid voltage offset key')
        try:
            equations._uv(value)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError(f'invalid voltage offset value: {exc}') from exc
    return data


def validate(state):
    allowed = {'schema', 'scope', 'saved_at', 'saved_ts', 'device', 'incomplete', 'voltage_offsets'}
    if (not isinstance(state, dict) or type(state.get('schema')) is not int
            or state['schema'] != 2 or state.get('scope') != 'voltage_offsets' or set(state) - allowed):
        raise ValueError('unsupported voltage offset profile scope or fields')
    if not isinstance(state.get('incomplete', []), list) or any(
            not isinstance(x, str) for x in state.get('incomplete', [])):
        raise ValueError('invalid completeness metadata')
    return _data(state.get('voltage_offsets'))


def check_live(data, snapshot):
    current = capture(snapshot, data['values'])
    if current['slot'] != data['slot']:
        raise ValueError('voltage offset adapter slot changed')
    return current


def restore(gpu, data):
    results = []
    for key, value in data['values'].items():
        try:
            check_live(data, live.read(gpu.slot()))
            live.set_offsets(gpu.slot(), {key: value})
            results.append((True, f'{key} restored'))
        except Exception as exc:
            results.append((False, f'{key} restore failed: {exc}; remaining offsets not applied'))
            break
    return results
