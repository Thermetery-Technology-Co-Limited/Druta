"""Offline Fan2X transport, role binding and exact semantic readback."""

import copy
import ctypes
import json
import struct
import threading
from types import SimpleNamespace

import pytest

from druta import fan_runtime as fan


def packets(indices=(2, 11), *, types=None, roles=None):
    info, control = bytearray(fan.INFO_BYTES), bytearray(fan.CONTROL_BYTES)
    info[0] = control[0] = 255
    mask = sum(1 << index for index in indices).to_bytes(4, "little")
    info[4:8] = control[4:8] = info[12:16] = mask
    role_indices = list(indices) + [None] * (4 - len(indices)) if roles is None else roles
    info[16:20] = bytes(255 if index is None else index for index in role_indices)
    for index in indices:
        off, ctl = fan.INFO_OFFSET + index * fan.INFO_STRIDE, fan.CONTROL_OFFSET + index * fan.CONTROL_STRIDE
        info[off] = control[ctl] = (types or {}).get(index, 3)
        info[off + 14] = 2
        struct.pack_into("<I", info, off + 8, 42 + index)
        struct.pack_into("<II", info, off + 20, 80 + index, 26000 + index)
        struct.pack_into("<II", control, ctl + 8, 750 + index, 4200 + index)
        struct.pack_into("<II", control, ctl + 24, fan._value("level_minimum", 25), 65536)
    return info, control


class FakeGpu:
    def __init__(self, *, architecture=7, slot="0000:72:1d.0", indices=(2, 11), types=None, roles=None):
        self._lock = threading.RLock()
        self.pairing_error = ""
        self.architecture, self.pci_slot = architecture, slot
        self.exports = {0x35AED5E8, 0x814B209F}
        selected = {"slot": slot, "handle": 907, "devid": 0xABCD, "subsys": 0x98765432}
        self.nvapi = SimpleNamespace(
            ok=True, gpu=907, selected=selected, _i=lambda ident, *args: object() if ident in self.exports else None
        )
        self.nvml = SimpleNamespace(selected={key: value for key, value in selected.items() if key != "handle"})
        info, control = packets(indices, types=types, roles=roles)
        self.payloads = {fan.INFO_COMMAND: info, fan.CONTROL_COMMAND: control}
        self._current_limit_transport = None
        self._current_limit_layout_choice = None
        self.calls, self.outcomes = [], {}
        self.after_call = None
        self.transport_reads = 0

    def slot(self):
        return self.pci_slot

    def arch(self):
        return self.architecture

    def _current_limit_transport_layouts(self):
        self.transport_reads += 1
        if self._current_limit_transport is None:
            header = [0] * 17
            header[2], header[14], header[15] = 54420, 0x2080A612, 54352
            fields = dict.fromkeys(("hAdapter", "hDevice", "Type", "Flags", "hContext"), 0)
            self._current_limit_transport = header, fields

    def _legacy_clk_escape(self, packet, fields):
        command = packet[14]
        assert (command, packet[15]) in fan._GETS
        assert ctypes.sizeof(packet) == packet[2] == packet[15] + 68
        self.calls.append(command)
        if command == fan.CONTROL_COMMAND:
            assert bytes(packet)[72:76] == self.payloads[fan.INFO_COMMAND][4:8]
        outcomes = self.outcomes.get(command, [])
        result = outcomes.pop(0) if outcomes else (0, 0)
        if isinstance(result, BaseException):
            raise result
        status, rm = result
        packet[16] = rm
        if not status and not rm:
            raw = bytes(self.payloads[command])
            ctypes.memmove(ctypes.addressof(packet) + 68, raw, len(raw))
        if self.after_call:
            self.after_call(packet)
        return status


