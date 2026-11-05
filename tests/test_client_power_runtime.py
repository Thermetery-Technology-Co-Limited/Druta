"""Synthetic wire tests; no installed driver, GPU or private capture is needed."""

import ctypes
import json
import struct
from collections import defaultdict
from types import SimpleNamespace

import pytest

from druta import client_power_runtime as cp


class Lock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True

    def __exit__(self, *args):
        self.held = False


def put(raw, offset, value):
    struct.pack_into("<I", raw, offset, value)


class FakeGpu:
    def __init__(
        self, *, client_index=37, native_index=31, architecture=8, slot="0000:72:1d.3", requested=321000, unrelated=True
    ):
        self._lock = Lock()
        self.client_index, self.native_index = client_index, native_index
        self.requested, self.original = requested, requested
        self.architecture, self.pci_slot = architecture, slot
        self.pairing_error = ""
        self.nvapi = SimpleNamespace(
            ok=True, gpu=929, selected={"slot": slot, "handle": 929, "devid": 0xABCD, "subsys": 0x87654321}
        )
        self.nvml = SimpleNamespace(selected={"slot": slot, "devid": 0xABCD, "subsys": 0x87654321})
        self._current_limit_transport = None
        self._current_limit_layout_choice = "cached"
        self.calls, self.set_payloads = [], []
        self.counts, self.faults = defaultdict(int), {}
        self.captures, self.transport_reads = 0, 0
        self.transport_faults = {}
        self.lower_pmu = None
        self.client_mask = 1 << client_index
        self.native_mask = 1 << native_index
        self.info = bytearray(cp.SIZES[cp.INFO])
        self.info[0:4] = bytes([219, 91, 42, 13])
        self.native_info = bytearray(cp.SIZES[cp.NATIVE_INFO])
        self.native_info[0:4] = b"ABCD"
        self.native_control = bytearray(cp.SIZES[cp.NATIVE_CONTROL])
        self.native_control[-17:] = b"nativeopaque_tail"
        self.client_opaque = b"more"
        self.native_peer = 2 if native_index != 2 else 19
        if unrelated:
            peer = 4 if client_index != 4 else 52
            self.client_mask |= 1 << peer
            pos = cp.INFO_BASE + peer * cp.INFO_STRIDE
            # Unknown, inconsistent row types intentionally do not erase the
            # selected role1 capability. No peer CONTROL request is sent.
            self.info[pos : pos + 4] = bytes([201, 199, 77, 9])
            self.native_mask |= 1 << self.native_peer
            pos = cp.NATIVE_INFO_BASE + self.native_peer * cp.NATIVE_INFO_STRIDE
            self.native_info[pos : pos + 7] = bytes([0x81, 3, 2, 1, 213, 4, 9])
            put(self.native_control, cp.NATIVE_CONTROL_BASE + self.native_peer * cp.NATIVE_CONTROL_STRIDE, 213)
            put(self.native_control, cp.NATIVE_CONTROL_BASE + self.native_peer * cp.NATIVE_CONTROL_STRIDE + 4, 87654)
        self.info[4 : cp.INFO_BASE] = self.client_mask.to_bytes(32, "little")
        pos = cp.INFO_BASE + client_index * cp.INFO_STRIDE
        self.info[pos : pos + 4] = bytes([83, 83, 1, 0])
        for offset, value in ((4, 200000), (8, 321000), (12, 550000)):
            put(self.info, pos + offset, value)
        self.info[pos + 27] = 0x69
        put(self.native_info, 4, self.native_mask)
        pos = cp.NATIVE_INFO_BASE + native_index * cp.NATIVE_INFO_STRIDE
        self.native_info[pos : pos + 7] = bytes([0x40, 7, 6, 5, 33, 13, 0])
        for offset, value in ((8, 200000), (12, 321000), (16, 550000)):
            put(self.native_info, pos + offset, value)
        self.native_info[pos + 200] = 0xD6
        struct.pack_into("<5I", self.native_control, 0, 0, 0, 0, 255, self.native_mask)
        put(self.native_control, cp.NATIVE_CONTROL_BASE + native_index * cp.NATIVE_CONTROL_STRIDE, 33)
        self._update(requested)

    def slot(self):
        return self.pci_slot

    def arch(self):
        return self.architecture

    def _update(self, value):
        self.requested = value
        put(self.native_control, cp.NATIVE_CONTROL_BASE + self.native_index * cp.NATIVE_CONTROL_STRIDE + 4, value)

    def _current_limit_transport_layouts(self):
        assert self._lock.held
        self.transport_reads += 1
        effect = self.transport_faults.get(self.transport_reads)
        if effect:
            effect(self)
        if self._current_limit_transport is None:
            self.captures += 1
            header = list(range(17))
            for index, value in cp.clock._ENVELOPE.items():
                header[index] = value
            header[5] = header[11] = 0
            header[2], header[14], header[15], header[16] = 54420, 0x2080A612, 54352, 0
            self._current_limit_transport = (
                header,
                {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0},
            )
        return ({},)

    def _status(self):
        raw = bytearray(cp.SIZES[cp.NATIVE_STATUS])
        put(raw, 4, self.native_mask)
        for index in range(cp.NATIVE_CAPACITY):
            if not self.native_mask >> index & 1:
                continue
            ip = cp.NATIVE_INFO_BASE + index * cp.NATIVE_INFO_STRIDE
            ctl = cp.NATIVE_CONTROL_BASE + index * cp.NATIVE_CONTROL_STRIDE
            at = 0x9C + index * 0x1730
            kind, requested = self.native_info[ip + 4], cp._u32(self.native_control, ctl + 4)
            raw[at] = kind
            effective = self.lower_pmu if index == self.native_index and self.lower_pmu is not None else requested
            put(raw, at + 4, effective)
            put(raw, at + 8, self.counts[cp.NATIVE_STATUS] * 100)
            for offset, value in ((0x40, requested), (0xE0, effective)):
                raw[at + offset : at + offset + 2] = bytes([0, 1])
                put(raw, at + offset + 4, value)
                put(raw, at + offset + 8, self.original)
                raw[at + offset + 12] = 254
                put(raw, at + offset + 16, value)
        return raw

    def _legacy_clk_escape(self, packet, fields):
        assert self._lock.held
        command, size = int(packet[14]), int(packet[15])
        assert size == cp.SIZES[command] and packet[2] == size + 68
        assert packet[5] == packet[11] == 0
        assert fields == {"hAdapter": 12, "hDevice": 0, "Type": 0, "Flags": 1, "hContext": 0}
        request = bytes(packet)[68:]
        self.calls.append((command, request))
        self.counts[command] += 1
        event = self.faults.get((command, self.counts[command]), {})
        if callable(event):
            event = event(self, request) or {}
        result = bytearray(request)
        if command == cp.INFO:
            assert request == bytes(size)
            result[:] = self.info
        elif command == cp.CONTROL:
            assert int.from_bytes(request[4:36], "little") == 1 << self.client_index
            at = cp.CONTROL_BASE + self.client_index * cp.CONTROL_STRIDE
            assert request[at + 8] == cp.CLIENT
            result[36:40] = self.client_opaque
            result[at + 2 : at + 4] = b"xy"
            result[at + 9 : at + 12] = b"abc"
            put(result, at + 4, self.requested)
            opaque = cp.CONTROL_BASE + (0 if self.client_index != 0 else 1) * cp.CONTROL_STRIDE + 2
            result[opaque : opaque + 7] = b"opaque!"
        elif command == cp.ROLE_INFO:
            assert request == bytes([1]) + bytes(size - 1)
            at = cp.NATIVE_INFO_BASE + self.native_index * cp.NATIVE_INFO_STRIDE
            result[4:] = self.native_info[at : at + cp.NATIVE_INFO_STRIDE]
        elif command == cp.NATIVE_INFO:
            assert request == bytes(size)
            result[:] = self.native_info
        elif command == cp.NATIVE_CONTROL:
            assert cp._u32(request, 16) == self.native_mask
            result[:] = self.native_control
        elif command == cp.NATIVE_STATUS:
            assert cp._u32(request, 4) == self.native_mask
            result[:] = self._status()
        elif command == cp.SET:
            self.set_payloads.append(request)
            assert int.from_bytes(request[4:36], "little") == 1 << self.client_index
            at = cp.CONTROL_BASE + self.client_index * cp.CONTROL_STRIDE
            assert request[at + 8] == cp.CLIENT
            if not event.get("ignore"):
                self._update(cp._u32(request, at + 4))
        if "mutate" in event:
            event["mutate"](self)
        if "payload" in event:
            event["payload"](result)
        ctypes.memmove(ctypes.addressof(packet) + 68, bytes(result), size)
        packet[5], packet[11], packet[16] = event.get("context", 1), event.get("completion", 1), event.get("rm", 0)
        if "header" in event:
            index, value = event["header"]
            packet[index] = value
        if "guard" in event:
            address = ctypes.addressof(packet) + (len(packet) * 4 if event["guard"] == "after" else -1)
            ctypes.c_ubyte.from_address(address).value = 0
        if "raise" in event:
            raise event["raise"]
        return event.get("nt", 0)


