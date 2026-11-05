"""BIOS bounds must agree with independent public runtime metadata."""

import copy
import threading
from types import SimpleNamespace

import pytest

from druta import bios_runtime as runtime


def fake_gpu():
    rows = {
        0: SimpleNamespace(domain=0, kind=1, flags=1, delta=SimpleNamespace(minimum=-800000, maximum=1200000)),
        2: SimpleNamespace(domain=4, kind=0, flags=1, delta=SimpleNamespace(minimum=-1500000, maximum=2500000)),
    }
    gpu = SimpleNamespace(
        pairing_error=None,
        nvapi=SimpleNamespace(ok=True),
        _lock=threading.RLock(),
        _read_pstates20=lambda: (rows, None),
        _pstate20_clock=lambda info, ctype: info.get(ctype),
    )
    return gpu, rows


def inspection():
    # Non-local BIOS indices prove the binding uses decoded domain identity.
    return {
        "fields": [
            {"key": f"clock.{index}.{bound}", "domain": name, "units": "MHz", "current": value}
            for index, name, low, high in ((7, "GPC", -800, 1200), (5, "DRAM", -1500, 2500))
            for bound, value in (("minimum", low), ("maximum", high))
        ]
    }


def test_public_khz_units_and_independent_domains():
    gpu, rows = fake_gpu()
    observed, error = runtime.read_clock_ranges(gpu)
    assert error is None and observed["DRAM"]["maximum"] == 2500000
    rows[0].flags = 0
    observed, error = runtime.read_clock_ranges(gpu)
    assert set(observed) == {"DRAM"} and error is None


def test_pairing_and_bounded_retry():
    gpu, rows = fake_gpu()
    results = iter([(None, "temporary"), (rows, None)])
    gpu._read_pstates20 = lambda: next(results)
    assert runtime.read_clock_ranges(gpu)[0]["GPC"]
    gpu._read_pstates20 = lambda: (None, "still unavailable")
    assert runtime.read_clock_ranges(gpu) == ({}, "still unavailable")
    gpu.pairing_error = "wrong device"
    gpu._read_pstates20 = lambda: pytest.fail("Read on mismatched GPU")
    assert runtime.read_clock_ranges(gpu)[0] == {}


@pytest.mark.parametrize("bad", ["domain", "kind", "order"])
def test_invalid_peer_does_not_remove_understood_peer(bad):
    gpu, rows = fake_gpu()
    if bad == "order":
        rows[2].delta.minimum = 9000000
    else:
        setattr(rows[2], bad, 99)
    observed, _error = runtime.read_clock_ranges(gpu)
    assert set(observed) == {"GPC"}


def test_contract_checks_both_bounds_and_converts_memory_once():
    gpu, _rows = fake_gpu()
    changes = {"clock.5.maximum": 2601, "clock.5.minimum": -1200, "clock.7.maximum": 1201}
    contracts = runtime.clock_contracts(gpu, inspection(), changes)
    assert contracts["DRAM"]["after"] == {"minimum": -1200000, "maximum": 2601000}
    baseline = runtime.read_clock_ranges(gpu)[0]
    runtime.verify_clock_ranges({"clock_ranges": baseline}, contracts, before=True)
    active = copy.deepcopy(baseline)
    for name, contract in contracts.items():
        active[name].update(contract["after"])
    assert set(runtime.verify_clock_ranges({"clock_ranges": active}, contracts)) == set(changes)
    assert set(runtime.verify_clock_ranges({"clock_ranges": baseline}, contracts, restore=True)) == set(changes)
    with pytest.raises(runtime.BiosRuntimeError, match="bounds"):
        runtime.verify_clock_ranges({"clock_ranges": baseline}, contracts)


@pytest.mark.parametrize("bad", ["unchanged_bound", "units", "domain", "missing_peer"])
def test_contract_refuses_ambiguous_or_adapted_bios_bounds(bad):
    gpu, _rows = fake_gpu()
    fields = inspection()
    if bad == "unchanged_bound":
        fields["fields"][0]["current"] = -900
    elif bad == "missing_peer":
        fields["fields"].pop(0)
    else:
        fields["fields"][0][bad] = "other"
    with pytest.raises(runtime.BiosRuntimeError, match="differs"):
        runtime.clock_contracts(gpu, fields, {"clock.7.maximum": 1201})


@pytest.mark.parametrize("bad", ["missing", "identity", "unchanged_bound"])
def test_readback_checks_identity_and_unselected_bound(bad):
    gpu, _rows = fake_gpu()
    contracts = runtime.clock_contracts(gpu, inspection(), {"clock.7.maximum": 1201})
    observed = runtime.read_clock_ranges(gpu)[0]
    observed["GPC"]["maximum"] = 1201000
    if bad == "missing":
        observed.pop("GPC")
    elif bad == "identity":
        observed["GPC"]["domain"] = 4
    else:
        observed["GPC"]["minimum"] = -900000
    with pytest.raises(runtime.BiosRuntimeError):
        runtime.verify_clock_ranges({"clock_ranges": observed}, contracts)


