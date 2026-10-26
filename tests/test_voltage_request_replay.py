"""Offline public SET, full native inventory, and uncertain-write recovery data."""

import copy
import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import voltage_request_replay as replay
from tests.test_voltage_rail_control_runtime import policy


class Gpu:
    def __init__(self, rails=(0, 1), *, extra=(), **ident):
        self.policy_data = policy(rails, **ident)
        self.identity = self.policy_data["identity"]
        self.offsets = {str(i): [-12345 - i, 23456, -34567, 45678, -987654, 0x12345678] for i in rails}
        self.boost = 37
        self.info = bytearray(replay.INFO_SIZE)
        self.info[0], self.info[3], self.info[-1] = 255, 0xCF, 0xD9
        struct.pack_into("<I", self.info, 4, sum(1 << i for i in (*rails, *extra)))
        for i in (*rails, *extra):
            self.info[12 + i * 76] = 5
        self.native_opaque = 0xB4
        self.public_opaque = 0xD2
        self._lock = threading.RLock()
        self.pairing_error = ""
        self.calls, self.set_calls = [], []
        self.get_fault = self.native_fault = self.set_fault = None
        self.setter_available = True
        selection = {
            "slot": self.identity["slot"],
            "handle": 123456,
            "devid": self.identity["device_id"],
            "subsys": self.identity["subsystem_id"],
        }
        self.nvapi = SimpleNamespace(
            ok=True,
            gpu=123456,
            selected=selection,
            VoltRailsCtlGet=self.get,
            VoltCtrlGet=self.boost_get,
            _i=self.export,
        )
        self.nvml = SimpleNamespace(selected=dict(selection))
        envelope = [0] * 17
        for index, value in replay.clock._ENVELOPE.items():
            envelope[index] = value
        envelope[14], envelope[15], envelope[2] = 0x2080A612, 0x414, 68 + 0x414
        self._current_limit_transport = (
            tuple(envelope),
            {"hAdapter": 19, "hDevice": 23, "Type": 0, "Flags": 0, "hContext": 0},
        )

    def slot(self):
        return self.identity["slot"]

    def arch(self):
        return self.identity["architecture"]

    def _current_limit_transport_layouts(self):
        return ()

    def policy(self):
        result = copy.deepcopy(self.policy_data)
        for index, row in result["rails"].items():
            row["control_type"] = 3
            row["offsets_uv"] = dict(zip(replay.voltage._OFFSETS, self.offsets[index][:4], strict=True))
            row["boost_uv"] += self.boost * 100
        return result, None

    def export(self, export, *args):
        assert export == replay.SET_EXPORT
        return self.set if self.setter_available else None

    def get(self, handle, pointer):
        assert handle == self.nvapi.gpu
        data = (ctypes.c_ubyte * replay.controls.SIZE).from_address(ctypes.cast(pointer, ctypes.c_void_p).value)
        version, mask = struct.unpack_from("<II", data)
        assert version == replay.controls.V2 and not any(data[8:])
        index = str(mask.bit_length() - 1)
        assert mask == 1 << int(index) and index in self.offsets
        self.calls.append(("public", index))
        data[8], data[9], data[-1] = self.boost, 0xA7, self.public_opaque
        at = 72 + int(index) * 84
        struct.pack_into("<I6i", data, at, 3, *self.offsets[index])
        data[at + 41] = 0xF2
        return self.get_fault(self, index, data) if self.get_fault else 0

    def boost_get(self, handle, pointer):
        data = (ctypes.c_ubyte * 40).from_address(ctypes.cast(pointer, ctypes.c_void_p).value)
        assert struct.unpack_from("<I", data)[0] == 0x10028
        data[4] = self.boost
        return 0

    def _legacy_clk_escape(self, packet, fields):
        command = packet[14]
        assert fields == self._current_limit_transport[1]
        assert command in (replay.INFO_COMMAND, replay.CONTROL_COMMAND)
        self.calls.append(("native", command))
        data = (ctypes.c_ubyte * packet[15]).from_address(ctypes.addressof(packet) + 68)
        if command == replay.INFO_COMMAND:
            ctypes.memmove(ctypes.addressof(data), bytes(self.info), len(self.info))
        else:
            mask = struct.unpack_from("<I", data, 4)[0]
            assert mask and not mask & ~sum(1 << int(i) for i in self.offsets)
            data[0], data[3], data[8], data[-1] = 255, 0xB8, self.boost, self.native_opaque
            for index, offsets in self.offsets.items():
                if not mask & (1 << int(index)):
                    continue
                at = 12 + int(index) * 32
                data[at], data[at + 1], data[at + 28], data[at + 29] = 5, 0xC8, 1, 0xE7
                struct.pack_into("<6i", data, at + 4, *offsets)
        packet[11] = 1
        return self.native_fault(self, command, data, packet) if self.native_fault else 0

    def set(self, handle, pointer):
        assert handle == self.nvapi.gpu
        data = (ctypes.c_ubyte * replay.controls.SIZE).from_address(ctypes.cast(pointer, ctypes.c_void_p).value)
        raw = bytes(data)
        self.set_calls.append(raw)
        version, mask = struct.unpack_from("<II", raw)
        assert version == replay.controls.V2
        expected = bytearray(len(raw))
        struct.pack_into("<II", expected, 0, version, mask)
        expected[8] = raw[8]
        for i in range(32):
            if mask & (1 << i):
                at = 72 + i * 84
                expected[at] = 3
                expected[at + 4 : at + 28] = raw[at + 4 : at + 28]
        assert raw == bytes(expected), "SET must not copy public/native GET-only opaque bytes"
        if self.set_fault:
            return self.set_fault(self, data)
        self.apply(data)
        return 0

    def apply(self, data, *, first_only=False):
        self.boost = data[8]
        for index in self.offsets:
            self.offsets[index] = list(struct.unpack_from("<6i", data, 72 + int(index) * 84 + 4))
            if first_only:
                break


