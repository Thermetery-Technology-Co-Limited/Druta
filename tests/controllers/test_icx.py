# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""EVGA iCX voltage requests stay on the captured 0xC8 framing."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from druta import i2c_cache, profiles, railctl
from druta.controllers import icx


class Bus:
    """In-memory NVAPI I2C. Nothing here reaches a GPU."""

    def __init__(self, *, devid=icx.DEVICE_ID, subsys=icx.SUBSYSTEM_ID,
                 firmware=icx.FIRMWARE, cb_size=None, status=0, write_status=0):
        self.ok = True
        self.selected = {"devid": devid, "subsys": subsys}
        self.gpu = ctypes.c_void_p(0x2203)
        self.firmware = firmware
        self.cb_size = len(firmware) if cb_size is None else cb_size
        self.status = status
        self.write_status = write_status
        self.write_statuses = None
        self.reads = []
        self.writes = []
        self.calls = 0

    def _i(self, identifier, *_signature):
        self.calls += 1
        if identifier == railctl.I2C_READ_EX:
            return self._read
        if identifier == railctl.I2C_WRITE_EX:
            return self._write
        return None

    def _read(self, _gpu, packet_pointer, _extra):
        packet = packet_pointer._obj
        self.reads.append((packet.i2cDevAddress, packet.portId, packet.bIsPortIdSet,
                           packet.regAddrSize, packet.pbI2cRegAddress[0],
                           packet.cbSize, packet.i2cSpeed))
        for index, byte in enumerate(self.firmware):
            if index < packet.cbSize:
                packet.pbData[index] = byte
        packet.cbSize = self.cb_size
        return self.status

    def _write(self, _gpu, packet_pointer, _extra):
        packet = packet_pointer._obj
        data = bytes(packet.pbData[index] for index in range(packet.cbSize))
        self.writes.append({
            "address": packet.i2cDevAddress, "port": packet.portId,
            "port_set": packet.bIsPortIdSet, "reg_size": packet.regAddrSize,
            "command": packet.pbI2cRegAddress[0], "size": packet.cbSize,
            "data": data, "speed": packet.i2cSpeed, "version": packet.version,
        })
        if self.write_statuses:
            return self.write_statuses.pop(0)
        return self.write_status


class PayloadTests(unittest.TestCase):
    def test_confirmed_nvvdd_and_fbvdd_bytes(self):
        cases = (
            (icx.nvvdd_payload, 750, bytes((0x09, 0x01, 0x96))),
            (icx.nvvdd_payload, 900, bytes((0x09, 0x01, 0xB4))),
            (icx.nvvdd_payload, 950, bytes((0x09, 0x01, 0xBE))),
            (icx.nvvdd_payload, 1000, bytes((0x09, 0x01, 0xC8))),
            (icx.nvvdd_payload, 1150, bytes((0x09, 0x01, 0xE6))),
            (icx.fbvdd_payload, 1250, bytes((0x00, 0xFA, 0x01))),
            (icx.fbvdd_payload, 1300, bytes((0x01, 0x04, 0x01))),
            (icx.fbvdd_payload, 1350, bytes((0x01, 0x0E, 0x01))),
            (icx.fbvdd_payload, 1380, bytes((0x01, 0x14, 0x01))),
            (icx.fbvdd_payload, 1450, bytes((0x01, 0x22, 0x01))),
        )
        for encode, mv, payload in cases:
            with self.subTest(mv=mv):
                self.assertEqual(encode(mv), payload)
                self.assertEqual(len(payload), 3)

    def test_off_grid_and_out_of_range_requests_raise(self):
        rejected = (
            (icx.nvvdd_payload, 1148), (icx.nvvdd_payload, 1155),
            (icx.nvvdd_payload, 1200), (icx.nvvdd_payload, 1275),
            (icx.nvvdd_payload, 749), (icx.nvvdd_payload, True),
            (icx.nvvdd_payload, float("nan")), (icx.fbvdd_payload, 1245),
            (icx.fbvdd_payload, 1455), (icx.fbvdd_payload, False),
        )
        for encode, value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    encode(value)

    def test_xoc_extends_the_byte_and_refuses_past_it(self):
        self.assertEqual(icx.nvvdd_payload(1200, xoc=True), bytes((0x09, 0x01, 0xF0)))
        self.assertEqual(icx.nvvdd_payload(1275, xoc=True), bytes((0x09, 0x01, 0xFF)))
        for mv in (1280, 1600, 1605):
            with self.subTest(mv=mv):
                with self.assertRaises(ValueError):
                    icx.nvvdd_payload(mv, xoc=True)
        with self.assertRaises(ValueError):
            icx.nvvdd_payload(1200, xoc=False)


