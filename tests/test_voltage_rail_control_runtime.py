"""Offline singleton rail CONTROL packets and exact opaque preservation."""

import copy
import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import voltage_rail_control_runtime as rawctl


def policy(rails=(0, 1), *, zero=False, **identity):
    ident = {"slot": "0000:74:1a.5", "architecture": 9, "device_id": 0xABCD, "subsystem_id": 0x12345678, **identity}
    result = {"identity": ident, "rails": {}}
    for rail in rails:
        result["rails"][str(rail)] = {
            "name": ("NVVDD", "MSVDD")[rail],
            "type": (1, 3)[rail],
            "control_type": 0 if zero else 47 + rail,
            "boost_uv": 5000,
            "policy_uv": {
                "reliability": 1050000,
                "alt_reliability": 1100000,
                "overvoltage": 1200000,
                "effective": 1055000,
                "vmin": 800000,
            },
            "offsets_uv": dict(
                zip(rawctl.voltage._OFFSETS, (0, 0, 0, 0) if zero else (-12345, 23456, -34567, 45678), strict=True)
            ),
        }
    return result


def payload(pol, index, version=rawctl.V2):
    raw = bytearray(rawctl.SIZE)
    struct.pack_into("<II", raw, 0, version, 1 << int(index))
    # Deliberately preserve nonzero unparsed data, including public byte+8.
    raw[8], raw[9], raw[71], raw[-1] = 73, 0xA4, 0xC6, 0xD8
    at = rawctl.ROW_OFFSET + int(index) * rawctl.ROW_STRIDE
    row = pol["rails"][str(index)]
    struct.pack_into("<I", raw, at, row["control_type"])
    values = [row["offsets_uv"][key] for key in rawctl.voltage._OFFSETS]
    struct.pack_into("<6i", raw, at + 4, *values, -987654, 0x12345678)
    raw[at + 41] = 0xF2
    return bytes(raw)


def observation(pol=None, versions=None):
    pol = policy() if pol is None else pol
    versions = versions or {}
    return {
        "identity": copy.deepcopy(pol["identity"]),
        "rails": {
            index: {
                "version": versions.get(index, rawctl.V2),
                "payload_hex": payload(pol, index, versions.get(index, rawctl.V2)).hex(),
            }
            for index in pol["rails"]
        },
    }


def mutate(state, index, offset, value, fmt="B"):
    raw = bytearray.fromhex(state["rails"][index]["payload_hex"])
    struct.pack_into("<" + fmt, raw, offset, value)
    state["rails"][index]["payload_hex"] = raw.hex()


class Gpu:
    def __init__(self, pol=None, *, versions=None, fault=None):
        self.policy = copy.deepcopy(policy() if pol is None else pol)
        self.identity = self.policy["identity"]
        self.versions = versions or {}
        self.fault = fault
        self.calls = []
        self._lock = threading.RLock()
        self.pairing_error = ""
        selected = {
            "slot": self.identity["slot"],
            "handle": 123456,
            "devid": self.identity["device_id"],
            "subsys": self.identity["subsystem_id"],
        }
        self.nvapi = SimpleNamespace(ok=True, gpu=123456, selected=selected, VoltRailsCtlGet=self.get)
        self.nvml = SimpleNamespace(selected=dict(selected))

    def slot(self):
        return self.identity["slot"]

    def arch(self):
        return self.identity["architecture"]

    def get(self, handle, pointer):
        assert handle == self.nvapi.gpu
        data = (ctypes.c_ubyte * rawctl.SIZE).from_address(ctypes.cast(pointer, ctypes.c_void_p).value)
        version, mask = struct.unpack_from("<II", data, 0)
        assert version in (rawctl.V2, rawctl.V1) and mask in (1, 2)
        assert not any(data[8:])
        index = str(mask.bit_length() - 1)
        assert index in self.policy["rails"]
        self.calls.append((index, version))
        supported = self.versions.get(index, rawctl.V2)
        if supported is None:
            return -104
        if version != supported:
            return -9
        raw = payload(self.policy, index, version)
        ctypes.memmove(ctypes.addressof(data), raw, len(raw))
        return self.fault(self, index, version, data) if self.fault else 0


