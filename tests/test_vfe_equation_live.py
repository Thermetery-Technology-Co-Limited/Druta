"""Synthetic selected-row VFE writes; no driver or GPU access."""

import copy
import ctypes

import pytest

from druta import vfe_equation_runtime as vfe
from tests.test_vfe_equation_runtime import (
    Gpu,
    clamp_observation,
    clamp_selections,
    mutate,
    observation,
    selections,
)


class WriteGpu(Gpu):
    def __init__(self, obs, fault=None):
        super().__init__(obs)
        self.write_fault = fault
        self.writes = []

    def _legacy_clk_escape(self, packet, fields):
        assert packet[14] == 0x2080E0B7
        assert packet[15] == vfe.CONTROL_SIZE
        assert packet[2] == 68 + vfe.CONTROL_SIZE
        raw = bytes(packet)[68:]
        self.writes.append(raw)
        if self.write_fault == "guard":
            ctypes.c_ubyte.from_address(ctypes.addressof(packet) + 68 + vfe.CONTROL_SIZE).value = 0
        if self.write_fault != "ignored":
            original = bytearray.fromhex(self.observation["control_hex"])
            mask = int.from_bytes(raw[4 : 4 + vfe.MASK_BYTES], "little")
            for i in range(vfe.CAPACITY):
                if mask & (1 << i):
                    start = vfe.HEADER + i * vfe.CONTROL_STRIDE
                    original[start : start + vfe.CONTROL_STRIDE] = raw[start : start + vfe.CONTROL_STRIDE]
            self.observation["control_hex"] = original.hex()
        if self.write_fault == "transport":
            return 1
        if self.write_fault == "rm":
            packet[16] = 0x40
        elif self.write_fault == "routing":
            packet[14] ^= 1
        elif self.write_fault == "incomplete":
            return 0
        packet[5] = packet[11] = 1
        return 0


@pytest.fixture
def fixture(monkeypatch):
    before = clamp_observation(slot="0000:03:00.1", architecture=10, device_id=41, subsystem_id=0x11223344)
    saved = vfe.clamp_contracts(before, clamp_selections(before))
    gpu = WriteGpu(before)
    monkeypatch.setattr(vfe, "read", lambda target: (copy.deepcopy(target.observation), None))
    return gpu, saved