def failure(call, kind):
    with pytest.raises(cp.ClientPowerError) as caught:
        call()
    assert caught.value.kind == kind
    return caught.value


@pytest.mark.parametrize(
    "client_index,native_index,architecture,slot",
    [
        (37, 31, 8, "0000:72:1d.3"),
        (63, 29, 10, "0000:ab:1f.7"),
        (4, 19, 4, "0000:02:01.0"),
    ],
)
def test_read_binds_independent_nonlocal_indices_and_identity(client_index, native_index, architecture, slot):
    gpu = FakeGpu(client_index=client_index, native_index=native_index, architecture=architecture, slot=slot)
    got = cp.ClientPowerController(gpu).read()
    assert got["schema"] == 1 and got["stable_reads"] == 2
    assert got["identity"] == {
        "slot": slot,
        "architecture": architecture,
        "device_id": 0xABCD,
        "subsystem_id": 0x87654321,
    }
    assert got["binding"] == {
        "client_index": client_index,
        "native_index": native_index,
        "semantic_role": 1,
        "client": 254,
        "units": "mW",
        "minimum_mw": 200000,
        "rated_mw": 321000,
        "maximum_mw": 550000,
    }
    assert got["requested_mw"] == 321000 and got["arbitration"]["available"]
    assert got["arbitration_error"] is None and not gpu.set_payloads
    assert gpu.counts == {
        cp.INFO: 2,
        cp.CONTROL: 2,
        cp.ROLE_INFO: 2,
        cp.NATIVE_INFO: 2,
        cp.NATIVE_CONTROL: 2,
        cp.NATIVE_STATUS: 1,
    }
    assert bytes.fromhex(got["native_info_hex"]) == gpu.native_info
    assert bytes.fromhex(got["native_control_hex"]) == gpu.native_control
    assert int.from_bytes(bytes.fromhex(got["control_hex"])[4:36], "little") == 1 << client_index
    json.dumps(got, allow_nan=False)


