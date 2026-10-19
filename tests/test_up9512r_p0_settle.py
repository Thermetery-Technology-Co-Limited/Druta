# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Voltage-only settling retains physical P0 and exact hold ownership checks."""
from contextlib import contextmanager
from itertools import count
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from druta import gpuload


def gpu_with_readings(kind):
    ticks = count()
    state = SimpleNamespace(pstate=0, held=True, exited=False, hold_checks=0)

    def read():
        index = next(ticks)
        sample = {"pstate": state.pstate}
        if kind == "drifting":
            sample.update(core=1000 + 150 * index, mem=400 + 200 * index)
        elif kind == "nonfinite":
            sample.update(core=float("nan"), mem=float("inf"))
        elif kind == "stable":
            sample.update(core=3000, mem=1438)
        return sample

    def check_hold():
        state.hold_checks += 1
        if not state.held:
            raise RuntimeError("exact held voltage changed")

    @contextmanager
    def hold(**kwargs):
        try:
            yield check_hold
        finally:
            state.exited = True

    gpu = SimpleNamespace(read=Mock(side_effect=read), verification_p0=hold)
    return gpu, state


def settle(gpu, callback, **options):
    with patch.object(gpuload.time, "sleep"), \
            patch.object(gpuload.time, "perf_counter", side_effect=count(0, 0.1)):
        return gpuload.verify_in_p0(gpu, callback, settle_timeout=1, **options)


@pytest.mark.parametrize("kind", ("drifting", "missing", "nonfinite"))
def test_voltage_only_settles_with_drifting_or_unavailable_frequencies(kind):
    gpu, state = gpu_with_readings(kind)
    callback = Mock(side_effect=lambda point: point())

    result = settle(gpu, callback, require_stable_clocks=False)

    callback.assert_called_once()
    assert result[0] == 0
    assert state.exited
    assert state.hold_checks >= 4


@pytest.mark.parametrize("kind", ("drifting", "missing", "nonfinite"))
def test_generic_default_still_requires_readable_stable_frequencies(kind):
    gpu, state = gpu_with_readings(kind)
    callback = Mock()

    with pytest.raises(gpuload.LoadError, match="did not settle"):
        settle(gpu, callback)

    callback.assert_not_called()
    assert state.exited


def test_generic_default_can_still_run_at_stable_clocks():
    gpu, state = gpu_with_readings("stable")
    callback = Mock(side_effect=lambda point: point())

    assert settle(gpu, callback) == (0, 3000, 1438)
    callback.assert_called_once()
    assert state.exited


def test_voltage_only_does_not_settle_outside_p0():
    gpu, state = gpu_with_readings("missing")
    state.pstate = 2
    callback = Mock()

    with pytest.raises(gpuload.LoadError, match="did not settle"):
        settle(gpu, callback, require_stable_clocks=False)

    callback.assert_not_called()
    assert state.exited


def test_voltage_only_operating_callback_still_detects_replaced_hold():
    gpu, state = gpu_with_readings("missing")

    def callback(point):
        state.held = False
        return point()

    with pytest.raises(RuntimeError, match="exact held voltage changed"):
        settle(gpu, callback, require_stable_clocks=False)

    assert state.exited


def test_voltage_only_operating_callback_still_detects_p0_loss():
    gpu, state = gpu_with_readings("missing")

    def callback(point):
        state.pstate = 2
        return point()

    with pytest.raises(gpuload.LoadError, match="physical P0"):
        settle(gpu, callback, require_stable_clocks=False)

    assert state.exited
