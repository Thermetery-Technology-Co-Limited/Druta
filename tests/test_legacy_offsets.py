# Druta - hardware-free regression tests for pre-NVML clock offsets.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta import nvbackend as n


class PstatesAPI:
    ok = True
    gpu = object()
    ver = staticmethod(n.NvAPI.ver)
    AllClocks = CurrentPstate = BoostLock = None

    def __init__(self):
        self.info = n._Pstates20V2(num_pstates=2, num_clocks=2)
        self.info.pstates[0].pstate = 2
        p0 = self.info.pstates[1]
        p0.pstate = 0
        for clock, domain, low, high, delta, maximum in (
                (p0.clocks[0], 4, -1000000, 3000000, 80000, 7001000),
                (p0.clocks[1], 0, -1000000, 1000000, 30000, 2160000)):
            clock.domain, clock.flags, clock.kind = domain, 1, 1
            clock.delta.value = delta
            clock.delta.minimum, clock.delta.maximum = low, high
            clock.data[0], clock.data[1] = 300000, maximum
        self.writes = []
        self.reads = []
        self.status = 0
        self.echo = True
        self.version = 3

    def Pstates20Get(self, handle, pointer):
        request_version = ctypes.cast(pointer, ctypes.POINTER(n.u32))[0]
        self.reads.append(request_version)
        if request_version >> 16 != self.version:
            return -9
        self.info.version = request_version
        size = request_version & 0xFFFF
        ctypes.memmove(pointer, ctypes.byref(self.info), size)
        return 0

    def Pstates20Set(self, handle, pointer):
        version = ctypes.cast(pointer, ctypes.POINTER(n.u32))[0]
        typ = n._Pstates20V1 if version >> 16 == 1 else n._Pstates20V2
        request = typ.from_buffer_copy(ctypes.string_at(pointer, ctypes.sizeof(typ)))
        self.writes.append(request)
        if self.status:
            return self.status
        if self.echo:
            target = request.pstates[0].clocks[0]
            for clock in self.info.pstates[1].clocks:
                if clock.domain == target.domain:
                    clock.delta.value = target.delta.value
                    break
        return 0


def gpu():
    g = n.GPU.__new__(n.GPU)
    g._lock = threading.RLock()
    g.nvapi = PstatesAPI()
    g.nvml = SimpleNamespace(ok=True, has=lambda name: False)
    g.static = {"mem_div": 4}
    g.clock_step_khz = Mock(return_value=15000)
    g._priv_clocks = Mock(return_value=None)
    g.read_voltage_boost = Mock(return_value=None)
    return g


