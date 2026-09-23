# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Configured power requests remain separate from enforced/lagging telemetry."""
import ctypes
import threading
from types import SimpleNamespace
import unittest

from druta import nvbackend as n


class PowerDriver:
    def __init__(self):
        self.requested = 360000
        self.enforced = 349200
        self.writes = []
        self.read_status = self.after_status = self.write_status = 0
        self.echo = True

    def get_requested(self, device, pointer):
        status = self.after_status if self.writes else self.read_status
        if status:
            return status
        ctypes.cast(pointer, ctypes.POINTER(n.u32))[0] = self.requested
        return 0

    def get_enforced(self, device, pointer):
        ctypes.cast(pointer, ctypes.POINTER(n.u32))[0] = self.enforced
        return 0

    def set_requested(self, device, value):
        self.writes.append(value.value)
        if not self.write_status and self.echo:
            self.requested = value.value
        return self.write_status

    def gpu(self):
        gpu = n.GPU.__new__(n.GPU)
        gpu._lock = threading.RLock()
        gpu.static = {"pl_min_mw": 100000, "pl_max_mw": 400000}
        gpu.nvapi = SimpleNamespace(ok=False)
        functions = SimpleNamespace(nvmlDeviceGetPowerManagementLimit=self.get_requested,
                                    nvmlDeviceGetEnforcedPowerLimit=self.get_enforced,
                                    nvmlDeviceSetPowerManagementLimit=self.set_requested)
        gpu.nvml = SimpleNamespace(ok=True, dev=object(), dll=functions,
                                   has=lambda name: hasattr(functions, name),
                                   errstr=lambda status: f"status {status}")
        return gpu


class PowerLimitReadbackTests(unittest.TestCase):
    def setUp(self):
        self.driver = PowerDriver()
        self.gpu = self.driver.gpu()

    def test_telemetry_preserves_requested_and_enforced_as_distinct_values(self):
        result = {}
        self.gpu._read_power(result)
        self.assertEqual(result, {"pl_requested_mw": 360000, "pl_now_mw": 349200})

    def test_configured_getter_never_falls_back_to_enforced_on_error(self):
        for failure in ("status", "missing", "exception", "zero"):
            with self.subTest(failure=failure):
                driver = PowerDriver()
                gpu = driver.gpu()
                if failure == "status":
                    driver.read_status = 4
                elif failure == "missing":
                    del gpu.nvml.dll.nvmlDeviceGetPowerManagementLimit
                elif failure == "exception":
                    def failed_read(*_):
                        raise OSError("getter unavailable")
                    gpu.nvml.dll.nvmlDeviceGetPowerManagementLimit = failed_read
                else:
                    driver.requested = 0
                self.assertIsNone(gpu.read_power_limit_mw())
                result = {}
                gpu._read_power(result)
                self.assertNotIn("pl_requested_mw", result)
                self.assertEqual(result["pl_now_mw"], 349200)
                self.assertFalse(gpu.set_power_limit_mw(350000)[0])
                self.assertEqual(driver.writes, [])

    def test_exact_configured_request_verifies_despite_lagging_enforced_limit(self):
        for request in (350000, 349200, 349000, 355000, 350001):
            with self.subTest(request=request):
                ok, message = self.gpu.set_power_limit_mw(request)
                self.assertTrue(ok, message)
                self.assertEqual(self.driver.requested, request)
                self.assertEqual(self.driver.enforced, 349200)
                self.assertIn(f"{request/1000:g} W", message)

    def test_failed_or_unverified_write_never_reports_requested_value_as_applied(self):
        for setting, value in (("write_status", 4), ("after_status", 4), ("echo", False)):
            with self.subTest(setting=setting):
                driver = PowerDriver()
                setattr(driver, setting, value)
                gpu = driver.gpu()
                self.assertFalse(gpu.set_power_limit_mw(350000)[0])
                self.assertEqual(driver.writes, [350000])

    def test_invalid_or_out_of_range_inputs_do_not_write(self):
        for value in (None, True, False, "350000", float("nan"), float("inf"),
                      -float("inf"), 10**1000, 350000.5, 99999, 400001):
            with self.subTest(value=repr(value)):
                self.assertFalse(self.gpu.set_power_limit_mw(value)[0])
                self.assertEqual(self.driver.writes, [])


if __name__ == "__main__":
    unittest.main()