def inspection(gpu):
    info = gpu.payloads[fan.INFO_COMMAND]
    roles = {str(i + 1): None if n == 255 else n for i, n in enumerate(info[16:20])}
    return {
        "fields": [
            {"key": f"fan.{index}.{role}", "units": units, "current": value, "client_id_to_index": dict(roles)}
            for index in range(16)
            if int.from_bytes(info[4:8], "little") & (1 << index)
            for role, units, value in (
                ("level_minimum", "%", 25),
                ("level_maximum", "%", 100),
                ("rpm_minimum", "RPM", 750 + index),
                ("rpm_maximum", "RPM", 4200 + index),
                ("pwm_hz", "Hz", 26000 + index),
            )
        ]
    }


def observe(gpu):
    result, error = fan.read(gpu)
    assert error is None
    return result


def apply(observation, changes):
    current = copy.deepcopy(observation)
    by_index = {row["index"]: row for row in current["entries"]}
    for key, value in changes.items():
        _, index, role = key.split(".")
        by_index[int(index)][fan._FIELDS[role][0]] = fan._value(role, value)
    return current


def test_unrelated_transactions_do_not_need_fan_capability():
    assert fan.contracts(None, None, {"clock.0.maximum": 200}) == {}
    assert fan.contracts(None, None, {}) == {}
    assert fan.verify(None, {}) == []
    with pytest.raises(fan.FanRuntimeError, match="semantics"):
        fan.contracts(None, None, {"fan.2.pwm_frequency": 40})


