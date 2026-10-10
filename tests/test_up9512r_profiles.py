# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compound I2C profiles retain all load states and preflight before GPU writes."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import profiles
from druta.controllers.up9512r import UP9512R
from tests.test_tune_profiles import hardware


def setup():
    gpu, _, gpu_writes = hardware()
    api = SimpleNamespace(ok=True, gpu=17,
                          selected={"slot": "0000:03:00.0", "devid": 0x1234, "subsys": 0x9876})
    rail = UP9512R(api, port=7, addr7=0x31)
    values = {0x27: 0, 0x28: 0x2B, 0x0A: 0x12, 0x0B: 0x34,
              0x0C: 0x5A, 0x2A: 0x60, 0x39: 0x94, 0x2D: 100,
              0x2C: 10, 0x3D: 10}
    writes = []
    rail.read = Mock(side_effect=lambda reg, width=1: values.get(reg))
    def write(reg, value, width=1):
        writes.append((reg, value, width))
        values[reg] = value
        return True
    rail._raw_write = Mock(side_effect=write)
    capture = profiles.capture(gpu, rail)
    state = {"schema": profiles.SCHEMA, "device": capture["device"],
             "i2c": capture["i2c"], "xoc": False}
    return gpu, rail, values, writes, gpu_writes, state


def test_json_roundtrip_and_restore_retains_nonuniform_offsets_and_enable():
    gpu, rail, values, writes, _, state = setup()
    assert state["i2c"]["format"] == profiles.MULTI_STATE_OFFSET_FORMAT
    assert state["i2c"]["control"] == {
        "kind": "up9512r-offset-v1", "offsets_mv": [10, 20, 30, 40, 50], "enabled": True}
    assert "offset_mv" not in state["i2c"]
    saved = json.loads(json.dumps(state))
    values.update({0x0A: 0, 0x0B: 0, 0x0C: 0x0A, 0x2A: 0x20})
    result = profiles.restore(gpu, saved, apply_curve=False, rail=rail, i2c_verified=True)
    assert all(ok for ok, _ in result), result
    assert rail.capture_control() == saved["i2c"]["control"]
    assert values[0x0C] & 0x0F == 0x0A
    assert values[0x2A] & ~0x40 == 0x20
    assert all(reg in (0x0A, 0x0B, 0x0C, 0x2A) for reg, _, _ in writes)
    assert "LCS0-4 +10/+20/+30/+40/+50 mV, enabled" in profiles.summarize(saved)


@pytest.mark.parametrize("change", [
    lambda s: s["i2c"]["control"].update(offsets_mv=[0, 10]),
    lambda s: s["i2c"]["control"].update(offsets_mv=[True, 0, 0, 0, 0]),
    lambda s: s["i2c"]["control"].update(offsets_mv=[-10, 0, 0, 0, 0]),
    lambda s: s["i2c"]["control"].update(offsets_mv=[15, 0, 0, 0, 0]),
    lambda s: s["i2c"]["control"].update(offsets_mv=[160, 0, 0, 0, 0]),
    lambda s: s["i2c"]["control"].update(kind="absolute_vid"),
    lambda s: s["i2c"].update(offset_mv=10),
    lambda s: s["i2c"].pop("format"),
    lambda s: s["i2c"].update(addr7=0x32),
    lambda s: s["device"].update(uuid="another GPU"),
])
def test_bad_or_mismatched_profile_refuses_before_any_write(change):
    gpu, rail, _, writes, gpu_writes, state = setup()
    change(state)
    result = profiles.restore(gpu, state, apply_curve=False, rail=rail, i2c_verified=True)
    assert result and not result[0][0], result
    assert not writes and not gpu_writes


@pytest.mark.parametrize("register,value", [(0x39, 0x00), (0x39, None), (0x28, 0x2A),
                                          (0x2A, 0xE0), (0x2D, None)])
def test_live_write_readiness_blocks_entire_profile(register, value):
    gpu, rail, values, writes, gpu_writes, state = setup()
    state["core_off_mhz"] = 90
    values[0x0A] = 0  # The saved control needs a write.
    values[register] = value
    result = profiles.restore(gpu, state, apply_curve=False, rail=rail, i2c_verified=True)
    assert result and not result[0][0], result
    assert not writes and not gpu_writes