def test_full_raw_packets_with_all_six_offsets_and_opaque_bytes_retained():
    pol = policy()
    gpu = Gpu(pol)
    state, error = rawctl.read(gpu, pol)
    assert error is None and state == observation(pol)
    assert gpu.calls == [("0", rawctl.V2), ("1", rawctl.V2)] * 2
    for index, row in state["rails"].items():
        packet = bytes.fromhex(row["payload_hex"])
        at = rawctl.ROW_OFFSET + int(index) * rawctl.ROW_STRIDE
        assert len(packet) == 2760 and packet[8] == 73 and packet[9] == 0xA4 and packet[-1] == 0xD8
        assert struct.unpack_from("<ii", packet, at + 20) == (-987654, 0x12345678)
    assert rawctl.validate_observation(state, pol) == state


@pytest.mark.parametrize("rails", [(0,), (1,)])
@pytest.mark.parametrize("version", [rawctl.V1, rawctl.V2])
def test_independent_singleton_rail_never_queries_absent_peer(rails, version):
    pol = policy(rails)
    index = str(rails[0])
    gpu = Gpu(pol, versions={index: version})
    state, error = rawctl.read(gpu, pol)
    assert error is None and set(state["rails"]) == {index}
    assert state["rails"][index]["version"] == version
    assert {rail for rail, _ in gpu.calls} == {index}
    assert len(gpu.calls) == (4 if version == rawctl.V1 else 2)


def test_versions_are_negotiated_independently_for_each_rail():
    pol = policy()
    gpu = Gpu(pol, versions={"0": rawctl.V2, "1": rawctl.V1})
    state, error = rawctl.read(gpu, pol)
    assert error is None and state == observation(pol, {"0": rawctl.V2, "1": rawctl.V1})
    assert gpu.calls == [("0", rawctl.V2), ("1", rawctl.V2), ("1", rawctl.V1)] * 2


@pytest.mark.parametrize("arch", [4, 5, 6, 7, 8, 9, 10])
@pytest.mark.parametrize("version", [rawctl.V1, rawctl.V2])
def test_zero_discriminator_and_offsets_are_valid_on_alternate_adapters(arch, version):
    pol = policy((1,), zero=True, architecture=arch, device_id=14, subsystem_id=0xDEADBEEF, slot="0000:03:00.1")
    state, error = rawctl.read(Gpu(pol, versions={"1": version}), pol)
    assert error is None and rawctl.validate_observation(state, pol) == state
    at = rawctl.ROW_OFFSET + rawctl.ROW_STRIDE
    assert bytes.fromhex(state["rails"]["1"]["payload_hex"])[at : at + 20] == bytes(20)


def test_validate_returns_independent_copy():
    pol, state = policy(), observation()
    value = rawctl.validate_observation(state, pol)
    value["identity"]["device_id"] = 2
    value["rails"]["0"]["version"] = rawctl.V1
    assert state == observation() and pol == policy()


@pytest.mark.parametrize(
    "where",
    [
        "identity",
        "missing_rail",
        "extra_rail",
        "rail_key_type",
        "missing_payload",
        "extra_field",
        "bad_version",
        "bool_version",
    ],
)
def test_malformed_state_structure(where):
    pol, state = policy(), observation()
    if where == "identity":
        state["identity"]["subsystem_id"] += 1
    elif where == "missing_rail":
        state["rails"].pop("1")
    elif where == "extra_rail":
        state["rails"]["2"] = state["rails"]["1"]
    elif where == "rail_key_type":
        state["rails"][0] = state["rails"].pop("0")
    elif where == "missing_payload":
        state["rails"]["0"].pop("payload_hex")
    elif where == "extra_field":
        state["rails"]["0"]["extra"] = 1
    else:
        state["rails"]["0"]["version"] = 0x30AC8 if where == "bad_version" else True
    with pytest.raises(rawctl.VoltageRailControlError):
        rawctl.validate_observation(state, pol)


@pytest.mark.parametrize("kind", ["short", "long", "upper", "nonhex", "none"])
def test_packet_geometry_and_canonical_encoding(kind):
    state = observation()
    value = state["rails"]["0"]["payload_hex"]
    state["rails"]["0"]["payload_hex"] = {
        "short": value[:-2],
        "long": value + "00",
        "upper": value.upper(),
        "nonhex": "xy" + value[2:],
        "none": None,
    }[kind]
    with pytest.raises(rawctl.VoltageRailControlError):
        rawctl.validate_observation(state, policy())


