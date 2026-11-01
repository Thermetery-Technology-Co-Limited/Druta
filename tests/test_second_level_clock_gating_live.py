"""Offline SLCG table, partial READ32 inventory, and owned-object lifecycle."""

import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import second_level_clock_gating_live as live
from druta.perf_diagnostics import PerfDiagnosticError


@pytest.fixture
def device(monkeypatch):
    data = {
        "identity": "paired",
        "parent": 0x5678,
        "native": (0x1B0, 6, 0xB2, 0x1357),
        "values": dict.fromkeys(live._DISABLED_VALUES, 0),
        "expected_values": dict(live._DISABLED_VALUES),
        "read_addresses": [],
        "operation_statuses": {},
        "writes": [],
        "reads": 0,
        "arch_reads": 0,
        "transient": 0,
        "malformed": None,
        "stub": False,
        "global": 0,
        "status": 0,
    }
    header = (
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
        data["parent"],
        0x2080A612,
        4,
        0,
    )
    fields = {key: i for i, key in enumerate(("hAdapter", "hDevice", "Type", "Flags", "hContext"))}
    gpu = SimpleNamespace(_lock=threading.RLock())

    class Reader:
        def __init__(self, gpu):
            self.gpu = gpu

        def _transport(self):
            words = list(header)
            words[13] = data["parent"]
            return words, dict(fields)

    monkeypatch.setattr(live, "PerfDiagnosticsReader", Reader)
    monkeypatch.setattr(live.clock, "_binding", lambda reader: data["identity"])

    def apply(held):
        for address, expected in data["expected_values"].items():
            value = data["values"][address]
            if not data["operation_statuses"].get(address) and value != 0xFFFFFFFF and value & 0xFFF00000 != 0xBAD00000:
                data["values"][address] = expected if held else 0

    def escape(packet, actual_fields):
        assert actual_fields == fields
        data["writes"].append(tuple(packet))
        packet[5] = packet[11] = 1
        if packet[4] == 0x0500002A:
            packet[19] = 0
        elif packet[4] == 0x0500000C:
            packet[15] = 0
        else:
            packet[16] = 0
            if packet[14] == 0xB0CC0301:
                assert packet[18] == 16
                packet[17] = data["global"]
                packet[19] = 0xFFFFFFCF | (data["status"] << 4)
                if not data["stub"] and not data["global"] and not data["status"]:
                    apply(True)
            elif packet[14] == 0xB0CC0302:
                assert packet[17] == 48
                if not data["stub"]:
                    apply(False)
        return 0

    def get(reader, identity, command, size, request):
        assert identity == data["identity"]
        if command == 0x20801701:
            assert size == 16 and request == bytes(16)
            data["arch_reads"] += 1
            result = struct.pack("<4I", *data["native"])
            return result[:-4] if data["malformed"] == "architecture_size" else result
        assert command == 0x20800122 and size == 48 and len(request) == 48
        data["reads"] += 1
        if data["transient"]:
            data["transient"] -= 1
            raise PerfDiagnosticError("transient", "temporary SLCG read failure")
        if data["malformed"] == "rmstatus":
            raise PerfDiagnosticError("unsupported", "RM status 0x56", 0x56)
        a, b, c, d, e, count, pointer, route, pad, route_value = struct.unpack("<6IQIIQ", request)
        assert (a, b, c, d, e, count, route, pad, route_value) == (0, 0, 0, 0, 0, len(data["expected_values"]), 0, 0, 0)
        ops = (ctypes.c_ubyte * (32 * count)).from_address(pointer)
        for index, address in enumerate(data["expected_values"]):
            data["read_addresses"].append(address)
            at = index * 32
            assert struct.unpack_from("<BBBB7I", ops, at) == (0, 0, 0, 0, 0, 0, address, 0, 0, 0, 0)
            ops[at + 2] = data["operation_statuses"].get(address, 0)
            struct.pack_into("<I", ops, at + 20, data["values"][address])
        if data["malformed"] == "opaque":
            ops[28] = 1
        elif data["malformed"] == "operation":
            ops[0] = 1
        elif data["malformed"] == "address":
            ops[12] ^= 4
        elif data["malformed"] == "guard":
            ctypes.c_ubyte.from_address(pointer + 32 * count).value = 0
        if data["malformed"] == "params":
            return request[:-1] + b"\x01"
        if data["malformed"] == "parameter_size":
            return request[:-4]
        return request

    gpu._legacy_clk_escape = escape
    monkeypatch.setattr(live.lpwr, "_get", get)
    return live.SecondLevelClockGatingController(gpu), data


