# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Pascal V1 clock-control negotiation and single-field writes."""
import ctypes
import unittest

from druta import native
from tests.fake_driver import ClkDomRig, GET_ID, SET_ID

if not native.available():
    raise unittest.SkipTest("native core not built")

V1 = 0x10964
V2 = 0x261A4
SIZE = 0x964
HEADER = 0x64
STRIDE = 0x48
FREQUENCY = 0x2C
NVVDD = 0x30
CDECL = ctypes.CFUNCTYPE


class LegacyRig(ClkDomRig):
    def __init__(self, *, arch=4, v2_status=-9, accepted=(0, 2), bad_echo=False,
                 v1_enabled=True, reassert=False, mixed_v2=False):
        super().__init__(arch=arch)
        original_get = self.nvapi.interfaces[GET_ID]
        original_set = self.nvapi.interfaces[SET_ID]
        self.versions = []
        self.v1_sets = []
        self.last_v1_read = None
        self.before_set = None
        self.frequency = {domain: 0 for domain in range(32)}
        self.nvvdd = {domain: 0 for domain in range(32)}
        self.v2_status = v2_status
        self.v1_enabled = v1_enabled
        self.bad_echo = bad_echo

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def get(handle, pointer):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            version, mask = words[0], words[2]
            self.versions.append(version)
            if version == V2:
                if mixed_v2 and mask == 2:
                    return -1
                if self.v2_status == 0:
                    return original_get(handle, pointer)
                return self.v2_status
            if version != V1 or not self.v1_enabled:
                return -9
            if not mask or mask & ~sum(1 << d for d in accepted):
                return -1
            for domain in accepted:
                if mask & (1 << domain):
                    base = (HEADER + domain * STRIDE) // 4
                    for i in range(STRIDE // 4):
                        words[base + i] = 0xAC000000 | (domain << 8) | i
                    words[base] = 4
                    words[base + FREQUENCY // 4] = self.frequency[domain] & 0xffffffff
                    words[base + NVVDD // 4] = self.nvvdd[domain] & 0xffffffff
            if self.bad_echo:
                words[2] = 0
            self.last_v1_read = ctypes.string_at(pointer, SIZE)
            return 0

        @CDECL(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def set_(handle, pointer):
            words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
            if words[0] != V1:
                return original_set(handle, pointer)
            data = ctypes.string_at(pointer, SIZE)
            self.before_set = self.last_v1_read
            self.v1_sets.append(data)
            if not reassert:
                for domain in accepted:
                    if words[2] & (1 << domain):
                        value = words[(HEADER + domain * STRIDE + FREQUENCY) // 4]
                        self.frequency[domain] = ctypes.c_int32(value).value
                        voltage = words[(HEADER + domain * STRIDE + NVVDD) // 4]
                        self.nvvdd[domain] = ctypes.c_int32(voltage).value
            return 0

        self.get_callback, self.set_callback = get, set_
        self.nvapi.interfaces[GET_ID] = self.names["ClkDomCtlGet"] = get
        self.nvapi.interfaces[SET_ID] = self.names["ClkDomCtlSet"] = set_


class PascalV1Tests(unittest.TestCase):
    def tearDown(self):
        native.set_export_resolver(None)

    def test_v2_remains_preferred(self):
        rig = LegacyRig(v2_status=0)
        gpu = rig.native_gpu()
        try:
            self.assertEqual(gpu.clkdom_layout().version, V2)
            self.assertNotIn(V1, rig.versions)
        finally:
            gpu.close()

    def test_v1_discovery_and_rails(self):
        rig = LegacyRig()
        gpu = rig.native_gpu()
        try:
            lay = gpu.clkdom_layout()
            self.assertEqual((lay.version, lay.size, lay.header, lay.stride, lay.freq_khz),
                             (V1, SIZE, HEADER, STRIDE, FREQUENCY))
            self.assertEqual(lay.nvvdd_uv, NVVDD)
            self.assertIsNone(lay.msvdd_uv)
            self.assertEqual(gpu.clkdom_domains(), [0, 2])
            self.assertTrue(gpu.rail_offset_capability(0, 0)["available"])
            self.assertFalse(gpu.rail_offset_capability(1, 2)["available"])
        finally:
            gpu.close()

    def test_v1_set_preserves_other_dwords_and_reads_back(self):
        rig = LegacyRig()
        gpu = rig.native_gpu()
        try:
            self.assertEqual(gpu.clkdom_layout().version, V1)
            self.assertTrue(gpu.set_clk_domain_offset(2, 1)[0])
            self.assertEqual(rig.frequency[2], 1000)
            self.assertEqual(len(rig.v1_sets), 1)
            sent = (ctypes.c_uint32 * (SIZE // 4)).from_buffer_copy(rig.v1_sets[0])
            before = (ctypes.c_uint32 * (SIZE // 4)).from_buffer_copy(rig.before_set)
            base = (HEADER + 2 * STRIDE) // 4
            self.assertEqual(sent[base + FREQUENCY // 4], 1000)
            self.assertEqual([i for i in range(len(sent)) if sent[i] != before[i]],
                             [base + FREQUENCY // 4])
            for i in range(STRIDE // 4):
                if i not in (0, FREQUENCY // 4, NVVDD // 4):
                    self.assertEqual(sent[base + i], 0xAC000000 | (2 << 8) | i)
        finally:
            gpu.close()

    def test_unverified_readback_is_failure(self):
        rig = LegacyRig(reassert=True)
        gpu = rig.native_gpu()
        try:
            self.assertEqual(gpu.clkdom_layout().version, V1)
            result = gpu.set_clk_domain_offset(2, 1)
            self.assertFalse(result[0])
            self.assertIn("readback differs", result[1])
        finally:
            gpu.close()

    def test_v1_nvvdd_preserves_other_dwords_and_checks_readback(self):
        rig = LegacyRig()
        gpu = rig.native_gpu()
        try:
            self.assertEqual(gpu.clkdom_layout().version, V1)
            self.assertTrue(gpu.set_rail_offset_mv(0.001, 0, 0)[0])
            self.assertEqual(rig.nvvdd[0], 1)
            sent = (ctypes.c_uint32 * (SIZE // 4)).from_buffer_copy(rig.v1_sets[0])
            before = (ctypes.c_uint32 * (SIZE // 4)).from_buffer_copy(rig.before_set)
            base = HEADER // 4
            self.assertEqual(sent[base + NVVDD // 4], 1)
            self.assertEqual([i for i in range(len(sent)) if sent[i] != before[i]],
                             [base + NVVDD // 4])
            for i in range(STRIDE // 4):
                if i not in (0, FREQUENCY // 4, NVVDD // 4):
                    self.assertEqual(sent[base + i], 0xAC000000 | i)
        finally:
            gpu.close()

    def test_only_definite_incompatible_version_permits_fallback(self):
        for kwargs in ({"v2_status": -1}, {"mixed_v2": True}, {"arch": 6}, {"bad_echo": True},
                       {"v1_enabled": False}):
            with self.subTest(**kwargs):
                rig = LegacyRig(**kwargs)
                gpu = rig.native_gpu()
                try:
                    self.assertIsNone(gpu.clkdom_layout())
                    if kwargs.get("v2_status") == -1 or kwargs.get("mixed_v2") or kwargs.get("arch") == 6:
                        self.assertNotIn(V1, rig.versions)
                finally:
                    gpu.close()

    def test_manual_refresh_can_recover_v1(self):
        rig = LegacyRig(v2_status=-1)
        gpu = rig.native_gpu()
        try:
            self.assertIsNone(gpu.clkdom_layout())
            rig.v2_status = -9
            gpu.refresh_capabilities()
            self.assertEqual(gpu.clkdom_layout().version, V1)
        finally:
            gpu.close()

    def test_delayed_retry_can_discover_v1_after_transient_v2_failure(self):
        rig = LegacyRig(v2_status=-1)
        gpu = rig.native_gpu()
        try:
            self.assertIsNone(gpu.clkdom_layout())
            rig.v2_status = -9
            self.assertFalse(gpu.retry_clock_capabilities(now=0))
            self.assertTrue(gpu.retry_clock_capabilities(now=1))
            self.assertEqual(gpu.clkdom_layout().version, V1)
            self.assertEqual(gpu.clkdom_domains(), [0, 2])
        finally:
            gpu.close()


if __name__ == "__main__":
    unittest.main()
