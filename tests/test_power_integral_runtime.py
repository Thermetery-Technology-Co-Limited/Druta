import copy
import struct
import threading

import pytest

from druta.power_integral_runtime import (
    CONTROL,
    INFO,
    SET,
    STATUS,
    PowerIntegralController,
    PowerIntegralError,
    percent_to_raw,
    raw_to_percent,
)

LAYOUT = {
    "info": (204, 260),
    "control": (20, 196),
    "status": (156, 5936),
    "sizes": {INFO: 20512, CONTROL: 13876, STATUS: 397048, SET: 13876},
}
IDENTITY = {"slot": "0000:01:00.0", "architecture": 9, "device_id": 1, "subsystem_id": 2}


def words(raw):
    return list(struct.unpack(f"<{len(raw) // 4}I", raw))


class GPU:
    def __init__(self, index=4):
        self._lock = threading.RLock()
        self.layout = copy.deepcopy(LAYOUT)
        self.i = index
        info, control, status = bytearray(20512), bytearray(13876), bytearray(397048)
        struct.pack_into("<I", info, 4, 1 << index)
        struct.pack_into("<5I", control, 0, 0, 0, 0, 255, 1 << index)
        struct.pack_into("<I", status, 4, 1 << index)
        a, b, s = 204 + index * 260, 20 + index * 196, 156 + index * 5936
        info[a] = 0x40
        info[a + 4 : a + 7] = bytes((7, 3, 0))
        struct.pack_into("<III", info, a + 8, 10, 50, 100)
        info[a + 32 : a + 36] = bytes((2, 77, 3, 4))
        struct.pack_into("<HH", info, a + 36, 1000, 4300)
        struct.pack_into("<II", control, b, 7, 50)
        control[b + 8 : b + 12] = bytes((2, 3, 4, 91))
        struct.pack_into("<HH", control, b + 12, 1000, 4200)
        control[b + 21] = 123
        struct.pack_into("<II", status, s, 7, 50)
        self.info, self.control, self.status = map(words, (info, control, status))
        self.sets = 0
        self.suppress = False
        self.raise_after = False

    def _current_limit_abi(self):
        return self.layout

    def _current_limit_rm(self, command, params=None, policy_mask=None, **kwargs):
        if command == INFO:
            return self.info.copy()
        if command == CONTROL:
            return self.control.copy()
        if command == STATUS:
            return self.status.copy()
        assert command == SET
        self.sets += 1
        assert params[4] == 1 << self.i
        if not self.suppress:
            result = params.copy()
            result[4] = self.control[4]
            self.control = result
        if self.raise_after:
            self.raise_after = False
            raise ValueError("partial setter failure")


def controller(gpu):
    c = PowerIntegralController(gpu)
    c._binding = lambda: copy.deepcopy(IDENTITY)
    return c


def test_change_noop_and_preservation():
    gpu = GPU()
    c = controller(gpu)
    before = gpu.control.copy()
    row = c.read()["policies"][0]
    assert (row["policy"], row["default_upper_raw"], row["upper_raw"]) == (4, 4300, 4200)
    assert c.set_upper(4, 4200)["policies"][0]["upper_raw"] == 4200
    assert gpu.sets == 0
    assert c.set_upper(4, 4100, expected_identity=IDENTITY)["policies"][0]["upper_raw"] == 4100
    old, new = (
        bytearray(struct.pack(f"<{len(before)}I", *before)),
        bytearray(struct.pack(f"<{len(gpu.control)}I", *gpu.control)),
    )
    struct.pack_into("<H", old, 20 + 4 * 196 + 14, 4100)
    assert old == new


def test_failed_write_restores_and_partial_raise():
    gpu = GPU()
    c = controller(gpu)
    gpu.suppress = True
    with pytest.raises(PowerIntegralError) as e:
        c.set_upper(4, 4100)
    assert e.value.rollback["verified"]
    gpu.suppress = False
    gpu.raise_after = True
    with pytest.raises(PowerIntegralError) as e:
        c.set_upper(4, 4100)
    assert e.value.rollback["verified"]
    assert c.read()["policies"][0]["upper_raw"] == 4200