@pytest.fixture(autouse=True)
def fake_policy(monkeypatch):
    monkeypatch.setattr(replay.voltage, "read", lambda gpu: gpu.policy())


def capture(gpu):
    state, error = replay.read(gpu)
    assert error is None
    return state


def test_noop_sets_once_and_keeps_all_requests_and_opaque_state():
    gpu = Gpu()
    state = capture(gpu)
    result = replay.replay(gpu, state)
    assert result["verified"] and result["attempted"] and result["status"] == 0
    assert result["before"] == result["after"] == state
    assert len(gpu.set_calls) == 1
    assert bytes.fromhex(state["native_hex"])[0] == 255 and state["complete"]
    assert set(state["rail_identity"]) == {"0", "1"}


def test_read_checked_returns_same_observation_and_holds_lock_for_capture():
    gpu = Gpu()
    expected = capture(gpu)
    ownership = []

    def check_lock(gpu, index, data):
        ownership.append(gpu._lock._is_owned())
        return 0

    gpu.get_fault = check_lock
    assert replay.read_checked(gpu) == expected
    assert ownership and all(ownership) and not gpu._lock._is_owned()


@pytest.mark.parametrize("kind", ["identity", "malformed", "unsupported", "transient"])
def test_read_checked_preserves_error_categories(kind):
    gpu = Gpu()
    if kind == "identity":
        gpu.nvapi.selected["handle"] += 1
    elif kind == "malformed":

        def bad_type(gpu, index, data):
            data[72 + int(index) * 84] = 4
            return 0

        gpu.get_fault = bad_type
    else:
        gpu.get_fault = lambda *args: -104 if kind == "unsupported" else -1
    with pytest.raises(replay.ReplayError) as error:
        replay.read_checked(gpu)
    assert error.value.kind == kind and not error.value.attempted
    assert not gpu._lock._is_owned() and not gpu.set_calls


