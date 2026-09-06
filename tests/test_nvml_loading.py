"""Driver discovery regressions; no actual NVIDIA library is opened."""
import os
import unittest
from unittest.mock import patch

from nvbackend import _load_nvml, Nvml


class NvmlLoadingTests(unittest.TestCase):
    def test_dch_driver_is_preferred(self):
        dll = object()
        with patch.dict(os.environ, {'SystemRoot': r'W:\Windows'}), \
                patch('nvbackend.ctypes.CDLL', return_value=dll) as load:
            self.assertIs(_load_nvml(), dll)
            load.assert_called_once_with(r'W:\Windows\System32\nvml.dll')

    def test_standard_driver_fallback_uses_native_program_files(self):
        dll = object()
        with patch.dict(os.environ, {'SystemRoot': r'W:\Windows',
                                     'ProgramW6432': r'W:\Program Files',
                                     'ProgramFiles': r'W:\Program Files (x86)'}), \
                patch('nvbackend.ctypes.CDLL',
                      side_effect=[OSError('missing'), dll]) as load:
            self.assertIs(_load_nvml(), dll)
            self.assertEqual(load.call_args_list[1].args,
                             (r'W:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll',))

    def test_missing_driver_is_reported_without_crashing(self):
        with patch('nvbackend.ctypes.CDLL', side_effect=OSError('not installed')):
            nvml = Nvml()
        self.assertFalse(nvml.ok)
        self.assertEqual(nvml.gpus, [])
        self.assertIn('System32', nvml.err_detail)
        self.assertIn('NVSMI', nvml.err_detail)


if __name__ == '__main__':
    unittest.main()
