# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent MCU setting changes using literal paired-capture fixtures."""
import ctypes

import pytest

from druta.controllers import icx
from tests.controllers.test_icx import AUTO_1350, Bus


class SettingBus(Bus):
    """A gated MCU whose setting application and readback can independently fail."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.ignore_setting = False
        self.pending_setting = None
        self.after_setting = None

    def _read(self, gpu, pointer, extra):
        register = pointer._obj.pbI2cRegAddress[0]
        if self.pending_setting is not None:
            target, changed, remaining = self.pending_setting
            if register == target:
                if remaining:
                    self.pending_setting = (target, changed, remaining - 1)
                else:
                    self.groups[target] = changed
                    self.pending_setting = None
        return super()._read(gpu, pointer, extra)

    def _write(self, gpu, pointer, extra):
        register = pointer._obj.pbI2cRegAddress[0]
        if register in (0xB2, 0x10, 0x18):
            return super()._write(gpu, pointer, extra)
        _packet, request, valid = self._request(pointer, extra)
        self.writes.append(request)
        # Literal contracts from the separate Classified ancillary capture.
        routes = {0x13: (0x1C, 1), 0x1F: (0x1C, 2),
                  0x12: (0x1B, 1), 0x1E: (0x1B, 2), 0x11: (0x1A, 1)}
        data = request["data"]
        if not valid or register not in routes or len(data) != 2 or data[0] != 1:
            return -1
        if ((register in (0x13, 0x1F) and data[1] not in range(3, 11))
                or (register in (0x12, 0x1E) and data[1] not in (0, 1))
                or (register == 0x11 and data[1] not in range(16))):
            return -1
        if self.write_status:
            return self.write_status
        if not self.enabled or self.ignore_setting:
            return 0  # Successful transport does not prove MCU application.
        target, field = routes[register]
        changed = bytearray(self.groups[target])
        changed[field] = data[1]
        if self.after_setting:
            self.after_setting(self, target, changed)
        self.pending_setting = (target, bytes(changed), self.stale_reads)
        self.read_statuses[target] = list(self.post_write_read_statuses)
        self.untouched_reads[target] = self.post_write_untouched_reads
        return 0


@pytest.fixture
def pair(monkeypatch):
    monkeypatch.setattr(icx.time, "sleep", lambda _delay: None)

    def make(**kwargs):
        bus = SettingBus(**kwargs)
        return icx.EVGAICx(bus, architecture=7), bus

    return make


def settings(bus):
    return {0x19: bus.state, **bus.groups}


def assert_packet(request, register, data):
    assert request["register"] == bytes((register,))
    assert request["data"] == bytes.fromhex(data)
    assert request["size"] == 2
    assert (request["address"], request["port"], request["port_set"]) == (0x5A, 1, 1)
    assert (request["version"], request["speed"], request["speed_enum"]) == (0x30040, 0xFFFF, 0)
    assert request["extra"] == bytes(8)


@pytest.mark.parametrize("rail,value,register,payload,before,after", [
    ("NVVDD", 300, 0x13, "0103", "020403", "020303"),
    ("nvvdd", 400, 0x13, "0104", "020303", "020403"),
    ("NVVDD", 1000, 0x13, "010a", "020303", "020a03"),
    ("FBVDD", 300, 0x1F, "0103", "020304", "020303"),
    ("fbvdd", 400, 0x1F, "0104", "020303", "020304"),
    ("FBVDD", 1000, 0x1F, "010a", "020303", "02030a"),
])
def test_captured_frequency_change_preserves_every_other_group(
        pair, rail, value, register, payload, before, after):
    controller, bus = pair(enabled=True)
    bus.groups[0x1C] = bytes.fromhex(before)
    initial = settings(bus)
    ok, message = controller.set_switching_frequency_khz(rail, value, acknowledged=True)
    assert ok, message
    assert settings(bus) == {**initial, 0x1C: bytes.fromhex(after)}
    assert len(bus.writes) == 1
    assert_packet(bus.writes[0], register, payload)
    assert controller.telemetry()[rail.lower() + "_frequency_khz"] == value


@pytest.mark.parametrize("rail,enabled,register,payload,before,after", [
    ("NVVDD", False, 0x12, "0100", "020101", "020001"),
    ("nvvdd", True, 0x12, "0101", "020001", "020101"),
    ("FBVDD", False, 0x1E, "0100", "020101", "020100"),
    ("fbvdd", True, 0x1E, "0101", "020100", "020101"),
])
def test_captured_ocp_change_preserves_every_other_group(
        pair, rail, enabled, register, payload, before, after):
    controller, bus = pair(enabled=True)
    bus.groups[0x1B] = bytes.fromhex(before)
    initial = settings(bus)
    ok, message = controller.set_ocp_enabled(rail, enabled, acknowledged=True)
    assert ok, message
    assert settings(bus) == {**initial, 0x1B: bytes.fromhex(after)}
    assert len(bus.writes) == 1
    assert_packet(bus.writes[0], register, payload)
    assert controller.telemetry()[rail.lower() + "_ocp_enabled"] is enabled


@pytest.mark.parametrize("level,payload,before,after", [
    (1, "0101", "020000", "020100"),
    (15, "010f", "020100", "020f00"),
    (0, "0100", "020f00", "020000"),
])
def test_captured_loadline_changes_preserve_every_other_group(pair, level, payload, before, after):
    controller, bus = pair(enabled=True)
    bus.groups[0x1A] = bytes.fromhex(before)
    initial = settings(bus)
    ok, message = controller.set_loadline(level, acknowledged=True)
    assert ok, message
    assert settings(bus) == {**initial, 0x1A: bytes.fromhex(after)}
    assert len(bus.writes) == 1
    assert_packet(bus.writes[0], 0x11, payload)
    assert controller.telemetry()["loadline_level"] == level


@pytest.mark.parametrize("method,args,acknowledged", [
    ("set_switching_frequency_khz", ("NVVDD", 400), False),
    ("set_ocp_enabled", ("FBVDD", False), False),
    ("set_loadline", (1,), False),
    ("set_switching_frequency_khz", ("MSVDD", 400), True),
    ("set_switching_frequency_khz", (None, 400), True),
    *[("set_switching_frequency_khz", ("NVVDD", value), True)
      for value in (True, "400", float("nan"), float("inf"), 299, 350, 1001, 400.5)],
    *[("set_ocp_enabled", ("FBVDD", value), True) for value in (0, 1, "Enable", None)],
    *[("set_loadline", (value,), True) for value in (True, 1.0, -1, 16, "1", None)],
])
def test_invalid_or_unacknowledged_setting_has_no_bus_access(pair, method, args, acknowledged):
    controller, bus = pair()
    assert not getattr(controller, method)(*args, acknowledged=acknowledged)[0]
    assert bus.calls == 0
    assert bus.reads + bus.writes == []


def test_telemetry_keeps_unknown_siblings_and_missing_groups_independent(pair):
    controller, bus = pair()
    bus.groups[0x1C] = bytes.fromhex("02047f")
    bus.groups[0x1B] = bytes.fromhex("027f00")
    bus.read_status[0x1A] = -1
    result = controller.telemetry()
    assert result["nvvdd_frequency_khz"] == 400
    assert result["fbvdd_frequency_khz"] is None
    assert result["nvvdd_ocp_enabled"] is None
    assert result["fbvdd_ocp_enabled"] is False
    assert result["frequency_state_available"] is True
    assert result["ocp_state_available"] is True
    assert result["loadline_state_available"] is False
    assert result["loadline_level"] is None
    assert result["nvvdd_mode"] == "auto"
    assert result["fbvdd_request_mv"] == 1350
    assert bus.writes == []


@pytest.mark.parametrize("missing", [0x19, 0x1A, 0x1B])
def test_unrelated_missing_readback_does_not_block_frequency(pair, missing):
    controller, bus = pair(enabled=True)
    bus.read_status[missing] = -1
    ok, message = controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)
    assert ok, message
    assert "unavailable" in message.lower()
    assert f"{missing:02X}" in message
    assert bus.groups[0x1C] == bytes.fromhex("020403")
    assert len(bus.writes) == 1


@pytest.mark.parametrize("fault", ["missing", "framing", "size", "geometry"])
def test_invalid_selected_readback_prevents_even_gate_enable(pair, fault):
    controller, bus = pair()
    if fault == "missing":
        bus.read_status[0x1C] = -1
    elif fault == "framing":
        bus.groups[0x1C] = bytes.fromhex("030303")
    elif fault == "size":
        bus.returned_sizes[0x1C] = 2
    else:
        def mutate(register, packet):
            if register == 0x1C:
                packet.portId = 2
        bus.geometry_mutation = mutate
    assert not controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)[0]
    assert bus.writes == []
    assert len(bus.reads) <= 4
    assert controller.telemetry()["fbvdd_request_mv"] == 1350


def test_unknown_sibling_is_preserved_without_disabling_selected_frequency(pair):
    controller, bus = pair(enabled=True)
    bus.groups[0x1C] = bytes.fromhex("02037f")
    ok, message = controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)
    assert ok, message
    assert bus.groups[0x1C] == bytes.fromhex("02047f")
    assert len(bus.writes) == 1


def test_unknown_ocp_sibling_does_not_prevent_known_selected_control(pair):
    controller, bus = pair(enabled=True)
    bus.groups[0x1B] = bytes.fromhex("027f01")
    ok, message = controller.set_ocp_enabled("FBVDD", False, acknowledged=True)
    assert ok, message
    assert bus.groups[0x1B] == bytes.fromhex("027f00")
    assert len(bus.writes) == 1


@pytest.mark.parametrize("register,raw,method,args", [
    (0x1C, "020203", "set_switching_frequency_khz", ("NVVDD", 400)),
    (0x1C, "02030b", "set_switching_frequency_khz", ("FBVDD", 400)),
    (0x1B, "027f01", "set_ocp_enabled", ("NVVDD", True)),
    (0x1B, "020102", "set_ocp_enabled", ("FBVDD", False)),
    (0x1A, "021000", "set_loadline", (1,)),
])
def test_unknown_selected_encoding_refuses_before_gate_enable(pair, register, raw, method, args):
    controller, bus = pair()
    bus.groups[register] = bytes.fromhex(raw)
    assert not getattr(controller, method)(*args, acknowledged=True)[0]
    assert bus.writes == []
    assert bus.enabled is False


def test_loadline_preserves_unknown_byte_and_does_not_invent_second_rail(pair):
    controller, bus = pair(enabled=True)
    bus.groups[0x1A] = bytes.fromhex("02007f")
    ok, message = controller.set_loadline(15, acknowledged=True)
    assert ok, message
    assert bus.groups[0x1A] == bytes.fromhex("020f7f")
    result = controller.telemetry()
    assert result["loadline_state_available"] is True
    assert result["loadline_level"] == 15
    assert not any("fbvdd" in key and "loadline" in key for key in result)
    assert len(bus.writes) == 1


def test_unknown_loadline_level_does_not_discard_other_settings(pair):
    controller, bus = pair()
    bus.groups[0x1A] = bytes.fromhex("021000")
    result = controller.telemetry()
    assert result["loadline_state_available"] is True
    assert result["loadline_level"] is None
    assert result["nvvdd_frequency_khz"] == 300
    assert result["fbvdd_ocp_enabled"] is True
    assert result["fbvdd_request_mv"] == 1350
    assert bus.writes == []


def test_loadline_can_be_verified_with_unrelated_voltage_and_frequency_unavailable(pair):
    controller, bus = pair(enabled=True)
    bus.read_status.update({0x19: -1, 0x1C: -1})
    ok, message = controller.set_loadline(1, acknowledged=True)
    assert ok, message
    assert "unavailable" in message.lower() and "19" in message and "1C" in message
    assert bus.groups[0x1A] == bytes.fromhex("020100")
    assert bus.groups[0x1B] == bytes.fromhex("020101")
    assert len(bus.writes) == 1


def test_loadline_unknown_byte_change_fails_verification_without_resending(pair):
    controller, bus = pair(enabled=True)

    def mutate(_bus, _register, changed):
        changed[2] = 1

    bus.after_setting = mutate
    assert not controller.set_loadline(1, acknowledged=True)[0]
    assert len(bus.writes) == 1


@pytest.mark.parametrize("fault", ["ignored", "failed", "unknown"])
def test_gate_failure_does_not_send_setting(pair, fault):
    controller, bus = pair()
    if fault == "ignored":
        bus.ignore_enable = True
    elif fault == "failed":
        bus.enable_status = -1
    else:
        bus.read_overrides[0xB2] = bytes.fromhex("0401000000")
    assert not controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)[0]
    assert all(write["register"] == b"\xb2" for write in bus.writes)
    assert len(bus.writes) <= 1
    assert bus.groups[0x1C] == bytes.fromhex("020303")


def test_disabled_gate_is_enabled_once_then_setting_verified(pair):
    controller, bus = pair()
    ok, message = controller.set_switching_frequency_khz("FBVDD", 400, acknowledged=True)
    assert ok, message
    assert [write["register"] for write in bus.writes] == [b"\xb2", b"\x1f"]
    assert bus.groups[0x1C] == bytes.fromhex("020304")


@pytest.mark.parametrize("fault", ["ignored", "status", "unreadable", "untouched", "stale"])
def test_unverified_setting_fails_bounded_without_resending(pair, fault):
    controller, bus = pair(enabled=True)
    if fault == "ignored":
        bus.ignore_setting = True
    elif fault == "status":
        bus.write_status = -1
    elif fault == "unreadable":
        bus.post_write_read_statuses = [-1] * 100
    elif fault == "untouched":
        bus.post_write_untouched_reads = 100
    else:
        bus.stale_reads = 100
    assert not controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)[0]
    assert len(bus.writes) == 1
    assert len(bus.reads) <= 15


@pytest.mark.parametrize("fault", ["stale", "unreadable", "untouched"])
def test_temporary_readback_failure_recovers_without_resending(pair, fault):
    controller, bus = pair(enabled=True)
    if fault == "stale":
        bus.stale_reads = 3
    elif fault == "unreadable":
        bus.post_write_read_statuses = [-1, -1, -1, 0]
    else:
        bus.post_write_untouched_reads = 1
    ok, message = controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)
    assert ok, message
    assert len(bus.writes) == 1
    assert bus.groups[0x1C] == bytes.fromhex("020403")


@pytest.mark.parametrize("changed_register", [0x19, 0x1A, 0x1B, 0x1C])
def test_collateral_or_sibling_change_is_not_success_and_never_rewritten(pair, changed_register):
    controller, bus = pair(enabled=True)

    def mutate(target_bus, _register, changed):
        if changed_register == 0x19:
            target_bus.state = bytes.fromhex("0901be00000000000e01")
        elif changed_register == 0x1C:
            changed[2] = 4
        else:
            value = bytearray(target_bus.groups[changed_register])
            value[1] ^= 1
            target_bus.groups[changed_register] = bytes(value)

    bus.after_setting = mutate
    assert not controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)[0]
    assert len(bus.writes) == 1


def test_lost_unrelated_readback_qualifies_selected_success(pair):
    controller, bus = pair(enabled=True)

    def lose_readback(target_bus, _register, _changed):
        target_bus.read_status[0x19] = -1

    bus.after_setting = lose_readback
    ok, message = controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)
    assert ok, message
    assert "unavailable" in message.lower() and "19" in message
    assert len(bus.writes) == 1
    assert bus.state == AUTO_1350


@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("fault", ["framing", "geometry", "binding"])
def test_malformed_collateral_is_not_reported_as_transiently_unavailable(pair, fault, after_write):
    controller, bus = pair(enabled=True)

    def install_fault(target_bus, _register=None, _changed=None):
        if fault == "framing":
            target_bus.read_overrides[0x19] = b"\x08" + AUTO_1350[1:]
        else:
            def mutate(register, packet):
                if register == 0x19:
                    if fault == "geometry":
                        packet.portId = 2
                    else:
                        target_bus.gpu = ctypes.c_void_p(0x987600)
            target_bus.geometry_mutation = mutate

    if after_write:
        bus.after_setting = install_fault
    else:
        install_fault(bus)
    ok, message = controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)
    assert not ok, message
    assert len(bus.writes) == int(after_write)
    assert "setting verified" not in message


@pytest.mark.parametrize("during_read", [False, True])
def test_changed_gpu_binding_prevents_setting(pair, during_read):
    controller, bus = pair(enabled=True)
    if during_read:
        def change_binding(_register, _packet):
            bus.gpu = ctypes.c_void_p(0x987600)
        bus.geometry_mutation = change_binding
    else:
        bus.gpu = ctypes.c_void_p(0x987600)
    assert not controller.set_switching_frequency_khz("NVVDD", 400, acknowledged=True)[0]
    assert bus.writes == []


def test_ancillary_state_read_tolerates_one_unavailable_group(pair):
    controller, bus = pair()
    bus.read_statuses[0x1C] = [-1, 0]
    bus.read_status[0x1B] = -1
    assert controller.read_ancillary_state() == {
        0x1A: bytes.fromhex("020000"), 0x1B: None, 0x1C: bytes.fromhex("020303")}
    assert bus.writes == []
