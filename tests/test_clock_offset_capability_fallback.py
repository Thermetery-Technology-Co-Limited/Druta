# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Present NVML exports can refuse a capability; exercise the real P0 fallback."""

import ctypes
from types import SimpleNamespace

import pytest

from druta import nvbackend as n
from tests.test_legacy_offsets import gpu as legacy_gpu


def capability_gpu(*, ctype=2, status=3, get_status=0):
    gpu = legacy_gpu()
    gpu.static.update(name="Synthetic adapter", driver="unrelated", vbios="opaque")
    state = {"set_status": status, "get_status": get_status,
             "offset": 30 if ctype == 0 else 160, "writes": [], "reads": 0}

    def read(_handle, pointer):
        state["reads"] += 1
        if state["get_status"]:
            return state["get_status"]
        co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
        co.mn, co.mx, co.off = -2000, 6000, state["offset"]
        return 0

    def write(_handle, pointer):
        co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
        state["writes"].append((co.type, co.pstate, co.off))
        if state["set_status"] == 0:
            state["offset"] = co.off
        return state["set_status"]

    gpu.nvml = SimpleNamespace(
        ok=True, dev=object(), ver=n.Nvml.ver,
        has=lambda name: name in ("nvmlDeviceGetClockOffsets", "nvmlDeviceSetClockOffsets"),
        dll=SimpleNamespace(nvmlDeviceGetClockOffsets=read,
                            nvmlDeviceSetClockOffsets=write),
        errstr=lambda code: f"NVML status {code}")
    gpu._vf_delta_snapshot = lambda: None
    return gpu, state


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("ctype,requested,units,domain,khz", [
    (0, 61, 60, 0, 60000), (2, 12.5, 100, 4, 50000),
    (2, -0.125, -1, 4, -500), (2, 0, 0, 4, 0),
])
def test_not_supported_uses_validated_sparse_p0_request(version, ctype, requested,
                                                      units, domain, khz):
    gpu, state = capability_gpu(ctype=ctype)
    gpu.nvapi.version = version
    original = bytes(gpu.nvapi.info.pstates[1].clocks[0 if ctype == 0 else 1])
    ok, message = gpu.set_clock_offset(ctype, requested)
    assert ok, message
    assert "NVAPI Pstates20" in message
    assert state["writes"] == [(ctype, 0, units)]
    assert len(gpu.nvapi.writes) == 1
    sent = gpu.nvapi.writes[0]
    typ = n._Pstates20V1 if version == 1 else n._Pstates20V2
    expected = typ(version=sent.version, num_pstates=1, num_clocks=1)
    expected.pstates[0].clocks[0].domain = domain
    expected.pstates[0].clocks[0].delta.value = khz
    assert bytes(sent) == bytes(expected)
    assert bytes(gpu.nvapi.info.pstates[1].clocks[0 if ctype == 0 else 1]) == original


@pytest.mark.parametrize("ctype,requested", [(0, 60), (2, 12.5)])
def test_both_nvml_exports_returning_not_supported_still_reach_pstates(ctype, requested):
    gpu, state = capability_gpu(ctype=ctype, get_status=3)
    ok, message = gpu.set_clock_offset(ctype, requested)
    assert ok, message
    assert len(state["writes"]) == len(gpu.nvapi.writes) == 1


@pytest.mark.parametrize("status", [1, 2, 4, 6, 9, 10, 15, 999])
@pytest.mark.parametrize("ctype,requested", [(0, 60), (2, 12.5)])
def test_other_nvml_failures_never_dispatch_another_setter(status, ctype, requested):
    gpu, state = capability_gpu(ctype=ctype, status=status)
    ok, message = gpu.set_clock_offset(ctype, requested)
    assert not ok
    assert f"NVML status {status}" in message
    assert len(state["writes"]) == 1
    assert gpu.nvapi.writes == gpu.nvapi.reads == []


@pytest.mark.parametrize("ctype,requested", [(0, 60), (2, 12.5)])
def test_supported_nvml_keeps_precedence(ctype, requested):
    gpu, state = capability_gpu(ctype=ctype, status=0)
    ok, message = gpu.set_clock_offset(ctype, requested)
    assert ok, message
    assert len(state["writes"]) == 1
    assert gpu.nvapi.writes == gpu.nvapi.reads == []


@pytest.mark.parametrize("missing", ["api", "setter", "getter"])
def test_unsupported_nvml_cannot_invent_a_legacy_capability(missing):
    gpu, state = capability_gpu()
    if missing == "api":
        gpu.nvapi.ok = False
    elif missing == "setter":
        gpu.nvapi.Pstates20Set = None
    else:
        gpu.nvapi.Pstates20Get = None
    ok, _message = gpu.set_clock_offset(2, 12.5)
    assert not ok
    assert len(state["writes"]) == 1
    assert gpu.nvapi.writes == []