def test_apply_preserves_every_byte_except_selection_and_upper(fixture):
    gpu, saved = fixture
    entered = []
    result = vfe.write_upper_clamps(gpu, saved, on_attempt=lambda: entered.append(len(gpu.writes)))
    assert entered == [0] and result == saved["after"]
    expected = bytearray.fromhex(saved["after"]["control_hex"])
    expected[4 : 4 + vfe.MASK_BYTES] = bytes(vfe.MASK_BYTES)
    expected[4 + 37 // 8] = 1 << (37 % 8)
    assert gpu.writes == [bytes(expected)]


def test_restore_preserves_fresh_unselected_rows(fixture):
    gpu, saved = fixture
    gpu.observation = copy.deepcopy(saved["after"])
    peer = vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 31
    mutate(gpu.observation, "control", peer, 0x51)
    current = copy.deepcopy(gpu.observation)
    result = vfe.write_upper_clamps(gpu, saved, restore=True)
    expected = bytearray.fromhex(current["control_hex"])
    old = bytes.fromhex(saved["before"]["control_hex"])
    start = vfe.HEADER + 37 * vfe.CONTROL_STRIDE
    expected[start : start + vfe.CONTROL_STRIDE] = old[start : start + vfe.CONTROL_STRIDE]
    assert bytes.fromhex(result["control_hex"]) == expected
    assert result["identity"] == current["identity"] and len(gpu.writes) == 1


@pytest.mark.parametrize("fault", ["transport", "rm", "routing", "incomplete", "ignored", "guard"])
def test_failed_or_ignored_write_never_retries(fixture, fault):
    gpu, saved = fixture
    gpu.write_fault = fault
    marker = []
    with pytest.raises((vfe.VfeEquationError, vfe.PerfDiagnosticError)):
        vfe.write_upper_clamps(gpu, saved, on_attempt=lambda: marker.append(True))
    assert marker == [True] and len(gpu.writes) == 1


def test_preflight_failure_never_marks_or_writes(fixture):
    gpu, saved = fixture
    mutate(gpu.observation, "control", vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 31, 0x52)
    marker = []
    with pytest.raises(vfe.VfeEquationError, match="changed before apply"):
        vfe.write_upper_clamps(gpu, saved, on_attempt=lambda: marker.append(True))
    assert not marker and not gpu.writes


def test_restore_rejects_identity_and_info_changes(fixture):
    gpu, saved = fixture
    gpu.observation["identity"]["subsystem_id"] ^= 1
    with pytest.raises(vfe.VfeEquationError, match="target or INFO"):
        vfe.write_upper_clamps(gpu, saved, restore=True)
    assert not gpu.writes


def test_marker_failure_does_not_enter_transport(fixture):
    gpu, saved = fixture

    def fail():
        raise RuntimeError("journal unavailable")

    with pytest.raises(RuntimeError, match="journal unavailable"):
        vfe.write_upper_clamps(gpu, saved, on_attempt=fail)
    assert not gpu.writes


@pytest.fixture
def constant_fixture(monkeypatch):
    before = observation(slot="0000:04:00.2", architecture=7, device_id=42, subsystem_id=0xDEADBEEF)
    # Unknown selected-row bytes must survive the complete native row write.
    mutate(before, "control", vfe.HEADER + 37 * vfe.CONTROL_STRIDE + 0x37, 0xA5)
    saved = vfe.contracts(before, selections())
    gpu = WriteGpu(before)
    monkeypatch.setattr(vfe, "read", lambda target: (copy.deepcopy(target.observation), None))
    return gpu, saved


def test_constant_apply_preserves_packet_and_uses_singleton_mask(constant_fixture):
    gpu, saved = constant_fixture
    markers = []
    assert vfe.write_constants(gpu, saved, on_attempt=lambda: markers.append(len(gpu.writes))) == saved["after"]
    payload = bytearray.fromhex(saved["after"]["control_hex"])
    payload[4 : 4 + vfe.MASK_BYTES] = bytes(vfe.MASK_BYTES)
    payload[4 + 37 // 8] = 1 << (37 % 8)
    assert gpu.writes == [bytes(payload)] and markers == [0]


def test_constant_restore_preserves_current_peer_and_original_row(constant_fixture):
    gpu, saved = constant_fixture
    gpu.observation = copy.deepcopy(saved["after"])
    mutate(gpu.observation, "control", vfe.HEADER + 401 * vfe.CONTROL_STRIDE + 31, 0x51)
    expected = bytearray.fromhex(gpu.observation["control_hex"])
    original = bytes.fromhex(saved["before"]["control_hex"])
    start = vfe.HEADER + 37 * vfe.CONTROL_STRIDE
    expected[start : start + vfe.CONTROL_STRIDE] = original[start : start + vfe.CONTROL_STRIDE]
    result = vfe.write_constants(gpu, saved, restore=True)
    assert bytes.fromhex(result["control_hex"]) == expected and len(gpu.writes) == 1


@pytest.mark.parametrize("fault", ["transport", "rm", "routing", "incomplete", "ignored", "guard"])
def test_constant_failure_is_marked_and_never_retried(constant_fixture, fault):
    gpu, saved = constant_fixture
    gpu.write_fault = fault
    marker = []
    with pytest.raises((vfe.VfeEquationError, vfe.PerfDiagnosticError)):
        vfe.write_constants(gpu, saved, on_attempt=lambda: marker.append(True))
    assert marker == [True] and len(gpu.writes) == 1


def test_constant_rejects_wrong_type_contract_and_changed_identity(constant_fixture):
    gpu, saved = constant_fixture
    bad = copy.deepcopy(saved)
    mutate(bad["before"], "info", vfe.HEADER + 37 * vfe.INFO_STRIDE, 6)
    with pytest.raises(vfe.VfeEquationError, match="type3"):
        vfe.write_constants(gpu, bad)
    bad = copy.deepcopy(saved)
    bad["selected"]["voltage.1.ALT.addend"]["after_uv"] += 1
    with pytest.raises(vfe.VfeEquationError):
        vfe.write_constants(gpu, bad)
    gpu.observation["identity"]["device_id"] ^= 1
    with pytest.raises(vfe.VfeEquationError, match="target or INFO"):
        vfe.write_constants(gpu, saved, restore=True)
    assert not gpu.writes
