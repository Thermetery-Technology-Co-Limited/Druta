# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from druta import gpuload
from druta.nvbackend import VF_LOCK_DOMAIN, VF_LOCK_MODE_POINT
from tests.test_vf_lock_recovery import LockDriver
from tests import test_mp_candidates_ui


class VerificationP0Tests(unittest.TestCase):
    def setUp(self):
        self.driver = LockDriver()
        self.gpu = self.driver.gpu()
        self.gpu.read_vcore_mv = Mock(return_value=743.0)
        self.gpu.read_vf_curve = Mock(return_value=([
            {"volt_mv": 737.5}, {"volt_mv": 750.0}], None))
        self.gpu.read = Mock(return_value={"pstate": 0, "core": 1200, "mem": 7000})
        self.original = bytes(self.driver.state)

    def run_verify(self, callback, **kwargs):
        with patch("druta.gpuload.time.sleep"):
            return gpuload.verify_in_p0(self.gpu, callback, **kwargs)

    def test_low_voltage_p0_passes_and_exact_lock_table_restored(self):
        def callback(point):
            self.assertEqual(point(), (0, 1200, 7000))
            self.assertEqual(self.gpu.read_vf_lock()["volt_uV"], 737500)
            return "rail restored"
        self.assertEqual(self.run_verify(callback), "rail restored")
        self.assertEqual(bytes(self.driver.state), self.original)

    def test_existing_point_hold_is_never_written(self):
        entry = self.driver.state.locks[VF_LOCK_DOMAIN]
        entry.lockMode, entry.volt_uV = VF_LOCK_MODE_POINT, 900000
        self.assertEqual(self.run_verify(lambda point: point()), (0, 1200, 7000))
        self.assertEqual(self.driver.writes, [])

    def test_cancel_and_callback_failure_restore(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                callback = Mock(side_effect=ValueError("rail failure"))
                with self.assertRaisesRegex(Exception, "cancelled|rail failure"):
                    self.run_verify(callback, cancelled=lambda: cancelled)
                self.assertEqual(bytes(self.driver.state), self.original)
                if cancelled:
                    callback.assert_not_called()

    def test_p2_never_reaches_rail_verifier(self):
        self.gpu.read.return_value["pstate"] = 2
        callback = Mock()
        with self.assertRaisesRegex(Exception, "did not settle"):
            self.run_verify(callback, settle_timeout=.001)
        callback.assert_not_called()
        self.assertEqual(bytes(self.driver.state), self.original)

    def test_lost_hold_invalidates_success_and_preserves_concurrent_request(self):
        def callback(point):
            self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV = 850000
            return True
        with self.assertRaisesRegex(Exception, "RESTORE FAILED"):
            self.run_verify(callback)
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV, 850000)
        self.assertTrue(self.gpu.vf_lock_recovery_pending())

    def test_restore_failure_remains_retryable(self):
        self.driver.write_actions[2] = -1
        with self.assertRaisesRegex(Exception, "RESTORE FAILED"):
            self.run_verify(lambda point: True)
        self.assertTrue(self.gpu._vf_lock_recovery["verification"])
        self.assertTrue(self.gpu.recover_vf_lock()[0])
        self.assertEqual(bytes(self.driver.state), self.original)

    def test_unreadable_hold_and_unknown_domain_mode_write_nothing(self):
        self.driver.read_actions[1] = -1
        with self.assertRaisesRegex(Exception, "unknown"):
            self.run_verify(Mock())
        self.assertEqual(self.driver.writes, [])

        self.driver.state.locks[VF_LOCK_DOMAIN].lockMode = 9
        with self.assertRaisesRegex(Exception, "already in use"):
            self.run_verify(Mock())
        self.assertEqual(self.driver.writes, [])

    def test_worker_stops_cuda_before_hold_and_restores_rail_before_release(self):
        fixture = test_mp_candidates_ui.CandidateUi()
        fixture.setUp()
        app = fixture.app
        events = []
        @contextmanager
        def hold(**kwargs):
            self.assertEqual(events, ["CUDA stopped"])
            events.append("hold")
            try:
                yield lambda: None
            finally:
                events.append("release")
        app.gpu.verification_p0 = hold
        def induce(gpu, **kwargs):
            value = kwargs["on_settled"]()
            events.append("CUDA stopped")
            return {"result": value}
        def verify(**kwargs):
            self.assertEqual(kwargs["operating_point"](), (0, 1965, 7000))
            events.append("rail restored")
            return True, "restored", []
        app.rail.verify.side_effect = verify
        with patch("druta.gpuload.induce", side_effect=induce), \
                patch("druta.gpuload.time.sleep"):
            app._i2c_verify_worker()
        self.assertEqual(events, ["CUDA stopped", "hold", "rail restored", "release"])
        self.assertTrue(app.i2c_verified())
