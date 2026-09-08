# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact memory replay and unchanged domain requests with in-memory drivers."""
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from druta import nvbackend as n
from tests.test_legacy_offsets import gpu as legacy_gpu


class ModernOffsets:
    def __init__(self):
        self.current = 0
        self.writes = []
        self.echo = True
        self.write_status = 0
        self.read_status = 0
        self.readback_status = 0

    def read(self, handle, pointer):
        status = self.readback_status if self.writes else self.read_status
        if status:
            return status
        co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
        co.mn, co.mx, co.off = -2000, 6000, self.current
        return 0

    def write(self, handle, pointer):
        co = ctypes.cast(pointer, ctypes.POINTER(n._ClockOffset)).contents
        self.writes.append((co.type, co.pstate, co.off))
        if self.echo and self.write_status == 0:
            self.current = co.off
        return self.write_status


def modern_gpu():
    gpu = legacy_gpu()
    gpu.nvapi.ok = False
    driver = ModernOffsets()
    gpu.nvml = SimpleNamespace(
        ok=True, dev=object(), ver=n.Nvml.ver,
        has=lambda name: name in ("nvmlDeviceGetClockOffsets", "nvmlDeviceSetClockOffsets"),
        dll=SimpleNamespace(nvmlDeviceGetClockOffsets=driver.read,
                            nvmlDeviceSetClockOffsets=driver.write),
        errstr=lambda status: f"status {status}")
    return gpu, driver


