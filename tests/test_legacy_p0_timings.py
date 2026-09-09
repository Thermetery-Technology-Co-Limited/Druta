# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Timing captures use legacy P0 without a V/F curve or fan writes."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta.druta import Druta
from druta.nvbackend import GPU
from tests.test_legacy_p0 import card


class LegacyP0TimingTests(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.gpu = self.app.gpu = card()
        self.gpu.nvapi.selected = {"devid": 0xFFFF, "subsys": 0x12345678}
        self.gpu.static = {"driver": "unknown", "vbios": "unknown"}
        self.gpu.set_fan = Mock()
        self.app._clk_lock = None
        self.app.unlocked = Mock(return_value=True)
        self.app.log = Mock()
        self.app.vf_read = Mock()
        self.app.hold_cap_point = Mock()
        self.app.vf_recovery_pending = Mock(return_value=False)
        self.app.set_lock_state = lambda state: setattr(self.app, "_clk_lock", state)
        self.app.vf_applicable = Mock(return_value=True)
        sleep = patch("druta.nvbackend.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def assert_no_curve_or_fan(self):
        self.app.vf_read.assert_not_called()
        self.app.hold_cap_point.assert_not_called()
        self.gpu.set_fan.assert_not_called()

    def test_unprofiled_kepler_and_maxwell_acquire_p0_without_editor_state(self):
        for generation in (GPU.ARCH_KEPLER, GPU.ARCH_MAXWELL):
            with self.subTest(generation=generation):
                self.gpu.arch.return_value = generation
                self.app.vf_applicable.return_value = generation != GPU.ARCH_KEPLER
                self.assertTrue(self.app.hold_for_read())
                self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": True})
                self.assertEqual(self.gpu.read.call_count, 3)
                self.assertEqual(self.gpu.nvapi.ForcePstate.call_count, 1)
                self.assert_no_curve_or_fan()
                self.assertTrue(self.gpu.release_legacy_p0()[0])
                self.app._clk_lock = None
                self.gpu.read.reset_mock()
                self.gpu.nvapi.ForcePstate.reset_mock()

    def test_locked_controls_recheck_existing_hold_without_reissuing_or_releasing(self):
        self.gpu._legacy_p0_owned = True
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": False}
        self.app.unlocked.return_value = False
        self.assertTrue(self.app.hold_for_read())
        self.assertTrue(self.app._clk_lock["verified"])
        self.assertEqual(self.gpu.read.call_count, 3)
        self.gpu.nvapi.ForcePstate.assert_not_called()
        self.assert_no_curve_or_fan()

    def test_locked_controls_cannot_acquire_new_request(self):
        self.app.unlocked.return_value = False
        self.assertFalse(self.app.hold_for_read())
        self.gpu.nvapi.ForcePstate.assert_not_called()
        self.gpu.read.assert_not_called()
        self.assertIsNone(self.app._clk_lock)
        self.assert_no_curve_or_fan()

    def test_failed_recheck_preserves_existing_ownership_as_unconfirmed(self):
        self.gpu._legacy_p0_owned = True
        self.gpu.read.side_effect = RuntimeError("telemetry unavailable")
        self.app._clk_lock = {"kind": Druta.LOCK_P0, "verified": True}
        self.assertFalse(self.app.hold_for_read())
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": False})
        self.assertTrue(self.gpu.legacy_p0_owned())
        self.gpu.nvapi.ForcePstate.assert_not_called()
        self.assert_no_curve_or_fan()

    def test_failed_new_hold_preserves_failed_release_for_retry(self):
        self.gpu.read.side_effect = RuntimeError("telemetry unavailable")
        self.gpu.nvapi.ForcePstate.side_effect = [0, -1, 0]
        self.assertFalse(self.app.hold_for_read())
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": False})
        self.assertTrue(self.gpu.legacy_p0_owned())
        self.assertEqual(self.gpu.nvapi.ForcePstate.call_count, 2)
        self.assertTrue(self.app.release_current()[0])
        self.assertIsNone(self.app._clk_lock)
        self.assert_no_curve_or_fan()

    def test_other_lock_must_release_before_request_and_failure_stops_request(self):
        self.app._clk_lock = {"kind": Druta.LOCK_NVML}
        self.gpu.reset_gpu_clocks = Mock(return_value=(False, "release failed"))
        self.assertFalse(self.app.hold_for_read())
        self.gpu.nvapi.ForcePstate.assert_not_called()
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_NVML})
        self.gpu.reset_gpu_clocks.return_value = True, "released"
        self.assertTrue(self.app.hold_for_read())
        self.assertEqual(self.app._clk_lock["kind"], Druta.LOCK_P0)
        self.assert_no_curve_or_fan()

    def test_unexpected_hold_exception_retains_backend_ownership(self):
        def fail():
            self.gpu._legacy_p0_owned = True
            raise RuntimeError("transport lost after request")
        self.gpu.hold_legacy_p0 = fail
        self.assertFalse(self.app.hold_for_read())
        self.assertEqual(self.app._clk_lock, {"kind": Druta.LOCK_P0, "verified": False})
        self.assertTrue(self.gpu.legacy_p0_owned())

    def test_worker_reads_existing_p0_directly_even_without_held_hint(self):
        self.run_worker(held=False, at_p0=True)
        self.assertIn("captured directly, no load started", self.app._tim_note)

    def test_worker_waits_for_held_band_without_cuda_and_names_generic_release(self):
        self.run_worker(held=True, at_p0=False)
        self.app.wait_for_band.assert_called_once_with(self.gpu)
        self.assertIn("Clocks > Release", self.app._tim_note)
        self.assertNotIn("V/F", self.app._tim_note)
        self.assertNotIn("Ctrl+H", self.app._tim_note)

    def run_worker(self, *, held, at_p0):
        self.app._gpu_gen = 1
        self.app._tim_lock = threading.Lock()
        self.app._tim_busy = True
        self.app._tim_caps = {}
        self.app.is_p0 = Mock(return_value=at_p0)
        self.app.wait_for_band = Mock(return_value=(True, (900, 0)))
        snap = SimpleNamespace(ok=True, mem_stable=True, key=900)
        with patch("druta.druta.timings.available", return_value=SimpleNamespace(ok=True)), \
                patch("druta.druta.timings.snapshot", return_value=snap) as capture, \
                patch("druta.druta.gpuload.induce") as induce:
            self.app._induce_worker(held=held)
        capture.assert_called_once_with(self.gpu)
        induce.assert_not_called()
        self.assertIs(self.app._tim, snap)
        self.assertFalse(self.app._tim_busy)
        self.assertTrue(self.app._tim_new)


if __name__ == "__main__":
    unittest.main()