def test_refusals_and_decimal():
    gpu = GPU(1)
    c = controller(gpu)
    assert percent_to_raw("100") == 4096
    assert raw_to_percent(4097) == "100.0244140625"
    for bad in ("NaN", "Infinity", "-1", "99999999"):
        with pytest.raises(ValueError):
            percent_to_raw(bad)
    with pytest.raises(PowerIntegralError):
        c.set_upper(1, 999)
    with pytest.raises(PowerIntegralError):
        c.set_upper(1, 4100, expected_identity={})
    gpu.layout["control"] = (20, 192)
    with pytest.raises(PowerIntegralError):
        c.read()
    gpu.layout = copy.deepcopy(LAYOUT)
    at = (20 + 196 + 8) // 4
    gpu.control[at] = (gpu.control[at] & ~255) | 9
    assert not c.read()["policies"][0]["available"]
    gpu.control[at] = (gpu.control[at] & ~255) | 2
    gpu.control[(20 + 196) // 4] = 8
    assert not c.read()["policies"][0]["available"]


def test_optional_status_failure_does_not_disable_control():
    gpu = GPU(9)
    original = gpu._current_limit_rm

    def call(command, *args, **kwargs):
        if command == STATUS:
            raise ValueError("unavailable telemetry")
        return original(command, *args, **kwargs)

    gpu._current_limit_rm = call
    c = controller(gpu)
    row = c.read()["policies"][0]
    assert row["available"] and row["effective"] is None and row["pmu_output"] is None
    assert c.set_upper(9, 4250)["policies"][0]["upper_raw"] == 4250


def test_transient_get_retry_and_status_type_mismatch():
    gpu = GPU()
    original = gpu._current_limit_rm
    failures = [True]

    def call(command, *args, **kwargs):
        if command == INFO and failures:
            failures.pop()
            raise ValueError("temporary failure")
        return original(command, *args, **kwargs)

    gpu._current_limit_rm = call
    c = controller(gpu)
    assert c.read()["policies"][0]["available"]
    gpu.status[(156 + 4 * 5936) // 4] = 8
    assert not c.read()["policies"][0]["available"]


def test_exact_half_step_conversion():
    assert percent_to_raw("0.01220703125") == 1
    assert percent_to_raw("0.012207031249999999999999999999999999999") == 0
    assert percent_to_raw("1599.9755859375") == 65535


def test_multiple_policy_peer_bytes_and_drift_detection():
    gpu = GPU(4)
    peer = 7
    gpu.info[1] |= 1 << peer
    gpu.control[4] |= 1 << peer
    gpu.status[1] |= 1 << peer
    peer_offset = (20 + peer * 196) // 4
    gpu.control[peer_offset + 5] = 0xFEDCBA98
    c = controller(gpu)
    before = gpu.control.copy()
    assert c.set_upper(4, 4400)["policies"][0]["upper_raw"] == 4400  # above INFO default
    assert gpu.control[peer_offset : peer_offset + 49] == before[peer_offset : peer_offset + 49]
    original = gpu._current_limit_rm
    drift = [True]

    def call(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if command == SET and drift:
            drift.pop()
            gpu.control[peer_offset + 5] ^= 1
        return result

    gpu._current_limit_rm = call
    with pytest.raises(PowerIntegralError) as exc:
        c.set_upper(4, 4100)
    assert exc.value.kind == "readback"
    assert exc.value.rollback["verified"]
    assert gpu.control[peer_offset + 5] == before[peer_offset + 5] ^ 1
    assert c.read()["policies"][0]["upper_raw"] == 4400


def test_real_binding_uses_alternate_pci_identity_and_detects_change():
    from types import SimpleNamespace

    gpu = GPU(3)
    gpu.pairing_error = ""
    gpu.slot = lambda: "0000:72:1d.0"
    gpu.arch = lambda: 8
    selected = {"slot": gpu.slot(), "handle": 987, "devid": 0x1234, "subsys": 0x10203040}
    gpu.nvapi = SimpleNamespace(ok=True, gpu=987, selected=selected)
    gpu.nvml = SimpleNamespace(selected={key: selected[key] for key in ("slot", "devid", "subsys")})
    c = PowerIntegralController(gpu)
    identity = c.read()["identity"]
    assert identity == {"slot": "0000:72:1d.0", "architecture": 8, "device_id": 0x1234, "subsystem_id": 0x10203040}
    assert c.set_upper(3, 4500, expected_identity=identity)["policies"][0]["upper_raw"] == 4500
    gpu.nvapi.selected["handle"] = 988
    with pytest.raises(PowerIntegralError) as exc:
        c.read()
    assert exc.value.kind == "identity"


def test_invalid_default_block_is_not_offered():
    gpu = GPU()
    c = controller(gpu)
    default_offset = (204 + 4 * 260 + 32) // 4
    for value in (0, 2 | 3 << 16, 2 | 4 << 24):
        original = gpu.info[default_offset]
        gpu.info[default_offset] = value
        assert not c.read()["policies"][0]["available"]
        gpu.info[default_offset] = original
    bounds_offset = default_offset + 1
    gpu.info[bounds_offset] = 0
    assert not c.read()["policies"][0]["available"]
