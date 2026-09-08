"""Reset-all distinguishes unsupported Pascal frequency locks from failures."""
import unittest
from unittest.mock import Mock

from druta.nvbackend import GPU
from tests.test_nvml_legacy import gpu_with, output


def reset_gpu(status=3, arch=4):
    gpu = gpu_with(nvmlDeviceResetGpuLockedClocks=Mock(return_value=status),
                   nvmlDeviceGetArchitecture=output(arch))
    gpu.nvml.errstr = lambda value: f"driver status {value}"
    gpu.static = {"pl_def_mw": 250000}
    gpu.set_clock_offset = Mock(return_value=(True, "offset reset"))
    gpu.clkdom_ok = Mock(return_value=False)
    gpu.volt_rail_limits_supported = Mock(return_value=False)
    gpu.set_power_limit_mw = Mock(return_value=(True, "power reset"))
    gpu._vf_lock_available = Mock(return_value=True)
    gpu.read_vf_lock = Mock(return_value={"volt_uV": 900000})
    gpu.clear_vf_lock = Mock(return_value=(True, "point released"))
    gpu.reset_fan = Mock(return_value=(True, "fan auto"))
    return gpu


class PascalStockResetTests(unittest.TestCase):
    def test_only_reset_all_treats_confirmed_pascal_not_supported_as_noop(self):
        gpu = reset_gpu()
        self.assertEqual(gpu.reset_gpu_clocks(), (False, "reset failed: driver status 3"))
        steps = {step.name: tuple(step) for step in gpu.reset_all()}
        self.assertTrue(steps[GPU.LOCK_STEP][0])
        self.assertIn("not applicable to Pascal", steps[GPU.LOCK_STEP][1])
        self.assertEqual(steps[GPU.VF_LOCK_STEP], (True, "point released"))
        gpu.clear_vf_lock.assert_called_once()

    def test_other_errors_and_other_or_unknown_architectures_remain_failures(self):
        for status, arch in ((4, 4), (15, 4), (999, 4), (3, 6), (3, 0)):
            with self.subTest(status=status, arch=arch):
                gpu = reset_gpu(status, arch)
                steps = {step.name: tuple(step) for step in gpu.reset_all()}
                self.assertFalse(steps[GPU.LOCK_STEP][0])
                gpu.clear_vf_lock.assert_called_once()

    def test_missing_export_is_not_treated_as_confirmed_unsupported(self):
        gpu = reset_gpu()
        del gpu.nvml.dll.nvmlDeviceResetGpuLockedClocks
        steps = {step.name: tuple(step) for step in gpu.reset_all()}
        self.assertEqual(steps[GPU.LOCK_STEP], (False, "ResetGpuLockedClocks not available"))

    def test_successful_release_keeps_normal_result(self):
        gpu = reset_gpu(0, 6)
        steps = {step.name: tuple(step) for step in gpu.reset_all()}
        self.assertEqual(steps[GPU.LOCK_STEP], (True, "GPU clock lock released"))

    def test_point_unlock_failure_is_preserved_alongside_pascal_noop(self):
        gpu = reset_gpu()
        gpu.clear_vf_lock.return_value = (False, "point unlock failed")
        steps = {step.name: tuple(step) for step in gpu.reset_all()}
        self.assertTrue(steps[GPU.LOCK_STEP][0])
        self.assertEqual(steps[GPU.VF_LOCK_STEP], (False, "point unlock failed"))


if __name__ == "__main__":
    unittest.main()
