# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Behavioral MCU regressions using independent literal API capture fixtures.

The fake enforces the write gate, including status-zero ignored writes.
No code in this module loads an NVIDIA library or reaches a GPU.
"""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from druta import i2c_cache, profiles, railctl
from druta.controllers import icx

FIRMWARE = bytes.fromhex("05fc2d13010e")
AUTO_1350 = bytes.fromhex("09009600000000000e01")
MANUAL_950 = bytes.fromhex("0901be00000000000e01")
MANUAL_1000 = bytes.fromhex("0901c800000000000e01")
ENABLE = bytes.fromhex("04c6ebea15")


class Bus:
    """MCU gate/state model with independent transport and readback faults."""

    def __init__(self, *, devid=0x2203, subsys=0x49983842,
                 firmware=FIRMWARE, state=AUTO_1350, enabled=False):
        self.ok = True
        self.selected = {"devid": devid, "subsys": subsys, "slot": "0000:01:00.0"}
        self.gpu = ctypes.c_void_p(0x123400)
        self.firmware, self.state, self.enabled = firmware, bytes(state), enabled
        self.groups = {0x1A: bytes.fromhex("020000"),
                       0x1B: bytes.fromhex("020101"), 0x1C: bytes.fromhex("020303")}
        self.calls, self.reads, self.writes = 0, [], []
        self.read_statuses, self.read_status = {}, {}
        self.untouched_reads = {}
        self.read_overrides, self.returned_sizes = {}, {}
        self.geometry_mutation = None
        self.write_status = self.enable_status = 0
        self.ignore_enable = self.ignore_voltage = False
        self.stale_reads = 0
        self.post_write_read_statuses = []
        self.post_write_untouched_reads = 0
        self.collateral_mutation = None
        self.pending, self.pending_reads = None, 0

    def _i(self, identifier, *_signature):
        self.calls += 1
        return {0x4D7B0709: self._read, 0x283AC65A: self._write}.get(identifier)

    def _request(self, pointer, extra):
        packet = pointer._obj
        prefix = bytes(packet.pbI2cRegAddress[i] for i in range(packet.regAddrSize))
        request = {"register": prefix,
                   "data": bytes(packet.pbData[i] for i in range(packet.cbSize)),
                   "size": packet.cbSize, "address": packet.i2cDevAddress,
                   "port": packet.portId, "port_set": packet.bIsPortIdSet,
                   "speed": packet.i2cSpeed, "speed_enum": packet.i2cSpeedKhz,
                   "version": packet.version, "extra": ctypes.string_at(extra, 8)}
        valid = (packet.i2cDevAddress == 0x5A and packet.portId == 1
                 and packet.bIsPortIdSet == 1 and len(prefix) == 1)
        return packet, request, valid

    def _read(self, _gpu, pointer, extra):
        packet, request, valid = self._request(pointer, extra)
        self.reads.append(request)
        if not valid:
            return -1
        reg = request["register"][0]
        failures = self.read_statuses.get(reg, [])
        status = failures.pop(0) if failures else self.read_status.get(reg, 0)
        if status:
            return status
        if self.untouched_reads.get(reg, 0):
            self.untouched_reads[reg] -= 1
            return 0  # Transport success but MCU output buffer was not filled.
        if reg == 0x19 and self.pending is not None:
            if self.pending_reads:
                self.pending_reads -= 1
            else:
                self.state, self.pending = self.pending, None
        values = {0xB1: self.firmware,
                  0xB2: bytes((4, 0xFC if self.enabled else 0xFE, 0, 0, 0)),
                  0x19: self.state, **self.groups}
        data = self.read_overrides.get(reg, values.get(reg))
        if data is None:
            return -1
        for i, byte in enumerate(data[:packet.cbSize]):
            packet.pbData[i] = byte
        if reg in self.returned_sizes:
            packet.cbSize = self.returned_sizes[reg]
        if self.geometry_mutation:
            self.geometry_mutation(reg, packet)
        return 0

    def _write(self, _gpu, pointer, extra):
        _packet, request, valid = self._request(pointer, extra)
        self.writes.append(request)
        if not valid:
            return -1
        reg, data = request["register"][0], request["data"]
        if reg == 0xB2:
            if data != ENABLE:
                return -1
            if self.enable_status == 0 and not self.ignore_enable:
                self.enabled = True
            return self.enable_status
        if reg not in (0x10, 0x18) or len(data) != 4 or data[0] != 3:
            return -1
        if self.write_status:
            return self.write_status
        # The driver reports zero even when the disabled MCU ignores the write.
        if not self.enabled or self.ignore_voltage:
            return 0
        changed = bytearray(self.state)
        if reg == 0x10:
            if data[1] == 0:
                changed[1] = 0
                changed[2:4] = (123).to_bytes(2, "little")
            elif data[1] == 1:
                changed[1:4] = data[1:4]
            else:
                return -1
        else:
            if data[1] != 1:
                return -1
            changed[7] = 1  # Observed transition; its meaning remains unknown.
            changed[8:10] = data[2:4]
        if self.collateral_mutation:
            self.collateral_mutation(reg, changed)
        self.pending, self.pending_reads = bytes(changed), self.stale_reads
        self.read_statuses[0x19] = list(self.post_write_read_statuses)
        self.untouched_reads[0x19] = self.post_write_untouched_reads
        return 0

    @property
    def rail_writes(self):
        return [item for item in self.writes if item["register"] in (b"\x10", b"\x18")]

    @property
    def gate_writes(self):
        return [item for item in self.writes if item["register"] == b"\xb2"]


class PayloadTests(unittest.TestCase):
    def test_captured_requests_and_little_endian_high_byte(self):
        for encode, mv, options, expected in (
            (icx.nvvdd_payload, 950, {}, "0301be00"),
            (icx.nvvdd_payload, 1000, {}, "0301c800"),
            (icx.fbvdd_payload, 1360, {}, "03011001"),
            (icx.fbvdd_payload, 1350, {}, "03010e01"),
            (icx.nvvdd_payload, 1280, {"xoc": True}, "03010001"),
            (icx.nvvdd_payload, 1600, {"xoc": True}, "03014001")):
            self.assertEqual(encode(mv, **options), bytes.fromhex(expected))

    def test_invalid_values_do_not_become_packets(self):
        for encode, value, kwargs in (
            (icx.nvvdd_payload, True, {}), (icx.nvvdd_payload, float("nan"), {}),
            (icx.nvvdd_payload, float("inf"), {}), (icx.nvvdd_payload, 949, {}),
            (icx.nvvdd_payload, 749, {}), (icx.nvvdd_payload, 1155, {}),
            (icx.nvvdd_payload, 1605, {"xoc": True}),
            (icx.fbvdd_payload, 1245, {}), (icx.fbvdd_payload, 1455, {}),
            (icx.fbvdd_payload, 1350.5, {})):
            with self.subTest(value=value, kwargs=kwargs), self.assertRaises(ValueError):
                encode(value, **kwargs)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(icx.time, "sleep", return_value=None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def pair(self, *, architecture=7, **kwargs):
        bus = Bus(**kwargs)
        return icx.EVGAICx(bus, architecture=architecture), bus

    def assert_packet(self, request, reg, data):
        self.assertEqual(request["register"], bytes((reg,)))
        self.assertEqual(request["data"], bytes.fromhex(data))
        self.assertEqual(request["size"], 4)
        self.assertEqual((request["address"], request["port"], request["port_set"]), (0x5A, 1, 1))
        self.assertEqual((request["version"], request["speed"], request["speed_enum"]), (0x00030040, 0xFFFF, 0))
        self.assertEqual(request["extra"], bytes(8))

    def test_identity_is_read_only_and_does_not_probe_write_gate(self):
        controller, bus = self.pair()
        self.assertTrue(controller.present())
        self.assertFalse(bus.enabled)
        self.assertEqual(bus.writes, [])
        self.assertTrue(bus.reads)
        self.assertTrue(all(item["register"] == b"\xb1" for item in bus.reads))

    def test_alternate_pci_identity_and_firmware_revision_are_supported(self):
        controller, bus = self.pair(devid=0x2208, subsys=0x12343842,
                                    firmware=bytes.fromhex("05fc2d13aa55"), enabled=True)
        self.assertTrue(controller.present())
        ok, message = controller.set_nvvdd_mv(950, acknowledged=True)
        self.assertTrue(ok, message)
        self.assertEqual(bus.state, MANUAL_950)

    def test_non_ampere_never_reads_bus(self):
        for architecture in (None, 6, 8, 9):
            with self.subTest(architecture=architecture):
                controller, bus = self.pair(architecture=architecture)
                self.assertFalse(controller.present())
                self.assertFalse(controller.set_nvvdd_mv(950, acknowledged=True)[0])
                self.assertEqual(bus.reads + bus.writes, [])

    def test_wrong_product_or_malformed_firmware_never_writes(self):
        for firmware in (bytes.fromhex("05fc2d12010e"), bytes.fromhex("05fe2d13010e"), bytes(6)):
            with self.subTest(firmware=firmware):
                controller, bus = self.pair(firmware=firmware)
                self.assertFalse(controller.present())
                self.assertFalse(controller.set_nvvdd_mv(950, acknowledged=True)[0])
                self.assertEqual(bus.writes, [])

    def test_unacknowledged_or_invalid_requests_have_no_bus_access(self):
        actions = [lambda c: c.set_nvvdd_mv(950), lambda c: c.set_nvvdd_auto(),
                   lambda c: c.set_fbvdd_mv(1350),
                   lambda c: c.set_nvvdd_mv(951, acknowledged=True),
                   lambda c: c.set_nvvdd_mv(1605, acknowledged=True, xoc=True),
                   lambda c: c.set_fbvdd_mv(float("nan"), acknowledged=True)]
        for action in actions:
            controller, bus = self.pair()
            self.assertFalse(action(controller)[0])
            self.assertEqual(bus.calls, 0)
            self.assertEqual(bus.reads + bus.writes, [])

    def test_bound_handle_or_selection_change_rejects_new_target(self):
        for change in (lambda bus: setattr(bus, "gpu", ctypes.c_void_p(0x987600)),
                       lambda bus: bus.selected.update(devid=0x2208),
                       lambda bus: bus.selected.update(subsys=0x77773842),
                       lambda bus: bus.selected.update(slot="0000:02:00.0")):
            controller, bus = self.pair()
            self.assertTrue(controller.present())
            bus.reads.clear()
            change(bus)
            self.assertFalse(controller.set_nvvdd_mv(950, acknowledged=True)[0])
            self.assertEqual(bus.reads + bus.writes, [])

    def test_disabled_gate_is_verified_enabled_before_single_voltage_write(self):
        controller, bus = self.pair()
        ok, message = controller.set_nvvdd_mv(950, acknowledged=True)
        self.assertTrue(ok, message)
        self.assertTrue(bus.enabled)
        self.assertEqual(bus.state, MANUAL_950)
        self.assertEqual([item["register"] for item in bus.writes], [b"\xb2", b"\x10"])
        self.assertEqual(bus.gate_writes[0]["data"], ENABLE)
        self.assert_packet(bus.rail_writes[0], 0x10, "0301be00")

    def test_enabled_gate_is_not_rewritten(self):
        controller, bus = self.pair(enabled=True)
        self.assertTrue(controller.set_nvvdd_mv(1000, acknowledged=True)[0])
        self.assertEqual(bus.state, MANUAL_1000)
        self.assertEqual(bus.gate_writes, [])
        self.assertEqual(len(bus.rail_writes), 1)
        self.assert_packet(bus.rail_writes[0], 0x10, "0301c800")

    def test_failed_ignored_or_malformed_enable_prevents_voltage_write(self):
        for fault in ("failed", "ignored", "malformed", "unknown"):
            with self.subTest(fault=fault):
                controller, bus = self.pair()
                if fault == "failed":
                    bus.enable_status = -1
                elif fault == "ignored":
                    bus.ignore_enable = True
                elif fault == "malformed":
                    bus.read_overrides[0xB2] = bytes.fromhex("03fc000000")
                else:
                    bus.read_overrides[0xB2] = bytes.fromhex("0401000000")
                self.assertFalse(controller.set_nvvdd_mv(950, acknowledged=True)[0])
                self.assertEqual(bus.rail_writes, [])
                self.assertLessEqual(len(bus.gate_writes), 1)
                self.assertLessEqual(len(bus.reads), 25)

    def test_status_zero_ignored_voltage_fails_without_retrying_write(self):
        controller, bus = self.pair(enabled=True)
        bus.ignore_voltage = True
        self.assertFalse(controller.set_nvvdd_mv(950, acknowledged=True)[0])
        self.assertEqual(bus.state, AUTO_1350)
        self.assertEqual(len(bus.rail_writes), 1)
        self.assertLessEqual(len(bus.reads), 25)

    def test_nonzero_voltage_status_is_failure_without_retry(self):
        controller, bus = self.pair(enabled=True)
        bus.write_status = -1
        self.assertFalse(controller.set_nvvdd_mv(950, acknowledged=True)[0])
        self.assertEqual(bus.state, AUTO_1350)
        self.assertEqual(len(bus.rail_writes), 1)

    def test_stale_state_recovers_by_reading_without_resending(self):
        controller, bus = self.pair(enabled=True)
        bus.stale_reads = 3
        ok, message = controller.set_nvvdd_mv(950, acknowledged=True)
        self.assertTrue(ok, message)
        self.assertEqual(bus.state, MANUAL_950)
        self.assertEqual(len(bus.rail_writes), 1)

    def test_xoc_count_above_255_is_written_and_verified_without_wrapping(self):
        controller, bus = self.pair(enabled=True)
        ok, message = controller.set_nvvdd_mv(1280, acknowledged=True, xoc=True)
        self.assertTrue(ok, message)
        self.assertEqual(bus.state[1:4], bytes.fromhex("010001"))
        self.assert_packet(bus.rail_writes[0], 0x10, "03010001")
        self.assertEqual(controller.telemetry()["nvvdd_request_mv"], 1280)

    def test_transient_read_recovers_and_persistent_failure_is_bounded(self):
        for statuses, expected in (([-1, -1, -1, 0], True), ([-1] * 100, False)):
            with self.subTest(expected=expected):
                controller, bus = self.pair(enabled=True)
                bus.post_write_read_statuses = statuses
                ok, message = controller.set_nvvdd_mv(950, acknowledged=True)
                self.assertEqual(ok, expected, message)
                self.assertEqual(len(bus.rail_writes), 1)
                self.assertLessEqual(len(bus.reads), 25)

    def test_status_zero_untouched_output_recovers_without_resending_voltage(self):
        with self.subTest(path="identity"):
            controller, bus = self.pair()
            bus.untouched_reads[0xB1] = 1
            self.assertTrue(controller.still_present())
            self.assertEqual(sum(item["register"] == b"\xb1" for item in bus.reads), 2)
            self.assertEqual(bus.writes, [])
        with self.subTest(path="requested state"):
            controller, bus = self.pair()
            bus.untouched_reads[0x19] = 1
            self.assertEqual(controller.read_state(), AUTO_1350)
            self.assertEqual(sum(item["register"] == b"\x19" for item in bus.reads), 2)
            self.assertEqual(bus.writes, [])
        with self.subTest(path="verification after one voltage write"):
            controller, bus = self.pair(enabled=True)
            bus.post_write_untouched_reads = 1
            ok, message = controller.set_nvvdd_mv(950, acknowledged=True)
            self.assertTrue(ok, message)
            self.assertEqual(bus.state, MANUAL_950)
            self.assertEqual(len(bus.rail_writes), 1)
            self.assertEqual(bus.gate_writes, [])
            self.assertLessEqual(len(bus.reads), 10)

    def test_auto_zero_request_does_not_validate_placeholder_count(self):
        controller, bus = self.pair(state=MANUAL_950, enabled=True)
        ok, message = controller.set_nvvdd_auto(acknowledged=True)
        self.assertTrue(ok, message)
        self.assertEqual(bus.state[1:4], bytes.fromhex("007b00"))
        self.assertEqual(bus.state[4:], MANUAL_950[4:])
        self.assertEqual(len(bus.rail_writes), 1)
        self.assert_packet(bus.rail_writes[0], 0x10, "03000000")
        telemetry = controller.telemetry()
        self.assertEqual(telemetry["nvvdd_mode"], "auto")
        self.assertIsNone(telemetry["nvvdd_request_mv"])

    def test_fbvdd_preserves_auto_and_accepts_unknown_byte7_change(self):
        for byte7 in (0, 1):
            with self.subTest(byte7=byte7):
                initial = bytearray(AUTO_1350)
                initial[2:4] = (123).to_bytes(2, "little")
                initial[7] = byte7
                controller, bus = self.pair(state=initial, enabled=True)
                ok, message = controller.set_fbvdd_mv(1360, acknowledged=True)
                self.assertTrue(ok, message)
                self.assertEqual(bus.state[1:7], bytes(initial[1:7]))
                self.assertEqual(bus.state[8:10], bytes.fromhex("1001"))
                self.assert_packet(bus.rail_writes[0], 0x18, "03011001")

    def test_unrelated_state_change_fails_each_setter(self):
        for method, value in (("set_nvvdd_mv", 950), ("set_fbvdd_mv", 1360)):
            with self.subTest(method=method):
                controller, bus = self.pair(enabled=True)
                def mutate(reg, state):
                    state[8 if reg == 0x10 else 2] ^= 1
                bus.collateral_mutation = mutate
                self.assertFalse(getattr(controller, method)(value, acknowledged=True)[0])
                self.assertEqual(len(bus.rail_writes), 1)

    def test_bad_state_size_header_or_geometry_prevents_write(self):
        for fault in ("size", "header", "geometry", "unchanged_sentinel"):
            with self.subTest(fault=fault):
                controller, bus = self.pair(enabled=True)
                if fault == "size":
                    bus.returned_sizes[0x19] = 9
                elif fault == "header":
                    bus.read_overrides[0x19] = b"\x08" + AUTO_1350[1:]
                elif fault == "geometry":
                    def mutate(reg, packet):
                        if reg == 0x19:
                            packet.portId = 2
                    bus.geometry_mutation = mutate
                else:
                    bus.read_overrides[0x19] = b"\xa5" * 10
                self.assertIsNone(controller.read_state())
                self.assertFalse(controller.set_nvvdd_mv(950, acknowledged=True)[0])
                self.assertEqual(bus.writes, [])

    def test_still_present_retries_transient_identity_without_writing(self):
        controller, bus = self.pair()
        bus.read_statuses[0xB1] = [-1, 0]
        self.assertTrue(controller.still_present())
        self.assertEqual(bus.writes, [])
        self.assertGreaterEqual(len(bus.reads), 2)

    def test_telemetry_is_requested_state_not_measured_voltage(self):
        controller, bus = self.pair(state=MANUAL_950, enabled=True)
        telemetry = controller.telemetry()
        self.assertTrue(telemetry["firmware_ok"])
        self.assertTrue(telemetry["state_available"])
        self.assertEqual(telemetry["nvvdd_mode"], "manual")
        self.assertEqual(telemetry["nvvdd_request_mv"], 950)
        self.assertEqual(telemetry["fbvdd_request_mv"], 1350)
        self.assertTrue(telemetry["write_enabled"])
        self.assertNotIn("offset_mv", telemetry)
        self.assertNotIn("measured_mv", telemetry)
        self.assertEqual(bus.writes, [])

    def test_partial_state_preserves_independent_fbvdd_telemetry(self):
        state = bytearray(AUTO_1350)
        state[1] = 0x7F
        controller, _bus = self.pair(state=state)
        telemetry = controller.telemetry()
        self.assertIsNone(telemetry["nvvdd_mode"])
        self.assertIsNone(telemetry["nvvdd_request_mv"])
        self.assertEqual(telemetry["fbvdd_request_mv"], 1350)

    def test_profiles_never_capture_or_replay_voltage(self):
        controller, bus = self.pair()
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(SimpleNamespace(static={}), state, controller)
        self.assertIsNone(state["i2c"])
        self.assertEqual(bus.calls, 0)
        info = {"uuid": "GPU-1", "name": "3090 Ti", "vbios": "94",
                "driver": "617.42", "slot": "0000:01:00.0"}
        saved = {"schema": 2, "device": info, "vf_applicable": False,
                 "i2c": dict(profiles.rail_identity(controller), offset_mv=0)}
        gpu = SimpleNamespace(static=info, vf_curve_applicable=lambda: False)
        self.assertIn("cannot be replayed", profiles.preflight(gpu, saved, controller) or "")
        self.assertTrue(i2c_cache._readable(controller))
        self.assertEqual(bus.calls, 0)


class DiscoveryTests(unittest.TestCase):
    def test_icx_scope_probes_one_route_and_forwards_architecture(self):
        seen = []
        def make(nvapi, *, architecture, port, addr7):
            seen.append((architecture, port, addr7))
            return SimpleNamespace(present=lambda: True, telemetry=lambda: {"firmware_ok": True})
        with patch("druta.railctl.load_profiles", return_value=[]), \
                patch("druta.controllers.icx.EVGAICx", side_effect=make) as ctor, \
                patch("druta.controllers.nct3933.NCT3933U") as nct, \
                patch("druta.controllers.ncp4206.NCP4206") as ncp, \
                patch("druta.controllers.up9512r.UP9512R") as up:
            hits = railctl.discover(SimpleNamespace(ok=True, selected={}),
                                    architecture=7, controller="EVGA iCX")
        self.assertEqual(seen, [(7, 1, 0x2D)])
        self.assertEqual(len(hits), 1)
        ctor.assert_called_once()
        nct.assert_not_called()
        ncp.assert_not_called()
        up.assert_not_called()

    def test_other_scope_or_route_does_not_construct_icx(self):
        with patch("druta.railctl.load_profiles", return_value=[]), \
                patch("druta.controllers.icx.EVGAICx") as ctor:
            hits = railctl.discover(SimpleNamespace(ok=True, selected={}),
                architecture=7, controller="EVGA iCX", routes=[(2, 0x30)])
        self.assertEqual(hits, [])
        ctor.assert_not_called()
        with patch("druta.railctl.load_profiles", return_value=[]), \
                patch("druta.controllers.icx.EVGAICx") as ctor, \
                patch("druta.controllers.ncp4206.NCP4206",
                      return_value=SimpleNamespace(present=lambda: False)):
            hits = railctl.discover(SimpleNamespace(ok=True, selected={}),
                architecture=7, controller="NCP4206", routes=[(1, 0x2D)])
        self.assertEqual(hits, [])
        ctor.assert_not_called()

    def test_discovery_is_read_only_with_disabled_gate(self):
        bus = Bus()
        with patch("druta.railctl.load_profiles", return_value=[]):
            hits = railctl.discover(bus, architecture=7, controller="EVGA iCX")
        self.assertEqual(len(hits), 1)
        self.assertEqual(bus.writes, [])
        self.assertFalse(bus.enabled)
        self.assertTrue(all(item["register"] in (b"\xb1", b"\x19", b"\xb2",
                                                 b"\x1a", b"\x1b", b"\x1c")
                            for item in bus.reads))


if __name__ == "__main__":
    unittest.main()