def commands(data):
    return [words[14] if words[4] == 0x0500002B else words[4] for words in data["writes"]]


def test_full_source_table_and_balanced_lifecycle_with_duplicate_hold(device):
    controller, data = device
    state = controller.read()
    assert state["support"] and state["status"] == "available"
    assert len(state["registers"]) == len(state["expected_disabled_values"]) == 13
    assert sum(value == 0x80000000 for value in state["expected_disabled_values"].values()) == 8
    assert sum(value == 1 for value in state["expected_disabled_values"].values()) == 5
    assert not any(state["registers"].values()) and not data["writes"]
    held = controller.hold()
    assert held["registers"] == held["expected_disabled_values"]
    assert held["observed_registers"] == list(held["registers"])
    assert held["owned_request"] and not held["uncertain_outcome"]
    controller.hold()
    assert commands(data) == [0x0500002A, 0xB0CC0301]
    released = controller.release()
    assert not any(released["registers"].values())
    assert not released["owned_request"] and not released["uncertain_outcome"]
    assert commands(data) == [0x0500002A, 0xB0CC0301, 0xB0CC0302, 0x0500000C]
    assert controller.close() is None


@pytest.mark.parametrize("implementation", [2, 3, 5, 6, 7])
def test_source_derived_implementations_with_arbitrary_native_auxiliary(device, implementation):
    controller, data = device
    data["native"] = (0x1B0, implementation, 0x12345678, 0x87654321)
    state = controller.read()
    assert state["support"] and state["architecture"]["raw_auxiliary"] == 0x87654321


@pytest.mark.parametrize("native", [(0x1B0, 4, 0, 0), (0x1A0, 3, 0, 0), (0x1B0, 99, 0, 0)])
def test_unknown_layout_never_touches_registers_or_allocates(device, native):
    controller, data = device
    data["native"] = native
    assert controller.read()["status"] == "unsupported_layout"
    with pytest.raises(live.SecondLevelClockGatingError, match="no valid register witness"):
        controller.hold()
    assert data["reads"] == 0 and not data["writes"]


@pytest.mark.parametrize(
    ("operation_status", "classification"),
    [
        (8, "unsupported_operation"),
        (4, "invalid_operation"),
        (32, "access_denied"),
        (128, "unknown_operation_status"),
        (12, "unsupported_operation"),
    ],
)
def test_per_operation_failures_are_classified_unverified_and_preserve_valid_peers(
    device, operation_status, classification
):
    controller, data = device
    address = 0x2AA49C
    data["operation_statuses"][address] = operation_status
    data["values"][address] = 0x12345678
    state = controller.read()
    assert state["support"] and state["status"] == "partial"
    assert hex(address) not in state["registers"]
    assert state["unavailable_registers"][hex(address)] == {
        "classification": classification,
        "operation_status": operation_status,
    }
    held = controller.hold()
    assert len(held["observed_registers"]) == 12
    assert hex(address) not in held["observed_registers"]
    assert not controller.release()["owned_request"]


