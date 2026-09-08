# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Variable-sized driver clock tables must remain complete and bounded."""
import ctypes
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from druta import nvbackend as n


class ScriptedGetter:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.capacities = []

    def __call__(self, device, *arguments):
        pointer, values = arguments[-2:]
        count = ctypes.cast(pointer, ctypes.POINTER(n.u32))
        self.capacities.append(count[0])
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        status, returned, data = response
        if values is not None:
            for index, value in enumerate(data[:len(values)]):
                values[index] = value
        count[0] = returned
        return status


class ClockDriver:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.writes = []

    def read(self, key, pointer, values):
        clocks = list(self.rows) if key == "memory" else self.rows[key]
        count = ctypes.cast(pointer, ctypes.POINTER(n.u32))
        capacity = count[0]
        self.calls.append((key, capacity))
        count[0] = len(clocks)
        if values is None or capacity < len(clocks):
            return 7
        for index, clock in enumerate(reversed(clocks)):
            values[index] = clock
        return 0

    def memory(self, device, pointer, values):
        return self.read("memory", pointer, values)

    def graphics(self, device, memory, pointer, values):
        return self.read(memory.value, pointer, values)

    def set_lock(self, device, low, high):
        self.writes.append((low.value, high.value))
        return 0

    def gpu(self):
        return fake_gpu(nvmlDeviceGetSupportedMemoryClocks=self.memory,
                        nvmlDeviceGetSupportedGraphicsClocks=self.graphics,
                        nvmlDeviceSetGpuLockedClocks=self.set_lock)


def fake_gpu(**functions):
    gpu = n.GPU.__new__(n.GPU)
    gpu._lock = threading.RLock()
    gpu.static = {}
    gpu.nvapi = SimpleNamespace(ok=False)
    dll = SimpleNamespace(**functions)
    gpu.nvml = SimpleNamespace(ok=True, selected=None, dev=object(), dll=dll,
                               has=lambda name: hasattr(dll, name), errstr=str)
    gpu.slot = Mock(return_value="0000:01:00.0")
    gpu._offset_range = Mock(return_value=None)
    gpu._native_fan_data = Mock(return_value=None)
    return gpu