@pytest.mark.parametrize("architecture", [4, 5, 6, 7, 8, 9, 10])
def test_alternate_identity_sparse_full_capacity_and_nonordinal_role_map(architecture):
    gpu = FakeGpu(architecture=architecture, slot="00000000:fe:1f.7", indices=(0, 15), roles=[15, None, 0, None])
    result = observe(gpu)
    assert result["identity"]["device_id"] == 0xABCD
    assert result["client_id_to_index"] == {"1": 15, "2": None, "3": 0, "4": None}
    assert result["entries"][0]["client_ids"] == [3]
    assert result["stable_reads"] == 2
    changes = {"fan.15.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    assert fan.verify({"fan_bounds": apply(result, changes)}, json.loads(json.dumps(saved))) == list(changes)
    assert gpu.calls == [fan.INFO_COMMAND, fan.CONTROL_COMMAND] * 4


def test_all_four_bounds_apply_and_restore_with_unchanged_peers():
    gpu = FakeGpu()
    original = observe(gpu)
    changes = {
        "fan.2.level_minimum": 26,
        "fan.2.level_maximum": 95,
        "fan.2.rpm_minimum": 800,
        "fan.11.rpm_maximum": 4000,
    }
    saved = fan.contracts(gpu, inspection(gpu), changes)
    assert saved["fan.2.level_minimum"]["before"] == 16384
    assert saved["fan.2.level_minimum"]["after"] == 17039
    current = apply(original, changes)
    assert fan.verify({"fan_bounds": current}, saved) == sorted(changes)
    assert fan.verify({"fan_bounds": original}, saved, restore=True) == sorted(changes)
    with pytest.raises(fan.FanRuntimeError, match="active value"):
        fan.verify({"fan_bounds": original}, saved)
    with pytest.raises(fan.FanRuntimeError, match="active value"):
        fan.verify({"fan_bounds": current}, saved, restore=True)


def test_q16_rounding_matches_integer_percent_conversion():
    assert fan._value("level_minimum", 30) == 19661
    assert fan._value("level_minimum", 31) == 20316
    assert fan._value("level_maximum", 100) == 65536
    assert fan._value("rpm_maximum", 65535) == 65535


@pytest.mark.parametrize("failure", [(123, 0), (0, 5), OSError("interrupted capture")])
def test_transient_failure_retries_whole_pair_with_fresh_transport(failure):
    gpu = FakeGpu()
    gpu.outcomes[fan.CONTROL_COMMAND] = [failure]
    result = observe(gpu)
    assert result["stable_reads"] == 2
    assert gpu.calls == [fan.INFO_COMMAND, fan.CONTROL_COMMAND] * 3
    assert gpu.transport_reads == 3


def test_exhausted_transient_failure_returns_no_stale_data_and_can_recover():
    gpu = FakeGpu()
    observe(gpu)
    gpu.outcomes[fan.INFO_COMMAND] = [(99, 0), (99, 0)]
    result, error = fan.read(gpu)
    assert result is None and "NTSTATUS" in error
    assert gpu._current_limit_transport is None
    assert observe(gpu)["stable_reads"] == 2


@pytest.mark.parametrize("rm", [0x1F, 0x56])
def test_unsupported_command_does_not_retry(rm):
    gpu = FakeGpu()
    gpu.outcomes[fan.INFO_COMMAND] = [(0, rm)]
    result, error = fan.read(gpu)
    assert result is None and "rejected" in error
    assert gpu.calls == [fan.INFO_COMMAND]


@pytest.mark.parametrize("mutation", ["command", "size", "packet_size", "mask", "group", "map", "client_mask"])
def test_corrupted_geometry_is_rejected_without_retry(mutation):
    gpu = FakeGpu()
    if mutation in ("command", "size", "packet_size"):
        field = {"command": 14, "size": 15, "packet_size": 2}[mutation]
        gpu.after_call = lambda packet: packet.__setitem__(field, packet[field] + 1)
    elif mutation == "mask":
        gpu.payloads[fan.CONTROL_COMMAND][4] ^= 1
    elif mutation == "group":
        gpu.payloads[fan.CONTROL_COMMAND][0] = 0
    elif mutation == "map":
        gpu.payloads[fan.INFO_COMMAND][16] = 12
    else:
        gpu.payloads[fan.INFO_COMMAND][12] |= 1
    result, error = fan.read(gpu)
    assert result is None and error
    assert gpu.calls.count(fan.INFO_COMMAND) == 1


@pytest.mark.parametrize("mutation", ["pairing", "handle", "nvml", "architecture", "export", "transport"])
def test_target_and_capability_checks_before_get(mutation):
    gpu = FakeGpu()
    if mutation == "pairing":
        gpu.pairing_error = "unpaired"
    elif mutation == "handle":
        gpu.nvapi.selected["handle"] = 999
    elif mutation == "nvml":
        gpu.nvml.selected["subsys"] ^= 1
    elif mutation == "architecture":
        gpu.architecture = 3
    elif mutation == "export":
        gpu.exports.clear()
    else:
        gpu._current_limit_transport_layouts()
        gpu._current_limit_transport[0][14] ^= 1
    result, error = fan.read(gpu)
    assert result is None and error and not gpu.calls


def test_identity_change_during_get_is_rejected():
    gpu = FakeGpu()
    gpu.after_call = lambda packet: gpu.nvapi.selected.__setitem__("devid", 0x1111)
    result, error = fan.read(gpu)
    assert result is None and "disagree" in error
    assert len(gpu.calls) == 1


def test_bound_change_between_reads_is_rejected():
    gpu = FakeGpu()

    def change(packet):
        if packet[14] == fan.CONTROL_COMMAND and len(gpu.calls) == 2:
            struct.pack_into(
                "<I", gpu.payloads[fan.CONTROL_COMMAND], fan.CONTROL_OFFSET + 2 * fan.CONTROL_STRIDE + 24, 19000
            )

    gpu.after_call = change
    result, error = fan.read(gpu)
    assert result is None and "changed between reads" in error


@pytest.mark.parametrize("peer", ["unknown", "mismatched_type", "bad_bounds"])
def test_unsupported_unrelated_row_remains_diagnostic_without_erasing_selected_capability(peer):
    gpu = FakeGpu(types={11: 99} if peer == "unknown" else None)
    off = fan.CONTROL_OFFSET + 11 * fan.CONTROL_STRIDE
    if peer == "mismatched_type":
        gpu.payloads[fan.CONTROL_COMMAND][off] = 77
    elif peer == "bad_bounds":
        struct.pack_into("<I", gpu.payloads[fan.CONTROL_COMMAND], off + 24, 65537)
    result = observe(gpu)
    assert result["entries"][1]["issues"]
    changes = {"fan.2.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    assert fan.verify({"fan_bounds": apply(result, changes)}, saved) == list(changes)
    with pytest.raises(fan.FanRuntimeError, match="understood"):
        fan.contracts(gpu, inspection(gpu), {"fan.11.level_minimum": 26})


@pytest.mark.parametrize(
    "mutation", ["units", "current", "missing", "map", "duplicate", "bad_map_type", "unmapped", "hidden"]
)
def test_selected_bios_metadata_requires_all_bounds_and_exact_role_map(mutation):
    gpu = FakeGpu()
    meta = inspection(gpu)
    if mutation == "units":
        meta["fields"][0]["units"] = "RPM"
    elif mutation == "current":
        meta["fields"][1]["current"] = 99
    elif mutation == "missing":
        meta["fields"].pop(2)
    elif mutation == "map":
        meta["fields"][0]["client_id_to_index"].update({"1": 11, "2": 2})
    elif mutation == "duplicate":
        meta["fields"][0]["client_id_to_index"]["2"] = 2
    elif mutation == "bad_map_type":
        meta["fields"][0]["client_id_to_index"]["1"] = 2.0
    elif mutation == "unmapped":
        gpu.payloads[fan.INFO_COMMAND][16] = 255
    else:
        gpu.payloads[fan.INFO_COMMAND][12] &= ~(1 << 2)
    with pytest.raises(fan.FanRuntimeError):
        fan.contracts(gpu, meta, {"fan.2.level_minimum": 26})


@pytest.mark.parametrize("value", [True, None, "26", -1, 101, 25.5, float("nan"), float("inf")])
def test_requested_duty_units_are_exact(value):
    gpu = FakeGpu()
    with pytest.raises(fan.FanRuntimeError):
        fan.contracts(gpu, inspection(gpu), {"fan.2.level_minimum": value})


def test_selected_minimum_and_maximum_cannot_cross():
    gpu = FakeGpu()
    with pytest.raises(fan.FanRuntimeError, match="minimum"):
        fan.contracts(gpu, inspection(gpu), {"fan.2.level_minimum": 100})
    with pytest.raises(fan.FanRuntimeError, match="minimum"):
        fan.contracts(gpu, inspection(gpu), {"fan.2.rpm_minimum": 5000})


@pytest.mark.parametrize("mutation", ["peer", "identity", "map", "type", "mask", "selector", "stable", "layout"])
def test_changed_peers_binding_or_layout_cannot_verify(mutation):
    gpu = FakeGpu()
    changes = {"fan.2.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    current = apply(observe(gpu), changes)
    if mutation == "peer":
        current["entries"][1]["rpm_maximum"] -= 1
    elif mutation == "identity":
        current["identity"]["subsystem_id"] += 1
    elif mutation == "map":
        current["client_id_to_index"].update({"1": 11, "2": 2})
        current["entries"][0]["client_ids"], current["entries"][1]["client_ids"] = [2], [1]
    elif mutation == "type":
        current["entries"][0].update(type=2, control_type=2)
    elif mutation == "mask":
        current["mask_hex"] = "00000000"
    elif mutation == "selector":
        current["entries"][0]["info_control_selector_raw"] = 1
    elif mutation == "stable":
        current["stable_reads"] = 1
    else:
        current["control_stride"] += 4
    with pytest.raises(fan.FanRuntimeError):
        fan.verify({"fan_bounds": current}, saved)


def test_dynamic_controller_request_and_raw_payloads_are_not_unchanged_bounds():
    gpu = FakeGpu()
    changes = {"fan.2.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    current = apply(observe(gpu), changes)
    for row in current["entries"]:
        row.update(control_request_raw=555, control_override_flag_raw=1, info_hex="aa", control_hex="bb")
    current["entries"].reverse()
    assert fan.verify({"fan_bounds": current}, saved) == list(changes)


@pytest.mark.parametrize("mutation", ["schema", "unit", "before", "peer_removed", "selected", "unknown", "peer_bool"])
def test_malformed_receipts_cannot_verify(mutation):
    gpu = FakeGpu()
    changes = {"fan.2.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    receipt = saved["fan.2.level_minimum"]
    if mutation == "schema":
        receipt["schema"] = True
    elif mutation == "unit":
        receipt["unit"] = "%"
    elif mutation == "before":
        receipt["before"] = float(receipt["before"])
    elif mutation == "peer_removed":
        receipt["unchanged"]["bounds"].pop("fan.11.rpm_maximum")
    elif mutation == "selected":
        receipt["selected_fields"] = []
    elif mutation == "unknown":
        saved["fan.2.untraced"] = saved.pop("fan.2.level_minimum")
    else:
        receipt["unchanged"]["bounds"]["fan.11.rpm_minimum"] = True
    with pytest.raises(fan.FanRuntimeError):
        fan.verify({"fan_bounds": apply(observe(gpu), changes)}, saved)


def test_snapshot_unavailability_never_verifies_historical_data():
    gpu = FakeGpu()
    saved = fan.contracts(gpu, inspection(gpu), {"fan.2.level_minimum": 26})
    with pytest.raises(fan.FanRuntimeError, match="unavailable"):
        fan.verify({"fan_bounds": None, "fan_bounds_error": "busy", "last_valid": observe(gpu)}, saved)


def test_process_interrupt_is_not_swallowed():
    gpu = FakeGpu()
    gpu.outcomes[fan.INFO_COMMAND] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        fan.read(gpu)


def test_pwm_loaded_frequency_is_decoded_from_info_with_independent_routing_identity():
    gpu = FakeGpu(indices=(0, 15), roles=[15, None, 0, None], types={0: 2, 15: 3})
    original = observe(gpu)
    assert original["entries"][0]["pwm_frequency_hz"] == 26000
    assert original["entries"][1]["pwm_frequency_hz"] == 26015
    changes = {"fan.15.pwm_hz": 26016}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    receipt = saved["fan.15.pwm_hz"]
    assert receipt["kind"] == "fan2x_pwm" and receipt["unit"] == "Hz"
    assert receipt["before"] == 26015 and receipt["after"] == 26016
    assert receipt["unchanged"]["pwm"]["bindings"]["15"] == {
        "type": 3,
        "control_selector_raw": 2,
        "gpio_function_raw": 57,
        "pwm_function_raw": 95,
    }
    assert fan.verify({"fan_bounds": apply(original, changes)}, json.loads(json.dumps(saved))) == list(changes)
    assert fan.verify({"fan_bounds": original}, saved, restore=True) == list(changes)
    with pytest.raises(fan.FanRuntimeError, match="active value"):
        fan.verify({"fan_bounds": original}, saved)


def test_mixed_pwm_and_bounds_contracts_preserve_all_understood_peers():
    gpu = FakeGpu()
    changes = {"fan.2.pwm_hz": 26003, "fan.2.level_minimum": 26, "fan.11.rpm_maximum": 4000}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    original = observe(gpu)
    assert fan.verify({"fan_bounds": apply(original, changes)}, saved) == sorted(changes)
    assert fan.verify({"fan_bounds": original}, saved, restore=True) == sorted(changes)
    for receipt in saved.values():
        assert receipt["unchanged"]["pwm"]["values"] == {"fan.11.pwm_hz": 26011}
        assert len(receipt["unchanged"]["bounds"]) == 6


@pytest.mark.parametrize("mutation", ["bad_bounds", "missing_bios_bounds", "mismatched_control_type"])
def test_pwm_availability_does_not_require_usable_duty_or_rpm_bounds(mutation):
    gpu = FakeGpu()
    ctl = fan.CONTROL_OFFSET + 2 * fan.CONTROL_STRIDE
    meta = inspection(gpu)
    if mutation == "bad_bounds":
        struct.pack_into("<I", gpu.payloads[fan.CONTROL_COMMAND], ctl + 24, 65537)
    elif mutation == "mismatched_control_type":
        gpu.payloads[fan.CONTROL_COMMAND][ctl] = 99
    else:
        meta["fields"] = [field for field in meta["fields"] if field["key"].endswith("pwm_hz")]
    changes = {"fan.2.pwm_hz": 26003}
    saved = fan.contracts(gpu, meta, changes)
    assert fan.verify({"fan_bounds": apply(observe(gpu), changes)}, saved) == list(changes)
    with pytest.raises(fan.FanRuntimeError):
        fan.contracts(gpu, meta, {"fan.2.level_minimum": 26})


@pytest.mark.parametrize("value", [0, 0x1000000, 0xFFFFFFFF])
def test_missing_pwm_capability_does_not_remove_valid_bound_controls(value):
    gpu = FakeGpu()
    struct.pack_into("<I", gpu.payloads[fan.INFO_COMMAND], fan.INFO_OFFSET + 2 * fan.INFO_STRIDE + 24, value)
    changes = {"fan.2.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    assert "2" not in saved["fan.2.level_minimum"]["unchanged"]["pwm"]["bindings"]
    assert fan.verify({"fan_bounds": apply(observe(gpu), changes)}, saved) == list(changes)
    with pytest.raises(fan.FanRuntimeError, match="understood"):
        fan.contracts(gpu, inspection(gpu), {"fan.2.pwm_hz": 26003})


def test_legacy_bounds_receipt_does_not_require_new_optional_pwm_metadata():
    gpu = FakeGpu()
    changes = {"fan.2.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    for receipt in saved.values():
        del receipt["unchanged"]["pwm"]
    original = observe(gpu)
    for row in original["entries"]:
        for field in ("pwm_frequency_hz", "info_gpio_function_raw", "info_pwm_function_raw"):
            del row[field]
    assert fan.verify({"fan_bounds": apply(original, changes)}, saved) == list(changes)
    assert fan.verify({"fan_bounds": original}, saved, restore=True) == list(changes)


def test_legacy_receipt_preserves_none_selector_for_mismatched_unrelated_control_row():
    gpu = FakeGpu()
    gpu.payloads[fan.CONTROL_COMMAND][fan.CONTROL_OFFSET + 11 * fan.CONTROL_STRIDE] = 77
    changes = {"fan.2.level_minimum": 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    receipt = saved["fan.2.level_minimum"]
    del receipt["unchanged"]["pwm"]
    # This is precisely the identity saved by the pre-PWM decoder, which only
    # returned a selector when INFO/CONTROL type both matched type2 or type3.
    receipt["unchanged"]["state"]["row_types"] = [[2, 3, 3, 2], [11, 3, 77, None]]
    original = observe(gpu)
    assert original["entries"][1]["info_control_selector_raw"] == 2
    assert fan.verify({"fan_bounds": apply(original, changes)}, saved) == list(changes)
    assert fan.verify({"fan_bounds": original}, saved, restore=True) == list(changes)


@pytest.mark.parametrize("selected", ["pwm_hz", "level_minimum"])
@pytest.mark.parametrize("mutation", ["pwm_peer", "pwm_routing", "base_routing", "pwm_unavailable", "known_bound"])
def test_new_contracts_preserve_understood_pwm_peers_and_identity(selected, mutation):
    gpu = FakeGpu()
    changes = {f"fan.2.{selected}": 26003 if selected == "pwm_hz" else 26}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    current = apply(observe(gpu), changes)
    peer = current["entries"][1]
    if mutation == "pwm_peer":
        peer["pwm_frequency_hz"] += 1
    elif mutation == "pwm_routing":
        current["entries"][0]["info_pwm_function_raw"] += 1
    elif mutation == "base_routing":
        peer["info_gpio_function_raw"] += 1
    elif mutation == "pwm_unavailable":
        peer["pwm_frequency_hz"] = 0
    else:
        peer["rpm_maximum"] -= 1
    with pytest.raises(fan.FanRuntimeError):
        fan.verify({"fan_bounds": current}, saved)


@pytest.mark.parametrize("mutation", ["units", "current", "map", "missing"])
def test_pwm_selected_bios_value_units_and_role_map_must_match(mutation):
    gpu = FakeGpu()
    meta = inspection(gpu)
    field = next(field for field in meta["fields"] if field["key"] == "fan.2.pwm_hz")
    if mutation == "units":
        field["units"] = "kHz"
    elif mutation == "current":
        field["current"] += 1
    elif mutation == "map":
        field["client_id_to_index"].update({"1": 11, "2": 2})
    else:
        meta["fields"].remove(field)
    with pytest.raises(fan.FanRuntimeError):
        fan.contracts(gpu, meta, {"fan.2.pwm_hz": 26003})


@pytest.mark.parametrize("value", [True, None, "26003", -1, 0, 0x1000000, 26000.5, float("nan"), float("inf")])
def test_pwm_requested_value_requires_nonzero_whole_bios_encoding(value):
    gpu = FakeGpu()
    with pytest.raises(fan.FanRuntimeError):
        fan.contracts(gpu, inspection(gpu), {"fan.2.pwm_hz": value})


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_pwm",
        "missing_peer",
        "missing_binding",
        "bad_peer",
        "bad_binding",
        "bad_unit",
        "bad_kind",
        "zero_before",
    ],
)
def test_malformed_pwm_receipt_cannot_verify(mutation):
    gpu = FakeGpu()
    changes = {"fan.2.pwm_hz": 26003}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    receipt = saved["fan.2.pwm_hz"]
    if mutation == "missing_pwm":
        del receipt["unchanged"]["pwm"]
    elif mutation == "missing_peer":
        receipt["unchanged"]["pwm"]["values"].clear()
    elif mutation == "missing_binding":
        del receipt["unchanged"]["pwm"]["bindings"]["2"]
    elif mutation == "bad_peer":
        receipt["unchanged"]["pwm"]["values"]["fan.11.pwm_hz"] = 26011.0
    elif mutation == "bad_binding":
        receipt["unchanged"]["pwm"]["bindings"]["2"]["control_selector_raw"] = 2.0
    elif mutation == "bad_unit":
        receipt["unit"] = "kHz"
    elif mutation == "bad_kind":
        receipt["kind"] = "fan2x_bound"
    else:
        receipt["before"] = 0
    with pytest.raises(fan.FanRuntimeError):
        fan.verify({"fan_bounds": apply(observe(gpu), changes)}, saved)


@pytest.mark.parametrize("field", ["frequency", "routing"])
def test_pwm_metadata_change_between_reads_is_rejected(field):
    gpu = FakeGpu()

    def change(packet):
        if packet[14] == fan.CONTROL_COMMAND and len(gpu.calls) == 2:
            offset = fan.INFO_OFFSET + 2 * fan.INFO_STRIDE + (24 if field == "frequency" else 20)
            struct.pack_into("<I", gpu.payloads[fan.INFO_COMMAND], offset, 12345)

    gpu.after_call = change
    result, error = fan.read(gpu)
    assert result is None and "changed between reads" in error


def test_dynamic_request_fields_do_not_invalidate_pwm_metadata():
    gpu = FakeGpu()
    changes = {"fan.2.pwm_hz": 26003}
    saved = fan.contracts(gpu, inspection(gpu), changes)
    current = apply(observe(gpu), changes)
    for row in current["entries"]:
        row.update(control_request_raw=12345, control_override_flag_raw=1, info_hex="aa", control_hex="bb")
    assert fan.verify({"fan_bounds": current}, saved) == list(changes)
