"""Offline PERF decoding, target binding and independent read-only recovery."""

import ctypes
import json
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import perf_diagnostics as perf


def packets(name, indices=None, *, types=None):
    layout = perf.LAYOUTS[name]
    indices = [1, 5] if indices is None else indices
    info, control = bytearray(layout.info_size), bytearray(layout.control_size)
    info[0] = control[0] = 0xFF
    mask = sum(1 << index for index in indices).to_bytes(layout.mask_bytes, "little")
    info[4 : 4 + layout.mask_bytes] = control[4 : 4 + layout.mask_bytes] = mask
    if name == "limits":
        info[0x44] = control[0x44] = 8
        control[0x4C], control[0x4D], info[0x7A88] = 0, 1, 1
    elif name == "cf_controller":
        control[8:10] = b"\1\1"
    else:
        for offset in (0x14, 0x18, 0x20):
            struct.pack_into("<H", info, offset, 0xFFFF)
        if indices:
            struct.pack_into("<H", info, 0x14, indices[-1])
    for index in indices:
        start = layout.info_offset + index * layout.info_stride
        ctl = layout.control_offset + index * layout.control_stride
        typ = (types or {}).get(index, 6 if name == "limits" else 0)
        info[start] = control[ctl] = typ
        if name == "limits":
            label = f"DRIVER_LIMIT_{index}".encode()
            info[start + 2 : start + 2 + len(label)] = label
        if name == "cf_policy":
            info[start + 2], info[start + 3] = index, 5 if index == indices[-1] else index
            control[ctl + 2] = index % 2
    return bytes(info), bytes(control)


class FakeGpu:
    def __init__(self, *, architecture=8, slot="0000:09:03.0", handle=501):
        self._lock = threading.RLock()
        self.pairing_error = ""
        self.architecture, self.pci_slot = architecture, slot
        self.exports = {ident for layout in perf.LAYOUTS.values() for ident in (layout.info_id, layout.control_id)}
        selected = {"slot": slot, "handle": handle, "devid": 0xAAAA, "subsys": 0x12345678}
        self.nvapi = SimpleNamespace(
            ok=True, gpu=handle, selected=selected, _i=lambda ident, *args: object() if ident in self.exports else None
        )
        self.nvml = SimpleNamespace(selected={"slot": slot, "devid": 0xAAAA, "subsys": 0x12345678})
        self.calls, self.outcomes = [], {}
        self.payloads = {}
        for name, layout in perf.LAYOUTS.items():
            info, control = packets(name)
            self.payloads[layout.info_command], self.payloads[layout.control_command] = info, control
        self._current_limit_transport = None
        self.transport_reads = 0
        self.after_call = None

    def arch(self):
        return self.architecture

    def slot(self):
        return self.pci_slot

    def _current_limit_transport_layouts(self):
        self.transport_reads += 1
        if self._current_limit_transport is None:
            header = [0] * 17
            header[2], header[14], header[15] = 54420, 0x2080A612, 54352
            self._current_limit_transport = (header, dict.fromkeys(perf._ESCAPE_FIELDS, 0))
        return ({},)

    def _legacy_clk_escape(self, packet, fields):
        command = packet[14]
        self.calls.append(command)
        assert (command, packet[15]) in perf._GET_COMMANDS
        assert ctypes.sizeof(packet) == packet[2] == packet[15] + 68
        outcomes = self.outcomes.get(command, [])
        status, rm = outcomes.pop(0) if outcomes else (0, 0)
        packet[16] = rm
        if status == 0 and rm == 0:
            data = self.payloads[command]
            layout = next(
                item for item in perf.LAYOUTS.values() if command in (item.info_command, item.control_command)
            )
            if command == layout.control_command:
                actual = bytes(packet)[72 : 72 + layout.mask_bytes]
                assert actual == self.payloads[layout.info_command][4 : 4 + layout.mask_bytes]
            ctypes.memmove(ctypes.addressof(packet) + 68, data, len(data))
        if self.after_call:
            self.after_call(packet)
        return status