class NvmlClockListTests(unittest.TestCase):
    def test_blackwell_full_rows_feed_static_bounds_and_frequency_lock(self):
        idle = [180 + (15 * index) // 2 for index in range(95)]
        performance = [180 + (15 * index) // 2 for index in range(389)]
        driver = ClockDriver({405: idle, 810: performance, 7001: performance,
                              14801: performance, 15001: performance})
        gpu = driver.gpu()
        with patch.object(n, "is_admin", return_value=False):
            gpu.static = gpu._read_static()
        self.assertEqual(gpu.static["mem_clocks"], sorted(driver.rows))
        self.assertEqual((gpu.static["gfx_min"], gpu.static["gfx_max"]), (180, 3090))
        self.assertEqual(gpu.lockable_clocks_by_mem(), list(driver.rows.items()))
        self.assertIn((15001, 389), driver.calls)
        ok, message = gpu.lock_gpu_clocks(1500, 1500)
        self.assertTrue(ok, message)
        self.assertEqual(driver.writes, [(1500, 1500)])
        self.assertEqual(gpu.last_clock_lock_request(), (1500, 1500))
        self.assertNotIn("snapped", message)

    def test_memory_lists_larger_than_the_former_64_entry_buffer_are_complete(self):
        driver = ClockDriver({memory: [360, 375, 390] for memory in range(100, 228)})
        gpu = driver.gpu()
        with patch.object(n, "is_admin", return_value=False):
            static = gpu._read_static()
        self.assertEqual(static["mem_clocks"], list(range(100, 228)))
        self.assertEqual((static["gfx_min"], static["gfx_max"]), (360, 390))
        self.assertEqual(driver.calls, [("memory", 0), ("memory", 128), (227, 0), (227, 3)])

    def test_smaller_legacy_rows_keep_their_values_and_round_down_behavior(self):
        for length in (64, 121, 256):
            with self.subTest(length=length):
                clocks = [360 + 15 * index for index in range(length)]
                driver = ClockDriver({405: [180, 195], 7000: clocks})
                gpu = driver.gpu()
                gpu.static = {"mem_clocks": [405, 7000]}
                self.assertEqual(gpu.lockable_clocks_by_mem(), list(driver.rows.items()))
                self.assertTrue(gpu.lock_gpu_clocks(1234, 1234)[0])
                self.assertEqual(driver.writes, [(1230, 1230)])
                self.assertEqual(gpu.last_clock_lock_request(), (1230, 1230))

    def test_failed_new_request_cannot_report_an_earlier_successful_range(self):
        driver = ClockDriver({7000: [1500, 1515]})
        gpu = driver.gpu()
        gpu.static = {"mem_clocks": [7000]}
        self.assertIsNone(gpu.last_clock_lock_request())
        self.assertTrue(gpu.lock_gpu_clocks(1501, 1501)[0])
        self.assertEqual(gpu.last_clock_lock_request(), (1500, 1500))
        gpu.nvml.dll.nvmlDeviceSetGpuLockedClocks = Mock(return_value=4)
        self.assertFalse(gpu.lock_gpu_clocks(1515, 1515)[0])
        self.assertIsNone(gpu.last_clock_lock_request())

    def test_successful_release_clears_command_record_but_a_failed_release_retains_it(self):
        driver = ClockDriver({7000: [1500, 1515]})
        gpu = driver.gpu()
        gpu.static = {"mem_clocks": [7000]}
        self.assertTrue(gpu.lock_gpu_clocks(1501, 1501)[0])
        reset = Mock(side_effect=[4, 0])
        gpu.nvml.dll.nvmlDeviceResetGpuLockedClocks = reset
        self.assertFalse(gpu.reset_gpu_clocks()[0])
        self.assertEqual(gpu.last_clock_lock_request(), (1500, 1500))
        self.assertTrue(gpu.reset_gpu_clocks()[0])
        self.assertIsNone(gpu.last_clock_lock_request())

    def test_count_growth_retries_with_the_returned_capacity(self):
        getter = ScriptedGetter((7, 2, []), (7, 4, []), (7, 5, []),
                                (0, 5, [600, 300, 450, 150, 750]))
        gpu = fake_gpu(clocks=getter)
        self.assertEqual(gpu._supported_nvml_clocks("clocks"), [150, 300, 450, 600, 750])
        self.assertEqual(getter.capacities, [0, 2, 4, 5])

    def test_continuously_growing_table_stops_after_three_allocated_reads(self):
        getter = ScriptedGetter((7, 1, []), (7, 2, []), (7, 3, []), (7, 4, []))
        self.assertEqual(fake_gpu(clocks=getter)._supported_nvml_clocks("clocks"), [])
        self.assertEqual(getter.capacities, [0, 1, 2, 3])

    def test_initial_count_is_bounded_before_allocation(self):
        for count in (0, 4097, 2**32 - 1):
            for status in (0, 7):
                with self.subTest(count=count, status=status):
                    getter = ScriptedGetter((status, count, []))
                    self.assertEqual(fake_gpu(clocks=getter)._supported_nvml_clocks("clocks"), [])
                    self.assertEqual(getter.capacities, [0])

    def test_malformed_or_failed_results_never_produce_a_partial_list(self):
        for response in ((0, 0, []), (0, 3, [150, 300]), (0, 2, [150, 0]),
                         (7, 2, [150, 300]), (7, 1, [150]), (7, 4097, []),
                         (4, 2, [150, 300])):
            with self.subTest(response=response):
                getter = ScriptedGetter((7, 2, []), response)
                self.assertEqual(fake_gpu(clocks=getter)._supported_nvml_clocks("clocks"), [])
                self.assertEqual(getter.capacities, [0, 2])

    def test_valid_shorter_success_is_sorted_and_deduplicated(self):
        getter = ScriptedGetter((0, 5, []), (0, 4, [600, 300, 450, 300]))
        self.assertEqual(fake_gpu(clocks=getter)._supported_nvml_clocks("clocks"), [300, 450, 600])

    def test_unavailable_or_failed_getters_return_no_list(self):
        self.assertEqual(fake_gpu()._supported_nvml_clocks("missing"), [])
        for failure in ((3, 389, []), OSError("driver unavailable")):
            with self.subTest(failure=failure):
                getter = ScriptedGetter(failure)
                self.assertEqual(fake_gpu(clocks=getter)._supported_nvml_clocks("clocks"), [])
                self.assertEqual(getter.capacities, [0])


if __name__ == "__main__":
    unittest.main()