def test_native_capacity_is_the_u32_inventory_not_observed_policy_count():
    assert cp.NATIVE_CAPACITY == 32
    gpu = FakeGpu(native_index=31)
    assert cp.ClientPowerController(gpu).read()["binding"]["native_index"] == 31


@pytest.mark.parametrize("client_index,native_index", [(37, 31), (63, 29), (4, 19)])
def test_metadata_uses_only_info_and_preserves_full_definitions(client_index, native_index):
    gpu = FakeGpu(client_index=client_index, native_index=native_index)
    got = cp.ClientPowerController(gpu).read_metadata()
    assert set(got) == {"schema", "stable_reads", "identity", "binding", "info_hex", "role_info_hex", "native_info_hex"}
    assert got["schema"] == 1 and got["stable_reads"] == 2
    assert got["binding"]["client_index"] == client_index
    assert got["binding"]["native_index"] == native_index
    assert got["identity"]["slot"] == gpu.pci_slot
    assert got["identity"]["device_id"] == 0xABCD
    assert bytes.fromhex(got["info_hex"]) == gpu.info
    assert bytes.fromhex(got["native_info_hex"]) == gpu.native_info
    at = cp.NATIVE_INFO_BASE + native_index * cp.NATIVE_INFO_STRIDE
    assert bytes.fromhex(got["role_info_hex"]) == bytes([1, 0, 0, 0]) + gpu.native_info[at : at + cp.NATIVE_INFO_STRIDE]
    assert gpu.counts == {cp.INFO: 2, cp.ROLE_INFO: 2, cp.NATIVE_INFO: 2}
    assert not gpu.set_payloads
    json.dumps(got, allow_nan=False)


@pytest.mark.parametrize("prior", [0, cp.MISSING])
def test_metadata_remains_available_without_a_restorable_fe_request(prior):
    gpu = FakeGpu(requested=prior)
    controller = cp.ClientPowerController(gpu)
    assert controller.read_metadata()["binding"]["rated_mw"] == 321000
    assert set(gpu.counts) == {cp.INFO, cp.ROLE_INFO, cp.NATIVE_INFO}
    failure(controller.read, "unsupported")
    assert not gpu.set_payloads