def test_alternate_adapter_and_full_capacity_named_slots():
    target = FakeGpu(architecture=7, slot="0000:72:1d.0", handle=987)
    layout = perf.LAYOUTS["limits"]
    target.payloads[layout.info_command], target.payloads[layout.control_command] = packets("limits", [0, 257, 511])
    result = perf.PerfDiagnosticsReader(target).read(["limits"])
    group = result["groups"]["limits"]
    assert group["status"] == "ready"
    assert [row["index"] for row in group["snapshot"]["entries"]] == [0, 257, 511]
    assert group["snapshot"]["entries"][2]["driver_label"] == "DRIVER_LIMIT_511"
    assert result["slot"] == "0000:72:1d.0"
    assert group["snapshot"]["flags"]["limits_info_0x7a88"]["raw"] == 1
    json.dumps(result, allow_nan=False)


def test_policy_target_comes_from_current_info_not_index_five():
    snapshot = perf.decode_group("cf_policy", *packets("cf_policy", [3, 24]))
    target = next(item for item in snapshot["registry_targets"] if item["field"] == "bits2_1")
    assert target == {
        "registry_name": "RmPerfCfPolicyOverrides",
        "field": "bits2_1",
        "index": 24,
        "present": True,
        "flag_raw": 0,
        "issue": None,
    }
    assert all(item["index"] is None for item in snapshot["registry_targets"] if item["field"] != "bits2_1")


def test_policy_role_and_controller_mask_are_independent_of_slot_index():
    info, control = packets("cf_policy", [3, 24])
    info = bytearray(info)
    start = perf.LAYOUTS["cf_policy"].info_offset + 24 * perf.LAYOUTS["cf_policy"].info_stride
    struct.pack_into("<I", info, start + 0xC, (1 << 6) | (1 << 31))
    row = perf.decode_group("cf_policy", info, control)["entries"][1]
    assert row["index"] == 24 and row["role_id"] == 5
    assert row["controller_mask"] == 0x80000040
    assert row["role_bytes_raw"] == [24, 5]
    assert row["driver_label"] is None


def test_policy_role_table_disagreement_does_not_claim_a_registry_target_flag():
    info, control = packets("cf_policy")
    info = bytearray(info)
    start = perf.LAYOUTS["cf_policy"].info_offset + 5 * perf.LAYOUTS["cf_policy"].info_stride
    info[start + 3] = 9
    snapshot = perf.decode_group("cf_policy", info, control)
    target = next(item for item in snapshot["registry_targets"] if item["field"] == "bits2_1")
    assert target["present"] and target["flag_raw"] is None
    assert "role" in target["issue"]
    assert snapshot["entries"][1]["role_id"] == 9


@pytest.mark.parametrize("name", perf.LAYOUTS)
def test_empty_inventory_has_no_control_call_or_decoded_flags(name):
    target = FakeGpu()
    layout = perf.LAYOUTS[name]
    target.payloads[layout.info_command], target.payloads[layout.control_command] = packets(name, [])
    result = perf.PerfDiagnosticsReader(target).read([name])["groups"][name]
    assert result["status"] == "empty"
    assert not result["snapshot"]["entries"] and not result["snapshot"]["flags"]
    assert target.calls == [layout.info_command]


@pytest.mark.parametrize("name", perf.LAYOUTS)
def test_sizes_mask_and_type_validation(name):
    info, control = packets(name)
    with pytest.raises(perf.PerfDiagnosticError, match="geometry"):
        perf.decode_group(name, info[:-1], control)
    with pytest.raises(perf.PerfDiagnosticError, match="geometry"):
        perf.decode_group(name, info, control[:-1])
    bad = bytearray(control)
    bad[4] ^= 1
    with pytest.raises(perf.PerfDiagnosticError, match="masks disagree"):
        perf.decode_group(name, info, bad)
    bad = bytearray(control)
    bad[0] ^= 1
    with pytest.raises(perf.PerfDiagnosticError, match="identifiers"):
        perf.decode_group(name, info, bad)
    with pytest.raises(perf.PerfDiagnosticError, match="populated"):
        perf.decode_group(name, info, None)


def test_unknown_row_types_and_invalid_labels_do_not_remove_peers():
    info, control = packets("limits", [1, 2, 3], types={2: 99})
    damaged = bytearray(info)
    pos = perf.LAYOUTS["limits"].info_offset + 3 * perf.LAYOUTS["limits"].info_stride
    damaged[pos + 2] = 0xFF
    rows = perf.decode_group("limits", damaged, control)["entries"]
    assert rows[0]["driver_label"] == "DRIVER_LIMIT_1"
    assert rows[1]["type"] == 99 and rows[1]["driver_label"] is None and rows[1]["issues"]
    assert rows[2]["driver_label"] is None and rows[2]["issues"]