@pytest.mark.parametrize("poison", [0xFFFFFFFF, 0xBADF1002, 0xBAD01234])
def test_poison_is_never_a_valid_register_value(device, poison):
    controller, data = device
    data["values"][0x2A0090] = poison
    state = controller.read()
    assert "0x2a0090" not in state["registers"]
    assert state["unavailable_registers"]["0x2a0090"] == {
        "classification": "poison",
        "operation_status": 0,
        "raw_value": hex(poison),
    }
    assert controller.hold()["owned_request"]
    controller.release()


def test_alternate_single_witness_outside_local_nine_is_supported(device):
    controller, data = device
    data["operation_statuses"] = dict.fromkeys(live._DISABLED_VALUES, 8)
    data["operation_statuses"][0x2B6CFC] = 0
    held = controller.hold()
    assert held["observed_registers"] == ["0x2b6cfc"]
    assert held["registers"] == {"0x2b6cfc": 1}
    assert not controller.release()["owned_request"]


def test_all_unavailable_has_no_support_and_never_allocates(device):
    controller, data = device
    data["values"] = dict.fromkeys(live._DISABLED_VALUES, 0xBADF1002)
    state = controller.read()
    assert not state["support"] and state["status"] == "unavailable"
    assert state["registers"] == {} and len(state["unavailable_registers"]) == 13
    with pytest.raises(live.SecondLevelClockGatingError, match="no valid register witness"):
        controller.hold()
    assert not data["writes"]


@pytest.mark.parametrize("value", [1, 0x80000000, 0x80])
def test_nonzero_valid_baseline_refused_before_allocation(device, value):
    controller, data = device
    data["values"][0x2B409C] = value
    with pytest.raises(live.SecondLevelClockGatingError, match="exact zero baseline"):
        controller.hold()
    assert not data["writes"]


@pytest.mark.parametrize(
    "malformed",
    ["architecture_size", "rmstatus", "opaque", "operation", "address", "params", "parameter_size", "guard"],
)
def test_malformed_geometry_or_overall_failure_never_dispatches_request(device, malformed):
    controller, data = device
    data["malformed"] = malformed
    with pytest.raises((live.SecondLevelClockGatingError, PerfDiagnosticError)):
        controller.hold()
    assert not data["writes"]


def test_transient_read_failure_retries_boundedly_and_retains_owned_request(device):
    controller, data = device
    data["transient"] = 1
    assert controller.hold()["owned_request"]
    data["transient"] = 2
    reads = data["reads"]
    with pytest.raises(PerfDiagnosticError, match="temporary"):
        controller.read()
    assert data["reads"] == reads + 2 and controller._owned
    controller.release()


def test_noop_stub_is_not_source_verified_and_can_be_explicitly_released(device):
    controller, data = device
    data["stub"] = True
    with pytest.raises(live.SecondLevelClockGatingError, match="source disabled"):
        controller.hold()
    assert controller.read()["uncertain_outcome"]
    with pytest.raises(live.SecondLevelClockGatingError, match="uncertain"):
        controller.hold()
    controller.close()
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]


@pytest.mark.parametrize(
    ("global_status", "status", "message"),
    [(1, 0, "global status"), (0, 1, "rejected"), (0, 2, "unsupported"), (0, 3, "failed")],
)
def test_profiler_global_and_selected_field_status_required(device, global_status, status, message):
    controller, data = device
    data.update({"global": global_status, "status": status})
    with pytest.raises(live.SecondLevelClockGatingError, match=message):
        controller.hold()
    controller.close()
    assert commands(data)[-2:] == [0xB0CC0302, 0x0500000C]


@pytest.mark.parametrize("change", ["value", "availability", "layout"])
def test_accepted_hold_requires_source_values_and_unchanged_inventory(device, monkeypatch, change):
    controller, data = device
    request = controller._profiler.request

    def changed():
        request()
        if change == "value":
            data["values"][0x2B409C] = 0x40
        elif change == "availability":
            data["operation_statuses"][0x2B409C] = 8
        else:
            data["native"] = (0x1B0, 7, 0xB2, 0x1357)

    monkeypatch.setattr(controller._profiler, "request", changed)
    with pytest.raises(live.SecondLevelClockGatingError, match="source disabled|classification changed"):
        controller.hold()
    if change == "value":
        controller.close()
    else:
        with pytest.raises(live.SecondLevelClockGatingError, match="original register inventory"):
            controller.close()
    assert controller._profiler.handle is None