class MemoryOffsetPrecision(unittest.TestCase):
    def blackwell_gpu(self):
        gpu, driver = modern_gpu()
        gpu.static.update(name="NVIDIA GeForce RTX 5080", driver="580.97",
                          vbios="98.03.3b.c0.6f", mem_div=8)
        gpu.nvml.selected = {"devid": 0x2C02, "subsys": 2313031747}
        return gpu, driver

    def test_measured_blackwell_memory_grid_refuses_odd_requests_before_write(self):
        for value in (0.0625, -0.0625, 12.5625, -12.5625):
            with self.subTest(value=value):
                gpu, driver = self.blackwell_gpu()
                ok, message = gpu.set_clock_offset(2, value)
                self.assertFalse(ok)
                self.assertIn("multiples of 0.125", message)
                self.assertEqual(driver.writes, [])

    def test_measured_blackwell_memory_grid_accepts_exact_even_units(self):
        for value, units in ((0, 0), (0.125, 2), (-0.125, -2),
                             (12.5, 200), (-12.5, -200)):
            with self.subTest(value=value):
                gpu, driver = self.blackwell_gpu()
                self.assertTrue(gpu.set_clock_offset(2, value)[0])
                self.assertEqual(driver.writes, [(2, 0, units)])
                self.assertEqual(driver.current, units)

    def test_memory_grid_is_pure_metadata_and_restricted_to_measured_transport_identity(self):
        gpu, _ = self.blackwell_gpu()
        gpu.arch = Mock(side_effect=AssertionError("metadata lookup must not query hardware"))
        self.assertEqual(gpu.memory_offset_step_units(), 2)
        gpu.arch.assert_not_called()
        for field, replacement in (("devid", 0x2C04), ("subsys", 0),
                                    ("vbios", "98.03.3b.c0.70"), ("driver", "581.01")):
            with self.subTest(field=field):
                gpu, _ = self.blackwell_gpu()
                target = gpu.nvml.selected if field in ("devid", "subsys") else gpu.static
                target[field] = replacement
                self.assertEqual(gpu.memory_offset_step_units(), 1)
                self.assertTrue(gpu.set_clock_offset(2, 0.0625)[0])
        gpu, _ = self.blackwell_gpu()
        gpu.nvml.ok = False
        self.assertEqual(gpu.memory_offset_step_units(), 1)
        gpu, _ = self.blackwell_gpu()
        gpu.nvml.has = lambda name: False
        self.assertEqual(gpu.memory_offset_step_units(), 1)

    def test_measured_blackwell_unknown_memory_type_keeps_reported_clock_units(self):
        for request in (0.5, -0.5, 12.5, -12.5):
            with self.subTest(request=request):
                gpu, driver = self.blackwell_gpu()
                gpu.static["mem_div"] = None
                ok, message = gpu.set_clock_offset(2, request)
                self.assertFalse(ok)
                self.assertIn("multiples of 1 MHz eff", message)
                self.assertEqual(driver.writes, [])
        for request in (1, -1, 12, -12):
            with self.subTest(request=request):
                gpu, driver = self.blackwell_gpu()
                gpu.static["mem_div"] = None
                self.assertTrue(gpu.set_clock_offset(2, request)[0])
                self.assertEqual(driver.writes, [(2, 0, request * 2)])

    def test_fractional_memory_offsets_use_exact_legacy_khz(self):
        for value, units in ((12.5, 100), (-12.5, -100), (0.125, 1),
                             (-0.125, -1), (0, 0), (-250, -2000), (750, 6000)):
            with self.subTest(value=value):
                gpu = legacy_gpu()
                core_before = bytes(gpu.nvapi.info.pstates[1].clocks[1])
                ok, message = gpu.set_clock_offset(2, value)
                self.assertTrue(ok, message)
                self.assertEqual(gpu.nvapi.writes[0].pstates[0].clocks[0].delta.value,
                                 units * 500)
                self.assertEqual(gpu._offset_range(2)[2], units)
                self.assertEqual(bytes(gpu.nvapi.info.pstates[1].clocks[1]), core_before)
                self.assertIn(f"{value:+g} MHz true", message)

    def test_fractional_memory_offsets_use_exact_modern_units_and_readback(self):
        for value, units in ((12.5, 100), (-12.5, -100), (0.125, 1),
                             (-0.125, -1), (0, 0), (-250, -2000), (750, 6000)):
            with self.subTest(value=value):
                gpu, driver = modern_gpu()
                ok, message = gpu.set_clock_offset(2, value)
                self.assertTrue(ok, message)
                self.assertEqual(driver.writes, [(2, 0, units)])
                self.assertEqual(driver.current, units)
                self.assertIn(f"{value:+g} MHz true", message)
                self.assertEqual(gpu.nvapi.writes, [])

    def test_invalid_unrepresentable_and_out_of_range_values_never_write(self):
        values = (True, False, "12.5", None, complex(1, 2), float("nan"),
                  float("inf"), -float("inf"), 10**1000, 12.3, 0.01,
                  750.125, -250.125)
        for value in values:
            for modern in (False, True):
                with self.subTest(value=repr(value), modern=modern):
                    gpu, driver = modern_gpu() if modern else (legacy_gpu(), None)
                    self.assertFalse(gpu.set_clock_offset(2, value)[0])
                    self.assertEqual(gpu.nvapi.writes, [])
                    if driver:
                        self.assertEqual(driver.writes, [])

    def test_invalid_clock_types_are_rejected_before_transport(self):
        for ctype in (False, True, 0.0, 2.0, "2", None, 1):
            with self.subTest(ctype=ctype):
                gpu, driver = modern_gpu()
                self.assertFalse(gpu.set_clock_offset(ctype, 12.5)[0])
                self.assertEqual(driver.writes, [])

    def test_missing_modern_readback_before_write_is_refused(self):
        gpu, driver = modern_gpu()
        driver.read_status = 4
        ok, message = gpu.set_clock_offset(2, 12.5)
        self.assertFalse(ok)
        self.assertIn("no write issued", message)
        self.assertEqual(driver.writes, [])

    def test_modern_dropped_failed_or_unreadable_write_is_not_success(self):
        for setting, value in (("echo", False), ("write_status", 4),
                               ("readback_status", 4)):
            with self.subTest(setting=setting):
                gpu, driver = modern_gpu()
                setattr(driver, setting, value)
                self.assertFalse(gpu.set_clock_offset(2, 12.5)[0])
                self.assertEqual(driver.writes, [(2, 0, 100)])
                self.assertEqual(gpu.nvapi.writes, [])

    def test_legacy_fractional_readback_mismatch_reports_requested_fraction(self):
        gpu = legacy_gpu()
        gpu.nvapi.echo = False
        ok, message = gpu.set_clock_offset(2, 12.5)
        self.assertFalse(ok)
        self.assertIn("+12.5 MHz true", message)

    def test_core_grid_semantics_are_unchanged(self):
        for step, request, expected in ((15000, 31.9, 30), (12657, 60, 64)):
            with self.subTest(step=step):
                gpu, driver = modern_gpu()
                gpu.clock_step_khz.return_value = step
                ok, message = gpu.set_clock_offset(0, request)
                self.assertTrue(ok, message)
                self.assertEqual(driver.writes, [(0, 0, expected)])