class LegacyOffsetTests(unittest.TestCase):
    def test_fallback_range_and_telemetry_match_existing_units(self):
        g = gpu()
        self.assertEqual(g._offset_range(0), (-1000, 1000, 30))
        self.assertEqual(g._offset_range(2), (-2000, 6000, 160))
        data = {}
        g._read_clocks(data)
        self.assertEqual(data, {"core_off": 30, "mem_off": 160})
        g._read_misc(data)
        self.assertEqual((data["core_p0max"], data["mem_p0max"]), (2160, 7001))
        self.assertEqual(g.nvapi.writes, [])

    def test_sparse_memory_request_preserves_other_clock_and_voltage_rows(self):
        g = gpu()
        before_core = bytes(g.nvapi.info.pstates[1].clocks[1])
        ok, message = g.set_clock_offset(2, 25)
        self.assertTrue(ok, message)
        request = g.nvapi.writes[0]
        self.assertEqual(request.num_pstates, 1)
        self.assertEqual(request.num_clocks, 1)
        self.assertEqual(request.num_voltages, 0)
        self.assertEqual(request.num_ov_voltages, 0)
        self.assertEqual(request.pstates[0].clocks[0].domain, 4)
        self.assertEqual(request.pstates[0].clocks[0].delta.value, 100000)
        expected = n._Pstates20V2(version=request.version, num_pstates=1, num_clocks=1)
        expected.pstates[0].clocks[0].domain = 4
        expected.pstates[0].clocks[0].delta.value = 100000
        self.assertEqual(bytes(request), bytes(expected))
        self.assertEqual(bytes(g.nvapi.info.pstates[1].clocks[1]), before_core)
        self.assertEqual(g._offset_range(2)[2], 200)

    def test_pascal_core_uses_same_physical_grid_rounding(self):
        g = gpu()
        g.clock_step_khz.return_value = 12657
        ok, message = g.set_clock_offset(0, 60)
        self.assertTrue(ok, message)
        request = g.nvapi.writes[0].pstates[0].clocks[0]
        self.assertEqual(request.domain, 0)
        self.assertEqual(request.delta.value, 64000)

    def test_zero_resets_only_requested_offset(self):
        g = gpu()
        ok, message = g.set_clock_offset(2, 0)
        self.assertTrue(ok, message)
        self.assertEqual(g._offset_range(2)[2], 0)
        self.assertEqual(g._offset_range(0)[2], 30)

    def test_out_of_range_or_noneditable_requests_never_write(self):
        for delta, editable in ((751, True), (25, False)):
            with self.subTest(delta=delta, editable=editable):
                g = gpu()
                g.nvapi.info.pstates[1].clocks[0].flags = int(editable)
                ok, _message = g.set_clock_offset(2, delta)
                self.assertFalse(ok)
                self.assertEqual(g.nvapi.writes, [])

    def test_driver_failure_or_dropped_write_does_not_claim_success(self):
        for status, echo in ((-6, True), (0, False)):
            with self.subTest(status=status, echo=echo):
                g = gpu()
                g.nvapi.status, g.nvapi.echo = status, echo
                ok, _message = g.set_clock_offset(2, 25)
                self.assertFalse(ok)
                self.assertEqual(len(g.nvapi.writes), 1)

    def test_versions_negotiate_but_invalid_tables_are_rejected(self):
        for version in (2, 1):
            with self.subTest(version=version):
                g = gpu()
                g.nvapi.version = version
                self.assertEqual(g._offset_range(0), (-1000, 1000, 30))
                self.assertEqual(len(g.nvapi.reads), 4 - version)
                ok, message = g.set_clock_offset(2, 25)
                self.assertTrue(ok, message)
                self.assertEqual(g._offset_range(2)[2], 200)
        g = gpu()
        g.nvapi.info.num_clocks = 9
        self.assertIsNone(g._offset_range(0))
        self.assertFalse(g.set_clock_offset(0, 60)[0])
        self.assertEqual(g.nvapi.writes, [])

    def test_ambiguous_p0_or_domain_is_not_used_for_writes(self):
        for duplicate in ("pstate", "domain"):
            with self.subTest(duplicate=duplicate):
                g = gpu()
                if duplicate == "pstate":
                    g.nvapi.info.pstates[0].pstate = 0
                else:
                    g.nvapi.info.pstates[1].clocks[0].domain = 0
                self.assertIsNone(g._offset_range(0))
                self.assertFalse(g.set_clock_offset(0, 60)[0])
                self.assertEqual(g.nvapi.writes, [])

    def test_modern_api_keeps_precedence_and_never_retries_a_failed_write(self):
        g = gpu()

        def read(_handle, pointer):
            co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
            co.mn, co.mx, co.off = -1000, 3000, 80
            return 0

        setter = Mock(return_value=4)
        g.nvml = SimpleNamespace(
            ok=True, dev=object(), ver=n.Nvml.ver,
            has=lambda name: name in ("nvmlDeviceGetClockOffsets", "nvmlDeviceSetClockOffsets"),
            dll=SimpleNamespace(nvmlDeviceGetClockOffsets=read,
                                nvmlDeviceSetClockOffsets=setter),
            errstr=lambda status: str(status))
        self.assertFalse(g.set_clock_offset(2, 25)[0])
        setter.assert_called_once()
        self.assertEqual(g.nvapi.writes, [])
        self.assertEqual(g.nvapi.reads, [])

    def test_modern_success_preserves_memory_scaling_and_core_rounding(self):
        for ctype, requested, expected in ((2, 25, 200), (2, 0, 0),
                                            (0, 31, 30)):
            with self.subTest(ctype=ctype, requested=requested):
                g = gpu()
                sent = []

                def read(_handle, pointer):
                    co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
                    co.mn, co.mx = -2000, 6000
                    co.off = sent[-1][2] if sent else 0
                    return 0

                def write(_handle, pointer):
                    co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
                    sent.append((co.type, co.pstate, co.off))
                    return 0

                g.nvml = SimpleNamespace(
                    ok=True, dev=object(), ver=n.Nvml.ver,
                    has=lambda name: name in ("nvmlDeviceGetClockOffsets", "nvmlDeviceSetClockOffsets"),
                    dll=SimpleNamespace(nvmlDeviceGetClockOffsets=read,
                                        nvmlDeviceSetClockOffsets=write))
                ok, message = g.set_clock_offset(ctype, requested)
                self.assertTrue(ok, message)
                self.assertEqual(sent, [(ctype, 0, expected)])
                self.assertEqual(g.nvapi.writes, [])
                self.assertEqual(g.nvapi.reads, [])

    def test_modern_p0_clock_caps_keep_precedence(self):
        g = gpu()

        def limits(_handle, ctype, pstate, minimum, maximum):
            ctypes.cast(minimum, ctypes.POINTER(n.u32))[0] = 100
            ctypes.cast(maximum, ctypes.POINTER(n.u32))[0] = 2222 if ctype == 0 else 7777
            return 0

        g.nvml = SimpleNamespace(
            ok=True, dev=object(),
            has=lambda name: name == "nvmlDeviceGetMinMaxClockOfPState",
            dll=SimpleNamespace(nvmlDeviceGetMinMaxClockOfPState=limits))
        data = {}
        g._read_misc(data)
        self.assertEqual((data["core_p0max"], data["mem_p0max"]), (2222, 7777))
        self.assertEqual(g.nvapi.reads, [])


if __name__ == "__main__":
    unittest.main()
