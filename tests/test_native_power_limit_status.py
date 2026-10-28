# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Configured/NVML power echo must not hide a lower native effective limit."""

import struct
import threading
from unittest.mock import Mock

import pytest

from druta.nvbackend import GPU


def native_gpu(index=2, value=360938):
    gpu = GPU.__new__(GPU)
    gpu._lock = threading.RLock()
    layout = {"info": (60, 132), "control": (20, 52), "status": (100, 2088)}
    info, control, status = [0] * 1380, [0] * 531, [0] * 17287
    meta, record, state = (60 + index * 132) // 4, (20 + index * 52) // 4, (100 + index * 2088) // 4
    info[1], info[meta + 1] = 1 << index, 0x0900
    control[record], control[record + 1] = 0, 400000
    status[state], status[state + 1] = 0, value
    observation = {"binding": {"native_index": index}, "requested_mw": 400000,
                   "native_info_hex": struct.pack(f"<{len(info)}I", *info).hex(),
                   "native_control_hex": struct.pack(f"<{len(control)}I", *control).hex()}
    gpu.read_live_power_request = Mock(return_value=(observation, None))
    gpu._power_policy_table = Mock(return_value=(layout, info, control, status))
    return gpu, observation, info, control, status, meta, record, state


@pytest.mark.parametrize("index", [0, 2, 31])
@pytest.mark.parametrize("value", [0, 7424, 350000, 360938, 412500])
def test_effective_value_comes_from_the_bound_native_status_including_zero(index, value):
    gpu, *_ = native_gpu(index, value)
    assert gpu.read_native_power_limit_mw() == (value, None)


@pytest.mark.parametrize("damage", ["info", "control", "mask", "unit", "control_type",
                                    "status_type", "request", "missing", "truncated"])
def test_unmatched_native_status_is_unavailable_instead_of_falling_back_to_configured(damage):
    gpu, obs, info, control, status, meta, record, state = native_gpu()
    if damage == "info":
        info[-1] = 1
    elif damage == "control":
        control[-1] = 1
    elif damage == "mask":
        info[1] = 0
    elif damage == "unit":
        info[meta + 1] |= 1 << 16
    elif damage == "control_type":
        control[record] = 3
    elif damage == "status_type":
        status[state] = 3
    elif damage == "request":
        obs["requested_mw"] = 399999
    elif damage == "missing":
        status[state + 1] = 0xFFFFFFFF
    else:
        del status[state:]
    value, error = gpu.read_native_power_limit_mw()
    assert value is None and error


def test_missing_binding_or_failed_native_get_never_reports_a_cached_limit():
    gpu, *_ = native_gpu()
    gpu.read_live_power_request.return_value = None, "read failed"
    assert gpu.read_native_power_limit_mw() == (None, "read failed")
    gpu._power_policy_table.assert_not_called()
    gpu.read_live_power_request.return_value = {"binding": {}}, None
    gpu._power_policy_table.side_effect = OSError("driver restarted")
    assert gpu.read_native_power_limit_mw() == (None, "driver restarted")