def test_read_delegates_to_typed_entry_point(monkeypatch):
    expected = {"sentinel": object()}
    calls = []

    def checked(gpu):
        calls.append(gpu)
        return expected

    monkeypatch.setattr(replay, "read_checked", checked)
    gpu = object()
    assert replay.read(gpu) == (expected, None) and calls == [gpu]

    def failed(gpu):
        raise replay.ReplayError("identity", "Adapter pairing changed")

    monkeypatch.setattr(replay, "read_checked", failed)
    assert replay.read(gpu) == (None, "Adapter pairing changed")


def test_read_checked_converts_transport_setup_category(monkeypatch):
    gpu = Gpu()

    def setup_failure(gpu):
        raise replay.PerfDiagnosticError("unsupported", "Transport geometry unavailable")

    monkeypatch.setattr(replay, "PerfDiagnosticsReader", setup_failure)
    with pytest.raises(replay.ReplayError, match="Transport geometry") as error:
        replay.read_checked(gpu)
    assert error.value.kind == "unsupported" and isinstance(error.value.__cause__, replay.PerfDiagnosticError)
    assert not gpu._lock._is_owned()


@pytest.mark.parametrize("rails", [(0,), (1,), (0, 1)])
@pytest.mark.parametrize("arch", [4, 8, 9, 10])
def test_alternate_identity_missing_unrelated_rail_and_six_word_restore(rails, arch):
    gpu = Gpu(rails, architecture=arch, device_id=14, subsystem_id=0xDEADBEEF, slot="0000:03:00.1")
    baseline = capture(gpu)
    offsets = {str(i): [-(2**31), 2**31 - 1, 1024, -1024, -1048576, 1048576] for i in rails}
    desired = replay.project(baseline, offsets_uv=offsets, boost_percent=100)
    result = replay.replay(gpu, desired)
    assert result["verified"] and gpu.offsets == offsets and gpu.boost == 100
    restored = replay.replay(gpu, baseline)
    assert restored["after"] == baseline and len(gpu.set_calls) == 2
    assert {i for kind, i in gpu.calls if kind == "public"} == {str(i) for i in rails}


def test_new_handles_after_restart_are_reacquired():
    original = Gpu()
    saved = capture(original)
    fresh = Gpu()
    fresh.nvapi.gpu = fresh.nvapi.selected["handle"] = fresh.nvml.selected["handle"] = 987654
    fresh.offsets = {i: [0] * 6 for i in fresh.offsets}
    fresh.boost = 0
    result = replay.replay(fresh, saved)
    assert result["verified"] and fresh.offsets == original.offsets and fresh.boost == original.boost
    assert result["after"]["info_hex"] == saved["info_hex"]
    assert result["after"]["info_hex"] == result["before"]["info_hex"]


def test_full_info_uncaptured_rail_is_reported_but_never_replayed():
    gpu = Gpu((1,), extra=(9,))
    state = capture(gpu)
    assert not state["complete"] and state["missing_indices"] == [9]
    with pytest.raises(replay.ReplayError, match="every active") as error:
        replay.replay(gpu, state)
    assert not error.value.attempted and gpu.set_calls == []


def test_unsupported_peer_type_preserves_supported_read_only_capability():
    gpu = Gpu()
    gpu.info[12] = 4
    state = capture(gpu)
    assert set(state["public"]) == {"1"} and state["missing_indices"] == [0]
    assert not state["complete"]
    with pytest.raises(replay.ReplayError, match="every active"):
        replay.replay(gpu, state)
    assert not gpu.set_calls


@pytest.mark.parametrize("target", ["identity", "native_opaque", "public_opaque", "inventory", "setter", "info"])
def test_preflight_failure_never_attempts_set(target):
    gpu = Gpu()
    saved = capture(gpu)
    if target == "identity":
        saved["identity"]["device_id"] += 1
        saved["policy"]["identity"]["device_id"] += 1
    elif target == "native_opaque":
        gpu.native_opaque ^= 1
    elif target == "public_opaque":
        gpu.public_opaque ^= 1
    elif target == "inventory":
        struct.pack_into("<I", gpu.info, 4, 7)
    elif target == "info":
        gpu.info[30] ^= 1
    else:
        gpu.setter_available = False
    with pytest.raises(replay.ReplayError) as error:
        replay.replay(gpu, saved)
    assert not error.value.attempted and not gpu.set_calls