def test_unrelated_fields_do_not_require_clock_api():
    assert runtime.clock_contracts(None, None, {"power.1.maximum": 999000}) == {}
    assert runtime.verify_clock_ranges({}, {}) == []


def thermal_gpu():
    import ctypes

    calls = []
    replies = {0: [(0, 97)], 1: [(0, 94)], 2: [(3, None)], 3: [(0, 86)]}

    def get(handle, index, value):
        calls.append(index)
        sequence = replies[index]
        reply = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if isinstance(reply, BaseException):
            raise reply
        code, reading = reply
        if code == 0:
            ctypes.cast(value, ctypes.POINTER(ctypes.c_uint))[0] = reading
        return code

    gpu = SimpleNamespace(
        pairing_error=None,
        _lock=threading.RLock(),
        nvml=SimpleNamespace(
            ok=True, has=lambda name: True, dev=123, dll=SimpleNamespace(nvmlDeviceGetTemperatureThreshold=get)
        ),
    )
    return gpu, calls, replies


def thermal_inspection(current=86):
    return {"fields": [{"key": "thermal.operating.maximum", "units": "C", "current": current}]}


def test_thermal_getter_retries_transient_and_preserves_independent_thresholds():
    gpu, calls, replies = thermal_gpu()
    replies[3] = [(999, None), (0, 86)]
    rows, error = runtime.read_thermal_thresholds(gpu)
    assert error is None and rows["gpu_max"]["degrees_c"] == 86
    assert rows["memory_max"] == {"status": 3, "degrees_c": None}
    assert calls.count(3) == 2 and calls.count(2) == 1


def test_thermal_exceptions_are_bounded_and_independent():
    gpu, calls, replies = thermal_gpu()
    replies[2] = [OSError("unavailable sensor")]
    replies[3] = [OSError("temporary"), (0, 86)]
    rows, error = runtime.read_thermal_thresholds(gpu)
    assert error is None and rows["gpu_max"] == {"status": 0, "degrees_c": 86}
    assert rows["memory_max"] == {"status": None, "degrees_c": None, "error": "unavailable sensor"}
    assert calls.count(2) == calls.count(3) == 2
    assert runtime.thermal_contracts(gpu, thermal_inspection(), {"thermal.operating.maximum": 85})


def test_thermal_reader_does_not_swallow_interrupts():
    gpu, _calls, replies = thermal_gpu()
    replies[2] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        runtime.read_thermal_thresholds(gpu)


def test_thermal_contract_and_restoration_preserve_other_supported_thresholds():
    gpu, _calls, _replies = thermal_gpu()
    contracts = runtime.thermal_contracts(gpu, thermal_inspection(), {"thermal.operating.maximum": 85})
    baseline, _error = runtime.read_thermal_thresholds(gpu)
    active = copy.deepcopy(baseline)
    active["gpu_max"]["degrees_c"] = 85
    assert runtime.verify_thermal_thresholds({"thermal_thresholds": active}, contracts) == ["thermal.operating.maximum"]
    runtime.verify_thermal_thresholds({"thermal_thresholds": baseline}, contracts, restore=True)
    active["shutdown"]["degrees_c"] = 98
    with pytest.raises(runtime.BiosRuntimeError, match="shutdown"):
        runtime.verify_thermal_thresholds({"thermal_thresholds": active}, contracts)


@pytest.mark.parametrize("value", [85.5, True, float("nan"), float("inf")])
def test_thermal_precision_is_not_silently_rounded(value):
    with pytest.raises(runtime.BiosRuntimeError, match="whole Celsius"):
        runtime.thermal_contracts(None, thermal_inspection(), {"thermal.operating.maximum": value})


def test_thermal_contract_rejects_unavailable_or_mismatched_metadata():
    gpu, _calls, replies = thermal_gpu()
    with pytest.raises(runtime.BiosRuntimeError, match="differs"):
        runtime.thermal_contracts(gpu, thermal_inspection(84), {"thermal.operating.maximum": 83})
    replies[3] = [(3, None)]
    with pytest.raises(runtime.BiosRuntimeError, match="unavailable"):
        runtime.thermal_contracts(gpu, thermal_inspection(), {"thermal.operating.maximum": 85})
    gpu.nvml.has = lambda name: False
    assert runtime.read_thermal_thresholds(gpu)[0] == {}


def test_unrelated_changes_do_not_require_thermal_api():
    assert runtime.thermal_contracts(None, None, {"clock.0.maximum": 2000}) == {}
    assert runtime.verify_thermal_thresholds({}, {}) == []