def test_metadata_is_independent_of_failed_controls_and_status():
    gpu = FakeGpu()
    for command in (cp.CONTROL, cp.NATIVE_CONTROL, cp.NATIVE_STATUS):
        gpu.faults[(command, 1)] = {"raise": RuntimeError("getter unavailable")}
        gpu.faults[(command, 2)] = {"raise": RuntimeError("getter unavailable")}
    controller = cp.ClientPowerController(gpu)
    got = controller.read_metadata()
    assert got["binding"]["maximum_mw"] == 550000
    assert gpu.counts == {cp.INFO: 2, cp.ROLE_INFO: 2, cp.NATIVE_INFO: 2}
    failure(controller.read, "transient")
    assert not gpu.set_payloads


@pytest.mark.parametrize("limits", [(0, 0, 0), (0, 321000, cp.MISSING), (cp.MISSING,) * 3])
def test_metadata_accepts_ordered_full_uint32_limits(limits):
    gpu = FakeGpu(requested=cp.MISSING)
    at = cp.INFO_BASE + gpu.client_index * cp.INFO_STRIDE
    native_at = cp.NATIVE_INFO_BASE + gpu.native_index * cp.NATIVE_INFO_STRIDE
    struct.pack_into("<III", gpu.info, at + 4, *limits)
    struct.pack_into("<III", gpu.native_info, native_at + 8, *limits)
    got = cp.ClientPowerController(gpu).read_metadata()
    assert tuple(got["binding"][key] for key in ("minimum_mw", "rated_mw", "maximum_mw")) == limits
    assert set(gpu.counts) == {cp.INFO, cp.ROLE_INFO, cp.NATIVE_INFO}


@pytest.mark.parametrize("command", [cp.INFO, cp.ROLE_INFO, cp.NATIVE_INFO])
def test_metadata_transient_get_recovers_with_one_fresh_pair(command):
    gpu = FakeGpu()
    gpu.faults[(command, 1)] = {"nt": -1}
    assert cp.ClientPowerController(gpu).read_metadata()["binding"]["rated_mw"] == 321000
    assert gpu.counts[command] == 3 and gpu.captures == 2
    assert set(gpu.counts) == {cp.INFO, cp.ROLE_INFO, cp.NATIVE_INFO}


def test_metadata_transient_retry_is_bounded():
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 1)] = gpu.faults[(cp.INFO, 2)] = {"nt": -1}
    failure(cp.ClientPowerController(gpu).read_metadata, "transient")
    assert gpu.counts == {cp.INFO: 2}


def test_metadata_does_not_require_stable_requests():
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 2)] = lambda g, _: g._update(cp.MISSING)
    got = cp.ClientPowerController(gpu).read_metadata()
    assert got["binding"]["rated_mw"] == 321000
    assert gpu.requested == cp.MISSING
    assert set(gpu.counts) == {cp.INFO, cp.ROLE_INFO, cp.NATIVE_INFO}


def test_metadata_rejects_changed_opaque_definition_between_reads():
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 2)] = {"payload": lambda raw: raw.__setitem__(-1, 1)}
    failure(cp.ClientPowerController(gpu).read_metadata, "unstable")
    assert gpu.counts == {cp.INFO: 2, cp.ROLE_INFO: 2, cp.NATIVE_INFO: 2}


def test_metadata_sticky_identity_applies_to_later_control_read():
    gpu = FakeGpu()
    controller = cp.ClientPowerController(gpu)
    controller.read_metadata()
    gpu.nvapi.gpu = gpu.nvapi.selected["handle"] = 1001
    failure(controller.read, "identity")
    assert gpu.counts == {cp.INFO: 2, cp.ROLE_INFO: 2, cp.NATIVE_INFO: 2}


@pytest.mark.parametrize("event", [{"guard": "after"}, {"header": (15, 4)}])
def test_metadata_keeps_geometry_validation_without_control_reads(event):
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 1)] = event
    failure(cp.ClientPowerController(gpu).read_metadata, "malformed")
    assert gpu.counts == {cp.INFO: 1}


def test_metadata_matches_the_definitions_in_full_read():
    gpu = FakeGpu()
    controller = cp.ClientPowerController(gpu)
    metadata = controller.read_metadata()
    full = controller.read()
    assert metadata == {key: full[key] for key in metadata}