def test_mismatched_policy_row_is_not_exposed_as_target_flag():
    info, control = packets("cf_policy")
    bad = bytearray(control)
    bad[8 + 5 * 4] = 4
    snapshot = perf.decode_group("cf_policy", info, bad)
    target = next(row for row in snapshot["registry_targets"] if row["field"] == "bits2_1")
    assert target["present"] and target["flag_raw"] is None and target["issue"]
    assert snapshot["entries"][0]["registry_controlled_flag_raw"] == 1


def test_target_outside_mask_and_nonboolean_values_remain_uncertain():
    info, control = packets("cf_policy")
    bad = bytearray(info)
    struct.pack_into("<H", bad, 0x18, 31)
    ctl = bytearray(control)
    ctl[8 + 5 * 4 + 2] = 2
    snapshot = perf.decode_group("cf_policy", bad, ctl)
    assert snapshot["registry_targets"][0]["issue"]
    assert snapshot["registry_targets"][1]["flag_raw"] is None
    info, control = packets("limits")
    ctl = bytearray(control)
    ctl[0x4C] = 7
    snapshot = perf.decode_group("limits", info, ctl)
    assert snapshot["flags"]["limits_control_0x4c"] == {"raw": 7, "boolean": None}


def test_unknown_limits_subclass_does_not_decode_extra_flag():
    info, control = map(bytearray, packets("limits"))
    info[0x44] = control[0x44] = 12
    snapshot = perf.decode_group("limits", info, control)
    assert "limits_info_0x7a88" not in snapshot["flags"]


def test_independent_failures_and_bounded_transient_recovery():
    target = FakeGpu()
    limits, controller, policy = [perf.LAYOUTS[name] for name in perf.LAYOUTS]
    target.outcomes[limits.info_command] = [(0, 0x56)]
    target.outcomes[controller.control_command] = [(-1, 0)]
    report = perf.PerfDiagnosticsReader(target).read()
    assert report["groups"]["limits"]["status"] == "unsupported"
    assert report["groups"]["limits"]["attempts"] == 1
    assert report["groups"]["cf_controller"]["status"] == "ready"
    assert report["groups"]["cf_controller"]["attempts"] == 2
    assert report["groups"]["cf_policy"]["status"] == "ready"
    assert target.calls.count(controller.info_command) == 2
    assert target.calls.count(controller.control_command) == 2
    assert policy.control_command in target.calls


def test_failed_refresh_retains_history_without_returning_it_as_fresh():
    target = FakeGpu()
    reader = perf.PerfDiagnosticsReader(target)
    reader.read(["limits"])
    command = perf.LAYOUTS["limits"].info_command
    target.outcomes[command] = [(0, 0x40), (0, 0x40)]
    group = reader.read(["limits"])["groups"]["limits"]
    assert group["status"] == "transient" and group["snapshot"] is None
    assert group["last_valid"]["present_count"] == 2 and group["attempts"] == 2
    group["last_valid"]["entries"].clear()
    assert reader.last_valid["limits"]["entries"]
    assert reader.read(["limits"])["groups"]["limits"]["status"] == "ready"


@pytest.mark.parametrize("failure", [RuntimeError, AttributeError, TypeError, IndexError])
def test_transport_exception_is_independent_and_retry_can_recapture(failure):
    target = FakeGpu()
    original = target._current_limit_transport_layouts
    failures = [failure("capture unavailable"), failure("capture unavailable")]

    def capture():
        if failures:
            raise failures.pop(0)
        return original()

    target._current_limit_transport_layouts = capture
    report = perf.PerfDiagnosticsReader(target).read()
    assert report["groups"]["limits"]["status"] == "transient"
    assert report["groups"]["limits"]["attempts"] == 2
    assert failure.__name__ in report["groups"]["limits"]["error"]
    assert report["groups"]["cf_controller"]["status"] == "ready"
    assert report["groups"]["cf_policy"]["status"] == "ready"
    assert perf.LAYOUTS["limits"].info_command not in target.calls


