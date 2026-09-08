# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise accepted V/F lock writes and recovery using an in-memory driver."""
import ctypes
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from druta.nvbackend import (
    GPU, NvAPI, _ClockLock, VF_LOCK_DOMAIN, VF_LOCK_MODE_FREQ,
    VF_LOCK_MODE_POINT, VF_LOCK_VERSION,
)


class LockDriver:
    def __init__(self):
        self.state = _ClockLock(version=NvAPI.ver(_ClockLock, VF_LOCK_VERSION),
                                flags=0x12345678, count=7)
        for index, entry in enumerate(self.state.locks):
            entry.domain = index
            entry.volt_uV = 800000 + index * 10000
            entry.unk1, entry.unk2, entry.unk3 = index + 100, index + 200, index + 300
        for index in (0, 1):
            self.state.locks[index].lockMode = VF_LOCK_MODE_FREQ
            self.state.locks[index].volt_uV = 1350000 + index * 150000
        self.read_count = 0
        self.read_actions = {}
        self.write_actions = {}
        self.writes = []

    def get(self, handle, pointer):
        self.read_count += 1
        action = self.read_actions.get(self.read_count, 0)
        if isinstance(action, Exception):
            raise action
        status = action(self) if callable(action) else action
        if status:
            return status
        ctypes.memmove(pointer, ctypes.byref(self.state), ctypes.sizeof(self.state))
        return 0

    def set(self, handle, pointer):
        data = ctypes.string_at(pointer, ctypes.sizeof(self.state))
        self.writes.append(data)
        action = self.write_actions.get(len(self.writes), 0)
        if isinstance(action, Exception):
            # Simulate a transport that accepted the bytes before raising.
            self.state = _ClockLock.from_buffer_copy(data)
            raise action
        if action:
            return action
        self.state = _ClockLock.from_buffer_copy(data)
        return 0

    def gpu(self):
        gpu = GPU.__new__(GPU)
        gpu._lock = threading.RLock()
        gpu.vf_curve_applicable = Mock(return_value=True)
        gpu.nvapi = SimpleNamespace(ok=True, gpu=object(), ver=NvAPI.ver,
                                    BoostLock=self.get, VfLockSet=self.set)
        return gpu


class VfLockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.driver = LockDriver()
        self.gpu = self.driver.gpu()

    def test_status_distinguishes_unlocked_from_failed_getter(self):
        self.assertEqual(self.gpu.read_vf_lock_status(), (None, None))
        self.driver.read_actions[2] = -1
        lock, error = self.gpu.read_vf_lock_status()
        self.assertIsNone(lock)
        self.assertIn("unknown", error)

    def test_success_changes_only_selected_mode_and_voltage(self):
        expected = _ClockLock.from_buffer_copy(bytes(self.driver.state))
        expected.locks[VF_LOCK_DOMAIN].lockMode = VF_LOCK_MODE_POINT
        expected.locks[VF_LOCK_DOMAIN].volt_uV = 900000
        self.assertTrue(self.gpu.set_vf_lock(900000)[0])
        self.assertEqual(self.driver.writes, [bytes(expected)])
        self.assertFalse(self.gpu.vf_lock_recovery_pending())
        self.assertEqual(self.gpu.read_vf_lock()["volt_uV"], 900000)

    def test_failed_initial_read_never_writes_or_claims_recovery(self):
        self.driver.read_actions[1] = -1
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        self.assertEqual(self.driver.writes, [])
        self.assertFalse(self.gpu.vf_lock_recovery_pending())

    def test_accepted_write_with_failed_readback_restores_original_exactly(self):
        original = bytes(self.driver.state)
        self.driver.read_actions[2] = -1
        ok, message = self.gpu.set_vf_lock(900000)
        self.assertFalse(ok)
        self.assertIn("restored and verified", message)
        self.assertEqual(bytes(self.driver.state), original)
        self.assertEqual(len(self.driver.writes), 2)
        self.assertFalse(self.gpu.vf_lock_recovery_pending())

    def test_rollback_restores_preexisting_point_instead_of_clearing_it(self):
        self.driver.state.locks[4].lockMode = VF_LOCK_MODE_POINT
        self.driver.state.locks[4].volt_uV = 875000
        original = bytes(self.driver.state)
        self.driver.read_actions[2] = -1
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        self.assertEqual(bytes(self.driver.state), original)
        self.assertEqual(self.gpu.read_vf_lock()["volt_uV"], 875000)

    def test_failed_rollback_getter_retains_recovery_and_blocks_another_hold(self):
        original = bytes(self.driver.state)
        self.driver.read_actions.update({2: -1, 3: -1})
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        self.assertTrue(self.gpu.vf_lock_recovery_pending())
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].lockMode, VF_LOCK_MODE_POINT)
        reads, writes = self.driver.read_count, len(self.driver.writes)
        self.assertFalse(self.gpu.set_vf_lock(950000)[0])
        self.assertEqual((self.driver.read_count, len(self.driver.writes)), (reads, writes))
        self.assertTrue(self.gpu.recover_vf_lock()[0])
        self.assertEqual(bytes(self.driver.state), original)
        self.assertFalse(self.gpu.vf_lock_recovery_pending())

    def test_refused_rollback_write_remains_retryable(self):
        original = bytes(self.driver.state)
        self.driver.read_actions[2] = -1
        self.driver.write_actions[2] = -1
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        self.assertTrue(self.gpu.vf_lock_recovery_pending())
        self.assertTrue(self.gpu.recover_vf_lock()[0])
        self.assertEqual(bytes(self.driver.state), original)

    def test_failed_rollback_verification_is_not_assumed_restored(self):
        original = bytes(self.driver.state)
        self.driver.read_actions.update({2: -1, 4: -1})
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        self.assertEqual(bytes(self.driver.state), original)
        self.assertTrue(self.gpu.vf_lock_recovery_pending())
        self.assertTrue(self.gpu.recover_vf_lock()[0])
        self.assertEqual(len(self.driver.writes), 2)  # fresh confirmation, no extra write

    def test_setter_and_verification_exceptions_still_attempt_recovery(self):
        for phase in ("setter", "readback"):
            with self.subTest(phase=phase):
                driver = LockDriver()
                original = bytes(driver.state)
                if phase == "setter":
                    driver.write_actions[1] = OSError("injected accepted-write transport failure")
                else:
                    driver.read_actions[2] = OSError("injected getter failure")
                gpu = driver.gpu()
                self.assertFalse(gpu.set_vf_lock(900000)[0])
                self.assertEqual(bytes(driver.state), original)
                self.assertFalse(gpu.vf_lock_recovery_pending())

    def test_rollback_preserves_new_frequency_lock_and_unknown_driver_bytes(self):
        self.driver.read_actions[2] = -1

        def concurrent_update(driver):
            driver.state.locks[0].volt_uV = 1800000
            driver.state.locks[1].volt_uV = 1500000
            driver.state.flags = 0x87654321
            driver.state.locks[VF_LOCK_DOMAIN].unk3 = 9999
            return 0

        self.driver.read_actions[3] = concurrent_update
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        state = self.driver.state
        self.assertEqual(state.locks[VF_LOCK_DOMAIN].lockMode, 0)
        self.assertEqual(state.locks[0].volt_uV, 1800000)
        self.assertEqual(state.locks[1].volt_uV, 1500000)
        self.assertEqual(state.flags, 0x87654321)
        self.assertEqual(state.locks[VF_LOCK_DOMAIN].unk3, 9999)

    def test_concurrent_different_target_is_not_overwritten_by_rollback(self):
        def other_tuner(driver):
            driver.state.locks[VF_LOCK_DOMAIN].volt_uV = 950000
            return 0

        self.driver.read_actions[2] = other_tuner
        ok, message = self.gpu.set_vf_lock(900000)
        self.assertFalse(ok)
        self.assertIn("concurrently", message)
        self.assertEqual(len(self.driver.writes), 1)
        self.assertEqual(self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV, 950000)
        self.assertTrue(self.gpu.vf_lock_recovery_pending())
        # Explicit Reset-all/Release-all may clear the current lock, unlike rollback.
        self.assertTrue(self.gpu.clear_vf_lock()[0])
        self.assertFalse(self.gpu.vf_lock_recovery_pending())

    def test_same_voltage_in_a_different_domain_does_not_verify_our_write(self):
        def wrong_domain(driver):
            driver.state.locks[VF_LOCK_DOMAIN].lockMode = 0
            driver.state.locks[3].lockMode = VF_LOCK_MODE_POINT
            driver.state.locks[3].volt_uV = 900000
            return 0

        self.driver.read_actions[2] = wrong_domain
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        self.assertEqual(len(self.driver.writes), 1)

    def test_failed_explicit_clear_readback_keeps_pending_until_confirmed_clear(self):
        self.driver.read_actions.update({2: -1, 3: -1, 5: -1})
        self.assertFalse(self.gpu.set_vf_lock(900000)[0])
        self.assertFalse(self.gpu.clear_vf_lock()[0])
        self.assertTrue(self.gpu.vf_lock_recovery_pending())
        self.assertTrue(self.gpu.clear_vf_lock()[0])
        self.assertFalse(self.gpu.vf_lock_recovery_pending())

    def test_targeted_status_is_not_hidden_by_an_earlier_foreign_hold(self):
        for domain, voltage in ((4, 875000), (VF_LOCK_DOMAIN, 900000)):
            self.driver.state.locks[domain].lockMode = VF_LOCK_MODE_POINT
            self.driver.state.locks[domain].volt_uV = voltage
        self.assertEqual(self.gpu.read_vf_lock()["domain"], 4)
        held, error = self.gpu.read_vf_lock_status(domain=VF_LOCK_DOMAIN)
        self.assertIsNone(error)
        self.assertEqual((held["domain"], held["volt_uV"]), (VF_LOCK_DOMAIN, 900000))

    def test_missing_target_is_unknown_instead_of_confirmed_unlocked(self):
        self.driver.state.count = VF_LOCK_DOMAIN
        held, error = self.gpu.read_vf_lock_status(domain=VF_LOCK_DOMAIN)
        self.assertIsNone(held)
        self.assertIn("unknown", error)
        self.assertFalse(self.gpu.clear_vf_lock(domain=VF_LOCK_DOMAIN, expected_uv=900000)[0])
        self.assertEqual(self.driver.writes, [])

    def test_scoped_release_preserves_foreign_holds_frequency_locks_and_other_bytes(self):
        for domain, voltage in ((4, 875000), (VF_LOCK_DOMAIN, 900000)):
            self.driver.state.locks[domain].lockMode = VF_LOCK_MODE_POINT
            self.driver.state.locks[domain].volt_uV = voltage
        expected = _ClockLock.from_buffer_copy(bytes(self.driver.state))
        expected.locks[VF_LOCK_DOMAIN].lockMode = 0
        ok, message = self.gpu.clear_vf_lock(domain=VF_LOCK_DOMAIN, expected_uv=900000)
        self.assertTrue(ok, message)
        self.assertEqual(self.driver.writes, [bytes(expected)])
        self.assertEqual(bytes(self.driver.state), bytes(expected))

    def test_scoped_release_does_not_overwrite_a_replacement_request(self):
        self.driver.state.locks[VF_LOCK_DOMAIN].lockMode = VF_LOCK_MODE_POINT
        self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV = 925000
        before = bytes(self.driver.state)
        self.assertTrue(self.gpu.clear_vf_lock(domain=VF_LOCK_DOMAIN, expected_uv=900000)[0])
        self.assertEqual(bytes(self.driver.state), before)
        self.assertEqual(self.driver.writes, [])

    def test_scoped_release_cannot_confirm_a_missing_target_after_write(self):
        self.driver.state.locks[VF_LOCK_DOMAIN].lockMode = VF_LOCK_MODE_POINT
        self.driver.state.locks[VF_LOCK_DOMAIN].volt_uV = 900000

        def omit_target(driver):
            driver.state.count = VF_LOCK_DOMAIN
            return 0

        self.driver.read_actions[2] = omit_target
        ok, message = self.gpu.clear_vf_lock(domain=VF_LOCK_DOMAIN, expected_uv=900000)
        self.assertFalse(ok)
        self.assertIn("missing", message)
        self.assertEqual(len(self.driver.writes), 1)


if __name__ == "__main__":
    unittest.main()