@pytest.mark.parametrize("minimum,maximum", [(0, 550000), (200000, 0xFFFFFFFF)])
def test_ordinary_limit_metadata_is_not_a_request_admission_gate(minimum, maximum):
    gpu = FakeGpu()
    client_at = cp.INFO_BASE + gpu.client_index * cp.INFO_STRIDE
    native_at = cp.NATIVE_INFO_BASE + gpu.native_index * cp.NATIVE_INFO_STRIDE
    put(gpu.info, client_at + 4, minimum)
    put(gpu.info, client_at + 12, maximum)
    put(gpu.native_info, native_at + 8, minimum)
    put(gpu.native_info, native_at + 16, maximum)
    result = cp.ClientPowerController(gpu).set_request(551000)
    assert result["after"]["binding"]["minimum_mw"] == minimum
    assert result["after"]["binding"]["maximum_mw"] == maximum


@pytest.mark.parametrize("unrelated", [False, True])
@pytest.mark.parametrize("status_error", [{"rm": 0x56}, {"rm": 0x55}, {"guard": "after"}, {"header": (15, 4)}])
def test_unknown_peers_and_unavailable_status_do_not_erase_valid_control(unrelated, status_error):
    gpu = FakeGpu(unrelated=unrelated)
    gpu.faults[(cp.NATIVE_STATUS, 1)] = gpu.faults[(cp.NATIVE_STATUS, 2)] = status_error
    got = cp.ClientPowerController(gpu).read()
    assert got["requested_mw"] == 321000
    assert got["arbitration"] is None and got["arbitration_error"]
    assert not gpu.set_payloads


def test_malformed_optional_status_mask_is_only_a_diagnostic_error():
    gpu = FakeGpu()
    gpu.faults[(cp.NATIVE_STATUS, 1)] = {"payload": lambda raw: put(raw, 4, 0)}
    got = cp.ClientPowerController(gpu).read()
    assert got["arbitration"] is None and "mask" in got["arbitration_error"]


@pytest.mark.parametrize("command", [cp.INFO, cp.CONTROL, cp.ROLE_INFO, cp.NATIVE_INFO, cp.NATIVE_CONTROL])
@pytest.mark.parametrize("event", [{"nt": -1}, {"rm": 0x55}, {"raise": RuntimeError("temporary failure")}])
def test_transient_get_retries_one_complete_pair(command, event):
    gpu = FakeGpu()
    gpu.faults[(command, 1)] = event
    got = cp.ClientPowerController(gpu).read()
    assert got["requested_mw"] == gpu.original
    assert gpu.counts[command] == 3 and gpu.captures == 2 and not gpu.set_payloads


def test_transient_second_capture_retries_both_fresh_reads():
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 2)] = {"nt": -1}
    assert cp.ClientPowerController(gpu).read()["requested_mw"] == gpu.original
    assert gpu.counts[cp.INFO] == 4 and gpu.counts[cp.CONTROL] == 3


@pytest.mark.parametrize(
    "event", [{"nt": None}, {"nt": False}, {"nt": 0.0}, {"nt": -5}, {"completion": 0}, {"rm": 0x55}]
)
def test_transient_failures_do_not_loop_or_write(event):
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 1)] = gpu.faults[(cp.INFO, 2)] = event
    exc = failure(cp.ClientPowerController(gpu).read, "transient")
    assert len(gpu.calls) == 2 and not gpu.set_payloads and exc.rollback is None


@pytest.mark.parametrize("architecture", [0, 1, 2, 3, 99])
def test_unsupported_architecture_makes_no_native_calls(architecture):
    gpu = FakeGpu(architecture=architecture)
    failure(cp.ClientPowerController(gpu).read, "unsupported")
    assert not gpu.calls


@pytest.mark.parametrize(
    "key,value", [("devid", None), ("devid", True), ("devid", 65536), ("subsys", -1), ("subsys", None)]
)
def test_identity_requires_complete_typed_pci_values(key, value):
    gpu = FakeGpu()
    gpu.nvapi.selected[key] = gpu.nvml.selected[key] = value
    failure(cp.ClientPowerController(gpu).read, "identity")
    assert not gpu.calls


def test_pairing_error_prevents_read_and_set():
    gpu = FakeGpu()
    gpu.nvml.selected["slot"] = "0000:01:00.0"
    failure(lambda: cp.ClientPowerController(gpu).set_request(361000), "identity")
    assert not gpu.calls


