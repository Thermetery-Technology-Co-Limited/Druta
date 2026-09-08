# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""A failed observation must not abandon a V/F hold or its pending recovery."""
from unittest.mock import Mock, patch

from druta.druta import Druta
from druta.nvbackend import VF_LOCK_DOMAIN, VF_LOCK_MODE_POINT
from tests.test_arch_ui_regressions import FakeUiTest
from tests.test_vf_lock_recovery import LockDriver


class VfLockUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.driver = LockDriver()
        self.app.gpu = self.driver.gpu()
        self.app._clk_lock = None
        point = dict(idx=1, volt_mv=900, freq_mhz=2400, delta_khz=0)
        self.app.vf_points, self.app.vf_by_idx, self.app.vf_sel = [point], {1: point}, 1
        self.app.vf_work = self.app.vf_orig = {1: 0}
        self.app.vf_applicable = Mock(return_value=True)
        self.values.update(hold_info="", vf_holdline=[[]])

    def exit_app(self):
        with patch("builtins.print"):
            self.app.release_on_exit()

    def test_failed_status_after_confirmed_set_retains_ownership_for_exit_cleanup(self):
        for failure in (-1, RuntimeError("getter failed")):
            with self.subTest(failure=failure):
                self.driver = LockDriver()
                self.app.gpu = self.driver.gpu()
                self.driver.read_actions[4] = failure
                self.app.hold_point()
                self.assertFalse(self.app._clk_lock["verified"])
                self.assertIn("unconfirmed", self.values["hold_info"])
                self.assertEqual(self.values["vf_holdline"], [[]])
                self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].lockMode, VF_LOCK_MODE_POINT)
                self.assertFalse(self.app.shutdown_is_clean())
                self.assertFalse(self.app.hold_for_read())
                with patch("druta.druta.GPU") as constructor:
                    self.assertFalse(self.app.swap_gpu("0000:02:00.0"))
                constructor.assert_not_called()
                self.exit_app()
                self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].lockMode, 0)
                self.assertIsNone(self.app._clk_lock)
                self.assertTrue(self.app.shutdown_is_clean())

    def test_failed_setter_rollback_retains_recovery_until_explicit_release(self):
        before = bytes(self.driver.state)
        self.driver.read_actions.update({3: -1, 4: -1})
        self.app.hold_point()
        self.assertTrue(self.app._clk_lock["recovery"])
        self.assertTrue(self.app.gpu.vf_lock_recovery_pending())
        self.assertIn("RECOVERY REQUIRED", self.values["hold_info"])
        self.assertFalse(self.app.handover(Druta.LOCK_NVML))
        self.assertFalse(self.app.handover(Druta.LOCK_VF))
        with patch("druta.druta.GPU") as constructor:
            self.assertFalse(self.app.swap_gpu("0000:02:00.0"))
        constructor.assert_not_called()
        self.assertEqual(len(self.driver.writes), 1)
        self.app.release_lock()
        self.assertEqual(bytes(self.driver.state), before)
        self.assertIsNone(self.app._clk_lock)
        self.assertFalse(self.app.gpu.vf_lock_recovery_pending())

    def test_failed_recovery_at_exit_keeps_record_and_unclean_session_until_retry(self):
        self.driver.read_actions.update({3: -1, 4: -1, 5: -1})
        self.app.hold_point()
        self.exit_app()
        self.assertTrue(self.app._clk_lock["recovery"])
        self.assertFalse(self.app.shutdown_is_clean())
        self.exit_app()
        self.assertIsNone(self.app._clk_lock)
        self.assertTrue(self.app.shutdown_is_clean())

    def test_recovery_restores_foreign_prior_hold_without_releasing_it(self):
        self.driver.state.locks[4].lockMode = VF_LOCK_MODE_POINT
        self.driver.state.locks[4].volt_uV = 875000
        before = bytes(self.driver.state)
        self.driver.read_actions.update({3: -1, 4: -1})
        self.app.hold_point()
        self.exit_app()
        self.assertEqual(bytes(self.driver.state), before)
        self.assertIsNone(self.app._clk_lock)

    def test_recovery_of_earlier_owned_hold_is_followed_by_its_release(self):
        self.driver.state.locks[VF_LOCK_DOMAIN].lockMode = VF_LOCK_MODE_POINT
        self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV = 875000
        self.app._clk_lock = dict(kind=Druta.LOCK_VF, idx=0, got_idx=0, req_mv=875,
                                  got_mv=875, got_mhz=2300, domain=VF_LOCK_DOMAIN)
        self.driver.read_actions.update({3: -1, 4: -1})
        self.app.hold_point()
        self.assertEqual(self.app._clk_lock["previous_lock"]["req_mv"], 875)
        self.exit_app()
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].lockMode, 0)
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV, 875000)
        self.assertIsNone(self.app._clk_lock)

    def test_confirmed_unlock_or_replacement_after_set_does_not_claim_foreign_state(self):
        for unlocked in (True, False):
            with self.subTest(unlocked=unlocked):
                self.driver = LockDriver()
                self.app.gpu = self.driver.gpu()

                def replace(driver):
                    entry = driver.state.locks[VF_LOCK_DOMAIN]
                    entry.lockMode = 0 if unlocked else VF_LOCK_MODE_POINT
                    entry.volt_uV = 925000
                    return 0

                self.driver.read_actions[4] = replace
                self.app.hold_point()
                self.assertIsNone(self.app._clk_lock)
                self.exit_app()
                self.assertEqual(len(self.driver.writes), 1)
                self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV, 925000)

    def test_refusal_before_write_never_claims_ownership(self):
        self.driver.read_actions[1] = -1
        self.app.hold_point()
        self.assertEqual(self.driver.writes, [])
        self.assertIsNone(self.app._clk_lock)
        self.assertTrue(self.app.shutdown_is_clean())

    def test_later_added_foreign_domain_never_hides_or_gets_cleared_with_our_hold(self):
        def add_foreign(driver):
            driver.state.locks[4].lockMode = VF_LOCK_MODE_POINT
            driver.state.locks[4].volt_uV = 875000
            return 0

        self.driver.read_actions[4] = add_foreign
        self.app.hold_point()
        self.assertEqual(self.app._clk_lock["domain"], VF_LOCK_DOMAIN)
        self.assertEqual(self.app._clk_lock["req_mv"], 900)
        # Moving our hold must keep its domain even though another point lock
        # now appears before it in the driver's table.
        self.app.hold_point()
        self.assertEqual(self.app._clk_lock["domain"], VF_LOCK_DOMAIN)
        self.assertEqual(self.driver.state.locks[4].volt_uV, 875000)
        self.exit_app()
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].lockMode, 0)
        self.assertEqual(self.driver.state.locks[4].lockMode, VF_LOCK_MODE_POINT)
        self.assertEqual(self.driver.state.locks[4].volt_uV, 875000)
        self.assertIsNone(self.app._clk_lock)

    def test_later_replacement_of_our_request_is_preserved_on_release(self):
        self.app.hold_point()
        self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV = 925000
        self.app.release_lock()
        self.assertEqual(len(self.driver.writes), 1)
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].lockMode, VF_LOCK_MODE_POINT)
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV, 925000)
        self.assertIsNone(self.app._clk_lock)

    def test_missing_owned_domain_stays_unconfirmed_until_it_can_be_released(self):
        def remove_target(driver):
            driver.state.count = VF_LOCK_DOMAIN
            return 0

        self.driver.read_actions[4] = remove_target
        self.app.hold_point()
        self.assertFalse(self.app._clk_lock["verified"])
        self.exit_app()
        self.assertIsNotNone(self.app._clk_lock)
        self.assertFalse(self.app.shutdown_is_clean())
        self.driver.state.count = 7
        self.exit_app()
        self.assertIsNone(self.app._clk_lock)
