# Druta - hardware-free tests for older NVML export surfaces.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from druta.nvbackend import GPU, Nvml, _FanSpeedInfo, u32, u64


def output(value, scalar=u32):
    def read(*args):
        ctypes.cast(args[-1], ctypes.POINTER(scalar))[0] = value
        return 0
    return Mock(side_effect=read)


def gpu_with(**exports):
    nv = Nvml.__new__(Nvml)
    nv.ok = True
    nv.dev = object()
    nv.dll = SimpleNamespace(**exports)
    gpu = GPU.__new__(GPU)
    gpu._lock = threading.RLock()
    gpu.nvml = nv
    gpu.nvapi = SimpleNamespace(ok=False)
    gpu.static = {}
    return gpu


class LegacyNvmlTests(unittest.TestCase):
    def test_47212_fan_v2_without_fan_count_or_rpm(self):
        speed = output(42)
        gpu = gpu_with(nvmlDeviceGetFanSpeed_v2=speed)
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"fans": [(42, None)]})
        self.assertEqual(speed.call_args.args[:2], (gpu.nvml.dev, 0))

    def test_legacy_fan_speed_fallback_without_indexed_export(self):
        speed = output(47)
        gpu = gpu_with(nvmlDeviceGetFanSpeed=speed)
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"fans": [(47, None)]})
        self.assertEqual(speed.call_args.args[0], gpu.nvml.dev)

    def test_legacy_fan_speed_fallback_after_indexed_read_failure(self):
        gpu = gpu_with(nvmlDeviceGetFanSpeed_v2=Mock(return_value=3),
                       nvmlDeviceGetFanSpeed=output(51))
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data["fans"], [(51, None)])

    def test_fan_count_failure_still_reads_first_fan(self):
        gpu = gpu_with(nvmlDeviceGetNumFans=Mock(return_value=3),
                       nvmlDeviceGetFanSpeed=output(38))
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"fans": [(38, None)]})

    def test_missing_telemetry_does_not_invent_stopped_fan(self):
        gpu = gpu_with()
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"fans": []})

    def test_confirmed_fanless_adapter_does_not_probe_fan_zero(self):
        speed = Mock()
        gpu = gpu_with(nvmlDeviceGetNumFans=output(0),
                       nvmlDeviceGetFanSpeed_v2=speed)
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"num_fans": 0, "fans": []})
        speed.assert_not_called()

    def test_modern_two_fan_telemetry_and_real_zero_are_preserved(self):
        def speed(dev, fan, ptr):
            ctypes.cast(ptr, ctypes.POINTER(u32))[0] = [0, 43][fan]
            return 0
        def rpm(dev, ptr):
            info = ctypes.cast(ptr, ctypes.POINTER(_FanSpeedInfo)).contents
            info.speed = [0, 1300][info.fan]
            return 0
        gpu = gpu_with(nvmlDeviceGetNumFans=output(2),
                       nvmlDeviceGetFanSpeed_v2=Mock(side_effect=speed),
                       nvmlDeviceGetFanSpeedRPM=Mock(side_effect=rpm))
        data = {}
        gpu._read_fan(data)
        self.assertEqual(data, {"num_fans": 2, "fans": [(0, 0), (43, 1300)]})

    def test_47212_throttle_alias_preserves_mask(self):
        gpu = gpu_with(nvmlDeviceGetCurrentClocksThrottleReasons=output(0x84, u64))
        data = {}
        gpu._read_throttle(data)
        self.assertEqual(data, {"event_mask": 0x84})

    def test_modern_event_name_is_preferred(self):
        legacy = Mock()
        gpu = gpu_with(nvmlDeviceGetCurrentClocksEventReasons=output(0x10, u64),
                       nvmlDeviceGetCurrentClocksThrottleReasons=legacy)
        data = {}
        gpu._read_throttle(data)
        self.assertEqual(data, {"event_mask": 0x10})
        legacy.assert_not_called()

    def test_missing_optional_power_and_throttle_reads_are_omitted(self):
        gpu = gpu_with()
        data = {}
        gpu._read_power(data)
        gpu._read_throttle(data)
        self.assertEqual(data, {})

    def test_missing_optional_writers_return_unavailable(self):
        gpu = gpu_with()
        for name, args in (("set_fan", (50,)), ("reset_fan", ()),
                           ("set_power_limit_mw", (250000,)),
                           ("lock_gpu_clocks", (1200, 1200)),
                           ("reset_gpu_clocks", ())):
            with self.subTest(name=name):
                ok, message = getattr(gpu, name)(*args)
                self.assertFalse(ok)
                self.assertIn("not available", message)

    def test_fan_writers_refuse_unknown_count_without_guessing_fan_zero(self):
        manual, auto = Mock(), Mock()
        gpu = gpu_with(nvmlDeviceSetFanSpeed_v2=manual,
                       nvmlDeviceSetDefaultFanSpeed_v2=auto)
        self.assertFalse(gpu.set_fan(50)[0])
        self.assertFalse(gpu.reset_fan()[0])
        manual.assert_not_called()
        auto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