def domain_gpu(value=0, layout=n.CLKDOM_LAYOUT_TURING, polarity=1):
    gpu = n.GPU.__new__(n.GPU)
    gpu.clkdom_ok = Mock(return_value=True)
    gpu.clkdom_layout = Mock(return_value=layout)
    gpu.clkdom_domains = Mock(return_value=[2])
    gpu.clkdom_control_polarity = Mock(return_value=polarity)
    gpu.clkdom_control_label = Mock(return_value="MEM")
    gpu.nvapi = SimpleNamespace(gpu=object(), ClkDomCtlSet=Mock(return_value=0))
    dw = (layout.header + 2 * layout.stride + layout.freq_khz) // 4

    def block():
        buf = ctypes.create_string_buffer(layout.size)
        words = ctypes.cast(buf, ctypes.POINTER(n.u32))
        words[0], words[layout.mask_dword] = layout.version, 1 << 2
        ctypes.cast(buf, ctypes.POINTER(n.i32))[dw] = value * polarity
        return buf

    first, second = block(), block()
    gpu._clkdom_get = Mock(side_effect=[(0, first), (0, second)])
    return gpu, first, second, dw


class UnchangedDomainOffsets(unittest.TestCase):
    def test_unchanged_validated_request_succeeds_without_write(self):
        for layout in (n.CLKDOM_LAYOUT_TURING, n.CLKDOM_LAYOUT_BLACKWELL):
            for value in (0, 25000, -25000):
                with self.subTest(layout=layout.name, value=value):
                    gpu, _, _, _ = domain_gpu(value, layout)
                    ok, message = gpu.set_clk_domain_offset(2, value / 1000)
                    self.assertTrue(ok, message)
                    self.assertIn("already", message)
                    self.assertEqual(gpu._clkdom_get.call_count, 2)
                    gpu.nvapi.ClkDomCtlSet.assert_not_called()

    def test_real_write_still_changes_exactly_one_dword(self):
        gpu, first, second, dw = domain_gpu(25000, polarity=-1)
        ok, message = gpu.set_clk_domain_offset(2, 30)
        self.assertTrue(ok, message)
        gpu.nvapi.ClkDomCtlSet.assert_called_once()
        left = ctypes.cast(first, ctypes.POINTER(n.u32))
        right = ctypes.cast(second, ctypes.POINTER(n.u32))
        diffs = [index for index in range(ctypes.sizeof(first) // 4)
                 if left[index] != right[index]]
        self.assertEqual(diffs, [dw])
        self.assertEqual(ctypes.cast(first, ctypes.POINTER(n.i32))[dw], -30000)

    def test_version_or_mask_mismatch_is_never_an_accepted_noop(self):
        for read_index in (0, 1):
            for field in (0, n.CLKDOM_LAYOUT_TURING.mask_dword):
                with self.subTest(read=read_index, field=field):
                    gpu, first, second, _ = domain_gpu()
                    ctypes.cast((first, second)[read_index], ctypes.POINTER(n.u32))[field] ^= 1
                    self.assertFalse(gpu.set_clk_domain_offset(2, 0)[0])
                    gpu.nvapi.ClkDomCtlSet.assert_not_called()

    def test_concurrent_change_is_refused_even_at_the_target_dword(self):
        for request in (0, 25):
            for target_field in (False, True):
                with self.subTest(request=request, target_field=target_field):
                    gpu, _, second, dw = domain_gpu()
                    ctypes.cast(second, ctypes.POINTER(n.i32))[dw if target_field else dw + 1] = 1000
                    ok, message = gpu.set_clk_domain_offset(2, request)
                    self.assertFalse(ok)
                    self.assertIn("changed between reads", message)
                    gpu.nvapi.ClkDomCtlSet.assert_not_called()

    def test_failed_short_or_unvalidated_read_cannot_report_success(self):
        for failure in ("first", "second", "short", "layout"):
            with self.subTest(failure=failure):
                gpu, first, second, _ = domain_gpu()
                if failure == "layout":
                    gpu.clkdom_layout.return_value = None
                else:
                    reads = [(0, first), (0, second)]
                    if failure == "short":
                        reads[0] = (0, ctypes.create_string_buffer(4))
                    else:
                        index = 0 if failure == "first" else 1
                        reads[index] = (-1, (first, second)[index])
                    gpu._clkdom_get.side_effect = reads
                self.assertFalse(gpu.set_clk_domain_offset(2, 0)[0])
                gpu.nvapi.ClkDomCtlSet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
