# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Frequency-lock readback uses live driver records, independent of V/F locks."""
import ctypes
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from druta import nvbackend as n


def records(low=1200, high=1500):
    rows = [[0] * 82 for _ in range(2)]
    for row, ident, mhz in zip(rows, (0x4C, 0x4B), (low, high)):
        row[0], row[1], row[3], row[4] = ident, 2, mhz * 1000, 1
    return rows


def gpu():
    g = n.GPU.__new__(n.GPU)
    g._lock = threading.RLock()
    g.static = {"driver": "472.12"}
    g.nvapi = SimpleNamespace(ok=True, selected={"devid": 0x1E02})
    g._vf_lock_read_raw = Mock(return_value=n._ClockLock(count=1))
    g._legacy_clk_limit_records = Mock(return_value=records())
    return g


class LegacyFrequencyLockTests(unittest.TestCase):
    def test_legacy_readback_is_live_and_independent_of_point_lock(self):
        g = gpu()
        point = g._vf_lock_read_raw.return_value.locks[0]
        point.domain, point.lockMode, point.volt_uV = 0, n.VF_LOCK_MODE_POINT, 900000
        before = bytes(g._vf_lock_read_raw.return_value)
        self.assertEqual(g.read_clk_lock(), (1200, 1500))
        g._legacy_clk_limit_records.return_value = records(1350, 1650)
        self.assertEqual(g.read_clk_lock(), (1350, 1650))
        self.assertEqual(g.read_vf_lock()["volt_uV"], 900000)
        self.assertEqual(bytes(g._vf_lock_read_raw.return_value), before)

    def test_modern_mode_two_records_take_precedence(self):
        g = gpu()
        raw = n._ClockLock(count=2)
        for row, domain, value in zip(raw.locks, (n.CLK_LOCK_DOMAIN_MIN,
                                                n.CLK_LOCK_DOMAIN_MAX),
                                       (1200000, 1500000)):
            row.domain, row.lockMode, row.volt_uV = domain, n.VF_LOCK_MODE_FREQ, value
        g._vf_lock_read_raw.return_value = raw
        g.static["driver"] = "580.97"
        self.assertEqual(g.read_clk_lock(), (1200, 1500))
        g._legacy_clk_limit_records.assert_not_called()

    def test_private_fallback_is_scoped_to_measured_gpu_and_driver(self):
        for driver, devid in (("580.97", 0x1E02), ("472.12", 0x1B02),
                              ("472.13", 0x1E02)):
            g = gpu()
            g.static["driver"], g.nvapi.selected["devid"] = driver, devid
            self.assertIsNone(g.read_clk_lock())
            g._legacy_clk_limit_records.assert_not_called()

    def test_disabled_or_malformed_records_do_not_invent_a_lock(self):
        samples = [None, [], records(1500, 1200)]
        for index, value in ((0, 0), (1, 0), (1, 3), (3, 0), (3, 0xffffffff), (4, 0)):
            row = records()
            row[0][index] = value
            samples.append(row)
        for value in samples:
            with self.subTest(value=value):
                g = gpu()
                g._legacy_clk_limit_records.return_value = value
                self.assertIsNone(g.read_clk_lock())

    def test_transport_cache_uses_only_getter_and_fresh_record_storage(self):
        g = gpu()
        del g._legacy_clk_limit_records
        header = [0] * 17
        header[12:14] = [123, 456]
        fields = dict(hAdapter=7, hDevice=8, Type=0, Flags=0, hContext=0)
        g._capture_legacy_clk_transport = Mock(return_value=(header, fields))
        status = [0]

        def escape_call(pointer):
            escape = ctypes.cast(pointer, ctypes.POINTER(n.GPU._Escape)).contents
            self.assertEqual(escape.hAdapter, 7)
            self.assertEqual(escape.PrivateDriverDataSize, 84)
            words = (n.u32 * 21).from_address(escape.pPrivateDriverData)
            self.assertEqual(list(words[12:16]), [123, 456, 0x20802077, 16])
            self.assertEqual((words[2], words[17]), (84, 2))
            address = words[19] | (words[20] << 32)
            target = ((n.u32 * 82) * 2).from_address(address)
            self.assertEqual([target[0][0], target[1][0]], [0x4C, 0x4B])
            self.assertEqual([target[0][3], target[1][3]], [0, 0])
            for dest, src in zip(target, records()):
                dest[:] = src
            return status[0]

        def issue(packet, fields):
            escape = n.GPU._Escape(**fields,
                                  pPrivateDriverData=ctypes.addressof(packet),
                                  PrivateDriverDataSize=ctypes.sizeof(packet))
            return escape_call(ctypes.byref(escape))

        with patch.object(g, "_legacy_clk_escape", side_effect=issue):
            self.assertEqual(g._legacy_clk_limit_records(), records())
            self.assertEqual(g._legacy_clk_limit_records(), records())
            self.assertEqual(g._capture_legacy_clk_transport.call_count, 1)
            status[0] = -1
            self.assertIsNone(g._legacy_clk_limit_records())
            self.assertIsNone(g._legacy_clk_transport)
            status[0] = 0
            self.assertEqual(g._legacy_clk_limit_records(), records())
            self.assertEqual(g._capture_legacy_clk_transport.call_count, 2)

    def test_adapter_identity_and_cleanup(self):
        g = gpu()
        g.nvapi.selected["slot"] = "0000:01:00.0"
        closed = []

        def enumerate_adapters(pointer):
            argument = pointer._obj
            argument.count = 3
            for item, handle in zip(argument.items[:3], (111, 222, 333)):
                item.handle = handle
            return 0

        def query(pointer):
            argument = pointer._obj
            address = (n.u32 * 3).from_address(argument.data)
            address[:] = (argument.handle // 111, 0, 0)
            return 0

        def escape(pointer):
            self.assertEqual(pointer._obj.hAdapter, 111)
            return 0

        dll = SimpleNamespace(
            D3DKMTEnumAdapters2=Mock(side_effect=enumerate_adapters),
            D3DKMTQueryAdapterInfo=Mock(side_effect=query),
            D3DKMTCloseAdapter=Mock(side_effect=lambda p: closed.append(p._obj.value)),
            D3DKMTEscape=Mock(side_effect=escape))
        with patch.object(n.ctypes, "WinDLL", return_value=dll):
            packet = (n.u32 * 21)()
            fields = dict(hAdapter=999, hDevice=0, Type=0, Flags=8, hContext=0)
            self.assertEqual(g._legacy_clk_escape(packet, fields), 0)
            self.assertEqual(closed, [111, 222, 333])
            closed.clear()
            g.nvapi.selected["slot"] = "0000:04:00.0"
            self.assertIsNone(g._legacy_clk_escape(packet, fields))
            self.assertEqual(closed, [111, 222, 333])
            self.assertEqual(dll.D3DKMTEscape.call_count, 1)

    def test_missing_gdi_export_keeps_optional_readback_unavailable(self):
        names = ("D3DKMTEnumAdapters2", "D3DKMTQueryAdapterInfo",
                 "D3DKMTCloseAdapter", "D3DKMTEscape")
        for missing in names:
            with self.subTest(missing=missing):
                g = gpu()
                g.nvapi.selected["slot"] = "0000:01:00.0"
                exports = {name: Mock() for name in names if name != missing}
                with patch.object(n.ctypes, "WinDLL", return_value=SimpleNamespace(**exports)):
                    self.assertIsNone(g._legacy_clk_escape((n.u32 * 21)(), {}))
                for function in exports.values():
                    function.assert_not_called()

    def test_missing_adapter_api_prevents_capture_hook_installation(self):
        g = gpu()
        del g._legacy_clk_limit_records
        g.nvapi.VoltRailsCtlGet = Mock()
        dll = SimpleNamespace(D3DKMTEscape=Mock())
        with patch.object(n.ctypes, "WinDLL", return_value=dll) as loader:
            self.assertIsNone(g.read_clk_lock())
        loader.assert_called_once_with("gdi32.dll")
        g.nvapi.VoltRailsCtlGet.assert_not_called()
        dll.D3DKMTEscape.assert_not_called()


if __name__ == "__main__":
    unittest.main()
