# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Driver-derived current votes may saturate at the reported native maximum."""

import struct
from unittest.mock import Mock

import pytest

from druta import nvbackend as n
from tests.test_current_limits import fixture


def derived_gpu():
    gpu, state = fixture(n.GPU.ARCH_TURING)
    word = (20 + 13 * 124) // 4 + 1
    status_word = (112 + 13 * 5204) // 4 + 1
    state.control[word], state.dynamic[status_word] = 390360, 390000

    def observe():
        return {"binding": {"maximum_mw": 350000}, "role_info_hex": "0100000030",
                "requested_mw": 351000,
                "native_info_hex": struct.pack(f"<{len(state.info)}I", *state.info).hex(),
                "native_control_hex": struct.pack(f"<{len(state.control)}I", *state.control).hex()}, None

    gpu.read_live_power_request = Mock(side_effect=observe)
    return gpu, state, word, status_word


@pytest.mark.parametrize("target", [350000, 390000])
def test_verified_derived_vote_keeps_actual_clamped_current_visible_and_editable(target):
    gpu, state, word, _ = derived_gpu()
    row = gpu.get_current_limits()[0]
    assert row["requested_ma"] == 390360 and row["limit_ma"] == 390000
    assert not gpu.set_current_limit_ma(13, 390001)[0] and not state.writes
    ok, message = gpu.set_current_limit_ma(13, target)
    assert ok, message
    assert state.control[word] == target and len(state.writes) == 1
    assert gpu.get_current_limits()[0]["limit_ma"] == target


@pytest.mark.parametrize("damage", ["effective_below_max", "effective_above_max", "normal_board",
                                    "wrong_table", "wrong_control", "unknown_definition"])
def test_unexplained_or_unpaired_current_disagreement_still_prevents_writes(damage):
    gpu, state, _, status_word = derived_gpu()
    observed, _ = gpu.read_live_power_request()
    if damage == "effective_below_max":
        state.dynamic[status_word] -= 1
    elif damage == "effective_above_max":
        state.dynamic[status_word] += 1
    elif damage == "normal_board":
        observed["requested_mw"] = 350000
    elif damage == "wrong_table":
        observed["native_info_hex"] = "00"
    elif damage == "wrong_control":
        observed["native_control_hex"] = "00"
    else:
        observed["role_info_hex"] = "0100000040"
    gpu.read_live_power_request = Mock(return_value=(observed, None))
    assert not gpu.get_current_limits()
    assert not gpu.set_current_limit_ma(13, 350000)[0] and not state.writes