@pytest.mark.parametrize(
    "offset,value,fmt",
    [
        (0, rawctl.V1, "I"),
        (4, 3, "I"),
        (4, 0, "I"),
        (rawctl.ROW_OFFSET, 0, "I"),
        (rawctl.ROW_OFFSET + 4, -12344, "i"),
        (rawctl.ROW_OFFSET + 8, 23457, "i"),
        (rawctl.ROW_OFFSET + 12, -34568, "i"),
        (rawctl.ROW_OFFSET + 16, 45679, "i"),
    ],
)
def test_exact_requested_version_inventory_type_and_offsets(offset, value, fmt):
    state = observation()
    mutate(state, "0", offset, value, fmt)
    with pytest.raises(rawctl.VoltageRailControlError):
        rawctl.validate_observation(state, policy())


@pytest.mark.parametrize(
    "offset", [8, 9, 71, rawctl.ROW_OFFSET + 20, rawctl.ROW_OFFSET + 24, rawctl.ROW_OFFSET + 41, rawctl.SIZE - 1]
)
def test_opaque_fields_are_retained_without_fabricated_semantics(offset):
    state = observation()
    previous = state["rails"]["0"]["payload_hex"]
    mutate(state, "0", offset, bytes.fromhex(previous)[offset] ^ 1)
    validated = rawctl.validate_observation(state, policy())
    assert validated == state and validated["rails"]["0"]["payload_hex"] != previous


@pytest.mark.parametrize("bad", [None, {}, [], {"identity": {}, "rails": {}}, {"identity": {}, "rails": {"0": {}}}])
def test_invalid_policy_has_no_native_calls(bad):
    gpu = Gpu()
    state, error = rawctl.read(gpu, bad)
    assert state is None and error and gpu.calls == []
    with pytest.raises(rawctl.VoltageRailControlError):
        rawctl.validate_observation(observation(), bad)


@pytest.mark.parametrize(
    "field,value", [("slot", "0001:00:00.0"), ("architecture", 3), ("device_id", True), ("subsystem_id", -1)]
)
def test_invalid_policy_identity_refuses(field, value):
    pol = policy()
    pol["identity"][field] = value
    state, error = rawctl.read(Gpu(), pol)
    assert state is None and error


@pytest.mark.parametrize("failure", ["status", "exception", "noninteger"])
def test_one_transient_read_is_retried_with_fresh_packets(failure):
    def fault(gpu, index, version, data):
        if len(gpu.calls) == 1:
            if failure == "exception":
                raise OSError("Transient transport failure")
            return -1 if failure == "status" else False
        return 0

    pol, gpu = policy(), Gpu(fault=fault)
    state, error = rawctl.read(gpu, pol)
    assert error is None and state == observation(pol) and len(gpu.calls) == 5
    assert all(version == rawctl.V2 for _, version in gpu.calls)


def test_persistent_transient_status_never_triggers_v1_fallback():
    gpu = Gpu(fault=lambda *args: -1)
    state, error = rawctl.read(gpu, policy())
    assert state is None and error and gpu.calls == [("0", rawctl.V2)] * 2


@pytest.mark.parametrize("status", [-3, -104])
def test_explicit_unsupported_status_is_not_a_version_fallback(status):
    gpu = Gpu(fault=lambda *args: status)
    state, error = rawctl.read(gpu, policy())
    assert state is None and error and gpu.calls == [("0", rawctl.V2)]


def test_second_known_rail_failure_is_not_silently_dropped():
    gpu = Gpu(versions={"1": None})
    state, error = rawctl.read(gpu, policy())
    assert state is None and error and gpu.calls == [("0", rawctl.V2), ("1", rawctl.V2)]


def test_both_versions_rejected_is_bounded_and_explicit():
    gpu = Gpu(versions={"0": 0x30AC8})
    state, error = rawctl.read(gpu, policy())
    assert state is None and "both" in error and gpu.calls == [("0", rawctl.V2), ("0", rawctl.V1)]


