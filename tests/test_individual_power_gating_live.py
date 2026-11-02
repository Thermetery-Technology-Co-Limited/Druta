"""Offline selected individual references through the real LPWR packet transport."""

import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import individual_power_gating_live as live
from druta.perf_diagnostics import PerfDiagnosticError


@pytest.fixture
def device(monkeypatch):
    data = {
        "identity": "paired",
        "native": (0x1B0, 6, 7, 8),
        "index": 12,
        "supported": True,
        "unavailable": set(),
        "masks": {0x48: 0x20, 0x49: 0x81},
        "count": 0,
        "sets": [],
        "gets": [],
        "get_failures": 0,
        "set_status": 0,
        "malformed": None,
        "stub": False,
        "after_set": None,
        "release_rm_status": 0,
    }
    header = [
        0x4E564441,
        0x10002,
        72,
        0x4E562A2A,
        0x0500002B,
        0,
        71,
        72,
        73,
        74,
        75,
        1,
        0x1234,
        0x5678,
        0x2080A612,
        4,
        0,
    ]
    fields = {"hAdapter": 3, "hDevice": 9, "Type": 0, "Flags": 0, "hContext": 0}
    gpu = SimpleNamespace(_lock=threading.RLock())

    class Reader:
        def __init__(self, gpu):
            self.gpu = gpu

        def _transport(self):
            return tuple(header), dict(fields)

    monkeypatch.setattr(live, "PerfDiagnosticsReader", Reader)
    monkeypatch.setattr(live.clock, "_binding", lambda reader: data["identity"])

    def escape(packet, actual_fields):
        assert actual_fields == fields
        command = packet[14]
        packet[5] = packet[11] = 1
        payload = (ctypes.c_ubyte * packet[15]).from_address(ctypes.addressof(packet) + 68)
        if command != live._SET_COMMAND:
            data["gets"].append(command)
            if data["get_failures"]:
                data["get_failures"] -= 1
                return -1
        if command == 0x20801701:
            assert len(payload) == 16 and bytes(payload) == bytes(16)
            ctypes.memmove(ctypes.addressof(payload), struct.pack("<4I", *data["native"]), 16)
            return 0
        assert len(payload) == 0x404 and struct.unpack_from("<I", payload)[0] == 1
        kind, index, attribute, flags, value, reserved = struct.unpack_from("<HHHHII", payload, 4)
        assert kind == 3 and index == data["index"] and flags == reserved == 0
        expected = bytearray(0x404)
        struct.pack_into("<IHHHHII", expected, 0, 1, kind, index, attribute, flags, value, reserved)
        assert bytes(payload) == bytes(expected)
        if command == 0x2080A801:
            assert attribute in (0x39, 0x48, 0x49) and value == 0
            data["gets"].append((index, attribute))
            if attribute not in data["unavailable"]:
                returned = int(data["supported"]) if attribute == 0x39 else data["masks"][attribute]
                struct.pack_into("<HI", payload, 10, 1, returned)
            return 0
        assert command == live._SET_COMMAND and attribute == 0x37 and value in (0, 1)
        data["sets"].append((tuple(packet), bytes(payload), value))
        if not data["stub"]:
            data["count"] = data["count"] + 1 if value == 0 else max(0, data["count"] - 1)
            data["masks"][0x48] = (data["masks"][0x48] & ~1) | int(data["count"] > 0)
        struct.pack_into("<H", payload, 10, 1)
        malformed = data["malformed"]
        if malformed == "flags0":
            struct.pack_into("<H", payload, 10, 0)
        elif malformed == "flags3":
            struct.pack_into("<H", payload, 10, 3)
        elif malformed == "value":
            struct.pack_into("<I", payload, 12, value ^ 1)
        elif malformed == "opaque":
            payload[-1] = 1
        elif malformed == "envelope":
            packet[6] ^= 1
        if data["after_set"]:
            data["after_set"](value)
        if value == 1:
            packet[16] = data["release_rm_status"]
        return data["set_status"]

    gpu._legacy_clk_escape = escape
    return live.IndividualPowerGatingController(gpu, 12), data


@pytest.mark.parametrize("index,name", [
    (0, "Graphics (GR)"), (4, "Memory subsystem (MS)"),
    (10, "MS DIFR SW-ASR"), (11, "MS DIFR CG"),
    (12, "NVDEC"), (13, "NVENC"), (14, "OFA"), (15, "NVJPG"),
])
def test_selected_only_balanced_lifecycle_preserves_other_reason_bits(device, index, name):
    initial, data = device
    data["index"] = index
    controller = live.IndividualPowerGatingController(initial.gpu, index)
    before = controller.read()
    assert before["support"] and before["available"] and not data["sets"]
    assert before["index"] == index and before["name"] == name
    held = controller.hold()
    assert held["owned_request"] and not held["uncertain_outcome"]
    assert held["reason_masks"] == {0x48: 0x21, 0x49: 0x81}
    controller.hold()
    assert [item[2] for item in data["sets"]] == [0]
    data["masks"][0x48] |= 0x400
    data["masks"][0x49] |= 0x800
    after = controller.release()
    assert after["reason_masks"] == {0x48: 0x420, 0x49: 0x881}
    assert not after["owned_request"] and not after["uncertain_outcome"]
    assert [item[2] for item in data["sets"]] == [0, 1]
    controller.release()
    assert controller.close() is None and len(data["sets"]) == 2
    assert {row[0] for row in data["gets"] if isinstance(row, tuple)} == {index}