def test_verification_and_controller_format_are_required():
    gpu, rail, values, writes, gpu_writes, state = setup()
    values[0x0A] = 0  # The saved control needs a write.
    result = profiles.restore(gpu, state, apply_curve=False, rail=rail, i2c_verified=False)
    assert not result[0][0] and "verified" in result[0][1]
    rail.multi_state_offset = False
    result = profiles.restore(gpu, state, apply_curve=False, rail=rail, i2c_verified=True)
    assert not result[0][0] and "load-state" in result[0][1]
    assert not writes and not gpu_writes


def test_disabled_full_range_snapshot_is_not_lost_or_promoted_to_live_offset():
    gpu, rail, values, _, _, _ = setup()
    gpu.voltage_xoc_enabled = False
    gpu.read_rail_offset_mv.return_value = 0
    gpu.read_clk_domain_offsets.return_value = ({0: {"freq_khz": 0}, 2: {"freq_khz": 0},
                                                7: {"freq_khz": 0}}, None)
    values.update({0x0A: 0xFF, 0x0B: 0xFF, 0x0C: 0xFA, 0x2A: 0x20})
    state = profiles.capture(gpu, rail)
    assert state["i2c"]["control"]["offsets_mv"] == [150] * 5
    assert state["i2c"]["control"]["enabled"] is False
    assert state["xoc"] is False


@pytest.mark.parametrize("register,value", [(0x39, 0x00), (0x39, None), (0x2D, None)])
@pytest.mark.parametrize("verified", (False, True))
def test_matching_saved_control_writes_nothing_and_does_not_block_the_rest(register, value, verified):
    gpu, rail, values, writes, gpu_writes, state = setup()
    state["core_off_mhz"] = 90
    values[register] = value
    result = profiles.restore(gpu, state, apply_curve=False, rail=rail, i2c_verified=verified)
    assert all(ok for ok, _ in result), result
    assert any("already match" in message for _, message in result)
    assert not writes
    assert gpu_writes, "the rest of the profile still applies"


def test_live_change_after_the_match_check_still_requires_verification():
    gpu, rail, values, writes, gpu_writes, state = setup()
    state["core_off_mhz"] = 90
    rail.matches_control = Mock(side_effect=[True, True, False])
    result = profiles.restore(gpu, state, apply_curve=False, rail=rail, i2c_verified=False)
    assert rail.matches_control.call_count == 3
    assert not result[-1][0] and "verified" in result[-1][1]
    assert not writes and not gpu_writes


@pytest.mark.parametrize("lock", (0x00, 0x87))
def test_locked_controller_is_read_only_and_not_captured(lock):
    gpu, rail, values, _, _, _ = setup()
    values[0x39] = lock
    state = profiles.capture(gpu, rail)
    assert state["i2c"] is None
    assert not any("I2C" in error for error in profiles.incomplete(state))


def test_unreadable_lock_status_is_an_incomplete_capture():
    gpu, rail, values, _, _, _ = setup()
    values[0x39] = None
    state = profiles.capture(gpu, rail)
    assert state["i2c"] is None
    assert any("I2C" in error and "identity or lock register unreadable" in error
               for error in profiles.incomplete(state))


def test_changed_identity_capture_says_so():
    gpu, rail, values, _, _, _ = setup()
    values[0x28] = 0x2A
    state = profiles.capture(gpu, rail)
    assert state["i2c"] is None
    assert any("identity no longer matches" in error for error in profiles.incomplete(state))


def test_one_failed_identity_read_does_not_refuse_the_profile():
    gpu, rail, values, writes, gpu_writes, state = setup()
    state["core_off_mhz"] = 90
    # Only preflight's presence check reads the identity in this profile.
    rail.matches_control = Mock(return_value=True)
    reads = []
    real = rail.read.side_effect

    def read(reg, width=1):
        reads.append(reg)
        if reg == 0x27 and reads.count(0x27) == 1:
            return None
        return real(reg, width)
    rail.read.side_effect = read
    result = profiles.restore(gpu, state, apply_curve=False, rail=rail, i2c_verified=False)
    assert all(ok for ok, _ in result), result
    assert not writes and gpu_writes


def test_failed_compound_capture_is_explicitly_incomplete():
    gpu, rail, _, _, _, _ = setup()
    rail.capture_control = Mock(side_effect=OSError("read failed"))
    state = profiles.capture(gpu, rail)
    assert state["i2c"] is None
    assert any("I2C" in error and "NOT captured" in error for error in profiles.incomplete(state))