def test_repeated_hold_validates_held_table_without_extra_request(device):
    controller, data = device
    controller.hold()
    data["values"][0x2B6CFC] = 0
    with pytest.raises(live.SecondLevelClockGatingError, match="no longer independently visible"):
        controller.hold()
    assert commands(data) == [0x0500002A, 0xB0CC0301]
    controller.close()


def test_unavailable_classification_change_on_release_reports_error_and_frees(device):
    controller, data = device
    data["operation_statuses"][0x2B409C] = 8
    controller.hold()
    data["operation_statuses"][0x2B409C] = 32
    with pytest.raises(live.SecondLevelClockGatingError, match="original register inventory"):
        controller.release()
    assert controller._profiler.handle is None
    assert not controller.read()["uncertain_outcome"]


def test_release_requires_exact_zero_and_frees_after_verification_error(device):
    controller, data = device
    controller.hold()
    data["stub"] = True
    with pytest.raises(live.SecondLevelClockGatingError, match="zero values"):
        controller.release()
    state = controller.read()
    assert state["last_verification_error"] and not state["owned_request"] and not state["uncertain_outcome"]
    assert commands(data)[-1] == 0x0500000C


def test_read_failure_during_close_still_frees_owned_object(device, monkeypatch):
    controller, data = device
    controller.hold()

    def fail():
        raise RuntimeError("register read failed")

    monkeypatch.setattr(controller, "_read_locked", fail)
    with pytest.raises(RuntimeError, match="register read failed"):
        controller.close()
    assert commands(data)[-1] == 0x0500000C
    assert not controller._owned and not controller._uncertain
    assert controller.close() is None


@pytest.mark.parametrize("target", ["identity", "parent"])
def test_target_drift_blocks_writes_without_replay(device, target):
    controller, data = device
    controller.hold()
    data[target] = "changed" if target == "identity" else data[target] + 1
    for action in (controller.read, controller.hold, controller.close):
        with pytest.raises(live.SecondLevelClockGatingError, match="changed"):
            action()
    assert commands(data) == [0x0500002A, 0xB0CC0301]


def test_changing_valid_value_fails_stability_before_allocation(device, monkeypatch):
    controller, data = device
    original = controller._once
    calls = []

    def unstable():
        result = original()
        calls.append(None)
        result["registers"]["0x2b409c"] = len(calls)
        return result

    monkeypatch.setattr(controller, "_once", unstable)
    with pytest.raises(live.SecondLevelClockGatingError, match="not stable"):
        controller.hold()
    assert len(calls) == 2 and not data["writes"]


def test_invalid_poison_contents_can_change_without_becoming_numeric_witness(device, monkeypatch):
    controller, data = device
    data["values"][0x2B6CFC] = 0xBADF1002
    original = controller._once
    calls = []

    def changing_poison():
        result = original()
        calls.append(None)
        result["unavailable_registers"]["0x2b6cfc"]["raw_value"] = hex(0xBAD00000 + len(calls))
        return result

    monkeypatch.setattr(controller, "_once", changing_poison)
    state = controller.read()
    assert state["support"] and "0x2b6cfc" not in state["registers"]
    assert state["unavailable_registers"]["0x2b6cfc"]["raw_value"] == "0xbad00002"


@pytest.fixture
def alternate_layout(device):
    # Use the independently source-qualified native170 layout, with all
    # register reads and named profiler operations still simulated offline.
    controller, data = device
    source_values = dict(live._LAYOUT_VALUES["ga10x-slcg"])
    data["native"] = (0x170, 4, 0x123, 0x456)
    data["expected_values"] = source_values
    data["values"] = dict.fromkeys(source_values, 0)
    return controller, data