@pytest.mark.parametrize("index", [1, 9, 16, True, "12"])
def test_only_explicit_individual_indices_are_admitted(index):
    with pytest.raises(ValueError, match="Only individual"):
        live.IndividualPowerGatingController(None, index)


def test_unknown_family_never_reads_attributes_or_writes(device):
    controller, data = device
    data["native"] = (0x999, 1, 0, 0)
    assert controller.read()["status"] == "unsupported_family"
    with pytest.raises(live.IndividualPowerGatingError, match="unsupported"):
        controller.hold()
    assert not data["sets"] and not any(isinstance(row, tuple) for row in data["gets"])


@pytest.mark.parametrize("architecture,implementation", [
    (architecture, implementation)
    for architecture, implementations in (
        (0x170, (2, 3, 4, 6, 7)),
        (0x190, (2, 3, 4, 6, 7)),
        (0x1B0, (2, 3, 5, 6, 7)),
    )
    for implementation in implementations
])
def test_ms_known_native_pairs_have_balanced_selected_lifecycle(device, architecture, implementation):
    original, data = device
    data["index"] = 4
    data["native"] = (architecture, implementation, 9, 10)
    controller = live.IndividualPowerGatingController(original.gpu, 4)
    assert controller.hold()["reason_masks"] == {0x48: 0x21, 0x49: 0x81}
    assert controller.release()["reason_masks"] == {0x48: 0x20, 0x49: 0x81}
    assert [row[2] for row in data["sets"]] == [0, 1]


@pytest.mark.parametrize("architecture,implementation", [(0x160, 4), (0x170, 0xF), (0x190, 0xF), (0x1B0, 4)])
def test_ms_unqualified_layout_is_refused_before_attribute_reads_or_set(device, architecture, implementation):
    original, data = device
    data["index"] = 4
    data["native"] = (architecture, implementation, 0, 0)
    controller = live.IndividualPowerGatingController(original.gpu, 4)
    assert controller.read()["status"] == "unsupported_layout"
    with pytest.raises(live.IndividualPowerGatingError, match="unsupported"):
        controller.hold()
    assert not data["sets"] and not any(isinstance(row, tuple) for row in data["gets"])


def test_turing_ms_refusal_does_not_remove_other_individual_controls(device):
    original, data = device
    data["native"] = (0x160, 4, 0, 0)
    data["index"] = 4
    assert live.IndividualPowerGatingController(original.gpu, 4).read()["status"] == "unsupported_layout"
    data["index"] = 12
    media = live.IndividualPowerGatingController(original.gpu, 12)
    assert media.hold()["owned_request"]
    media.release()


def test_ms_missing_selected_support_refuses_before_set(device):
    original, data = device
    data.update(index=4, supported=False)
    controller = live.IndividualPowerGatingController(original.gpu, 4)
    assert controller.read()["status"] == "unsupported"
    with pytest.raises(live.IndividualPowerGatingError, match="unsupported"):
        controller.hold()
    assert not data["sets"]


@pytest.mark.parametrize("index", [10, 11])
def test_difr_component_hold_requires_reason_masks_not_eligibility_predicate(device, index):
    original, data = device
    data["index"] = index
    controller = live.IndividualPowerGatingController(original.gpu, index)
    assert controller.hold()["reason_masks"][0x48] == 0x21
    assert controller.release()["reason_masks"][0x48] == 0x20
    assert {row[1] for row in data["gets"] if isinstance(row, tuple)} == {0x39, 0x48, 0x49}
    assert [row[2] for row in data["sets"]] == [0, 1]


@pytest.mark.parametrize("unavailable", [0x39, 0x48, 0x49])
def test_partial_unavailable_selected_metadata_refuses_without_peer_prerequisites(device, unavailable):
    controller, data = device
    data["unavailable"].add(unavailable)
    state = controller.read()
    assert not state["support"] and not state["available"]
    with pytest.raises(live.IndividualPowerGatingError, match="unavailable or unsupported"):
        controller.hold()
    assert not data["sets"]


def test_selected_unsupported_retains_available_and_avoids_reason_reads(device):
    controller, data = device
    data["supported"] = False
    state = controller.read()
    assert state["available"] and not state["support"]
    assert {row[1] for row in data["gets"] if isinstance(row, tuple)} == {0x39}
    assert controller.close() is None


def test_preexisting_reason0_is_not_ours_to_acquire_or_release(device):
    controller, data = device
    data["masks"][0x48] |= 1
    data["count"] = 2
    with pytest.raises(live.IndividualPowerGatingError, match="bit0 clear"):
        controller.hold()
    controller.release()
    assert not data["sets"] and data["count"] == 2


