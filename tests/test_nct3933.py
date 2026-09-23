# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""NCT3933U current-DAC contract, with a simulated one-byte I2C transport."""

import ctypes
import unittest
from types import SimpleNamespace

from druta.controllers import nct3933 as nct
from druta import railctl


class FakeNCT(nct.NCT3933U):
    def __init__(self, registers=None):
        super().__init__(SimpleNamespace(ok=True, selected={}, gpu=None),
                         port=1, addr7=0x15)
        self.regs = {0x5D: 0x39, 0x5E: 0x33,
                     0x01: 0, 0x02: 0, 0x03: 0, 0x05: 0}
        if registers:
            self.regs.update(registers)
        self.reads = []
        self.writes = []
        self.read_hook = None
        self.write_hook = None

    def read(self, reg, width):
        self.reads.append((reg, width))
        if self.read_hook is not None:
            override = self.read_hook(reg, width)
            if override is not Ellipsis:
                return override
        return self.regs.get(reg)

    def _raw_write(self, reg, value, width):
        self.writes.append((reg, value, width))
        if self.write_hook is not None:
            override = self.write_hook(reg, value, width)
            if override is not Ellipsis:
                return override
        self.regs[reg] = value
        return True


class CurrentEncodingTests(unittest.TestCase):
    def test_sign_zero_and_full_scale(self):
        self.assertEqual(nct.decode_current_ua(0x00, 0, 1), 0)
        self.assertEqual(nct.decode_current_ua(0x80, 0, 1), 0)
        self.assertEqual(nct.decode_current_ua(0x03, 0, 3), -30)
        self.assertEqual(nct.decode_current_ua(0x83, 0, 3), 30)
        self.assertEqual(nct.encode_current_ua(-1270, 0, 1), 0x7F)
        self.assertEqual(nct.encode_current_ua(1270, 0, 1), 0xFF)

    def test_each_twofold_bit_changes_only_its_channel(self):
        for channel, bit in ((1, 0), (2, 2), (3, 4)):
            with self.subTest(channel=channel):
                config = 1 << bit
                self.assertEqual(nct.decode_current_ua(0x02, config, channel), -40)
                self.assertEqual(nct.encode_current_ua(40, config, channel), 0x82)
                self.assertEqual(nct.decode_current_ua(0x02, config,
                                 1 if channel != 1 else 2), -20)
                with self.assertRaises((TypeError, ValueError)):
                    nct.encode_current_ua(10, config, channel)
                self.assertEqual(nct.encode_current_ua(-2540, config, channel), 0x7F)

    def test_non_integral_and_out_of_range_current_is_rejected(self):
        self.assertEqual(nct.encode_current_ua(10.0, 0, 1), 0x81)
        for value in (True, 10.5, float("nan"), float("inf"), "10", 5,
                      1280, -1280):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                nct.encode_current_ua(value, 0, 1)
        for channel in (0, 4, True):
            with self.subTest(channel=channel), self.assertRaises((TypeError, ValueError)):
                nct.encode_current_ua(10, 0, channel)


