# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable Turing recovery primes the ABI and verifies the saved target."""

import importlib.util
from pathlib import Path

import pytest

from druta import client_power_runtime as cp
from tests.test_client_power_runtime import put
from tests.test_client_power_turing import TuringGpu


def validation_module():
    path = Path(__file__).resolve().parents[1] / "experiments" / "validate_turing_live_power.py"
    spec = importlib.util.spec_from_file_location("turing_validation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_watchdog_controller_discovers_geometry_before_exact_recovery():
    gpu = TuringGpu(native_index=31)
    put(gpu.native_control, 20 + gpu.peer * 124 + 4, 51000)
    original = cp.ClientPowerController(gpu)
    before = original.read()
    original.set_request(551000)
    watchdog = cp.ClientPowerController(gpu)
    assert watchdog.sizes[cp.SET] != len(bytes.fromhex(before["control_hex"]))
    restored = validation_module().restore_baseline(watchdog, before)
    assert restored["attempted"] and restored["verified"]
    assert cp._state(restored["readback"]) == cp._state(before)
    assert watchdog.sizes[cp.SET] == 392
    assert watchdog.sizes[cp.NATIVE_CONTROL] == 4432
    assert gpu.requested == gpu.original and len(gpu.set_payloads) == 2
    gpu.restore_peer.assert_called_once_with(gpu.peer, 51000)


@pytest.mark.parametrize("changed", ["device", "subsystem", "slot", "binding", "native_definition"])
def test_saved_identity_or_definition_mismatch_prevents_watchdog_set(changed):
    before = cp.ClientPowerController(TuringGpu()).read()
    kwargs = {
        "device": {"device": 0x1234},
        "subsystem": {"subsystem": 0xABCDEF01},
        "slot": {"slot": "0000:ab:1f.7"},
        "binding": {"native_index": 31},
        "native_definition": {},
    }[changed]
    gpu = TuringGpu(**kwargs)
    if changed == "native_definition":
        gpu.native_info[-1] ^= 1
    with pytest.raises(RuntimeError, match="Restoration target or definitions changed"):
        validation_module().restore_baseline(cp.ClientPowerController(gpu), before)
    assert gpu.requested == gpu.original
    assert not gpu.set_payloads
    gpu.restore_peer.assert_not_called()


def test_unreadable_fresh_geometry_prevents_watchdog_set():
    before = cp.ClientPowerController(TuringGpu()).read()
    gpu = TuringGpu()
    gpu.faults[cp.NATIVE_INFO, 8620, 1] = {"rm": 0x1F}
    with pytest.raises(cp.ClientPowerError):
        validation_module().restore_baseline(cp.ClientPowerController(gpu), before)
    assert not gpu.set_payloads
    gpu.restore_peer.assert_not_called()