def test_selected_layout_uses_only_its_source_offsets_and_disabled_values(alternate_layout):
    controller, data = alternate_layout
    held = controller.hold()
    assert held["expected_disabled_values"] == {"0x248018": 3, "0x251a18": 3, "0x251818": 3}
    assert held["registers"] == held["expected_disabled_values"]
    assert held["observed_registers"] == list(held["registers"])
    controller.hold()
    assert commands(data) == [0x0500002A, 0xB0CC0301]
    assert not any(controller.release()["registers"].values())
    assert set(data["read_addresses"]) == {0x248018, 0x251A18, 0x251818}
    assert not set(data["read_addresses"]) & set(live._DISABLED_VALUES)


def test_selected_layout_partial_inventory_does_not_borrow_blackwell_offsets(alternate_layout):
    controller, data = alternate_layout
    data["operation_statuses"][0x248018] = 8
    data["values"][0x251A18] = 0xBADF1002
    held = controller.hold()
    assert held["status"] == "partial"
    assert held["registers"] == {"0x251818": 3}
    assert held["observed_registers"] == ["0x251818"]
    assert len(held["unavailable_registers"]) == 2
    assert not controller.release()["owned_request"]
    assert set(data["read_addresses"]) == {0x248018, 0x251A18, 0x251818}


def test_selected_layout_source_value_mismatch_fails_without_guessing(alternate_layout, monkeypatch):
    controller, data = alternate_layout
    request = controller._profiler.request

    def wrong_table_value():
        request()
        data["values"][0x248018] = 0x80000000

    monkeypatch.setattr(controller._profiler, "request", wrong_table_value)
    with pytest.raises(live.SecondLevelClockGatingError, match="source disabled"):
        controller.hold()
    controller.close()
    assert controller._profiler.handle is None


def test_unqualified_native_170_pairs_never_fall_back_to_blackwell_registers(device):
    controller, data = device
    data["native"] = (0x170, 31, 0x123, 0x456)
    state = controller.read()
    assert not state["support"] and state["expected_disabled_values"] == {}
    with pytest.raises(live.SecondLevelClockGatingError, match="no valid register witness"):
        controller.hold()
    assert not data["read_addresses"] and not data["writes"]


@pytest.mark.parametrize("implementation", [2, 3, 4, 6, 7])
def test_source_qualified_ampere_pairs_use_three_registers_only(alternate_layout, implementation):
    controller, data = alternate_layout
    data["native"] = (0x170, implementation, 0x12345678, 0x87654321)
    state = controller.read()
    assert state["support"] and state["layout"] == "ga10x-slcg"
    assert state["architecture"]["revision"] == 0x12345678
    assert state["architecture"]["raw_auxiliary"] == 0x87654321
    assert state["expected_disabled_values"] == {"0x248018": 3, "0x251a18": 3, "0x251818": 3}
    assert controller.hold()["registers"] == state["expected_disabled_values"]
    assert not any(controller.release()["registers"].values())
    assert set(data["read_addresses"]) == {0x248018, 0x251A18, 0x251818}
    assert not set(data["read_addresses"]) & set(live._DISABLED_VALUES)


@pytest.mark.parametrize("implementation", [0, 0xF])
def test_unqualified_ampere_implementations_never_read_a_fallback_layout(device, implementation):
    controller, data = device
    data["native"] = (0x170, implementation, 0x12345678, 0x87654321)
    state = controller.read()
    assert not state["support"] and state["status"] == "unsupported_layout"
    assert state["expected_disabled_values"] == {}
    with pytest.raises(live.SecondLevelClockGatingError, match="no valid register witness"):
        controller.hold()
    assert not data["read_addresses"] and not data["writes"]
