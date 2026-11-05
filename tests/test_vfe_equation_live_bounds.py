"""Offline tests of the singleton native voltage-bound writer."""
import copy

import pytest

from druta import vfe_equation_runtime as vfe
from tests.test_vfe_equation_live import WriteGpu
from tests.test_vfe_equation_runtime import bounds_selections, clamp_observation, mutate


def test_bounds_apply_and_compensate_exact_rows(monkeypatch):
    before = clamp_observation(slot="0000:03:00.1", architecture=10, device_id=41, subsystem_id=22)
    saved = vfe.voltage_bounds_contracts(before, bounds_selections(before))
    gpu = WriteGpu(before)
    monkeypatch.setattr(vfe, "read", lambda target: (copy.deepcopy(target.observation), None))
    assert vfe.write_voltage_bounds(gpu, saved) == saved["after"]
    payload = bytearray.fromhex(saved["after"]["control_hex"])
    payload[4:4 + vfe.MASK_BYTES] = bytes(vfe.MASK_BYTES)
    payload[4 + 37 // 8] = 1 << (37 % 8)
    assert gpu.writes == [bytes(payload)]
    assert vfe.write_voltage_bounds(gpu, saved, restore=True) == before
    assert len(gpu.writes) == 2


def test_tampered_contract_and_preflight_refuse_without_dispatch(monkeypatch):
    before = clamp_observation()
    saved = vfe.voltage_bounds_contracts(before, bounds_selections(before))
    gpu = WriteGpu(before)
    monkeypatch.setattr(vfe, "read", lambda target: (copy.deepcopy(target.observation), None))
    bad = copy.deepcopy(saved)
    mutate(bad["after"], "control", vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 31, 0x51)
    with pytest.raises(vfe.VfeEquationError):
        vfe.write_voltage_bounds(gpu, bad)
    mutate(gpu.observation, "control", vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 31, 0x52)
    with pytest.raises(vfe.VfeEquationError):
        vfe.write_voltage_bounds(gpu, saved)
    assert not gpu.writes