class NCT3933Tests(unittest.TestCase):
    def test_presence_requires_both_exact_vendor_bytes(self):
        device = FakeNCT()
        self.assertTrue(device.present())
        self.assertIn((0x5D, 1), device.reads)
        self.assertIn((0x5E, 1), device.reads)
        for regs in ({0x5D: 0xFF, 0x5E: 0xFF},
                     {0x5D: 0x39, 0x5E: 0x00},
                     {0x5D: 0x00, 0x5E: 0x33}):
            with self.subTest(regs=regs):
                self.assertFalse(FakeNCT(regs).present())
        self.assertNotIn((0x04, 1), device.reads)

    def test_capture_and_telemetry_expose_raw_and_signed_currents(self):
        device = FakeNCT({0x01: 0x80, 0x02: 0x02, 0x03: 0x83,
                          0x05: 0x15})
        self.assertEqual(device.capture_control(),
                         {"outputs": [0x80, 0x02, 0x83],
                          "configuration": 0x15})
        telemetry = device.telemetry()
        self.assertEqual(telemetry["outputs"], [0x80, 0x02, 0x83])
        self.assertEqual(telemetry["configuration"], 0x15)
        self.assertEqual(telemetry["currents_ua"], [0, -40, 60])
        self.assertNotIn("vout_mv", telemetry)
        self.assertNotIn((0x04, 1), device.reads)

    def test_failed_capture_never_substitutes_zero_for_unknown(self):
        device = FakeNCT()
        device.read_hook = lambda reg, _width: None if reg == 0x02 else Ellipsis
        with self.assertRaises(ValueError):
            device.capture_control()
        self.assertEqual(device.writes, [])

    def test_selected_channel_write_preserves_other_raw_bytes_and_configuration(self):
        device = FakeNCT({0x01: 0x80, 0x05: 0x20})
        ok, _message = device.set_current_ua(2, 20, acknowledged=True)
        self.assertTrue(ok)
        self.assertEqual(device.capture_control(),
                         {"outputs": [0x80, 0x82, 0],
                          "configuration": 0x20})
        self.assertEqual(device.writes, [(0x02, 0x82, 1)])
        self.assertTrue(all(reg in (0x01, 0x02, 0x03) for reg, _, _ in device.writes))
        self.assertNotIn((0x04, 1), device.reads)

    def test_invalid_raw_outputs_and_stale_expected_state_do_not_write(self):
        for outputs in ([0, True, 0], [0, 256, 0], [0, -1, 0],
                        [0, 1.0, 0], [0, 0], "000"):
            with self.subTest(outputs=outputs):
                device = FakeNCT()
                ok, _ = device.set_outputs(outputs, acknowledged=True)
                self.assertFalse(ok)
                self.assertEqual(device.writes, [])
        device = FakeNCT()
        expected = {"outputs": [0, 0, 0], "configuration": 0}
        device.regs[0x03] = 1
        ok, _ = device.set_outputs([1, 0, 1], acknowledged=True,
                                   expected=expected)
        self.assertFalse(ok)
        self.assertEqual(device.writes, [])

    def test_validate_control_rejects_malformed_state_before_bus_access(self):
        device = FakeNCT()
        for invalid in ({"outputs": [0, 0, True], "configuration": 0},
                        {"outputs": [0, 0, 0], "configuration": 0.0},
                        {"outputs": [0, 0], "configuration": 0}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                device.validate_control(invalid)
            self.assertEqual(device.reads, [])
        device.validate_control({"outputs": [0, 0, 0], "configuration": 0})
        with self.assertRaises(ValueError):
            device.validate_control({"outputs": [0, 0, 0], "configuration": 1})
        self.assertEqual(device.writes, [])

    def test_write_requires_acknowledgment(self):
        device = FakeNCT()
        ok, _ = device.set_outputs([1, 0, 0])
        self.assertFalse(ok)
        self.assertEqual(device.writes, [])

    def test_set_current_rejects_external_change_since_expected_snapshot(self):
        device = FakeNCT()
        snapshot = device.capture_control()
        device.regs[0x02] = 0x02  # changed outside this dialog after autosave
        ok, message = device.set_current_ua(3, -10, acknowledged=True,
                                            expected=snapshot)
        self.assertFalse(ok)
        self.assertIn("changed", message.lower())
        self.assertEqual(device.writes, [])

    def test_zero_outputs_rejects_external_change_since_expected_snapshot(self):
        device = FakeNCT({0x01: 0x01})
        snapshot = device.capture_control()
        device.regs[0x05] = 0x01  # the DAC scale changed after capture
        ok, message = device.zero_outputs(acknowledged=True, expected=snapshot)
        self.assertFalse(ok)
        self.assertIn("changed", message.lower())
        self.assertEqual(device.writes, [])
        self.assertEqual(device.regs[0x01], 0x01)

    def test_fresh_expected_snapshots_allow_apply_and_zero(self):
        device = FakeNCT({0x01: 0x80})
        original = device.capture_control()
        ok, _ = device.set_current_ua(3, -10, acknowledged=True,
                                      expected=original)
        self.assertTrue(ok)
        changed = device.capture_control()
        self.assertEqual(changed["outputs"], [0x80, 0, 1])
        ok, _ = device.zero_outputs(acknowledged=True, expected=changed)
        self.assertTrue(ok)
        self.assertEqual(device.capture_control()["outputs"], [0, 0, 0])

    def test_power_saving_is_reported_without_claiming_active_output(self):
        device = FakeNCT({0x05: 0x40})
        self.assertFalse(device.telemetry()["outputs_enabled"])
        ok, _ = device.set_current_ua(1, -10, acknowledged=True)
        self.assertTrue(ok)
        self.assertEqual(device.capture_control(),
                         {"outputs": [1, 0, 0], "configuration": 0x40})
        self.assertFalse(device.telemetry()["outputs_enabled"])
        self.assertEqual(device.writes, [(0x01, 1, 1)])

    def test_failed_configuration_read_blocks_current_write(self):
        device = FakeNCT()
        device.read_hook = lambda reg, _width: None if reg == 0x05 else Ellipsis
        ok, _ = device.set_current_ua(1, -10, acknowledged=True)
        self.assertFalse(ok)
        self.assertEqual(device.writes, [])

    def test_partial_write_failure_restores_all_attempted_channels(self):
        device = FakeNCT()
        failed = False

        def write(reg, value, _width):
            nonlocal failed
            # Simulate a bus status failure *after* the second data byte lands.
            device.regs[reg] = value
            if reg == 0x02 and value == 2 and not failed:
                failed = True
                return False
            return True

        device.write_hook = write
        ok, _ = device.set_outputs([1, 2, 0], acknowledged=True)
        self.assertFalse(ok)
        self.assertTrue(failed)
        self.assertEqual(device.capture_control(),
                         {"outputs": [0, 0, 0], "configuration": 0})
        self.assertTrue(any(reg == 0x01 and val == 0 for reg, val, _ in device.writes))
        self.assertTrue(any(reg == 0x02 and val == 0 for reg, val, _ in device.writes))

    def test_identity_change_after_dispatch_stops_untrusted_rollback(self):
        device = FakeNCT()

        def write(reg, value, _width):
            if reg == 0x01 and value == 1:
                device.regs[reg] = value
                device.regs[0x5D] = 0x00
                return True
            return Ellipsis

        device.write_hook = write
        ok, message = device.set_outputs([1, 2, 0], acknowledged=True)
        self.assertFalse(ok)
        self.assertEqual(device.regs[0x01], 1)
        self.assertNotIn((0x01, 0, 1), device.writes)
        self.assertIn("uncertain", message.lower())

    def test_configuration_change_after_dispatch_stops_untrusted_rollback(self):
        device = FakeNCT()

        def write(reg, value, _width):
            if reg == 0x01 and value == 1:
                device.regs[reg] = value
                device.regs[0x05] = 0x01  # another actor enabled OUT1 doubling
                return True
            return Ellipsis

        device.write_hook = write
        ok, message = device.set_outputs([1, 2, 0], acknowledged=True)
        self.assertFalse(ok)
        self.assertEqual(device.regs[0x01], 1)
        self.assertNotIn((0x01, 0, 1), device.writes)
        self.assertIn("uncertain", message.lower())

    def test_restore_control_replays_exact_bytes_without_config_write(self):
        device = FakeNCT({0x01: 0x80, 0x02: 0x03, 0x03: 0x81, 0x05: 0x20})
        saved = device.capture_control()
        device.regs.update({0x01: 0, 0x02: 0, 0x03: 0})
        ok, _ = device.restore_control(saved)
        self.assertTrue(ok)
        self.assertEqual(device.capture_control(), saved)
        self.assertEqual([reg for reg, _, _ in device.writes], [0x01, 0x02, 0x03])
        self.assertNotIn((0x04, 1), device.reads)

    def test_partial_restore_failure_recovers_entry_state(self):
        device = FakeNCT({0x01: 4, 0x02: 5, 0x03: 6})
        entry = device.capture_control()
        failed = False

        def write(reg, value, _width):
            nonlocal failed
            device.regs[reg] = value
            if reg == 0x03 and value == 3 and not failed:
                failed = True
                return False
            return True

        device.write_hook = write
        ok, _ = device.restore_control({"outputs": [1, 2, 3],
                                        "configuration": 0})
        self.assertFalse(ok)
        self.assertTrue(failed)
        self.assertEqual(device.capture_control(), entry)


class TransportGeometryTests(unittest.TestCase):
    def test_real_packet_path_uses_one_byte_internal_read_and_checks_returned_size(self):
        packets = []
        returned_size = [1]

        def read_ex(_gpu, packet_pointer, _extra_pointer):
            packet = packet_pointer._obj
            packets.append((packet.i2cDevAddress, packet.portId,
                            packet.bIsPortIdSet, packet.regAddrSize,
                            packet.pbI2cRegAddress[0], packet.cbSize,
                            packet.i2cSpeed))
            packet.pbData[0] = 0x39
            packet.cbSize = returned_size[0]
            return 0

        nvapi = SimpleNamespace(ok=True, selected={},
                                gpu=ctypes.c_void_p(0x1234),
                                _i=lambda identifier, *_signature:
                                read_ex if identifier == railctl.I2C_READ_EX else None)
        device = nct.NCT3933U(nvapi, port=6, addr7=0x13)
        self.assertEqual(device.read(0x5D, 1), 0x39)
        self.assertEqual(packets, [(0x26, 6, 1, 1, 0x5D, 1, 0xFFFF)])
        returned_size[0] = 2
        self.assertIsNone(device.read(0x5D, 1))
        with self.assertRaises(ValueError):
            device.read(0x04, 1)  # status read clears a watchdog flag
        with self.assertRaises(ValueError):
            device.read(0x5D, 2)
        self.assertEqual(len(packets), 2)


if __name__ == "__main__":
    unittest.main()
