# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verification uses available headroom without accepting ADC noise as a response."""
from collections import defaultdict
from unittest.mock import patch

import pytest

from druta.controllers import up9512r as u
from tests.controllers.test_up9512r import Bus


def controls(bus):
    return tuple(bus.regs[reg] for reg in u.CONTROL_REGISTERS)


def measured_trial(bus, *, baseline_mv=1000, qualifying_step=None):
    """Supply quantized ADC noise; only the requested larger rung has signal."""
    rail = u.UP9512R(bus)
    original = controls(bus)
    effective_first = (original[0] >> 4) * 10 if original[3] & 0x40 else 0
    counters = defaultdict(int)

    def feedback(reg, packet):
        if reg != 0x2D:
            return None
        raw = controls(bus)
        if raw == original or not raw[3] & 0x40:
            value = baseline_mv // 10
        else:
            step = (raw[0] >> 4) * 10 - effective_first
            # Low rungs alternate between baseline and one ADC quantum.
            # Their median shift cannot exceed their peak-to-peak noise.
            signal = 2 if qualifying_step is not None and step >= qualifying_step else 0
            value = baseline_mv // 10 + signal + counters[step] % 2
            counters[step] += 1
        packet.pbData[0] = value
        return 0

    bus.read_hook = feedback
    with patch("druta.controllers.up9512r.time.sleep"):
        result = rail.verify(acknowledged=True, operating_point=lambda: (0, 3000, 1438))
    assert controls(bus) == original
    assert rail._verification_restore_ok
    assert not rail._verification_restore_error
    return rail, result


@pytest.mark.parametrize("qualifying_step", (40, 50))
def test_noisy_small_rungs_reach_larger_measurable_response_then_restore(qualifying_step):
    bus = Bus()
    _, (ok, message, ladder) = measured_trial(bus, qualifying_step=qualifying_step)

    assert ok, message
    assert [row["offset_increment_mv"] for row in ladder] == list(range(10, qualifying_step + 1, 10))
    assert all(not row["moved"] and row["delta_mv"] <= row["response_noise_mv"]
               for row in ladder[:-1])
    hit = ladder[-1]
    assert hit["moved"]
    assert hit["delta_mv"] > hit["response_noise_mv"] == 10
    assert hit["reversal_mv"] > hit["response_noise_mv"]
    assert hit["restored_vout_mv"] == hit["baseline_mv"] == 1000
    assert len(hit["restored_samples_mv"]) == 25
    assert all(max(row["trial_control"]["offsets_mv"]) <= u.NORMAL_MAX_MV for row in ladder)


def test_noise_through_normal_limit_remains_inconclusive_and_restores():
    bus = Bus()
    _, (ok, message, ladder) = measured_trial(bus)

    assert not ok
    assert "INCONCLUSIVE" in message
    assert [row["offset_increment_mv"] for row in ladder] == [10, 20, 30, 40, 50]
    assert not any(row["moved"] for row in ladder)
    assert all(row["delta_mv"] <= row["response_noise_mv"] == 10 for row in ladder)
    # Enable once, retain the five-state request while climbing, disable once.
    assert [(reg, value) for reg, value in bus.writes if reg == 0x2A] == [(0x2A, 0x6C), (0x2A, 0x2C)]
    assert max(value for reg, value in bus.writes if reg == 0x0A) == 0x55


def test_voltage_ceiling_filters_larger_rungs_before_dispatch():
    bus = Bus()
    _, (ok, message, ladder) = measured_trial(bus, baseline_mv=1170)

    assert not ok
    assert "INCONCLUSIVE" in message
    assert [row["offset_increment_mv"] for row in ladder] == [10, 20, 30]
    assert all(row["baseline_mv"] + row["offset_increment_mv"] <= u.NORMAL_CEILING_MV
               for row in ladder)
    assert max(value for reg, value in bus.writes if reg == 0x0A) == 0x33


@pytest.mark.parametrize("initial, expected_steps", (
    ((0x23, 0x10, 0x2B, 0x6C), [10, 20]),
    ((0x44, 0x44, 0x4B, 0x6C), [10]),
))
def test_largest_existing_load_state_bounds_increment_not_just_first_state(initial, expected_steps):
    bus = Bus()
    bus.regs.update(dict(zip(u.CONTROL_REGISTERS, initial)))
    initial_offsets = u._decode(initial)["offsets_mv"]
    _, (ok, message, ladder) = measured_trial(bus)

    assert not ok
    assert "INCONCLUSIVE" in message
    assert [row["offset_increment_mv"] for row in ladder] == expected_steps
    for row in ladder:
        assert row["trial_control"]["offsets_mv"] == [v + row["offset_increment_mv"] for v in initial_offsets]
        assert max(row["trial_control"]["offsets_mv"]) <= u.NORMAL_MAX_MV
    assert not any(reg == 0x2A for reg, _ in bus.writes), "An already enabled entry need not be toggled"


