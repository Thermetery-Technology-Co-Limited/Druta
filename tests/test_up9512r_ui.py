# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Five-state offset presentation and callbacks, without a GUI or hardware."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import druta
from druta.druta import Druta
from tests.test_arch_ui_regressions import FakeUiTest


class UP9512RUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        app = self.app
        self.telemetry = dict(offsets_mv=[0, 10, 20, 30, 40], offset_mv=None,
                              offset_enabled=True, write_locked=False,
                              vout_mv=920.0, imon_mv=350.0)
        self.control = dict(kind="up9512r-offset-v1",
                            offsets_mv=[0, 10, 20, 30, 40], enabled=True)
        app.rail = self.rail = SimpleNamespace(
            multi_state_offset=True, requires_verification=True, xoc=False,
            p=SimpleNamespace(regulator="uPI uP9512R", name="controller at selected route",
                              rail="Controller feedback", port=5, addr7=0x32,
                              read_only=False, env_min=0, env_max=50,
                              hw_min_mv=0, hw_max_mv=150, lsb_mv=10,
                              rungs=(10, 20), src={"bus": {"port": 5, "addr7": 0x32}}),
            addr7=0x32, present=Mock(return_value=True),
            telemetry=Mock(side_effect=lambda: dict(self.telemetry)),
            plan=Mock(return_value=(True, "five-state offset plan")),
            capture_control=Mock(return_value=self.control),
            validate_control=Mock(return_value=True),
            set_offset_mv=Mock(return_value=(True, "offset applied")),
            reset=Mock(return_value=(True, "offsets cleared")),
            verify=Mock(), _verification_restore_ok=True,
            discovery_telemetry=self.telemetry)
        app.gpu = SimpleNamespace(read_rail_offset_mv=lambda *_: None,
                                  read_clk_domain_offsets=lambda: ({}, ""))
        app._i2c_busy = app._i2c_scan_busy = False
        app._i2c_discovery_complete = True
        app._i2c_status_built = True
        app._profile_pending = app._profile_applying = None
        app._i2c_recovery_for = None
        app._i2c_verified = True
        app._i2c_verified_for = app.i2c_connection()
        app.guard = Mock(return_value=True)
        app.bind = Mock()
        app.s = lambda value: value
        app._carryover_hi = {}
        app._xoc_bounds = app._knob_sync = False
        app._knob_cb = {}
        app._ctl_widgets = []
        self.values.update(i2c_mode=True, i2c_write_status="", i2c_offset_state="",
                           live_i2crail="", sl_i2crail=70, in_i2crail=70)

    def test_row_names_compound_request_and_preserves_positive_controller_bounds(self):
        self.app.slider_row = Mock()
        self.app.build_i2c_rail_row()
        args = self.app.slider_row.call_args
        self.assertEqual(args.args[:4], ("i2crail", "Offset (all load states) (mV)", 0, 50))
        self.assertEqual((args.kwargs["xoc_lo"], args.kwargs["xoc_hi"]), (0, 150))
        self.assertEqual([label for label, _ in args.kwargs["extra"]], ["Verify", "Reset"])
        self.assertIn("mixed", self.values["i2c_offset_state"])
        self.assertIn("LCS4 +40 mV", self.values["i2c_offset_state"])
        self.assertIn("10 mV steps", self.app.i2c_verify_description())

    def test_common_disabled_and_unknown_states_are_not_reported_as_applied_zero(self):
        text = Druta.i2c_offset_state_text(dict(offsets_mv=[20] * 5, offset_enabled=False))
        self.assertIn("disabled", text)
        self.assertIn("all load states +20 mV", text)
        for offsets in (None, [], [0] * 4, [0, 0, 0, 0, float("nan")]):
            text = Druta.i2c_offset_state_text(dict(offsets_mv=offsets))
            self.assertIn("unavailable", text)
            self.assertNotIn("+0 mV", text)

    def test_feedback_and_imon_remain_input_voltages_without_gpu_comparison_or_amps(self):
        self.assertEqual(self.app.i2c_rail_text(1200), "FB 920 mV; IMON 350 mV")
        self.telemetry.pop("imon_mv")
        self.assertEqual(self.app.i2c_rail_text(1200), "FB 920 mV")
        self.assertTrue(self.app.i2c_gate()[0])

    def test_transient_telemetry_failure_shows_unknown_without_removing_control(self):
        self.rail.telemetry.side_effect = OSError("bus busy")
        self.assertIsNone(self.app.i2c_rail_text(None))
        self.assertIn("unavailable", self.values["i2c_offset_state"])
        self.assertIn("writes blocked", self.values["i2c_write_status"])
        self.assertEqual(self.values["sl_i2crail"], 70)
        self.rail.telemetry.side_effect = lambda: dict(self.telemetry)
        self.assertEqual(self.app.i2c_rail_text(None), "FB 920 mV; IMON 350 mV")
        self.assertTrue(self.app.i2c_gate()[0])
        self.assertFalse(self.app.i2c_verified())

    def test_empty_telemetry_clears_verification_until_a_fresh_check(self):
        self.telemetry.clear()
        self.assertIsNone(self.app.i2c_rail_text(None))
        self.assertFalse(self.app.i2c_verified())
        self.assertIn("writes blocked", self.values["i2c_write_status"])

    def test_locked_controller_is_readable_but_apply_and_verify_do_not_write(self):
        self.telemetry["write_locked"] = True
        self.assertIn("FB 920", self.app.i2c_rail_text(None))
        self.assertIn("locked", self.values["i2c_write_status"])
        self.app.apply_i2c_rail(20)
        with patch.object(druta.threading, "Thread") as worker:
            self.app.verify_i2c_rail()
        worker.assert_not_called()
        self.rail.plan.assert_not_called()
        self.rail.verify.assert_not_called()
        self.rail.set_offset_mv.assert_not_called()
        self.assertIn("does not issue an unlock", self.app.log.call_args.args[0])

    def test_locked_reset_and_unavailable_lock_status_refuse_without_capture(self):
        self.telemetry["write_locked"] = True
        result = self.app.reset_i2c_rail()
        self.assertFalse(result[0])
        self.assertIn("locked", result[1])
        self.rail.capture_control.assert_not_called()
        self.rail.reset.assert_not_called()
        self.telemetry.pop("write_locked")
        self.assertFalse(self.app.i2c_gate()[0])
        self.assertIn("unavailable", self.app.i2c_gate()[1])

    def test_opt_in_and_app_guard_refuse_before_bus_reads(self):
        self.values["i2c_mode"] = False
        self.app.apply_i2c_rail(20)
        self.rail.present.assert_not_called()
        self.rail.telemetry.assert_not_called()
        self.values["i2c_mode"] = True
        self.app.guard.return_value = False
        self.app.apply_i2c_rail(20)
        self.rail.present.assert_not_called()
        self.rail.set_offset_mv.assert_not_called()

    def test_apply_requires_current_connection_verification(self):
        self.app._gpu_gen += 1
        self.app.apply_i2c_rail(20)
        self.rail.plan.assert_not_called()
        self.rail.set_offset_mv.assert_not_called()
        self.assertIn("Verify first", self.app.log.call_args.args[0])

    def test_apply_uses_ten_mv_grid_and_captures_all_states_before_transaction(self):
        self.app.apply_i2c_rail(26)
        self.rail.plan.assert_called_once_with(30)
        self.rail.validate_control.assert_called_once_with(self.control, xoc=True)
        self.rail.set_offset_mv.assert_called_once_with(30, acknowledged=True, expected=self.control)
        self.app.autosave_before.assert_called_once_with("i2c-rail-offset")

    def test_failed_plan_or_capture_never_reaches_setter(self):
        self.rail.plan.return_value = (False, "outside envelope")
        self.app.apply_i2c_rail(150)
        self.rail.capture_control.assert_not_called()
        self.rail.set_offset_mv.assert_not_called()
        self.rail.plan.return_value = (True, "plan")
        self.rail.capture_control.side_effect = OSError("LCS4 unavailable")
        self.app.apply_i2c_rail(20)
        self.rail.set_offset_mv.assert_not_called()
        self.app.autosave_before.assert_not_called()
        self.assertIn("nothing written", self.app.log.call_args.args[0])

    def test_reset_requires_verified_or_tried_connection_and_passes_captured_state(self):
        self.app.invalidate_i2c_verification()
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.rail.reset.assert_not_called()
        self.app._i2c_recovery_for = self.app.i2c_connection()
        self.assertTrue(self.app.reset_i2c_rail()[0])
        self.rail.reset.assert_called_once_with(expected=self.control)
        self.app.autosave_before.assert_called_once_with("i2c-load-state-offset-reset")

    def test_release_only_controller_resets_without_a_prior_verify(self):
        self.rail.reset_only_lowers = True
        self.app.invalidate_i2c_verification()
        self.assertTrue(self.app.reset_i2c_rail()[0])
        self.rail.reset.assert_called_once_with(expected=self.control)
        self.app.autosave_before.assert_called_once_with("i2c-load-state-offset-reset")
        # The release still needs the opt-in and an identified, unlocked controller.
        self.rail.reset.reset_mock()
        self.telemetry["write_locked"] = True
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.telemetry["write_locked"] = False
        self.values["i2c_mode"] = False
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.rail.reset.assert_not_called()

    def test_fresh_session_at_the_normal_maximum_can_reset_on_the_real_controller(self):
        from tests.test_up9512r_profiles import setup

        _, rail, registers, writes, _, _ = setup()
        registers.update({0x0A: 0x55, 0x0B: 0x55, 0x0C: 0x5A, 0x2A: 0x60})
        self.app.rail = rail
        self.app.invalidate_i2c_verification()
        ok, message, _ = rail.verify(acknowledged=True,
                                     operating_point=lambda: (0, 2865, 1438))
        self.assertFalse(ok)
        self.assertIn("nothing written", message)
        self.assertFalse(writes)
        ok, message = self.app.reset_i2c_rail()
        self.assertTrue(ok, message)
        self.assertEqual(rail.capture_control(),
                         {"kind": "up9512r-offset-v1", "offsets_mv": [0] * 5, "enabled": False})
        self.assertEqual(writes[0], (0x2A, 0x20, 1), "disable is written first")
        self.assertEqual(registers[0x0C] & 0x0F, 0x0A, "preserved low nibble")

    def reset_all_order(self, *, complete):
        order = []
        app = self.app
        app.autosave_before.side_effect = (
            lambda action, **_: order.append("autosave:" + action) or complete)
        self.rail.reset.side_effect = lambda **_: order.append("rail.reset") or (True, "cleared")
        app._reset_armed, app._clk_lock = True, None
        app.restore_headroom_before_unpin = Mock(return_value=True)
        app.gpu.reset_all = Mock(side_effect=lambda: order.append("gpu.reset_all") or [])
        for name in ("sync_sliders_from_gpu", "sync_profile_rail_sliders", "sync_knob_boxes",
                     "refresh_volt_limits", "refresh_current_limits", "vf_read"):
            setattr(app, name, Mock())
        app.reset_all()
        self.assertEqual(self.values["sl_i2crail"], 0)
        return order

    def test_reset_all_takes_its_one_undo_point_before_any_write(self):
        self.assertEqual(self.reset_all_order(complete=True),
                         ["autosave:reset-all", "gpu.reset_all", "rail.reset"])

    def test_incomplete_reset_all_snapshot_gets_the_controller_reset_undo_point(self):
        self.assertEqual(self.reset_all_order(complete=False),
                         ["autosave:reset-all", "gpu.reset_all",
                          "autosave:i2c-load-state-offset-reset", "rail.reset"])

    def test_release_only_bypass_needs_the_gated_load_state_path(self):
        self.rail.reset_only_lowers = True
        self.rail.multi_state_offset = False
        self.app.invalidate_i2c_verification()
        result = self.app.reset_i2c_rail()
        self.assertFalse(result[0])
        self.assertIn("not been verified", result[1])
        self.rail.reset.assert_not_called()

    def test_one_failed_identity_read_keeps_the_gate_and_verification(self):
        from tests.test_up9512r_profiles import setup

        _, rail, registers, _, _, _ = setup()
        self.app.rail = rail
        self.app._i2c_verified_for = self.app.i2c_connection()
        reads = []
        real = rail.read.side_effect

        def read(reg, width=1):
            reads.append(reg)
            if reg == 0x27 and reads.count(0x27) == 1:
                return None
            return real(reg, width)
        rail.read.side_effect = read
        self.assertEqual(self.app.i2c_gate(), (True, ""))
        self.assertTrue(self.app.i2c_verified())

    def test_confirmed_reset_or_verify_clears_the_uncertain_status_line(self):
        self.app._i2c_restore_failed = True
        self.app._i2c_offset_restore_failure = (self.rail, "LCS2 restore not confirmed")
        self.app.i2c_rail_text(None)
        self.assertIn("state is uncertain", self.values["i2c_write_status"])
        self.assertTrue(self.app.reset_i2c_rail()[0])
        self.app.i2c_rail_text(None)
        self.assertNotIn("uncertain", self.values["i2c_write_status"])
        self.assertTrue(self.app._i2c_restore_failed, "the session stays unclean")

        self.app._i2c_offset_restore_failure = (self.rail, "LCS2 restore not confirmed")
        self.app.gpu.read_vcore_mv = Mock(return_value=900)
        with patch.object(druta.gpuload, "induce", return_value={"result": 900}), \
                patch.object(druta.gpuload, "verify_in_p0",
                             return_value=(True, "WRITE PATH CONFIRMED", [])):
            self.app._i2c_verify_worker()
        self.assertTrue(self.app.i2c_verified())
        self.app.i2c_rail_text(None)
        self.assertNotIn("uncertain", self.values["i2c_write_status"])

    def test_failed_reset_keeps_the_uncertain_status_line(self):
        self.app._i2c_offset_restore_failure = (self.rail, "LCS2 restore not confirmed")
        self.rail.reset.return_value = (False, "0x0A readback mismatch")
        self.rail._verification_restore_ok = False
        self.assertFalse(self.app.reset_i2c_rail()[0])
        self.app.i2c_rail_text(None)
        self.assertIn("state is uncertain", self.values["i2c_write_status"])

    def test_voltage_only_verify_needs_no_cuda_but_offset_rails_still_do(self):
        from tests.test_up9512r_profiles import setup

        unavailable = (False, "nvcuda.dll could not be loaded")
        self.app._tim_busy = False
        self.app.sync_lock_ui = Mock()
        with patch.object(druta.gpuload, "available", return_value=unavailable) as cuda, \
                patch.object(druta.threading, "Thread") as worker:
            self.app.verify_i2c_rail()  # The mock offset rail is not voltage-only.
            worker.assert_not_called()
            self.assertIn("nvcuda", self.app.log.call_args.args[0])
            self.app.rail = setup()[1]
            self.app.verify_i2c_rail()
            worker.assert_called_once()
            self.assertEqual(cuda.call_count, 1)
        self.app._i2c_busy = False

    def test_profile_whose_load_state_offsets_already_match_needs_no_verify(self):
        from tests.test_up9512r_profiles import setup

        _, rail, registers, writes, _, state = setup()
        app = self.app
        app.gpu, app.rail = setup()[0], rail
        app.invalidate_i2c_verification()
        app.rail_for_profile = Mock(return_value=rail)
        app.verify_i2c_rail = Mock()
        app.finish_profile_load = Mock()
        app.sync_risk_ui = Mock()
        registers[0x39] = 0x87  # Locked: the matching entry still needs no write.
        app.begin_profile_load("saved offsets", state)
        app.verify_i2c_rail.assert_not_called()
        app.finish_profile_load.assert_called_once()
        self.assertFalse(writes)
        app.finish_profile_load.reset_mock()
        registers.update({0x39: 0x94, 0x0A: 0})
        app.begin_profile_load("saved offsets", state)
        app.verify_i2c_rail.assert_called_once()

    def test_reset_capture_failure_is_a_refusal(self):
        self.rail.validate_control.side_effect = ValueError("entry state unreadable")
        result = self.app.reset_i2c_rail()
        self.assertFalse(result[0])
        self.assertIn("capture failed", result[1])
        self.rail.reset.assert_not_called()

    def test_reset_can_capture_entry_above_normal_envelope_after_xoc_is_unticked(self):
        self.control["offsets_mv"] = [150] * 5
        def validate(control, *, xoc):
            if not xoc:
                raise ValueError("entry exceeds the normal request envelope")
            return True
        self.rail.validate_control.side_effect = validate
        self.assertFalse(self.rail.xoc)
        self.assertTrue(self.app.reset_i2c_rail()[0])
        self.rail.reset.assert_called_once_with(expected=self.control)

    def test_readback_after_profile_restore_keeps_mixed_state_explicit(self):
        self.app.sync_profile_rail_sliders()
        self.assertEqual(self.values["sl_i2crail"], 70)  # Still a staged request.
        self.assertIn("mixed", self.values["i2c_offset_state"])
        self.telemetry.update(offset_mv=20, offsets_mv=[20] * 5)
        self.app.sync_profile_rail_sliders()
        self.assertEqual(self.values["sl_i2crail"], 20)
        self.assertEqual(self.values["in_i2crail"], 20)
        self.assertIn("all load states +20", self.values["i2c_offset_state"])

    def test_failed_rollback_remains_visible_after_readback_refresh(self):
        self.rail._verification_restore_ok = False
        self.rail._verification_restore_error = "LCS2 restore not confirmed"
        self.rail.set_offset_mv.return_value = (False, "write failed")
        self.app.apply_i2c_rail(20)
        self.assertTrue(self.app._i2c_restore_failed)
        self.assertFalse(self.app.i2c_verified())
        self.app.i2c_rail_text(None)
        self.assertIn("state is uncertain", self.values["i2c_write_status"])
        self.assertIn("LCS2", self.values["i2c_write_status"])

    def test_profile_replay_failed_rollback_invalidates_writes_and_clean_shutdown(self):
        from tests.test_up9512r_profiles import setup

        gpu, rail, registers, writes, gpu_writes, state = setup()
        app = self.app
        app.gpu, app.rail = gpu, rail
        app._i2c_verified_for = app.i2c_connection()
        registers.update({0x0A: 0, 0x0B: 0, 0x0C: 0x0A, 0x2A: 0x20})

        def uncertain_write(reg, value, width=1):
            writes.append((reg, value, width))
            if value:
                registers[reg] = value
            return False  # Initial write lands; restoring zero fails.

        rail._raw_write.side_effect = uncertain_write
        for name in ("sync_sliders_from_gpu", "apply_profile_policy_names",
                     "refresh_volt_limits", "vf_read", "sync_hold_headroom",
                     "refresh_profile_list"):
            setattr(app, name, Mock())

        app.finish_profile_load("saved offsets", state)

        self.assertEqual(writes, [(0x0A, 0x12, 1), (0x0A, 0, 1)])
        self.assertEqual(registers[0x0A], 0x12)
        self.assertFalse(gpu_writes)
        self.assertFalse(rail._verification_restore_ok)
        self.assertFalse(app.i2c_verified())
        self.assertTrue(app._i2c_restore_failed)
        self.assertFalse(app.shutdown_is_clean())
        app.i2c_rail_text(None)  # Readable/unlocked telemetry cannot erase the failure.
        self.assertIn("state is uncertain", self.values["i2c_write_status"])
        self.assertIn("complete entry control not restored", self.values["i2c_write_status"])
        writes_before = list(writes)
        app.apply_i2c_rail(20)
        self.assertEqual(writes, writes_before)

    def test_verifier_failed_restoration_remains_visible_after_status_refresh(self):
        self.app.gpu.read_vcore_mv = Mock(return_value=900)
        def verify(*args, **kwargs):
            self.rail._verification_write_attempted = True
            self.rail._verification_restore_ok = False
            self.rail._verification_restore_error = "enable bit restore not confirmed"
            return False, "RESTORE FAILED", []
        with patch.object(druta.gpuload, "induce", return_value={"result": 900}), \
                patch.object(druta.gpuload, "verify_in_p0", side_effect=verify):
            self.app._i2c_verify_worker()
        self.app.i2c_rail_text(None)
        self.assertTrue(self.app._i2c_restore_failed)
        self.assertFalse(self.app.i2c_verified())
        self.assertIn("enable bit restore", self.values["i2c_write_status"])
        self.assertIn("state is uncertain", self.values["i2c_write_status"])

    def test_named_controller_scope_uses_dynamic_catalog_and_explicit_scan(self):
        self.app._i2c_controller_choice = "Unknown -- Full Scan"
        self.app.start_i2c_discovery = Mock(return_value=True)
        with patch.object(druta.railctl, "controller_names", return_value=["uPI uP9512R"]):
            self.app.select_i2c_controller(app_data="uPI uP9512R")
        self.app.start_i2c_discovery.assert_not_called()
        self.assertTrue(self.app.rescan_i2c())
        self.app.start_i2c_discovery.assert_called_once_with(
            controller="uPI uP9512R", prefer_saved=True)

    def test_candidate_metadata_uses_feedback_imon_and_leaves_selection_explicit(self):
        self.telemetry.update(write_locked=True, iout_a=999)
        self.app._rail_candidates = [self.rail]
        self.app.rail = None
        self.app.update_i2c_scan_ui = Mock()
        self.app.build_i2c_candidates()
        text = "\n".join(str(call.args[0]) for call in self.ui.add_text.call_args_list)
        self.assertIn("port 5, 0x32", text)
        self.assertIn("920.0 mV FB", text)
        self.assertIn("350.0 mV IMON", text)
        self.assertIn("locked / read-only", text)
        self.assertNotIn("999", text)
        self.assertIsNone(self.app.rail)