@pytest.mark.parametrize("invalid", ["editable", "bounds", "duplicate", "table"])
def test_fallback_validates_its_own_current_table_before_set(invalid):
    gpu, state = capability_gpu()
    p0 = gpu.nvapi.info.pstates[1]
    if invalid == "editable":
        p0.clocks[0].flags = 0
    elif invalid == "bounds":
        p0.clocks[0].delta.maximum = 49000
    elif invalid == "duplicate":
        p0.clocks[1].domain = 4
    else:
        gpu.nvapi.info.num_pstates = 17
    ok, _message = gpu.set_clock_offset(2, 12.5)
    assert not ok
    assert len(state["writes"]) == 1
    assert gpu.nvapi.writes == []


@pytest.mark.parametrize("failure", ["status", "dropped", "readback"])
def test_failed_fallback_has_exact_readback_and_never_retries(failure):
    gpu, state = capability_gpu()
    if failure == "status":
        gpu.nvapi.status = -6
    elif failure == "dropped":
        gpu.nvapi.echo = False
    else:
        reader = gpu.nvapi.Pstates20Get

        def fail_after_write(handle, pointer):
            return -6 if gpu.nvapi.writes else reader(handle, pointer)

        gpu.nvapi.Pstates20Get = fail_after_write
    ok, message = gpu.set_clock_offset(2, 12.5)
    assert not ok
    expected = {"status": "NVAPI status -6", "dropped": "reported",
                "readback": "readback failed"}[failure]
    assert expected in message
    assert len(state["writes"]) == len(gpu.nvapi.writes) == 1


def test_successful_nvml_write_with_unsupported_readback_does_not_fallback_set():
    gpu, state = capability_gpu(status=0)
    setter = gpu.nvml.dll.nvmlDeviceSetClockOffsets

    def write_then_lose_readback(handle, pointer):
        result = setter(handle, pointer)
        state["get_status"] = 3
        return result

    gpu.nvml.dll.nvmlDeviceSetClockOffsets = write_then_lose_readback
    ok, message = gpu.set_clock_offset(2, 12.5)
    assert not ok
    assert "reported" in message
    assert len(state["writes"]) == 1
    assert gpu.nvapi.writes == []


def curve_gpu(ctype=2):
    gpu, state = capability_gpu(ctype=ctype)
    saved = {2: -450000, 57: 120000}
    curve = dict(saved)
    gpu._vf_delta_snapshot = lambda: {"freq_div": 2, "deltas": dict(curve)}
    gpu.read_vf_curve = lambda: ([{"idx": i, "delta_khz": d}
                                 for i, d in curve.items()], None)

    def apply(deltas):
        curve.update(deltas)
        return True, "written"

    gpu.apply_vf_deltas = apply
    setter = gpu.nvapi.Pstates20Set

    def flatten(handle, pointer):
        result = setter(handle, pointer)
        curve.update(dict.fromkeys(curve, 0))
        return result

    gpu.nvapi.Pstates20Set = flatten
    return gpu, state, curve, saved


@pytest.mark.parametrize("ctype,requested,shift", [(0, 60, 60000), (2, 12.5, 0)])
def test_successful_fallback_keeps_curve_and_correct_domain_shift(ctype, requested, shift):
    gpu, _state, curve, saved = curve_gpu(ctype)
    ok, message = gpu.set_clock_offset(ctype, requested)
    assert ok, message
    assert curve == {i: d + shift for i, d in saved.items()}
    assert "V/F shape kept" in message


@pytest.mark.parametrize("failure", ["status", "dropped"])
def test_failed_core_fallback_restores_original_curve_without_shifting(failure):
    gpu, _state, curve, saved = curve_gpu(0)
    if failure == "status":
        gpu.nvapi.status = -6
    else:
        gpu.nvapi.echo = False
    ok, message = gpu.set_clock_offset(0, 60)
    assert not ok
    assert curve == saved
    assert "V/F shape kept" in message


def test_fallback_curve_restore_failure_is_reported():
    gpu, _state, curve, _saved = curve_gpu()
    gpu.apply_vf_deltas = lambda _deltas: (False, "synthetic restore failure")
    ok, message = gpu.set_clock_offset(2, 12.5)
    assert not ok
    assert set(curve.values()) == {0}
    assert "synthetic restore failure" in message


def test_unsupported_nvml_that_flattens_curve_is_recovered_when_legacy_refuses():
    gpu, state, curve, saved = curve_gpu()
    gpu.nvapi.info.pstates[1].clocks[0].flags = 0
    setter = gpu.nvml.dll.nvmlDeviceSetClockOffsets

    def flatten_before_refusal(handle, pointer):
        curve.update(dict.fromkeys(curve, 0))
        return setter(handle, pointer)

    gpu.nvml.dll.nvmlDeviceSetClockOffsets = flatten_before_refusal
    ok, message = gpu.set_clock_offset(2, 12.5)
    assert not ok
    assert "not editable" in message
    assert "V/F shape kept" in message
    assert curve == saved
    assert len(state["writes"]) == 1
    assert gpu.nvapi.writes == []


def test_reset_fallback_respects_preserve_curve_false():
    gpu, _state, curve, _saved = curve_gpu()
    ok, message = gpu.set_clock_offset(2, 0, preserve_curve=False)
    assert ok, message
    assert set(curve.values()) == {0}