@pytest.mark.parametrize("malformed", ["flags0", "flags3", "value", "opaque", "envelope"])
def test_failed_acquire_reply_still_retains_observed_pending_reference(device, malformed):
    controller, data = device
    data["malformed"] = malformed
    with pytest.raises((live.IndividualPowerGatingError, PerfDiagnosticError)):
        controller.hold()
    assert controller.read()["owned_request"] and controller.read()["uncertain_outcome"]
    data["malformed"] = None
    assert not controller.release()["owned_request"]
    assert [item[2] for item in data["sets"]] == [0, 1] and data["count"] == 0


def test_transport_failure_after_increment_is_not_retried_and_can_be_balanced_once(device):
    controller, data = device
    data["set_status"] = -1
    with pytest.raises(PerfDiagnosticError, match="transport failed"):
        controller.hold()
    assert controller.read()["owned_request"] and len(data["sets"]) == 1
    data["set_status"] = 0
    controller.close()
    assert len(data["sets"]) == 2 and data["count"] == 0


def test_stub_does_not_qualify_and_close_never_blindly_decrements(device):
    controller, data = device
    data["stub"] = True
    with pytest.raises(live.IndividualPowerGatingError, match="exact selected reason0 transition"):
        controller.hold()
    assert not controller.read()["owned_request"] and controller.read()["uncertain_outcome"]
    controller.close()
    assert [item[2] for item in data["sets"]] == [0]


def test_transient_get_retry_is_bounded_and_never_repeats_a_set(device):
    controller, data = device
    data["get_failures"] = 1
    assert controller.hold()["owned_request"]
    assert len(data["sets"]) == 1
    data["get_failures"] = 2
    count = len(data["gets"])
    with pytest.raises(PerfDiagnosticError):
        controller.read()
    assert len(data["gets"]) == count + 2
    controller.release()


def test_failed_acquire_readback_is_inspected_before_one_explicit_release(device):
    controller, data = device
    data["after_set"] = lambda value: data.update(get_failures=2) if value == 0 else None
    with pytest.raises(PerfDiagnosticError):
        controller.hold()
    assert controller.read()["uncertain_outcome"]
    controller.close()
    assert [item[2] for item in data["sets"]] == [0, 1] and data["count"] == 0


def test_successful_release_followed_by_failed_readback_never_decrements_twice(device):
    controller, data = device
    controller.hold()
    data["after_set"] = lambda value: data.update(get_failures=2) if value == 1 else None
    with pytest.raises(PerfDiagnosticError):
        controller.release()
    assert data["count"] == 0
    state = controller.close()
    assert not state["owned_request"] and state["uncertain_outcome"]
    controller.release()
    assert [item[2] for item in data["sets"]] == [0, 1]


def test_release_error_after_decrement_is_not_blindly_retried(device):
    controller, data = device
    controller.hold()
    data["set_status"] = -1
    with pytest.raises(PerfDiagnosticError):
        controller.release()
    data["set_status"] = 0
    controller.close()
    assert data["count"] == 0 and [item[2] for item in data["sets"]] == [0, 1]


def test_runtime_callback_failure_after_decrement_remains_failed_without_double_release(device):
    controller, data = device
    controller.hold()
    data["release_rm_status"] = 0x56
    with pytest.raises(PerfDiagnosticError, match="RM status 0x56"):
        controller.release()
    after = controller.read()
    assert after["uncertain_outcome"] and not after["owned_request"]
    assert after["reason_masks"][0x48] == 0x20 and data["count"] == 0
    controller.close()
    assert [item[2] for item in data["sets"]] == [0, 1]


def test_other_owner_can_keep_reason0_after_our_single_release(device):
    controller, data = device
    controller.hold()
    data["count"] += 1
    after = controller.release()
    assert not after["owned_request"] and not after["uncertain_outcome"]
    assert after["reason_masks"][0x48] & 1 and data["count"] == 1
    assert "no unique ownership" in after["verification_scope"]
    controller.close()
    assert len(data["sets"]) == 2


def test_release_unrelated_bit_change_reports_failure_without_another_decrement(device):
    controller, data = device
    controller.hold()
    data["after_set"] = lambda value: data["masks"].update({0x49: 0x82}) if value == 1 else None
    with pytest.raises(live.IndividualPowerGatingError, match="unrelated"):
        controller.release()
    controller.close()
    assert len(data["sets"]) == 2 and data["count"] == 0


def test_alternate_binding_and_native_auxiliary_do_not_form_a_device_allowlist(device):
    original, data = device
    data["identity"] = ("another-slot", 999, 888)
    data["native"] = (0x170, 0xFE, 0x12345678, 0xABCDEF01)
    controller = live.IndividualPowerGatingController(original.gpu, 12)
    assert controller.hold()["architecture"]["implementation"] == 0xFE
    controller.release()


def test_target_drift_never_sends_release_into_another_binding(device):
    controller, data = device
    controller.hold()
    data["identity"] = "other adapter"
    for action in (controller.read, controller.hold, controller.release, controller.close):
        with pytest.raises(PerfDiagnosticError, match="target changed"):
            action()
    assert [item[2] for item in data["sets"]] == [0]
