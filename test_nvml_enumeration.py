"""NVML enumeration with old export surfaces, without opening any driver."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nvbackend import Nvml, PTR, _NvmlPciInfo, u32


def make_library(version=2, pci_version=2):
    def count(ptr):
        ctypes.cast(ptr, ctypes.POINTER(u32))[0] = 2
        return 0
    def handle(index, ptr):
        ctypes.cast(ptr, ctypes.POINTER(PTR))[0] = index + 1
        return 0
    def pci(dev, ptr):
        info = ctypes.cast(ptr, ctypes.POINTER(_NvmlPciInfo)).contents
        info.bus = [2, 1][dev.value - 1]  # Reversed driver ordinal order.
        info.pciDeviceId = (0x1B02 << 16) | 0x10DE
        info.pciSubSystemId = 0x11DF10DE
        return 0
    suffix = '_v2' if version == 2 else ''
    return SimpleNamespace(**{
        'nvmlInit' + suffix: Mock(return_value=0),
        'nvmlDeviceGetCount' + suffix: Mock(side_effect=count),
        'nvmlDeviceGetHandleByIndex' + suffix: Mock(side_effect=handle),
        'nvmlDeviceGetPciInfo_v' + str(pci_version): Mock(side_effect=pci),
    })


def initialize(dll, slot=None):
    with patch('nvbackend._load_nvml_library', return_value=(dll, 'installed')):
        return Nvml(slot)


class NvmlEnumerationTests(unittest.TestCase):
    def test_v2_pci_keeps_lowest_slot_order_without_optional_name_uuid(self):
        nv = initialize(make_library())
        self.assertTrue(nv.ok)
        self.assertEqual(nv.selected['slot'], '0000:01:00.0')
        self.assertEqual(nv.selected['nvml_index'], 1)
        self.assertEqual(nv.selected['devid'], 0x1B02)
        self.assertEqual(nv.selected['subsys'], 0x11DF10DE)
        self.assertEqual(nv.selected['uuid'], '')

    def test_legacy_init_and_matching_index_pair_select_exact_card(self):
        nv = initialize(make_library(version=1), '0000:02:00.0')
        self.assertTrue(nv.ok)
        self.assertEqual(nv.selected['nvml_index'], 0)

    def test_v3_is_preferred_and_v2_is_not_called(self):
        dll = make_library(pci_version=3)
        dll.nvmlDeviceGetPciInfo_v2 = Mock()
        self.assertTrue(initialize(dll).ok)
        dll.nvmlDeviceGetPciInfo_v2.assert_not_called()

    def test_exported_v3_stub_falls_back_to_v2(self):
        dll = make_library()
        dll.nvmlDeviceGetPciInfo_v3 = Mock(return_value=13)
        self.assertTrue(initialize(dll).ok)
        self.assertEqual(dll.nvmlDeviceGetPciInfo_v2.call_count, 2)

    def test_real_v3_error_is_not_hidden_by_v2(self):
        dll = make_library()
        dll.nvmlDeviceGetPciInfo_v3 = Mock(return_value=15)  # GPU lost.
        nv = initialize(dll)
        self.assertFalse(nv.ok)
        self.assertEqual(nv.gpus, [])
        dll.nvmlDeviceGetPciInfo_v2.assert_not_called()

    def test_unidentified_handles_never_fall_back_to_card_zero(self):
        for reader in (None, Mock(return_value=0)):
            dll = make_library()
            del dll.nvmlDeviceGetPciInfo_v2
            if reader is not None:
                dll.nvmlDeviceGetPciInfo_v2 = reader  # Zero identity payload.
            self.assertFalse(initialize(dll).ok)

    def test_missing_mandatory_initialization_is_nonfatal(self):
        nv = initialize(SimpleNamespace())
        self.assertFalse(nv.ok)
        self.assertIn('initialization export', nv.err_detail)

    def test_index_versions_are_never_mixed(self):
        dll = make_library()
        del dll.nvmlDeviceGetCount_v2
        dll.nvmlDeviceGetCount = Mock()
        nv = initialize(dll)
        self.assertFalse(nv.ok)
        self.assertIn('matching device-count/index', nv.err_detail)
        dll.nvmlDeviceGetCount.assert_not_called()

    def test_missing_requested_pci_slot_does_not_choose_another_card(self):
        nv = initialize(make_library(), '0000:03:00.0')
        self.assertFalse(nv.ok)
        self.assertIn('no NVML device at slot', nv.err_detail)


if __name__ == '__main__':
    unittest.main()
