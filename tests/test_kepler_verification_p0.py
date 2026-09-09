# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import Mock, patch
from druta import gpuload
from druta.druta import Druta
from tests import test_legacy_p0
from tests.test_verification_lifecycle import bare_app


class KeplerVerificationP0(unittest.TestCase):
    def setUp(self):
        self.gpu = test_legacy_p0.card()
        self.gpu.arch.return_value = self.gpu.ARCH_KEPLER
        # Deliberately absent from the persistent UI's measured-board list.
        self.gpu.nvapi.selected = {"devid": 0x1184, "subsys": 0x1033196e}

    def run_verify(self, callback, **kwargs):
        with patch("druta.gpuload.time.sleep"):
            return gpuload.verify_in_p0(self.gpu, callback, **kwargs)

    def test_runtime_capability_requires_p0_and_releases_new_request(self):
        self.assertFalse(self.gpu.legacy_p0_supported())
        def callback(point):
            self.assertTrue(self.gpu.legacy_p0_owned())
            self.assertEqual(point(), (0, 540, 900))
            return "controller restored"
        self.assertEqual(self.run_verify(callback), "controller restored")
        calls = self.gpu.nvapi.ForcePstate.call_args_list
        self.assertEqual([[v.value for v in c.args[1:]] for c in calls], [[0, 2], [16, 2]])
        self.assertTrue(all(c.args[0] == self.gpu.nvapi.gpu for c in calls))
        self.assertFalse(self.gpu.legacy_p0_owned())

    def test_existing_session_hold_is_preserved(self):
        self.gpu._legacy_p0_owned = True
        self.assertEqual(self.run_verify(lambda point: point()), (0, 540, 900))
        self.gpu.nvapi.ForcePstate.assert_not_called()
        self.assertTrue(self.gpu.legacy_p0_owned())

    def test_p8_cannot_reach_verifier_even_after_accepted_request(self):
        self.gpu.read.return_value["pstate"] = 8
        callback = Mock()
        with self.assertRaisesRegex(Exception, "did not settle"):
            self.run_verify(callback, settle_timeout=.001)
        callback.assert_not_called()
        self.assertFalse(self.gpu.legacy_p0_owned())

    def test_transport_exception_and_callback_exception_both_release(self):
        for during_request in (True, False):
            with self.subTest(during_request=during_request):
                self.setUp()
                if during_request:
                    self.gpu.nvapi.ForcePstate.side_effect = [RuntimeError("transport"), 0]
                with self.assertRaisesRegex(Exception, "transport|controller"):
                    self.run_verify(Mock(side_effect=RuntimeError("controller")))
                self.assertFalse(self.gpu.legacy_p0_owned())
                self.assertEqual(self.gpu.nvapi.ForcePstate.call_count, 2)

    def test_release_failure_is_retryable_blocks_swap_and_marks_shutdown_unclean(self):
        self.gpu.nvapi.ForcePstate.side_effect = [0, -1, 0]
        with self.assertRaisesRegex(Exception, "RELEASE FAILED"):
            self.run_verify(lambda point: True)
        app = Druta.__new__(Druta)
        app.gpu = self.gpu
        app._clk_lock = None
        app.log = Mock()
        app.set_lock_state = Mock()
        app.sync_legacy_p0_lock = Mock()
        self.assertFalse(app.shutdown_is_clean())
        self.assertFalse(app.swap_gpu("0000:05:00.0"))
        app.release_on_exit()
        self.assertTrue(app.shutdown_is_clean())
        self.assertFalse(self.gpu.legacy_p0_owned())

    def test_missing_api_and_cancellation_write_nothing(self):
        callback = Mock()
        with self.assertRaisesRegex(Exception, "cancelled"):
            self.run_verify(callback, cancelled=lambda: True)
        self.gpu.nvapi.ForcePstate.assert_not_called()
        self.gpu.nvapi.ForcePstate = None
        with self.assertRaisesRegex(Exception, "unavailable"):
            self.run_verify(callback)
        callback.assert_not_called()

    def test_production_worker_reads_direct_vmon_below_800_without_cuda_or_nvapi_voltage(self):
        app = bare_app()
        app.gpu = self.gpu
        app._i2c_busy = True
        self.gpu.read_vcore_mv = Mock(side_effect=AssertionError("NVAPI voltage must not be read"))
        rail = app.rail
        self.assertTrue(rail.present())
        original = dict(rail.regs)
        read = rail.read
        events = []

        def vmon(reg, width):
            if reg != 0xd7:
                return read(reg, width)
            self.assertTrue(self.gpu.legacy_p0_owned())
            # LINEAR11 exponent -10: 750 mV baseline, 761.71875 mV response.
            raised = bool(rail.regs[0xd3] & 8)
            events.append("raised" if raised else "baseline")
            return ((-10 & 31) << 11) | (780 if raised else 768)

        def force(_handle, state, _flags):
            if state.value == 16:
                self.assertEqual(rail.regs, original)
                self.assertEqual(events[-1], "baseline")
                events.append("release")
            return 0

        rail.read = vmon
        self.gpu.nvapi.ForcePstate.side_effect = force
        with patch("druta.gpuload.time.sleep"), \
                patch("druta.gpuload.induce", side_effect=AssertionError("no CUDA on Kepler")) as load:
            app._i2c_verify_worker()
        self.assertTrue(app.i2c_verified(), app.log.call_args_list)
        self.assertFalse(app._i2c_busy)
        self.assertTrue(app.shutdown_is_clean())
        self.assertEqual(events, ["baseline"] * 25 + ["raised"] * 25 + ["baseline"] * 25 + ["release"])
        self.assertEqual(rail.regs, original)
        self.gpu.read_vcore_mv.assert_not_called()
        load.assert_not_called()
