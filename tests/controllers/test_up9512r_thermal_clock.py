# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Frequency is diagnostic; held-P0 voltage response and recovery determine Verify."""
from unittest.mock import patch

import pytest

from druta.controllers import up9512r as u
from tests.controllers.test_up9512r import Bus


def controls(bus):
    return tuple(bus.regs[reg] for reg in u.CONTROL_REGISTERS)


def setup():
    bus = Bus()
    bus.response = True
    rail = u.UP9512R(bus)
    rail.p.rungs = [10]
    return bus, rail


def verify(rail, point):
    with patch("druta.controllers.up9512r.time.sleep"):
        return rail.verify(acknowledged=True, operating_point=point)


@pytest.mark.parametrize("trial_clocks", ((3015, 1438), (9000, 25000), (250, 405)))
def test_core_and_memory_changes_do_not_gate_voltage_response(trial_clocks):
    bus, rail = setup()
    original = controls(bus)

    def point():
        return (0, *trial_clocks) if bus.regs[0x2A] & 0x40 else (0, 3000, 1438)

    ok, message, ladder = verify(rail, point)

    assert ok, message
    assert ladder[0]["delta_mv"] == ladder[0]["reversal_mv"] == 10
    assert ladder[0]["observed_core_clock_range_mhz"] == sorted((3000, trial_clocks[0]))
    assert controls(bus) == original
    assert rail._verification_restore_ok
    assert len(bus.writes) == 8


@pytest.mark.parametrize("clocks", ((None, None), (float("nan"), float("inf")),
                                   (-float("inf"), float("nan")), (0, -1), ("unavailable", None)))
def test_missing_or_unusable_frequency_telemetry_does_not_gate_voltage(clocks):
    bus, rail = setup()
    original = controls(bus)

    ok, message, ladder = verify(rail, lambda: (0, *clocks))

    assert ok, message
    assert ladder[0]["delta_mv"] == ladder[0]["reversal_mv"] == 10
    assert len(ladder[0]["restored_samples_mv"]) == 25
    assert controls(bus) == original
    assert rail._verification_restore_ok


@pytest.mark.parametrize("pstate", (2, None, float("nan"), False))
def test_loss_of_physical_p0_still_refuses_and_restores(pstate):
    bus, rail = setup()
    original = controls(bus)

    def point():
        return (pstate if bus.regs[0x2A] & 0x40 else 0, None, None)

    ok, message, _ = verify(rail, point)

    assert not ok
    assert "P0" in message
    assert controls(bus) == original
    assert rail._verification_restore_ok
    assert len(bus.writes) == 8


def test_held_voltage_callback_failure_still_refuses_and_restores():
    bus, rail = setup()
    original = controls(bus)

    def point():
        if bus.regs[0x2A] & 0x40:
            raise RuntimeError("exact voltage hold was replaced")
        return (0, None, None)

    ok, message, _ = verify(rail, point)

    assert not ok
    assert "exact voltage hold was replaced" in message
    assert controls(bus) == original
    assert rail._verification_restore_ok


def test_stable_frequencies_cannot_substitute_for_voltage_response():
    bus, rail = setup()
    bus.response = False
    original = controls(bus)

    ok, message, ladder = verify(rail, lambda: (0, 3000, 1438))

    assert not ok
    assert "INCONCLUSIVE" in message
    assert not ladder[0]["moved"]
    assert ladder[0]["delta_mv"] == 0
    assert controls(bus) == original
    assert rail._verification_restore_ok


def test_missing_frequencies_do_not_relax_required_voltage_reversal():
    bus, rail = setup()
    original = controls(bus)

    def stale_feedback(reg, packet):
        if reg == 0x2D and bus.writes:
            packet.pbData[0] = 94
            return 0

    bus.read_hook = stale_feedback
    ok, message, ladder = verify(rail, lambda: (0, None, None))

    assert not ok
    assert "INCONCLUSIVE" in message
    assert ladder[0]["moved"]
    assert ladder[0]["reversal_mv"] == 0
    assert controls(bus) == original
    assert rail._verification_restore_ok
