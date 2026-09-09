# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""NCP4206 verification accounts for loadline drop and always restores control."""

import unittest
from unittest.mock import Mock, patch

from druta import ncp4206
from tests import test_ncp4206


class NcpVerifyTests(unittest.TestCase):
    def verify(self, samples, *, restore_failure=False, xoc=False):
        rail = test_ncp4206.NCPTests().rail()
        rail.xoc = xoc
        before = dict(rail.regs)
        rail._verification_vmon = Mock(side_effect=[(v, 1000 / 512) for v in samples])
        if restore_failure:
            restore = rail.restore_control

            def fail_recovery(state, *, recovery=False):
                return (False, "test restore failure") if recovery else restore(state)

            rail.restore_control = Mock(side_effect=fail_recovery)
        with patch("druta.ncp4206.time.sleep"):
            result = rail.verify(acknowledged=True, operating_point=lambda: (0, 1000, 3000))
        if not restore_failure:
            self.assertEqual(rail.regs, before)
        return rail, result

    def test_first_rung_confirmed_and_original_mode_command_restored(self):
        rail, (ok, message, ladder) = self.verify([1200] * 25 + [1210] * 25 + [1200] * 25)
        self.assertTrue(ok)
        self.assertEqual(len(ladder), 1)
        self.assertEqual(ladder[0]["target_mv"], 1225)
        self.assertTrue(ladder[0]["moved"])
        self.assertIn("10.00", message)
        self.assertEqual(rail._verification_vmon.call_count, 75)

    def test_flat_first_rung_then_loadline_response_on_second(self):
        _, (ok, _, ladder) = self.verify([1200] * 25 + [1200] * 25 + [1212] * 25 + [1200] * 25)
        self.assertTrue(ok)
        self.assertEqual([r["target_mv"] for r in ladder], [1225, 1237.5])
        self.assertEqual([r["moved"] for r in ladder], [False, True])
        self.assertGreater(ladder[1]["target_mv"] - max(ladder[1]["samples_mv"]), 15)

    def test_all_flat_rungs_fail_and_restore(self):
        _, (ok, message, ladder) = self.verify([1200] * 100)
        self.assertFalse(ok)
        self.assertEqual([r["target_mv"] for r in ladder], [1225, 1237.5, 1250])
        self.assertIn("no positive I2C VMON response", message)

    def test_missing_baseline_reads_write_nothing(self):
        rail, (ok, _, ladder) = self.verify([1200, 1200, None, 1200, 1200])
        self.assertFalse(ok)
        self.assertEqual(rail.calls, [])
        self.assertEqual(ladder, [])

    def test_missing_rung_read_stops_and_restores(self):
        _, (ok, _, ladder) = self.verify([1200] * 25 + [1210, None, 1210, 1210, 1210])
        self.assertFalse(ok)
        self.assertEqual(len(ladder), 1)
        self.assertFalse(ladder[0]["moved"])

    def test_overshoot_stops_without_trying_a_higher_target(self):
        _, (ok, message, ladder) = self.verify([1200] * 25 + [1241] * 25)
        self.assertFalse(ok)
        self.assertEqual(len(ladder), 1)
        self.assertTrue(ladder[0]["overshoot"])
        self.assertIn("exceeded the command", message)

    def test_restore_failure_overrides_success(self):
        _, (ok, message, ladder) = self.verify([1200] * 25 + [1210] * 25 + [1200] * 25,
                                              restore_failure=True)
        self.assertFalse(ok)
        self.assertTrue(ladder[0]["moved"])
        self.assertIn("restoration failed", message)

    def test_baseline_median_and_noise_raise_required_response(self):
        _, (ok, _, ladder) = self.verify([1194, 1200, 1200, 1200, 1206] * 5
                                        + [1210] * 25 + [1225] * 25 + [1200] * 25)
        self.assertTrue(ok)
        self.assertEqual(ladder[0]["baseline_mv"], 1200)
        self.assertEqual(ladder[0]["threshold_mv"], 12)
        self.assertEqual([r["moved"] for r in ladder], [False, True])

    def test_upper_bound_is_enforced_even_with_xoc_available(self):
        _, (ok, _, ladder) = self.verify([1250] * 50, xoc=True)
        self.assertFalse(ok)
        self.assertEqual([r["target_mv"] for r in ladder], [1275])
        self.assertTrue(all(r["target_mv"] <= ncp4206.NORMAL_MAX_MV for r in ladder))
        rail, (ok, _, ladder) = self.verify([1275] * 25)
        self.assertFalse(ok)
        self.assertEqual(rail.calls, [])
        self.assertEqual(ladder, [])


if __name__ == "__main__":
    unittest.main()