@pytest.mark.parametrize(
    "failure",
    [
        "ignored",
        "rounded",
        "partial_status",
        "exception",
        "input_mutation",
        "guard",
        "peer_opaque",
        "info",
        "identity",
        "bool_status",
    ],
)
def test_failed_or_partial_write_records_after_state_without_retry(failure):
    gpu = Gpu()
    baseline = capture(gpu)
    desired = replay.project(
        baseline, offsets_uv={"0": [1, 2, 3, 4, 5, 6], "1": [7, 8, 9, 10, 11, 12]}, boost_percent=1
    )

    def fault(gpu, data):
        if failure == "ignored":
            return 0
        gpu.apply(data, first_only=failure == "partial_status")
        if failure == "rounded":
            gpu.offsets["1"][5] += 1
        elif failure == "partial_status":
            return -1
        elif failure == "exception":
            raise RuntimeError("ambiguous return after write")
        elif failure == "input_mutation":
            data[10] ^= 1
        elif failure == "guard":
            ctypes.c_ubyte.from_address(ctypes.addressof(data) - 1).value = 0
        elif failure == "peer_opaque":
            gpu.native_opaque ^= 1
        elif failure == "info":
            gpu.info[30] ^= 1
        elif failure == "identity":
            gpu.nvapi.selected["handle"] += 1
        elif failure == "bool_status":
            return False
        return 0

    gpu.set_fault = fault
    with pytest.raises(replay.ReplayError) as error:
        replay.replay(gpu, desired)
    result = error.value.result
    assert error.value.attempted and result["attempted"] and not result["verified"]
    assert result["write_error"] or result["readback_error"]
    assert len(gpu.set_calls) == 1
    if failure != "identity":
        assert result["after"] is not None


def test_nonzero_status_remains_failure_even_if_all_requests_applied():
    gpu = Gpu()
    saved = capture(gpu)

    def fail_after_all(gpu, data):
        gpu.apply(data)
        return -104

    gpu.set_fault = fail_after_all
    with pytest.raises(replay.ReplayError) as error:
        replay.replay(gpu, saved)
    assert error.value.result["after"] == saved
    assert error.value.result["write_error"] and not error.value.result["verified"]


def test_interruption_still_performs_readback_then_propagates():
    gpu = Gpu()
    saved = capture(gpu)
    count = []

    def interrupt(gpu, data):
        count.append(len(gpu.calls))
        raise KeyboardInterrupt

    gpu.set_fault = interrupt
    with pytest.raises(KeyboardInterrupt) as error:
        replay.replay(gpu, saved)
    assert len(gpu.set_calls) == 1 and len(gpu.calls) > count[0]
    assert error.value.attempted and error.value.result["after"] == saved


@pytest.mark.parametrize("field", ["architecture", "device_id", "subsystem_id"])
def test_identity_does_not_accept_equal_float_values(field):
    gpu = Gpu()
    saved = capture(gpu)
    saved["identity"][field] = float(saved["identity"][field])
    with pytest.raises(replay.ReplayError):
        replay.replay(gpu, saved)
    assert not gpu.set_calls


@pytest.mark.parametrize("failure", ["guard", "envelope", "mask", "type", "offset", "boost", "identity"])
def test_malformed_native_readback_fails_before_write(failure):
    gpu = Gpu()

    def native_fault(gpu, command, data, packet):
        if command != replay.CONTROL_COMMAND:
            return 0
        if failure == "guard":
            ctypes.c_ubyte.from_address(ctypes.addressof(packet) - 1).value = 0
        elif failure == "envelope":
            packet[11] = 2
        elif failure == "mask":
            data[4] ^= 1
        elif failure == "type":
            data[12] = 4
        elif failure == "offset":
            data[16] ^= 1
        elif failure == "boost":
            data[8] += 1
        else:
            gpu.nvapi.selected["handle"] += 1
        return 0

    gpu.native_fault = native_fault
    state, error = replay.read(gpu)
    assert state is None and error and not gpu.set_calls


