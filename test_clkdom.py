# Druta - pure unit tests for the architecture-specific clock-domain layer.
# Copyright (C) 2026 Thermetery Technology Co Limited
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests that do not load NVAPI or write GPU state.

The actual RTX 50-series validation is intentionally a separate, explicit
runtime probe because the mapping is a property of the installed driver and
VBIOS, not something a Windows CI runner can infer from Python alone.
"""

import ctypes
import unittest

from nvbackend import (
    CLKDOM_LAYOUT_BLACKWELL,
    CLKDOM_LAYOUT_TURING,
    CLKDOM_VERSION,
    GPU,
    clkdom_entry,
    i32,
    u32,
)


class ClkDomUnitTests(unittest.TestCase):
    @staticmethod
    def gpu_named(name):
        gpu = GPU.__new__(GPU)
        gpu.static = {"name": name}
        return gpu

    def test_only_rtx_50_series_selects_blackwell(self):
        self.assertTrue(
            self.gpu_named("NVIDIA GeForce RTX 5080").clkdom_is_blackwell())
        self.assertTrue(
            self.gpu_named("NVIDIA GeForce RTX 5090 Laptop GPU")
            .clkdom_is_blackwell())
        self.assertFalse(
            self.gpu_named("NVIDIA GeForce RTX 4080 SUPER")
            .clkdom_is_blackwell())

    def test_architecture_specific_entry_geometry(self):
        self.assertEqual(
            clkdom_entry(1, CLKDOM_LAYOUT_TURING.freq_khz,
                         CLKDOM_LAYOUT_TURING),
            0x124 + 0x304 + 0x10C)
        self.assertEqual(
            clkdom_entry(1, CLKDOM_LAYOUT_BLACKWELL.freq_khz,
                         CLKDOM_LAYOUT_BLACKWELL),
            0x124 + 0x304 + 0x114)
        self.assertNotEqual(CLKDOM_LAYOUT_TURING.freq_khz,
                            CLKDOM_LAYOUT_BLACKWELL.freq_khz)
        self.assertNotEqual(CLKDOM_LAYOUT_TURING.msvdd_uv,
                            CLKDOM_LAYOUT_BLACKWELL.msvdd_uv)

    def test_blackwell_control_names_have_no_turing_private_mapping(self):
        gpu = self.gpu_named("NVIDIA GeForce RTX 5080")
        self.assertEqual(gpu.clkdom_control_label(1), "XBAR")
        self.assertEqual(gpu.clkdom_control_label(3), "SYSCLK")
        self.assertEqual(gpu.clkdom_control_label(5), "VIDEO")
        self.assertEqual(gpu.clkdom_step_mhz(), 1)

    def test_blackwell_read_uses_shifted_frequency_field(self):
        gpu = self.gpu_named("NVIDIA GeForce RTX 5080")
        gpu._clkdom_layout_cache = None
        gpu._clkdom_valid = [1, 3, 5]
        gpu._set_calls = []

        class FakeNvapi:
            ok = True
            gpu = object()
            ClkDomCtlGet = object()
            ClkDomCtlSet = object()
            ClkMeasureFreq = None

        gpu.nvapi = FakeNvapi()

        def fake_get(mask):
            buf = (ctypes.c_ubyte * gpu._CLKDOM_BUF)()
            ctypes.memset(buf, 0, gpu._CLKDOM_BUF)
            p = ctypes.cast(buf, ctypes.POINTER(u32))
            p[0] = CLKDOM_VERSION
            p[2] = mask
            for domain in gpu._clkdom_valid:
                base = (CLKDOM_LAYOUT_BLACKWELL.header
                        + domain * CLKDOM_LAYOUT_BLACKWELL.stride)
                p[(base + CLKDOM_LAYOUT_BLACKWELL.mode) // 4] = 8
                ctypes.cast(buf, ctypes.POINTER(i32))[
                    (base + CLKDOM_LAYOUT_BLACKWELL.freq_khz) // 4] = 123000
            return 0, buf

        gpu._clkdom_get = fake_get
        gpu.nvapi.ClkDomCtlSet = lambda handle, buf: (gpu._set_calls.append(
            ctypes.string_at(buf, gpu._CLKDOM_BUF)) or 0)

        layout = gpu.clkdom_layout()
        self.assertEqual(layout, CLKDOM_LAYOUT_BLACKWELL)
        rows, err = gpu.read_clk_domain_offsets()
        self.assertIsNone(err)
        self.assertEqual(rows[1]["freq_khz"], 123000)

        ok, _message = gpu.set_clk_domain_offset(1, 128)
        self.assertTrue(ok)
        self.assertEqual(len(gpu._set_calls), 1)
        written = gpu._set_calls[0]
        field = clkdom_entry(1, CLKDOM_LAYOUT_BLACKWELL.freq_khz,
                             CLKDOM_LAYOUT_BLACKWELL)
        self.assertEqual(
            ctypes.cast(ctypes.create_string_buffer(written),
                        ctypes.POINTER(i32))[field // 4],
            128000)


if __name__ == "__main__":
    unittest.main()