def test_sticky_binding_rejects_replaced_handle_between_reads():
    gpu = FakeGpu()
    controller = cp.ClientPowerController(gpu)
    controller.read()
    gpu.nvapi.gpu = gpu.nvapi.selected["handle"] = 1001
    failure(controller.read, "identity")


@pytest.mark.parametrize("index,value", [(0, 0), (2, 88), (3, 0), (4, 0), (5, 3), (6, 0xBAD), (14, 0), (15, 12)])
def test_response_envelope_fault_is_not_retried(index, value):
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 1)] = {"header": (index, value)}
    failure(cp.ClientPowerController(gpu).read, "malformed")
    assert len(gpu.calls) == 1 and not gpu.set_payloads


@pytest.mark.parametrize("side", ["before", "after"])
def test_allocations_detect_native_buffer_overrun(side):
    gpu = FakeGpu()
    gpu.faults[(cp.NATIVE_INFO, 1)] = {"guard": side}
    failure(cp.ClientPowerController(gpu).read, "malformed")
    assert gpu.counts[cp.NATIVE_INFO] == 1


@pytest.mark.parametrize(
    "command,mutate,kind",
    [
        (cp.INFO, lambda raw: raw.__setitem__(12, 1), "malformed"),
        (cp.INFO, lambda raw: raw.__setitem__(cp.INFO_BASE + 37 * cp.INFO_STRIDE + 1, 99), "malformed"),
        (cp.INFO, lambda raw: raw.__setitem__(cp.INFO_BASE + 37 * cp.INFO_STRIDE + 3, 1), "unsupported"),
        (cp.INFO, lambda raw: raw.__setitem__(cp.INFO_BASE + 37 * cp.INFO_STRIDE + 2, 7), "unsupported"),
        (cp.INFO, lambda raw: put(raw, cp.INFO_BASE + 37 * cp.INFO_STRIDE + 4, 600000), "malformed"),
        (cp.CONTROL, lambda raw: raw.__setitem__(0, 8), "malformed"),
        (cp.CONTROL, lambda raw: raw.__setitem__(4, 2), "malformed"),
        (cp.CONTROL, lambda raw: raw.__setitem__(cp.CONTROL_BASE + 37 * cp.CONTROL_STRIDE, 7), "malformed"),
        (cp.CONTROL, lambda raw: raw.__setitem__(cp.CONTROL_BASE + 37 * cp.CONTROL_STRIDE + 8, 253), "malformed"),
        (cp.ROLE_INFO, lambda raw: raw.__setitem__(0, 2), "malformed"),
        (cp.ROLE_INFO, lambda raw: raw.__setitem__(30, 99), "malformed"),
        (cp.NATIVE_INFO, lambda raw: put(raw, 4, 1 << 2), "malformed"),
        (cp.NATIVE_CONTROL, lambda raw: put(raw, 16, 0), "malformed"),
        (cp.NATIVE_CONTROL, lambda raw: put(raw, 12, 0), "malformed"),
        (
            cp.NATIVE_CONTROL,
            lambda raw: put(raw, cp.NATIVE_CONTROL_BASE + 31 * cp.NATIVE_CONTROL_STRIDE, 11),
            "malformed",
        ),
        (
            cp.NATIVE_CONTROL,
            lambda raw: put(raw, cp.NATIVE_CONTROL_BASE + 31 * cp.NATIVE_CONTROL_STRIDE + 4, 321001),
            "unstable",
        ),
    ],
)
def test_mask_type_unit_binding_and_geometry_faults_prevent_set(command, mutate, kind):
    gpu = FakeGpu()
    gpu.faults[(command, 1)] = {"payload": mutate}
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(361000), kind)
    assert not gpu.set_payloads and exc.rollback is None


def test_duplicate_role_native_match_is_refused():
    gpu = FakeGpu()
    source = cp.NATIVE_INFO_BASE + gpu.native_index * cp.NATIVE_INFO_STRIDE
    peer = cp.NATIVE_INFO_BASE + gpu.native_peer * cp.NATIVE_INFO_STRIDE
    gpu.native_info[peer : peer + cp.NATIVE_INFO_STRIDE] = gpu.native_info[source : source + cp.NATIVE_INFO_STRIDE]
    failure(cp.ClientPowerController(gpu).read, "malformed")