def test_missing_export_needs_no_native_request():
    gpu = Gpu()
    gpu.nvapi.VoltRailsCtlGet = None
    state, error = rawctl.read(gpu, policy())
    assert state is None and "export" in error and gpu.calls == []


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("status", [0, -9])
def test_overflow_is_checked_even_before_version_fallback(side, status):
    def fault(gpu, index, version, data):
        ctypes.c_ubyte.from_address(ctypes.addressof(data) + (-1 if side == "before" else rawctl.SIZE)).value = 0
        return status

    gpu = Gpu(fault=fault)
    state, error = rawctl.read(gpu, policy())
    assert state is None and "guarded" in error and len(gpu.calls) == 1


def test_overflow_after_native_exception_is_not_retried_as_transient():
    def fault(gpu, index, version, data):
        ctypes.c_ubyte.from_address(ctypes.addressof(data) + rawctl.SIZE).value = 0
        raise RuntimeError("Native failure after corrupting guard")

    gpu = Gpu(fault=fault)
    assert "guarded" in rawctl.read(gpu, policy())[1] and len(gpu.calls) == 1


@pytest.mark.parametrize("kind", ["header", "mask", "type", "offset"])
def test_successful_malformed_response_is_never_used(kind):
    def fault(gpu, index, version, data):
        at = {"header": 0, "mask": 4, "type": rawctl.ROW_OFFSET, "offset": rawctl.ROW_OFFSET + 4}[kind]
        data[at] ^= 1
        return 0

    gpu = Gpu(fault=fault)
    state, error = rawctl.read(gpu, policy())
    assert state is None and error and len(gpu.calls) == 1


@pytest.mark.parametrize("offset", [8, 9, rawctl.ROW_OFFSET + 20, rawctl.ROW_OFFSET + 24, rawctl.SIZE - 1])
def test_opaque_packet_drift_is_retried_then_requires_exact_stability(offset):
    def fault(gpu, index, version, data):
        if len(gpu.calls) == 3:
            data[offset] ^= 1
        return 0

    gpu = Gpu(fault=fault)
    state, error = rawctl.read(gpu, policy())
    assert error is None and state == observation() and len(gpu.calls) == 8


def test_permanent_opaque_drift_is_bounded():
    def fault(gpu, index, version, data):
        data[-1] = len(gpu.calls)
        return 0

    gpu = Gpu(fault=fault)
    state, error = rawctl.read(gpu, policy())
    assert state is None and "changed" in error and len(gpu.calls) == 8


@pytest.mark.parametrize("kind", ["slot", "nvml", "handle", "devid", "architecture", "pairing"])
def test_paired_target_must_remain_unchanged(kind):
    def fault(gpu, index, version, data):
        if kind == "slot":
            gpu.nvapi.selected["slot"] = "0000:04:00.0"
        elif kind == "nvml":
            gpu.nvml.selected["devid"] = 9
        elif kind == "handle":
            gpu.nvapi.gpu = 654321
        elif kind == "devid":
            gpu.nvapi.selected["devid"] = gpu.nvml.selected["devid"] = 12
        elif kind == "architecture":
            gpu.identity["architecture"] = 10
        else:
            gpu.pairing_error = "Target pairing changed"
        return 0

    gpu = Gpu(fault=fault)
    state, error = rawctl.read(gpu, policy())
    assert state is None and error and len(gpu.calls) == 1


def test_policy_identity_is_authority_before_any_get():
    pol, gpu = policy(), Gpu()
    pol["identity"]["device_id"] = 7
    assert rawctl.read(gpu, pol)[0] is None and gpu.calls == []


def test_requested_policy_values_are_not_modified_on_success_or_failure():
    pol = policy()
    before = copy.deepcopy(pol)
    assert rawctl.read(Gpu(pol), pol)[1] is None and pol == before
    assert rawctl.read(Gpu(pol, fault=lambda *args: -1), pol)[0] is None and pol == before


def test_cancellation_is_never_swallowed():
    def fault(gpu, index, version, data):
        raise KeyboardInterrupt()

    gpu = Gpu(fault=fault)
    with pytest.raises(KeyboardInterrupt):
        rawctl.read(gpu, policy())
    assert len(gpu.calls) == 1
