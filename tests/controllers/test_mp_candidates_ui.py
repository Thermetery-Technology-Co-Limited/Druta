# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Controller selection and verification ownership, using no driver or GUI."""
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from druta import profiles
from druta.druta import Druta


def candidate(port=1, address=0x20):
    return SimpleNamespace(
        p=SimpleNamespace(regulator="MPS MP2888A", name="candidate", port=port,
                          rail="NVVDD", src={"bus": {"port": port, "addr7": address}}),
        addr7=address, present=Mock(return_value=True), requires_verification=True,
        reset=Mock(return_value=(True, "reset to stock")),
        verify=Mock(return_value=(True, "restored", [])))


class CandidateUi(unittest.TestCase):
    def setUp(self):
        self.app = Druta.__new__(Druta)
        self.app.gpu = SimpleNamespace(nvapi=object(), arch=lambda: 4,
                                       read_vcore_mv=Mock())
        self.first, self.second = candidate(), candidate(3, 0x22)
        self.app.rail = self.first
        self.app._rail_candidates = [self.first, self.second]
        self.app._gpu_gen = 4
        self.app._i2c_busy = False
        self.app._profile_pending = None
        self.app._profile_applying = False
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.app._i2c_recovery_for = None
        self.app.log = Mock()
        self.app.refresh_i2c_candidates = Mock()

    def test_discovery_does_not_choose_an_ambiguous_bus_map(self):
        with patch("druta.railctl.discover", return_value=[self.first, self.second]):
            self.app.find_rail()
        self.assertIsNone(self.app.rail)
        self.assertFalse(self.app.i2c_verified())
        self.assertEqual(self.app._rail_candidates, [self.first, self.second])

    def test_unambiguous_discovery_selects_but_does_not_verify(self):
        with patch("druta.railctl.discover", return_value=[self.second]):
            self.app.find_rail()
        self.assertIs(self.app.rail, self.second)
        self.assertFalse(self.app.i2c_verified())

    def test_selection_invalidates_verification_and_preserves_other_edits(self):
        edits = self.app.edited = {7: 24000}
        self.app.build_ui = Mock()
        self.assertTrue(self.app.select_i2c_candidate(
            app_data=self.app.i2c_candidate_label(self.second)))
        self.assertIs(self.app.rail, self.second)
        self.assertFalse(self.app.i2c_verified())
        self.assertIs(self.app.edited, edits)
        self.app.build_ui.assert_not_called()
        self.app.refresh_i2c_candidates.assert_called_once()

    def test_selection_and_scan_refuse_while_bus_or_profile_owned(self):
        for field, value in (("_i2c_busy", True), ("_profile_pending", ("p",)),
                             ("_profile_applying", True)):
            with self.subTest(field=field), patch("druta.druta.dpg.does_item_exist", return_value=True), \
                    patch("druta.druta.dpg.set_value") as set_value, \
                    patch("druta.railctl.discover") as scan:
                setattr(self.app, field, value)
                self.assertFalse(self.app.select_i2c_candidate(
                    app_data=self.app.i2c_candidate_label(self.second)))
                self.assertFalse(self.app.rescan_i2c())
                self.assertIs(self.app.rail, self.first)
                self.assertTrue(self.app.i2c_verified())
                set_value.assert_called_with("i2c_candidate", self.app.i2c_candidate_label(self.first))
                scan.assert_not_called()
                setattr(self.app, field, None if field == "_profile_pending" else False)

    def test_verification_is_bound_to_gpu_generation_port_and_recipe(self):
        for mutate in (lambda: setattr(self.app, "gpu", object()),
                       lambda: setattr(self.app, "_gpu_gen", 5),
                       lambda: setattr(self.app, "rail", self.second),
                       lambda: self.first.p.src.update(offset={"scale": 12.5})):
            self.setUp()
            self.assertTrue(self.app.i2c_verified())
            mutate()
            self.assertFalse(self.app.i2c_verified())

    def test_disappearing_controller_clears_a_previously_valid_pass(self):
        self.first.present.return_value = False
        with patch("druta.druta.dpg.does_item_exist", return_value=True), \
                patch("druta.druta.dpg.get_value", return_value=True):
            self.assertFalse(self.app.i2c_gate()[0])
        self.assertFalse(self.app.i2c_verified())

    def test_reset_all_does_not_interrupt_manual_verification(self):
        self.app._i2c_busy = True
        self.app._reset_armed = True
        self.app.autosave_before = Mock()
        self.app.gpu.reset_all = Mock(side_effect=AssertionError("reset interrupted Verify"))
        self.app.reset_all()
        self.app.autosave_before.assert_not_called()
        self.app.gpu.reset_all.assert_not_called()

    def test_reset_refuses_untouched_mp_candidate_without_writing(self):
        self.app.invalidate_i2c_verification()
        ok, message = self.app.reset_i2c_rail()
        self.assertFalse(ok)
        self.assertIn("no reset write", message)
        self.first.reset.assert_not_called()

    def test_verified_connection_can_reset(self):
        self.assertTrue(self.app.reset_i2c_rail()[0])
        self.first.reset.assert_called_once_with()

    def test_failed_verification_can_recover_only_the_same_connection(self):
        for change in (None, "gpu", "rail", "generation", "recipe"):
            with self.subTest(change=change):
                self.setUp()
                self.app.invalidate_i2c_verification()
                self.app._i2c_recovery_for = self.app.i2c_connection()
                if change == "gpu":
                    self.app.gpu = object()
                elif change == "rail":
                    self.app.rail = self.second
                elif change == "generation":
                    self.app._gpu_gen += 1
                elif change == "recipe":
                    self.first.p.src.update(offset={"scale": 12.5})
                self.assertEqual(self.app.reset_i2c_rail()[0], change is None)
                self.assertEqual(self.first.reset.call_count, 1 if change is None else 0)
                self.second.reset.assert_not_called()

    def test_busy_reset_refuses_even_a_verified_or_recoverable_connection(self):
        self.app._i2c_busy = True
        self.app._i2c_recovery_for = self.app.i2c_connection()
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.first.reset.assert_not_called()

    def test_selecting_a_controller_clears_recovery_authorization(self):
        self.app._i2c_recovery_for = self.app.i2c_connection()
        self.app.select_i2c_candidate(app_data=self.app.i2c_candidate_label(self.second))
        self.assertIsNone(self.app._i2c_recovery_for)
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.second.reset.assert_not_called()

    def test_rescan_clears_recovery_even_when_it_returns_the_same_controller(self):
        self.app._i2c_recovery_for = self.app.i2c_connection()
        with patch("druta.railctl.discover", return_value=[self.first]):
            self.assertTrue(self.app.rescan_i2c())
        self.assertIsNone(self.app._i2c_recovery_for)
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.first.reset.assert_not_called()

    def test_worker_success_requires_same_connection_and_clean_load(self):
        for change, error, expected in ((False, "", True), (True, "", False),
                                        (False, "copy mismatch", False)):
            with self.subTest(change=change, error=error):
                self.setUp()
                self.app.invalidate_i2c_verification()
                self.app._i2c_busy = True
                def induce(gpu, **kwargs):
                    result = kwargs["on_settled"]()
                    if change:
                        self.app.rail = self.second
                    return {"result": result, "error": error}
                with patch("druta.gpuload.induce", side_effect=induce):
                    self.app._i2c_verify_worker()
                self.assertEqual(self.app.i2c_verified(), expected)
                self.assertFalse(self.app._i2c_busy)
                self.first.verify.assert_called_once()
                self.second.verify.assert_not_called()

    def test_load_failure_before_callback_does_not_authorize_recovery(self):
        self.app._i2c_busy = True
        # A stale flag from an earlier run must not count as this run's write.
        self.first._verification_write_attempted = True
        with patch("druta.gpuload.induce", return_value={"error": "CUDA unavailable"}):
            self.app._i2c_verify_worker()
        self.first.verify.assert_not_called()
        self.assertIsNone(self.app._i2c_recovery_for)
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.first.reset.assert_not_called()

    def test_precondition_refusal_before_write_does_not_authorize_recovery(self):
        self.app._i2c_busy = True
        self.first.verify.return_value = (False, "headroom unavailable", [])
        def induce(gpu, **kw):
            return {"result": kw["on_settled"]()}
        with patch("druta.gpuload.induce", side_effect=induce):
            self.app._i2c_verify_worker()
        self.first.verify.assert_called_once()
        self.assertIsNone(self.app._i2c_recovery_for)
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.first.reset.assert_not_called()

    def test_attempted_write_and_failed_restore_allow_only_bound_recovery(self):
        self.app._i2c_busy = True
        def failed_verify(**kw):
            self.first._verification_write_attempted = True
            return False, "restoration failed", []
        self.first.verify.side_effect = failed_verify
        def induce(gpu, **kw):
            return {"result": kw["on_settled"]()}
        with patch("druta.gpuload.induce", side_effect=induce):
            self.app._i2c_verify_worker()
        self.assertFalse(self.app.i2c_verified())
        self.assertEqual(self.app._i2c_recovery_for, self.app.i2c_connection())
        self.assertTrue(self.app.reset_i2c_rail()[0])
        self.app.rail = self.second
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.second.reset.assert_not_called()

    def test_failed_load_preserves_recovery_from_a_previous_write_attempt(self):
        token = self.app._i2c_recovery_for = self.app.i2c_connection()
        self.app._i2c_busy = True
        with patch("druta.gpuload.induce", side_effect=RuntimeError("CUDA unavailable")):
            self.app._i2c_verify_worker()
        self.assertEqual(self.app._i2c_recovery_for, token)
        self.assertFalse(self.app.i2c_verified())
        self.assertTrue(self.app.reset_i2c_rail()[0])

    def test_saved_identity_resolves_the_unique_second_candidate(self):
        state = {"i2c": dict(profiles.rail_identity(self.second), offset_mv=12.5)}
        self.assertIs(self.app.rail_for_profile(state), self.second)
        self.assertIs(self.app.rail, self.first)  # Resolution itself is read-only.

    def test_saved_identity_does_not_resolve_duplicate_candidates(self):
        self.app._rail_candidates.append(candidate(3, 0x22))
        state = {"i2c": profiles.rail_identity(self.second)}
        self.assertIsNone(self.app.rail_for_profile(state))

    def test_saved_identity_rejects_a_changed_register_recipe(self):
        state = {"i2c": profiles.rail_identity(self.second)}
        self.second.p.src.update(offset={"scale": 12.5})
        self.assertIsNone(self.app.rail_for_profile(state))

    def test_profile_without_i2c_preserves_current_selection(self):
        for state in ({}, {"i2c": None}, {"i2c": {}}):
            with self.subTest(state=state):
                self.assertIs(self.app.rail_for_profile(state), self.first)

    def test_profile_load_selects_saved_candidate_and_requires_fresh_verification(self):
        state = {"i2c": profiles.rail_identity(self.second)}
        self.app.guard = Mock(return_value=True)
        self.app.autosave_before = Mock(return_value=True)
        self.app.sync_lock_ui = Mock()
        self.app.finish_profile_load = Mock()
        self.app.verify_i2c_rail = Mock(side_effect=lambda: setattr(self.app, "_i2c_busy", True))
        self.app._i2c_recovery_for = self.app.i2c_connection()
        with patch("druta.profiles.preflight", return_value=None) as preflight, \
                patch("druta.profiles.summarize", return_value="I2C offset"), \
                patch("druta.druta.dpg.configure_item"):
            self.app.begin_profile_load("saved", state)
        preflight.assert_called_once_with(self.app.gpu, state, self.second)
        self.assertIs(self.app.rail, self.second)
        self.assertIsNone(self.app._i2c_recovery_for)
        self.assertFalse(self.app.i2c_verified())
        self.assertEqual(self.app._profile_pending, ("saved", state, False))
        self.app.verify_i2c_rail.assert_called_once()
        self.app.finish_profile_load.assert_not_called()

    def test_refresh_rebuilds_only_regulator_controls(self):
        self.app._ctl_widgets = ["sl_core", "sl_i2crail", "go_i2crail_x", "sl_mem"]
        self.app._slider_ranges = {"core": object(), "i2crail": object()}
        self.app._knob_cb = {"core": object(), "i2crail": object()}
        self.app.build_i2c_candidates = Mock()
        self.app.sync_lock_ui = Mock()
        self.app.sync_risk_ui = Mock()
        self.app.relayout = Mock()
        with patch("druta.druta.dpg.does_item_exist", return_value=True), \
                patch("druta.druta.dpg.delete_item") as delete, \
                patch("druta.druta.dpg.group", side_effect=lambda **kw: nullcontext()):
            Druta.refresh_i2c_candidates(self.app)
        self.assertEqual(self.app._ctl_widgets, ["sl_core", "sl_mem"])
        self.assertEqual(set(self.app._slider_ranges), {"core"})
        self.assertEqual(set(self.app._knob_cb), {"core"})
        delete.assert_called_once_with("i2c_candidates", children_only=True)


if __name__ == "__main__":
    unittest.main()
