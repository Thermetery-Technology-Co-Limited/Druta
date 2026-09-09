# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""MP offset verification cannot pass without confirmed restoration."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from druta import railctl


class MPVerifyRestoreTests(unittest.TestCase):
    def setUp(self):
        self.rail = railctl.Rail.__new__(railctl.Rail)
        self.rail.p = SimpleNamespace(wreg=0x23, wbytes=2, wbits=(7, 0),
                                     lsb_mv=6.25, min_loaded_mv=800,
                                     rungs=[6.25, 12.5, 25.0])
        self.rail.addr7 = 0x20
        self.rail.present = Mock(return_value=True)
        # A negative starting offset exercises two's-complement restoration;
        # upper transaction bits are reserved, not part of the offset field.
        self.rail.read = Mock(side_effect=[0xABFE, 0x00FE])
        self.rail._sample = Mock(side_effect=[(0.0, 1.0), (6.25, 1.0)])
        self.rail.set_offset_mv = Mock(return_value=(True, "written"))
        self.log = Mock()
        self.sleep = patch("druta.railctl.time.sleep").start()
        self.addCleanup(patch.stopall)

    def verify(self):
        return self.rail.verify(acknowledged=True, log=self.log,
                                ref=lambda: 1050.0)

    def assert_restoration_attempted(self):
        self.assertEqual(self.rail.set_offset_mv.call_args,
                         call(-12.5, acknowledged=True))
        self.assertEqual(self.rail.read.call_args, call(0x23, 2))

    def assert_restore_failed(self):
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED", message)
        self.assertNotIn("Restored to", message)
        self.assertFalse(any(c.args[0].startswith("restored to")
                             for c in self.log.call_args_list))
        self.assert_restoration_attempted()
        return message, ladder

    def test_response_passes_only_after_exact_original_field_restored(self):
        ok, message, ladder = self.verify()
        self.assertTrue(ok)
        self.assertIn("Restored to -12.50 mV", message)
        self.assertTrue(ladder[0]["moved"])
        self.assertEqual(self.rail.set_offset_mv.call_args_list,
                         [call(-6.25, acknowledged=True),
                          call(-12.5, acknowledged=True)])
        self.assert_restoration_attempted()

    def test_refused_restore_overrides_success_even_if_readback_matches(self):
        self.rail.set_offset_mv.side_effect = [(True, "written"),
                                               (False, "identity changed")]
        message, _ = self.assert_restore_failed()
        self.assertIn("identity changed", message)
        self.assertEqual(self.rail.read.call_count, 2)

    def test_wrong_restore_field_overrides_success(self):
        self.rail.read.side_effect = [0xABFE, 0x00FF]
        message, _ = self.assert_restore_failed()
        self.assertIn("expected field 0xFE", message)

    def test_unreadable_restore_field_overrides_success(self):
        self.rail.read.side_effect = [0xABFE, None]
        message, _ = self.assert_restore_failed()
        self.assertIn("unreadable", message)

    def test_restore_exception_still_attempts_readback_and_fails(self):
        self.rail.set_offset_mv.side_effect = [(True, "written"),
                                               RuntimeError("bus gone")]
        message, _ = self.assert_restore_failed()
        self.assertIn("bus gone", message)
        self.assertEqual(self.rail.read.call_count, 2)

    def test_restore_readback_exception_overrides_success(self):
        self.rail.read.side_effect = [0xABFE, RuntimeError("read exploded")]
        message, _ = self.assert_restore_failed()
        self.assertIn("read exploded", message)

    def test_measurement_read_failure_stops_ladder_and_restores(self):
        self.rail._sample.side_effect = [(0.0, 1.0), (None, None)]
        ok, message, ladder = self.verify()
        self.assertFalse(ok)
        self.assertIn("INCONCLUSIVE", message)
        self.assertIn("Restored to", message)
        self.assertTrue(ladder[0]["read_failed"])
        self.assertEqual(self.rail.set_offset_mv.call_count, 2)
        self.assert_restoration_attempted()

    def test_measurement_exception_restores_and_returns_failure(self):
        self.rail._sample.side_effect = [(0.0, 1.0), RuntimeError("read lost")]
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("read lost", message)
        self.assertIn("Restored to", message)
        self.assert_restoration_attempted()

    def test_early_rung_refusal_does_not_bypass_failed_restoration(self):
        self.rail.set_offset_mv.side_effect = [(False, "no headroom"),
                                               (False, "restore failed")]
        message, ladder = self.assert_restore_failed()
        self.assertIn("no headroom", message)
        self.assertEqual(ladder, [{"rung_mv": 6.25,
                                   "refused": "no headroom"}])

    def test_early_rung_refusal_restores_and_preserves_inconclusive_verdict(self):
        self.rail.set_offset_mv.side_effect = [(False, "no headroom"),
                                               (True, "restored")]
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("INCONCLUSIVE", message)
        self.assertIn("Restored to", message)
        self.assert_restoration_attempted()

    def test_rung_write_exception_also_attempts_restoration(self):
        self.rail.set_offset_mv.side_effect = [RuntimeError("write lost"),
                                               (True, "restored")]
        ok, message, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("write lost", message)
        self.assert_restoration_attempted()


if __name__ == "__main__":
    unittest.main()