def test_transient_get_retries_reads_only_and_rejects_v1():
    gpu = Gpu()
    failures = [True]

    def transient(gpu, index, data):
        return -1 if failures.pop() else 0

    gpu.get_fault = lambda gpu, index, data: transient(gpu, index, data) if failures else 0
    assert capture(gpu)["complete"] and not gpu.set_calls
    gpu.get_fault = lambda *args: -9
    state, error = replay.read(gpu)
    assert state is None and error and not gpu.set_calls


@pytest.mark.parametrize("where", ["policy_public", "public_native", "boost"])
def test_sequential_request_race_gets_bounded_read_only_recovery(where):
    gpu = Gpu()
    faulted = []
    if where == "policy_public":

        def public_fault(gpu, index, data):
            if not faulted:
                faulted.append(True)
                gpu.offsets[index][0] += 1
                struct.pack_into("<i", data, 72 + int(index) * 84 + 4, gpu.offsets[index][0])
            return 0

        gpu.get_fault = public_fault
    else:

        def native_fault(gpu, command, data, packet):
            if command == replay.CONTROL_COMMAND and not faulted:
                faulted.append(True)
                if where == "boost":
                    gpu.boost += 1
                else:
                    gpu.offsets["0"][5] += 1
                    struct.pack_into("<i", data, 12 + 24, gpu.offsets["0"][5])
            return 0

        gpu.native_fault = native_fault
    assert capture(gpu)["complete"] and faulted and not gpu.set_calls


def test_read_only_native_helper_rejects_any_set_command_without_transport():
    gpu = Gpu()
    with pytest.raises(replay.ReplayError, match="read-only"):
        replay._native_get(gpu, object(), 0x2080F214, replay.CONTROL_SIZE)
    assert not gpu.calls and not gpu.set_calls


@pytest.mark.parametrize("value", [True, -1, 101, 1.5, "1"])
def test_invalid_boost_projection(value):
    with pytest.raises(replay.ReplayError):
        replay.project(capture(Gpu()), boost_percent=value)


@pytest.mark.parametrize("value", [[], [1] * 5, [1] * 7, [True] * 6, [2**31] * 6, [-(2**31) - 1] * 6, [1.0] * 6])
def test_invalid_six_word_projection(value):
    with pytest.raises(replay.ReplayError):
        replay.project(capture(Gpu()), offsets_uv={"0": value})


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "complete",
        "missing",
        "hex",
        "native_mask",
        "public_version",
        "type",
        "policy_type",
        "rail_id",
        "native_request",
        "boost",
        "extra",
    ],
)
def test_pure_tampered_observations_cannot_be_replayed(field):
    gpu = Gpu()
    saved = capture(gpu)
    if field == "schema":
        saved["schema"] = True
    elif field == "complete":
        saved["complete"] = 1
    elif field == "missing":
        saved["missing_indices"] = [True]
    elif field == "hex":
        saved["info_hex"] = saved["info_hex"].upper()
    elif field == "native_mask":
        raw = bytearray.fromhex(saved["native_hex"])
        raw[4] = 1
        saved["native_hex"] = raw.hex()
    elif field == "public_version":
        raw = bytearray.fromhex(saved["public"]["0"])
        struct.pack_into("<I", raw, 0, replay.controls.V1)
        saved["public"]["0"] = raw.hex()
    elif field in ("type", "policy_type"):
        saved["rail_identity"]["0"]["control_type" if field == "type" else "policy_type"] = True
    elif field == "rail_id":
        saved["public"][0] = saved["public"].pop("0")
    elif field == "native_request":
        raw = bytearray.fromhex(saved["native_hex"])
        raw[16] ^= 1
        saved["native_hex"] = raw.hex()
    elif field == "boost":
        saved["boost_percent"] += 1
    else:
        saved["extra"] = 1
    with pytest.raises(replay.ReplayError):
        replay.replay(gpu, saved)
    assert not gpu.set_calls