def test_native_exception_retains_history_and_reestablishes_transport():
    target = FakeGpu()
    reader = perf.PerfDiagnosticsReader(target)
    reader.read(["limits"])
    original = target._legacy_clk_escape
    command = perf.LAYOUTS["limits"].info_command
    failures = [RuntimeError("device removed"), RuntimeError("device removed")]

    def escape(packet, fields):
        if packet[14] == command and failures:
            raise failures.pop(0)
        return original(packet, fields)

    target._legacy_clk_escape = escape
    report = reader.read()
    result = report["groups"]["limits"]
    assert result["status"] == "transient" and result["snapshot"] is None
    assert result["last_valid"]["present_count"] == 2
    assert report["groups"]["cf_controller"]["status"] == "ready"
    assert report["groups"]["cf_policy"]["status"] == "ready"
    assert reader.read(["limits"])["groups"]["limits"]["status"] == "ready"


def test_process_exit_signal_is_not_suppressed():
    target = FakeGpu()

    def capture():
        raise KeyboardInterrupt()

    target._current_limit_transport_layouts = capture
    with pytest.raises(KeyboardInterrupt):
        perf.PerfDiagnosticsReader(target).read()


@pytest.mark.parametrize("reason", ["pairing", "slot", "segment", "function", "handle", "devid", "subsys"])
def test_target_mismatch_prevents_all_calls(reason):
    target = FakeGpu()
    if reason == "pairing":
        target.pairing_error = "Mismatch"
    elif reason == "slot":
        target.nvapi.selected["slot"] = "0000:08:03.0"
    elif reason == "segment":
        target.pci_slot = "0001:09:03.0"
    elif reason == "function":
        target.pci_slot = "0000:09:03.1"
    elif reason == "handle":
        target.nvapi.selected["handle"] = 999
    else:
        target.nvml.selected[reason] = 7
    result = perf.PerfDiagnosticsReader(target).read()
    assert all(group["status"] == "identity" for group in result["groups"].values())
    assert not target.calls and target.transport_reads == 0


def test_midcall_target_change_invalidates_result_and_reader():
    target = FakeGpu()
    target.after_call = lambda packet: setattr(target.nvapi, "gpu", 999)
    report = perf.PerfDiagnosticsReader(target).read(["limits"])
    assert report["groups"]["limits"]["status"] == "identity"
    assert report["groups"]["limits"]["snapshot"] is None and len(target.calls) == 1


@pytest.mark.parametrize("architecture", [1, 2, 3, 99])
def test_architecture_rejection_has_no_native_calls(architecture):
    target = FakeGpu(architecture=architecture)
    report = perf.PerfDiagnosticsReader(target).read()
    assert all(group["status"] == "unsupported" for group in report["groups"].values())
    assert not target.calls


def test_missing_export_is_independent():
    target = FakeGpu()
    target.exports.remove(perf.LAYOUTS["limits"].control_id)
    report = perf.PerfDiagnosticsReader(target).read()
    assert report["groups"]["limits"]["status"] == "unsupported"
    assert report["groups"]["cf_policy"]["status"] == "ready"
    assert perf.LAYOUTS["limits"].info_command not in target.calls


def test_legacy_transport_is_not_reused_for_modern_commands():
    target = FakeGpu()
    target._current_limit_transport_layouts()
    target._current_limit_transport[0][14] = 0x20802612
    report = perf.PerfDiagnosticsReader(target).read()
    assert all(group["status"] == "unsupported" for group in report["groups"].values())
    assert not target.calls


def test_response_command_echo_is_validated():
    target = FakeGpu()
    target.after_call = lambda packet: packet.__setitem__(14, 0xDEADBEEF)
    report = perf.PerfDiagnosticsReader(target).read(["limits"])
    assert report["groups"]["limits"]["status"] == "malformed"


def test_no_setter_command_can_reach_transport():
    target = FakeGpu()
    reader = perf.PerfDiagnosticsReader(target)
    with pytest.raises(ValueError, match="read-only"):
        reader._get(0x2080A025, 0x16850, ([], {}))
    assert not target.calls


def test_internal_get_rejects_oversized_or_missing_mask_before_transport():
    target = FakeGpu()
    reader = perf.PerfDiagnosticsReader(target)
    layout = perf.LAYOUTS["cf_policy"]
    for mask in (None, b"\xff" * 5):
        with pytest.raises(ValueError, match="exact INFO mask"):
            reader._get(layout.control_command, layout.control_size, ([], {}), mask)
    assert not target.calls


@pytest.mark.parametrize("attempts", [0, 4, True, 1.2])
def test_invalid_attempt_count(attempts):
    with pytest.raises(ValueError):
        perf.PerfDiagnosticsReader(FakeGpu(), attempts=attempts)