class TransportTests(unittest.TestCase):
    def _pair(self, **kwargs):
        bus = Bus(**kwargs)
        return icx.EVGAICx(bus), bus

    def test_only_port_1_address_2d_can_be_constructed(self):
        bus = Bus()
        with self.assertRaises(ValueError):
            icx.EVGAICx(bus, port=0, addr7=icx.ADDR7)
        with self.assertRaises(ValueError):
            icx.EVGAICx(bus, port=icx.PORT, addr7=0x2E)
        self.assertEqual(bus.calls, 0)

    def test_wrong_board_does_not_touch_i2c(self):
        mcu, bus = self._pair(devid=0x9999, subsys=0x12345678)
        self.assertFalse(mcu.present())
        ok, _message = mcu.set_nvvdd_mv(950, acknowledged=True)
        self.assertFalse(ok)
        self.assertEqual(bus.calls, 0)
        self.assertEqual(bus.reads, [])
        self.assertEqual(bus.writes, [])

    def test_firmware_must_be_the_exact_six_bytes(self):
        for kwargs in ({"firmware": bytes(6)}, {"cb_size": 5}, {"status": -1}):
            with self.subTest(kwargs=kwargs):
                mcu, bus = self._pair(**kwargs)
                self.assertFalse(mcu.present())
                self.assertEqual(bus.writes, [])
                self.assertEqual(bus.reads[0][4], icx.FIRMWARE_REG)
                self.assertEqual(bus.reads[0][0], icx.ADDR7 << 1)
                self.assertEqual(bus.reads[0][1], icx.PORT)

    def test_acknowledged_write_is_command_c8_plus_three_data_bytes(self):
        requests = (
            ("set_nvvdd_mv", 950, bytes((0x09, 0x01, 0xBE))),
            ("set_nvvdd_mv", 1000, bytes((0x09, 0x01, 0xC8))),
            ("set_fbvdd_mv", 1300, bytes((0x01, 0x04, 0x01))),
            ("set_fbvdd_mv", 1380, bytes((0x01, 0x14, 0x01))),
        )
        for method, mv, payload in requests:
            with self.subTest(method=method, mv=mv):
                mcu, bus = self._pair()
                ok, message = getattr(mcu, method)(mv, acknowledged=True)
                self.assertTrue(ok, message)
                self.assertIn("does not read this setpoint back", message)
                self.assertEqual(len(bus.writes), 1)
                write = bus.writes[0]
                self.assertEqual(write["address"], icx.ADDR7 << 1)
                self.assertEqual(write["port"], icx.PORT)
                self.assertEqual(write["port_set"], 1)
                self.assertEqual(write["reg_size"], 1)
                self.assertEqual(write["command"], icx.COMMAND)
                self.assertEqual(write["data"], payload)
                self.assertEqual(write["size"], 3)
                self.assertEqual(write["speed"], 0xFFFF)
                self.assertEqual(write["version"], railctl.VER3)
                self.assertEqual([item[4] for item in bus.reads], [icx.FIRMWARE_REG])
                self.assertNotIn(bytes((0x0E, 0x01)), [write["data"]])

    def test_auto_packet_is_not_a_manual_750_mv_write(self):
        self.assertEqual(icx.nvvdd_payload(750), bytes((0x09, 0x01, 0x96)))
        self.assertEqual(icx.NVVDD_AUTO_PAYLOAD, bytes((0x09, 0x00, 0x96)))
        mcu, bus = self._pair()
        ok, message = mcu.set_nvvdd_auto(acknowledged=True)
        self.assertTrue(ok, message)
        self.assertIn("not a manual 750 mV write", message)
        self.assertEqual([item["command"] for item in bus.writes], [icx.COMMAND, icx.COMMAND])
        self.assertEqual([item["data"] for item in bus.writes], [
            bytes((0x09, 0x00, 0x96)), bytes((0x0E, 0x01))])
        self.assertEqual([item["size"] for item in bus.writes], [3, 2])
        self.assertEqual([item[4] for item in bus.reads], [icx.FIRMWARE_REG])

        mcu, bus = self._pair()
        ok, _message = mcu.set_nvvdd_auto(acknowledged=False)
        self.assertFalse(ok)
        self.assertEqual(bus.writes, [])
        mcu, bus = self._pair(firmware=bytes(6))
        ok, message = mcu.set_nvvdd_auto(acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("identity", message)
        self.assertEqual(bus.writes, [])
        mcu, bus = self._pair()
        bus.write_statuses = [0, -1]
        ok, message = mcu.set_nvvdd_auto(acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("trailer", message)
        self.assertEqual([item["data"] for item in bus.writes], [
            bytes((0x09, 0x00, 0x96)), bytes((0x0E, 0x01))])

    def test_unacknowledged_wrong_length_and_rejected_writes_send_nothing(self):
        mcu, bus = self._pair()
        ok, _message = mcu.set_nvvdd_mv(950, acknowledged=False)
        self.assertFalse(ok)
        ok, _message = mcu.set_fbvdd_mv(1300)
        self.assertFalse(ok)
        ok, message = mcu.set_nvvdd_mv(1200, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("750..1150", message)
        ok, message = mcu.set_nvvdd_mv(1600, acknowledged=True, xoc=True)
        self.assertFalse(ok)
        self.assertIn("stops at 1275", message)
        self.assertEqual(bus.writes, [])
        ok, _message = mcu.set_nvvdd_mv(float("nan"), acknowledged=True)
        self.assertFalse(ok)
        for payload in (b"", b"\x09\x01", b"\x09\x01\xbe\x0e"):
            with self.assertRaises(ValueError):
                mcu._write_payload(payload)
        self.assertEqual(bus.calls, 0)
        self.assertEqual(bus.writes, [])

    def test_xoc_1200_uses_the_same_one_byte_command(self):
        mcu, bus = self._pair()
        ok, message = mcu.set_nvvdd_mv(1200, acknowledged=True, xoc=True)
        self.assertTrue(ok, message)
        self.assertEqual([item["data"] for item in bus.writes], [bytes((0x09, 0x01, 0xF0))])
        self.assertEqual(bus.writes[0]["command"], icx.COMMAND)
        self.assertEqual(len(bus.reads), 1)

    def test_identity_loss_and_a_rejected_status_do_not_report_success(self):
        mcu, bus = self._pair(firmware=bytes(6))
        ok, message = mcu.set_fbvdd_mv(1300, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("identity", message)
        self.assertEqual(bus.writes, [])

        mcu, bus = self._pair(write_status=-1)
        ok, message = mcu.set_nvvdd_mv(950, acknowledged=True)
        self.assertFalse(ok)
        self.assertIn("not acknowledged", message)
        self.assertEqual(len(bus.writes), 1)
        self.assertEqual(bus.writes[0]["data"], bytes((0x09, 0x01, 0xBE)))

    def test_offset_reset_and_verify_paths_do_not_write(self):
        mcu, bus = self._pair()
        self.assertFalse(mcu.plan(950)[0])
        self.assertFalse(mcu.set_offset_mv(0, acknowledged=True)[0])
        self.assertFalse(mcu.reset()[0])
        self.assertEqual(mcu.verify(acknowledged=True)[0], False)
        self.assertEqual(mcu.telemetry(), {"firmware_ok": True})
        self.assertNotIn("offset_mv", mcu.telemetry())
        self.assertEqual(bus.writes, [])
        self.assertTrue(all(item[4] == icx.FIRMWARE_REG for item in bus.reads))

    def test_profiles_do_not_store_or_replay_a_voltage(self):
        mcu, bus = self._pair()
        state = {profiles.INCOMPLETE_KEY: []}
        profiles.capture_rails(SimpleNamespace(static={}), state, mcu)
        self.assertIsNone(state["i2c"])
        self.assertEqual(bus.calls, 0)
        info = {"uuid": "GPU-1", "name": "3090 Ti", "vbios": "94",
                "driver": "617.42", "slot": "0000:01:00.0"}
        saved = {"schema": 2, "device": info, "vf_applicable": False,
                 "i2c": dict(profiles.rail_identity(mcu), offset_mv=0)}
        gpu = SimpleNamespace(static=info, vf_curve_applicable=lambda: False)
        error = profiles.preflight(gpu, saved, mcu)
        self.assertIn("cannot be replayed", error or "")
        self.assertEqual(bus.writes, [])
        self.assertTrue(i2c_cache._readable(mcu))
        self.assertEqual(bus.calls, 0)


class DiscoveryTests(unittest.TestCase):
    def test_icx_scope_probes_only_the_kingpin_mcu(self):
        seen = []

        def make(nvapi, *, port, addr7):
            seen.append((port, addr7))
            return SimpleNamespace(present=lambda: True,
                                   telemetry=lambda: {"firmware_ok": True})

        with patch("druta.railctl.load_profiles", return_value=[]), \
                patch("druta.controllers.icx.EVGAICx", side_effect=make) as ctor, \
                patch("druta.controllers.nct3933.NCT3933U") as nct, \
                patch("druta.controllers.ncp4206.NCP4206") as ncp, \
                patch("druta.controllers.up9512r.UP9512R") as up:
            hits = railctl.discover(SimpleNamespace(ok=True, selected={}),
                                    controller="EVGA iCX")
        self.assertEqual(seen, [(icx.PORT, icx.ADDR7)])
        self.assertEqual(len(hits), 1)
        ctor.assert_called_once()
        nct.assert_not_called()
        ncp.assert_not_called()
        up.assert_not_called()

    def test_other_scopes_and_other_routes_do_not_construct_icx(self):
        def make_ncp(nvapi, *, architecture, port, addr7):
            return SimpleNamespace(present=lambda: False)

        with patch("druta.railctl.load_profiles", return_value=[]), \
                patch("druta.controllers.icx.EVGAICx") as ctor, \
                patch("druta.controllers.ncp4206.NCP4206", side_effect=make_ncp):
            hits = railctl.discover(
                SimpleNamespace(ok=True, selected={"devid": 1, "subsys": 2}),
                controller="NCP4206", routes=[(1, icx.ADDR7)])
        ctor.assert_not_called()
        self.assertEqual(hits, [])
        with patch("druta.railctl.load_profiles", return_value=[]), \
                patch("druta.controllers.icx.EVGAICx") as ctor:
            hits = railctl.discover(SimpleNamespace(ok=True, selected={}),
                                    controller="EVGA iCX", routes=[(2, 0x30)])
        ctor.assert_not_called()
        self.assertEqual(hits, [])

    def test_discovery_on_another_board_does_not_read_i2c(self):
        bus = Bus(devid=0x9999, subsys=0x12345678)
        with patch("druta.railctl.load_profiles", return_value=[]):
            events = []
            hits = railctl.discover(bus, controller="EVGA iCX",
                                    progress=lambda *event: events.append(event))
        self.assertEqual(hits, [])
        self.assertEqual([event[:2] for event in events], [(0, 1), (1, 1)])
        self.assertEqual(bus.calls, 0)


if __name__ == "__main__":
    unittest.main()