def test_duplicate_client_role_is_refused():
    gpu = FakeGpu()
    gpu.info[cp.INFO_BASE + 4 * cp.INFO_STRIDE + 2] = 1
    failure(cp.ClientPowerController(gpu).read, "unsupported")


@pytest.mark.parametrize("wanted", [None, True, False, "361000", 361000.0, 0, -1, 0xFFFFFFFF, 0x100000000])
def test_bad_request_does_not_read_or_write(wanted):
    gpu = FakeGpu()
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(wanted), "invalid")
    assert not gpu.calls and exc.rollback is None


@pytest.mark.parametrize("prior", [0, 0xFFFFFFFF])
def test_missing_or_unrestorable_prior_request_prevents_set(prior):
    gpu = FakeGpu(requested=prior)
    failure(lambda: cp.ClientPowerController(gpu).set_request(361000), "unsupported")
    assert not gpu.set_payloads


@pytest.mark.parametrize("wanted", [1, 199000, 321000, 551000, 0xFFFFFFFE])
def test_below_minimum_above_maximum_and_independent_pmu_output(wanted):
    gpu = FakeGpu()
    gpu.lower_pmu = 180000
    result = cp.ClientPowerController(gpu).set_request(wanted)
    assert result["before"]["requested_mw"] == 321000
    assert result["after"]["requested_mw"] == wanted
    assert cp._state(result["after"]) == cp._project(result["before"], wanted)
    assert len(gpu.set_payloads) == 1
    old, new = bytes.fromhex(result["before"]["control_hex"]), gpu.set_payloads[0]
    at = cp.CONTROL_BASE + gpu.client_index * cp.CONTROL_STRIDE + 4
    assert old[:at] == new[:at] and old[at + 4 :] == new[at + 4 :]
    row = next(r for r in result["after"]["arbitration"]["policies"] if r["policy"] == gpu.native_index)
    assert row["requested"] == wanted and row["effective"] == 180000


@pytest.mark.parametrize(
    "event,kind",
    [
        ({"ignore": True}, "readback"),
        ({"mutate": lambda gpu: gpu._update(550000)}, "readback"),
        ({"rm": 0x55}, "transient"),
        ({"nt": -4}, "transient"),
        ({"raise": RuntimeError("partial write")}, "transient"),
        ({"header": (15, 4)}, "malformed"),
        ({"guard": "after"}, "malformed"),
        ({"payload": lambda raw: raw.__setitem__(37, 4)}, "malformed"),
    ],
)
def test_ignored_or_failed_set_compensates_exactly_once(event, kind):
    gpu = FakeGpu()
    gpu.faults[(cp.SET, 1)] = event
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), kind)
    assert len(gpu.set_payloads) == 2
    assert exc.rollback["attempted"] and exc.rollback["verified"]
    assert exc.rollback["error"] is None
    assert exc.rollback["readback"]["requested_mw"] == gpu.original
    assert cp._u32(gpu.set_payloads[1], cp.CONTROL_BASE + gpu.client_index * cp.CONTROL_STRIDE + 4) == gpu.original
    assert gpu.requested == gpu.original


@pytest.mark.parametrize("surface", ["client_info", "native_info", "native_control", "client_opaque"])
def test_peer_or_opaque_mutation_is_not_ignored_and_rollback_is_reported(surface):
    gpu = FakeGpu()

    def mutate(g):
        if surface == "client_info":
            g.info[-1] ^= 1
        elif surface == "native_info":
            g.native_info[-1] ^= 1
        elif surface == "native_control":
            g.native_control[-1] ^= 1
        else:
            g.client_opaque = b"lost"

    gpu.faults[(cp.SET, 1)] = {"mutate": mutate}
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "readback")
    assert len(gpu.set_payloads) == 2 and gpu.requested == gpu.original
    assert exc.rollback["attempted"] and not exc.rollback["verified"]
    assert exc.rollback["readback"] is not None and exc.rollback["error"]


def test_failed_rollback_is_reported_without_a_third_set():
    gpu = FakeGpu()
    gpu.faults[(cp.SET, 1)] = {"rm": 0x55}
    gpu.faults[(cp.SET, 2)] = {"ignore": True, "rm": 0x1F}
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "transient")
    assert len(gpu.set_payloads) == 2 and gpu.requested == 551000
    assert exc.rollback["attempted"] and not exc.rollback["verified"] and exc.rollback["error"]
    assert exc.rollback["readback"]["requested_mw"] == 551000


