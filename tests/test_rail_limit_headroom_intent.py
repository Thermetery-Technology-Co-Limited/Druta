# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit rail choices that equal an owned temporary hold raise."""
import copy

import pytest

from tests.test_hold_headroom import CARDS, FakeRails, bare_gpu, stock_row


def held_gpu():
    gpu = bare_gpu()
    arch, base, boost = CARDS['turing']
    rails = FakeRails(gpu, stock_row('turing'), arch=arch, second=stock_row('turing'))
    gpu.volt_limits_write_enabled = True
    gpu.stock_limit_mv = lambda rail, field: None
    ceiling = min(base[0] + boost, base[1], base[2])
    ok, message = gpu.apply_hold_headroom(ceiling, 25.0)
    assert ok, message
    assert {'reliability', 'alt_reliability'} <= set(gpu.hold_headroom_record()['written_uv'])
    return gpu, rails


@pytest.mark.parametrize('raw', [False, True])
def test_selected_hold_value_becomes_user_request_and_other_raise_releases(raw):
    gpu, rails = held_gpu()
    record = copy.deepcopy(gpu.hold_headroom_record())
    field, other = 'reliability', 'alt_reliability'
    held = record['written_uv'][field]
    assert held != record['prior_uv'][field]
    other_rail = copy.deepcopy(rails.rows[1])
    foreign = {'rail': 1, 'prior_uv': {field: 111}, 'written_uv': {field: 222}}
    gpu._foreign_hold_headroom = copy.deepcopy(foreign)
    if raw:
        ok, message = gpu.set_volt_rail_limits_raw(0, **{field: held})
    else:
        ok, message = gpu.set_volt_rail_limits(0, **{field: rails.absolute(field)})
    assert ok, message
    updated = gpu.hold_headroom_record()
    assert updated['prior_uv'][field] == held
    assert updated['prior_uv'][other] == record['prior_uv'][other]
    assert updated['input_uv'] == record['input_uv']
    assert updated['written_uv'] == record['written_uv']
    assert gpu._foreign_hold_headroom == foreign
    assert gpu.user_rail_limits()[0][field] * 1000 == held
    assert gpu.user_rail_limits()[0][other] * 1000 == record['prior_uv'][other]
    ok, message = gpu.restore_hold_headroom()
    assert ok, message
    assert rails.rows[0][field] * 1000 == held
    assert rails.rows[0][other] * 1000 == record['prior_uv'][other]
    assert rails.rows[1] == other_rail


def test_failed_verification_does_not_change_owned_record():
    gpu, rails = held_gpu()
    before = copy.deepcopy(gpu.hold_headroom_record())
    rails.corrupt_next_write = True
    ok, _ = gpu.set_volt_rail_limits(0, reliability=rails.absolute('reliability'))
    assert not ok
    assert gpu.hold_headroom_record() == before


def test_write_other_rail_does_not_change_owned_record():
    gpu, rails = held_gpu()
    before = copy.deepcopy(gpu.hold_headroom_record())
    ok, message = gpu.set_volt_rail_limits(1, reliability=rails.absolute('reliability', rail=1))
    assert ok, message
    assert gpu.hold_headroom_record() == before


def test_selected_stock_equal_to_hold_survives_release():
    gpu = bare_gpu()
    arch, _, _ = CARDS['turing']
    rails = FakeRails(gpu, stock_row('turing'), arch=arch)
    rails.rows[0]['reliability'] = -25.0
    rails.rows[0]['alt_reliability'] = -25.0
    gpu._volt_rail_profile = lambda: {
        'fields': {0: gpu.VOLT_LIMIT_FIELDS}, 'poweron': {0: [0, 0, 0, 0]}}
    ok, message = gpu.apply_hold_headroom(1068.75, 25.0)
    assert ok, message
    record = gpu.hold_headroom_record()
    assert record['written_uv']['reliability'] == 0
    assert record['written_uv']['alt_reliability'] == 0
    ok, message = gpu.reset_volt_rail_limits(0, ('reliability',))
    assert ok, message
    assert gpu.hold_headroom_record()['prior_uv'] == {
        'reliability': 0, 'alt_reliability': -25000}
    ok, message = gpu.restore_hold_headroom()
    assert ok, message
    assert rails.rows[0]['reliability'] == 0
    assert rails.rows[0]['alt_reliability'] == -25.0


def test_failed_stock_verification_preserves_hold_record():
    gpu, rails = held_gpu()
    gpu._volt_rail_profile = lambda: {
        'fields': {0: gpu.VOLT_LIMIT_FIELDS}, 'poweron': {0: [0, 0, 0, 0]}}
    before = copy.deepcopy(gpu.hold_headroom_record())
    rails.corrupt_next_write = True
    ok, _ = gpu.reset_volt_rail_limits(0, ('reliability',))
    assert not ok
    assert gpu.hold_headroom_record() == before