def _cycling_feedback(bus, cycles):
    """Repeat each 25-sample cycle so any aligned window has the same median."""
    counters = defaultdict(int)
    seen_trial = {"yes": False}

    def feedback(reg, packet):
        if reg != 0x2D:
            return None
        raw = controls(bus)
        if raw[3] & 0x40:
            seen_trial["yes"] = True
            step = (raw[0] >> 4) * 10
            cycle = cycles[step]
        else:
            cycle = cycles["restore" if seen_trial["yes"] else "baseline"]
        key = ("restore" if seen_trial["yes"] and not raw[3] & 0x40
               else step if raw[3] & 0x40 else "baseline")
        packet.pbData[0] = cycle[counters[key] % len(cycle)] // 10
        counters[key] += 1
        return 0

    return feedback


def test_one_count_reversal_inside_baseline_band_confirms_issue_34():
    """A rise above peak-to-peak, then a smaller drop back inside the band, passes.

    Issue 34's failing run qualified at +40 mV (1100 -> 1140 against 30 mV
    variation) and then rejected the downward check. A restored median inside
    the baseline band, one or more ADC counts below the elevated median, is
    enough. The rise gate stays strict.
    """
    bus = Bus()
    rail = u.UP9512R(bus)
    original = controls(bus)
    spread = [1100] * 23 + [1090, 1120]
    bus.read_hook = _cycling_feedback(bus, {
        "baseline": spread,
        10: [1100] * 25,
        20: [1110] * 25,
        30: [1130] * 25,
        40: [1140] * 23 + [1130, 1160],
        "restore": [1120] * 23 + [1110, 1130],
    })
    with patch("druta.controllers.up9512r.time.sleep"):
        ok, message, ladder = rail.verify(
            acknowledged=True, operating_point=lambda: (0, 2820, 11501))

    assert ok, message
    assert [row["offset_increment_mv"] for row in ladder] == [10, 20, 30, 40]
    assert [row["moved"] for row in ladder] == [False, False, False, True]
    hit = ladder[-1]
    assert hit["delta_mv"] == 40 and hit["response_noise_mv"] == 30
    assert hit["reversal_mv"] == 20
    assert abs(hit["restored_vout_mv"] - hit["baseline_mv"]) <= 30
    assert hit["reversal_mv"] <= hit["response_noise_mv"]
    assert controls(bus) == original
    assert rail._verification_restore_ok


def test_noisy_restore_window_cannot_invent_a_downward_response():
    bus = Bus()
    rail = u.UP9512R(bus)
    original = controls(bus)
    bus.read_hook = _cycling_feedback(bus, {
        "baseline": [1100] * 23 + [1090, 1120],
        10: [1160] * 25,
        "restore": [1160] * 23 + [1100, 1200],
    })
    with patch("druta.controllers.up9512r.time.sleep"):
        ok, message, ladder = rail.verify(
            acknowledged=True, operating_point=lambda: (0, 2820, 11501))

    assert not ok
    assert "one ADC count" in message
    assert ladder[0]["moved"] and ladder[0]["reversal_mv"] == 0
    assert controls(bus) == original
    assert rail._verification_restore_ok


def test_first_qualifying_rung_stops_before_any_larger_request():
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    original = controls(bus)
    with patch("druta.controllers.up9512r.time.sleep"):
        ok, message, ladder = rail.verify(
            acknowledged=True, operating_point=lambda: (0, 3000, 1438))

    assert ok, message
    assert [row["offset_increment_mv"] for row in ladder] == [10]
    assert ladder[0]["delta_mv"] == ladder[0]["reversal_mv"] == 10
    assert bus.writes == [(0x0A, 0x11), (0x0B, 0x11), (0x0C, 0x1B), (0x2A, 0x6C),
                          (0x2A, 0x2C), (0x0A, 0), (0x0B, 0), (0x0C, 0x0B)]
    assert controls(bus) == original
    assert rail._verification_restore_ok