def test_failure_before_set_dispatch_does_not_compensate():
    gpu = FakeGpu()
    # Fail the transport used to prepare SET, independently of how many
    # read-only geometry checks a complete before-state capture requires.
    probe = FakeGpu()
    cp.ClientPowerController(probe).read()
    gpu.transport_faults[probe.transport_reads + 1] = lambda _: (_ for _ in ()).throw(RuntimeError("transport disappeared"))
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "transient")
    assert not gpu.set_payloads and exc.rollback is None


def test_identity_change_after_set_never_writes_to_the_replacement():
    gpu = FakeGpu()

    def replace(g):
        g.nvapi.gpu = g.nvapi.selected["handle"] = 1002

    gpu.faults[(cp.SET, 1)] = {"mutate": replace}
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "identity")
    assert len(gpu.set_payloads) == 1
    assert exc.rollback == {
        "attempted": False,
        "verified": False,
        "error": "ClientPowerError: GPU binding changed; create a fresh PERF reader",
        "readback": None,
    }


def test_optional_status_identity_change_is_not_hidden():
    gpu = FakeGpu()
    gpu.faults[(cp.NATIVE_STATUS, 1)] = {"mutate": lambda g: setattr(g.nvapi, "gpu", 1002)}
    failure(cp.ClientPowerController(gpu).read, "identity")


def test_unsupported_set_still_compensates_once_because_it_was_dispatched():
    gpu = FakeGpu()
    gpu.faults[(cp.SET, 1)] = {"ignore": True, "rm": 0x56}
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "unsupported")
    assert len(gpu.set_payloads) == 2 and exc.rollback["verified"]


def test_changing_requests_between_fresh_reads_never_dispatches_set():
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 2)] = lambda g, _: g._update(322000)
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "unstable")
    assert not gpu.set_payloads and exc.rollback is None


def test_transient_verification_get_recovers_without_repeating_set():
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 3)] = {"nt": -1}
    result = cp.ClientPowerController(gpu).set_request(551000)
    assert result["after"]["requested_mw"] == 551000
    assert len(gpu.set_payloads) == 1 and gpu.counts[cp.INFO] == 5


def test_failed_verification_get_compensates_once_and_reads_restoration():
    gpu = FakeGpu()
    gpu.faults[(cp.INFO, 3)] = gpu.faults[(cp.INFO, 4)] = {"nt": -1}
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "transient")
    assert len(gpu.set_payloads) == 2 and exc.rollback["verified"]
    assert exc.rollback["readback"]["requested_mw"] == gpu.original


def test_transient_rollback_read_recovers_without_another_compensation():
    gpu = FakeGpu()
    gpu.faults[(cp.SET, 1)] = {"rm": 0x55}
    gpu.faults[(cp.INFO, 3)] = {"nt": -1}
    exc = failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "transient")
    assert len(gpu.set_payloads) == 2 and exc.rollback["verified"]


def test_nonzero_native_type_disagreement_in_unrelated_row_is_preserved():
    gpu = FakeGpu()
    peer = cp.NATIVE_CONTROL_BASE + gpu.native_peer * cp.NATIVE_CONTROL_STRIDE
    put(gpu.native_control, peer, 121)
    result = cp.ClientPowerController(gpu).set_request(551000)
    assert bytes.fromhex(result["after"]["native_control_hex"])[peer : peer + 4] == struct.pack("<I", 121)
    assert cp._state(result["after"]) == cp._project(result["before"], 551000)


@pytest.mark.parametrize("fault", ["envelope", "field"])
def test_bad_captured_transport_fails_before_any_command(fault):
    gpu = FakeGpu()
    with gpu._lock:
        gpu._current_limit_transport_layouts()
    header, fields = gpu._current_limit_transport
    if fault == "envelope":
        header[0] = 0
    else:
        fields["Flags"] = True
    failure(lambda: cp.ClientPowerController(gpu).set_request(551000), "malformed")
    assert not gpu.calls


def test_invalid_internal_geometry_never_dispatches():
    gpu = FakeGpu()
    controller = cp.ClientPowerController(gpu)
    with gpu._lock:
        failure(lambda: controller._exchange(cp.INFO, b"short"), "malformed")
        failure(lambda: controller._exchange(0x2080E61E, bytes(12)), "malformed")
        failure(lambda: controller._exchange(cp.SET, bytes(cp.SIZES[cp.SET])), "malformed")
    assert not gpu.calls
