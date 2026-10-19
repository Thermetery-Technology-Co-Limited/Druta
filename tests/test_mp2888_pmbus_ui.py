# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-only PMBus control owns its command and restores before unpinning."""
import math
import threading
try:
    import tomllib
except ImportError:  # Python 3.8 is the Windows 7 runtime.
    import tomli as tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import railctl
from druta.controllers.mp2888 import MP2888Candidate
from druta.druta import Druta
from tests.controllers.test_mp2888_pmbus import PMBus
from tests.controllers.test_mp_candidates_ui import candidate
from tests.test_arch_ui_regressions import FakeUiTest


class PmbusUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.events = []
        self.control = {"kind": "mp2888-pmbus-v1", "enabled": False, "command": 560, "gain": 32}
        self.entry = dict(self.control)
        self.clock = None
        self.clock_error = None
        self.point = {"pstate": 8, "core": 405, "mem": 810}
        self.app.gpu = SimpleNamespace(
            static={"gfx_min": 405},
            read_vf_lock_status=Mock(return_value=(None, None)),
            read_clk_lock_status=Mock(side_effect=lambda: (self.clock, self.clock_error)),
            lock_gpu_clocks=Mock(side_effect=self.lock_clock),
            last_clock_lock_request=lambda: self.clock,
            reset_gpu_clocks=Mock(side_effect=self.reset_clock),
            read=Mock(side_effect=lambda: dict(self.point)),
            vf_lock_recovery_pending=lambda: False,
            legacy_p0_owned=lambda: False)
        self.app.rail = SimpleNamespace(
            p=SimpleNamespace(regulator="MPS MP2888A", name="candidate", rail="Controller output",
                              port=1, src={"control": "mp2888-pmbus-v1"}, read_only=False,
                              env_min=300, env_max=1200, hw_min_mv=300, hw_max_mv=2000),
            addr7=0x20, requires_verification=True, absolute_voltage=True,
            requires_voltage_clamp=True, session_only_voltage=True, controller_reported_vout=True,
            capture_control=Mock(side_effect=lambda: dict(self.control)),
            validate_control=Mock(),
            normalize_voltage_mv=lambda mv: math.floor(mv * 512 / 1000) * 1000 / 512,
            plan=Mock(return_value=(True, "nominal request")),
            set_voltage_mv=Mock(side_effect=self.set_voltage),
            restore_control=Mock(side_effect=self.restore_control),
            reset=Mock(side_effect=self.auto),
            telemetry=Mock(side_effect=lambda: {"vout_mv": 1066,
                                                "target_mv": 1100 if self.control["enabled"] else None}),
            present=Mock(return_value=True), xoc=False,
            _verification_restore_ok=True, _verification_write_attempted=False)
        self.app._clk_lock = None
        self.app._i2c_busy = False
        self.app._profile_pending = None
        self.app._i2c_restore_failed = False
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.app.guard = Mock(return_value=True)
        self.app.i2c_gate = Mock(return_value=(True, ""))
        self.app.sync_profile_rail_sliders = Mock()
        self.app.sync_lock_ui = Mock()
        self.app.set_lock_state = Mock(side_effect=self.set_lock_state)
        self.app.autosave_before.side_effect = lambda *_args, **_kwargs: self.events.append("autosave")
        sleeper = patch("druta.druta.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def set_lock_state(self, state):
        self.app._clk_lock = state

    def lock_clock(self, low, high):
        self.events.append("clamp")
        self.clock = low, high
        return True, "clock request accepted"

    def reset_clock(self):
        self.events.append("release")
        self.clock = None
        return True, "clock request released"

    def set_voltage(self, value, *, acknowledged, expected):
        self.assertTrue(acknowledged)
        self.assertEqual(self.control, expected)
        self.assertEqual(self.clock, (405, 405))
        self.events.append("voltage")
        self.control.update(enabled=True, command=round(value * 512 / 1000))
        self.app.rail.last_written_control = dict(self.control)
        return True, "nominal request confirmed"

    def restore_control(self, state, *, expected, recovery):
        self.assertTrue(recovery)
        self.events.append("restore")
        if self.control != expected:
            return False, "controller changed externally"
        self.control = dict(state)
        return True, "entry restored"

    def auto(self, *, expected):
        self.events.append("auto")
        if self.control != expected:
            return False, "controller changed externally"
        self.control["enabled"] = False
        return True, "GPU VID restored"

    def apply(self, voltage=1100):
        self.app.apply_i2c_rail(voltage)

    def test_apply_uses_adapter_grid_after_autosave_and_observed_clock_settling(self):
        points = [dict(self.point, core=1500)] + [dict(self.point)] * 5
        self.app.gpu.read.side_effect = points
        self.apply()
        self.assertEqual(self.events, ["autosave", "clamp", "voltage"])
        self.app.rail.set_voltage_mv.assert_called_once_with(
            1099.609375, acknowledged=True, expected=self.entry)
        self.assertEqual(self.app.gpu.read.call_count, 6)
        self.assertEqual(self.app._i2c_manual_voltage["entry"], self.entry)

    def test_second_apply_preserves_original_entry_and_compares_last_command(self):
        self.apply()
        first = dict(self.control)
        self.apply(1000)
        self.assertEqual(self.app._i2c_manual_voltage["entry"], self.entry)
        self.assertEqual(self.app.rail.set_voltage_mv.call_args.kwargs["expected"], first)
        self.assertNotIn("release", self.events)
        self.assertNotIn("restore", self.events)

    def test_missing_unrelated_memory_and_pstate_telemetry_does_not_block_apply(self):
        self.point.update(pstate=None, mem=None)
        self.apply()
        self.app.rail.set_voltage_mv.assert_called_once()

    def test_captured_initial_manual_control_is_restored_exactly_on_release(self):
        self.control.update(enabled=True, command=590)
        self.entry = dict(self.control)
        self.apply()
        ok, _ = self.app.release_current()
        self.assertTrue(ok)
        self.assertEqual(self.control, self.entry)

    def test_unverified_apply_never_takes_a_clock_or_voltage_request(self):
        self.app.invalidate_i2c_verification()
        self.apply()
        self.assertEqual(self.events, [])

    def test_foreign_locks_and_unreadable_status_are_not_adopted(self):
        cases = (({"volt_uV": 950000}, None, None),
                 (None, (900, 1800), None), (None, None, "clock read failed"))
        for held, frequency, error in cases:
            with self.subTest(held=held, frequency=frequency, error=error):
                self.app.gpu.read_vf_lock_status.return_value = held, None
                self.clock, self.clock_error = frequency, error
                self.apply()
        self.app.gpu.lock_gpu_clocks.assert_not_called()
        self.app.gpu.reset_gpu_clocks.assert_not_called()
        self.app.rail.set_voltage_mv.assert_not_called()

    def test_failed_settling_keeps_clock_without_sending_voltage(self):
        self.point["core"] = 1500
        with patch("druta.druta.time.monotonic", side_effect=range(20)):
            self.apply()
        self.app.rail.set_voltage_mv.assert_not_called()
        self.assertEqual(self.clock, (405, 405))
        self.assertIsNotNone(self.app._i2c_manual_clock_for)
        self.assertIsNone(getattr(self.app, "_i2c_manual_voltage", None))

    def test_auto_can_recover_without_verify_and_restores_vid_before_clock_release(self):
        self.control["enabled"] = True
        self.app.invalidate_i2c_verification()
        ok, _ = self.app.reset_i2c_rail()
        self.assertTrue(ok)
        self.assertEqual(self.events, ["autosave", "clamp", "auto", "release"])
        self.assertFalse(self.control["enabled"])
        self.assertIsNone(self.app._clk_lock)

    def test_auto_does_not_treat_the_old_command_as_a_new_voltage_request(self):
        self.control.update(enabled=True, command=1000)
        self.app.rail.validate_control.side_effect = ValueError("old command is outside the envelope")
        ok, _ = self.app.reset_i2c_rail()
        self.assertTrue(ok)
        self.app.rail.validate_control.assert_not_called()
        self.assertFalse(self.control["enabled"])

    def test_release_restores_exact_entry_before_owned_clock(self):
        self.apply()
        self.events.clear()
        ok, _ = self.app.release_current()
        self.assertTrue(ok)
        self.assertEqual(self.events, ["restore", "release"])
        self.assertEqual(self.control, self.entry)

    def test_restore_does_not_depend_on_gpu_telemetry_or_clock_status(self):
        self.apply()
        self.events.clear()
        self.app.gpu.read.side_effect = RuntimeError("temperature unavailable")
        self.clock_error = "clock status unavailable"
        ok, message = self.app.release_current()
        self.assertFalse(ok)
        self.assertIn("clock status unavailable", message)
        self.assertEqual(self.events, ["restore"])
        self.assertEqual(self.control, self.entry)
        self.assertEqual(self.clock, (405, 405))

    def test_other_clients_replaced_clock_is_not_released(self):
        self.apply()
        self.events.clear()
        self.clock = 900, 1800
        ok, message = self.app._finish_manual_voltage("Auto cleanup")
        self.assertTrue(ok)
        self.assertIn("other client's lock is untouched", message)
        self.assertEqual(self.events, ["restore"])
        self.assertEqual(self.clock, (900, 1800))
        self.assertIsNone(self.app._clk_lock)

    def test_user_adjusted_clock_survives_auto_but_explicit_release_can_release_it(self):
        self.apply()
        self.clock = 600, 600
        self.app._clk_lock = {"kind": Druta.LOCK_NVML, "lo": 600, "hi": 600, "verified": True}
        self.events.clear()
        ok, _ = self.app.reset_i2c_rail()
        self.assertTrue(ok)
        self.assertEqual(self.events, ["auto"])
        self.assertEqual(self.clock, (600, 600))
        self.assertEqual(self.app._clk_lock["hi"], 600)
        ok, _ = self.app.release_current()
        self.assertTrue(ok)
        self.assertEqual(self.events, ["auto", "release"])

    def test_foreign_controller_replacement_refuses_restore_and_keeps_clamp(self):
        self.apply()
        self.control["command"] += 1
        self.events.clear()
        ok, message = self.app.release_current()
        self.assertFalse(ok)
        self.assertIn("controller changed externally", message)
        self.assertEqual(self.events, ["restore"])
        self.assertEqual(self.clock, (405, 405))
        self.assertTrue(self.app._i2c_restore_failed)
        self.assertFalse(self.app.shutdown_is_clean())

    def test_later_apply_cannot_adopt_a_foreign_controller_request(self):
        self.apply()
        self.control["command"] += 1
        self.events.clear()
        self.apply(1000)
        self.assertEqual(self.app.rail.set_voltage_mv.call_count, 1)
        self.assertEqual(self.events, ["autosave"])

    def leave_uncertain_owned_write(self):
        def failed_write(_value, *, expected, **_kwargs):
            self.assertEqual(self.control, expected)
            self.control.update(enabled=True, command=550)
            self.app.rail.pending_recovery_states = [dict(self.entry), dict(self.control)]
            self.app.rail._verification_restore_ok = False
            return False, "write or rollback readback failed"

        def recover(target):
            self.events.append("recover")
            if self.control not in self.app.rail.pending_recovery_states:
                return False, "foreign replacement is not an owned recovery state"
            self.control = dict(target)
            self.app.rail.pending_recovery_states = []
            self.app.rail._verification_restore_ok = True
            return True, "owned entry recovered"

        self.app.rail.set_voltage_mv.side_effect = failed_write
        self.app.rail.recover_control = Mock(side_effect=recover)
        self.apply()
        self.assertTrue(self.app._i2c_restore_failed)
        self.events.clear()

    def test_uncertain_own_write_can_recover_to_auto_before_clock_release(self):
        self.leave_uncertain_owned_write()
        ok, _ = self.app.reset_i2c_rail()
        self.assertTrue(ok)
        self.assertEqual(self.events, ["auto", "recover", "auto", "release"])
        self.assertFalse(self.control["enabled"])
        self.assertFalse(self.app._i2c_restore_failed)
        self.assertIsNone(self.clock)

    def test_retry_of_uncertain_own_write_still_preserves_foreign_replacement(self):
        self.leave_uncertain_owned_write()
        self.control["command"] = 600
        foreign = dict(self.control)
        ok, message = self.app.reset_i2c_rail()
        self.assertFalse(ok)
        self.assertIn("foreign replacement", message)
        self.assertEqual(self.events, ["auto", "recover"])
        self.assertEqual(self.control, foreign)
        self.assertEqual(self.clock, (405, 405))

    def test_exit_restores_controller_before_clock(self):
        self.apply()
        self.events.clear()
        self.app.headroom_raise_pending = Mock(return_value=False)
        self.app.release_on_exit()
        self.assertEqual(self.events, ["restore", "release"])
        self.assertIsNone(self.clock)

    def test_failed_exit_restoration_keeps_the_clock_clamp(self):
        self.apply()
        self.events.clear()
        self.app.rail.restore_control.return_value = False, "context changed"
        self.app.rail.restore_control.side_effect = None
        self.app.release_on_exit()
        self.assertEqual(self.events, [])
        self.assertEqual(self.clock, (405, 405))
        self.assertTrue(self.app._i2c_restore_failed)

    def test_reset_all_restores_auto_before_the_gpu_can_release_clocks(self):
        self.apply()
        self.events.clear()
        self.app._reset_armed = True
        self.app.restore_headroom_before_unpin = Mock(return_value=True)

        def gpu_reset():
            self.assertFalse(self.control["enabled"])
            self.events.append("gpu reset")
            raise RuntimeError("stop after ordering check")

        self.app.gpu.reset_all = gpu_reset
        with self.assertRaisesRegex(RuntimeError, "ordering check"):
            self.app.reset_all()
        self.assertEqual(self.events, ["auto", "release", "autosave", "gpu reset"])

    def test_profile_transition_restores_before_preflight_or_driver_writes(self):
        self.apply()
        self.events.clear()
        self.app.profile_failure = Mock()
        self.app.rail_for_profile = Mock(return_value=self.app.rail)
        with patch("druta.druta.profiles.preflight", return_value="stop at preflight") as preflight:
            preflight.side_effect = lambda *_args: (
                self.assertEqual(self.control, self.entry) or "stop at preflight")
            self.app.begin_profile_load("test", {})
        self.assertEqual(self.events, ["restore", "release"])
        self.app.profile_failure.assert_called_once_with("stop at preflight", False)

    def test_disconnect_refuses_to_discard_an_uncertain_controller(self):
        self.apply()
        rail = self.app.rail
        self.control["command"] += 1
        self.assertFalse(self.app.clear_i2c_discovery())
        self.assertIs(self.app.rail, rail)
        self.values["i2c_mode"] = False
        self.app.on_i2c_mode(app_data=False)
        self.assertTrue(self.values["i2c_mode"])
        self.assertIs(self.app.rail, rail)

    def test_queued_disconnect_or_release_cannot_interrupt_the_verification_owner(self):
        self.apply()
        self.events.clear()
        self.app._i2c_busy = True
        self.app._i2c_modes = {"i2c_mode": True}
        self.values["i2c_mode"] = False
        self.app.on_i2c_mode(app_data=False)
        self.assertTrue(self.values["i2c_mode"])
        self.assertFalse(self.app.clear_i2c_discovery())
        self.assertFalse(self.app.release_current()[0])
        self.assertEqual(self.events, [])

    def test_manual_verify_uses_low_clock_context_and_releases_after_restore(self):
        started = threading.Event()

        def verify(*, acknowledged, operating_point, **_kwargs):
            self.assertTrue(acknowledged)
            self.assertEqual(operating_point(), (8, 405, 810))
            self.events.append("verify")
            started.set()
            return True, "response restored", []

        self.app.rail.verify = verify
        with patch("druta.druta.gpuload.verify_in_p0") as p0, \
                patch("druta.druta.gpuload.induce") as load:
            self.app.verify_i2c_rail()
            self.app._i2c_thread.join(2)
        self.assertTrue(started.is_set())
        self.assertFalse(self.app._i2c_thread.is_alive())
        self.assertEqual(self.events, ["clamp", "verify", "restore", "release"])
        self.assertTrue(self.app.i2c_verified())
        p0.assert_not_called()
        load.assert_not_called()

    def test_shutdown_waits_for_verifier_restore_before_releasing_clamp(self):
        raised = threading.Event()

        def verify(*, operating_point, cancelled, **_kwargs):
            operating_point()
            self.app.rail._verification_write_attempted = True
            self.control.update(enabled=True, command=550)
            self.events.append("temporary voltage")
            raised.set()
            try:
                while not cancelled():
                    threading.Event().wait(0.01)
            finally:
                self.control = dict(self.entry)
                self.events.append("backend restore")
            return False, "cancelled and restored", []

        self.app.rail.verify = verify
        self.app.cancel_i2c_discovery = Mock()
        self.app.verify_i2c_rail()
        self.assertTrue(raised.wait(2))
        self.app.stop_i2c_verification()
        self.assertFalse(self.app._i2c_thread.is_alive())
        self.assertEqual(self.events, ["clamp", "temporary voltage", "backend restore", "restore", "release"])
        self.assertIsNone(self.clock)
        self.assertFalse(self.app.i2c_verified())

    def test_manual_verify_worker_never_mutates_dpg(self):
        self.app.rail.verify = Mock(return_value=(True, "restored", []))
        main_thread = threading.get_ident()

        def ui_only(*_args, **_kwargs):
            self.assertEqual(threading.get_ident(), main_thread)
            return False

        self.ui.does_item_exist.side_effect = ui_only
        self.app.verify_i2c_rail()
        self.app._i2c_thread.join(2)
        self.assertTrue(self.app.i2c_verified())

    def test_manual_verify_restores_an_existing_owned_frequency_lock(self):
        self.clock = 1500, 1500
        prior = {"kind": Druta.LOCK_NVML, "lo": 1500, "hi": 1500, "verified": True}
        self.app._clk_lock = dict(prior)
        self.app.rail.verify = Mock(return_value=(True, "restored", []))
        self.app.verify_i2c_rail()
        self.app._i2c_thread.join(2)
        self.assertTrue(self.app.i2c_verified())
        self.assertEqual(self.events, ["clamp", "restore", "clamp"])
        self.assertEqual(self.clock, (1500, 1500))
        self.assertEqual(self.app._clk_lock, prior)
        self.assertEqual(self.control, self.entry)
        self.app.gpu.reset_gpu_clocks.assert_not_called()

    def test_failed_verify_clock_settling_restores_an_existing_owned_request(self):
        self.clock = 1500, 1500
        self.app._clk_lock = {"kind": Druta.LOCK_NVML, "lo": 1500, "hi": 1500, "verified": True}
        self.point["core"] = 1500
        self.app.rail.verify = Mock()
        with patch("druta.druta.time.monotonic", side_effect=range(20)):
            self.app.verify_i2c_rail()
        self.app.rail.verify.assert_not_called()
        self.assertEqual(self.clock, (1500, 1500))
        self.assertEqual(self.events, ["clamp", "clamp"])
        self.assertEqual(self.control, self.entry)

    def test_manual_row_says_nominal_and_explains_clamp_and_restoration(self):
        self.app.slider_row = Mock()
        self.app.s = lambda value: value
        self.app.build_i2c_rail_row()
        self.assertEqual(self.app.slider_row.call_args.args[1], "Controller output nominal PMBus request (mV)")
        description = self.app.i2c_verify_description()
        self.assertIn("not calibrated physical voltage", description)
        self.assertIn("Auto returns control to GPU VID", description)
        self.assertIn("before releasing", description)

    def use_register_backend(self):
        with (Path(__file__).resolve().parents[1] / "i2c/rtx2080ti-mp2888a.toml").open("rb") as source:
            profile = railctl.Profile(tomllib.load(source), "original")
        bus = PMBus()
        self.app.rail = MP2888Candidate(profile, bus, 1, 0x20).for_control_method("pmbus")
        self.app._i2c_verified_for = self.app.i2c_connection()
        return bus

    def test_ui_and_register_backend_agree_on_exact_manual_ownership_and_restore(self):
        bus = self.use_register_backend()
        initial = self.app.rail.capture_control()
        self.apply(1003)
        self.assertEqual(bus.regs[0x21], 160)
        self.assertTrue(bus.regs[0xE1] & 0x10)
        self.assertEqual(self.app._i2c_manual_voltage["expected"], self.app.rail.capture_control())
        ok, message = self.app.release_current()
        self.assertTrue(ok, message)
        self.assertEqual(self.app.rail.capture_control(), initial)
        self.assertIsNone(self.clock)

    def test_ui_and_register_backend_verify_under_low_clock_and_restore_exact_entry(self):
        self.use_register_backend()
        initial = self.app.rail.capture_control()
        self.app.verify_i2c_rail()
        self.app._i2c_thread.join(2)
        self.assertFalse(self.app._i2c_thread.is_alive())
        self.assertTrue(self.app.i2c_verified(), self.app.log.call_args_list)
        self.assertEqual(self.app.rail.capture_control(), initial)
        self.assertIsNone(self.clock)


class ReportedVoutUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app.rail = candidate()
        self.app.rail.p.read_only = False
        self.app.rail.controller_reported_vout = True
        self.app.rail.read_vout = Mock(return_value=1096)
        self.app.gpu = object()
        self.app._i2c_busy = False
        self.app._i2c_verified = True
        self.app._i2c_verified_for = self.app.i2c_connection()
        self.values["live_i2crail"] = ""

    def test_reported_voltage_does_not_become_a_physical_gpu_comparison(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                self.app._i2c_verified = verified
                self.assertEqual(self.app.i2c_rail_text(1055), "controller reported 1096 mV")

    def test_absolute_control_labels_automatic_and_manual_reports(self):
        self.app.rail.absolute_voltage = True
        for target, expected in ((None, "Auto; controller reported 1096 mV"),
                                 (1100, "controller reported 1096 mV")):
            with self.subTest(target=target):
                self.app.rail.telemetry = Mock(return_value={"vout_mv": 1096, "target_mv": target})
                self.assertEqual(self.app.i2c_rail_text(1055), expected)

    def test_other_controllers_retain_existing_readout(self):
        self.app.rail.controller_reported_vout = False
        self.assertEqual(self.app.i2c_rail_text(1055), "1096 mV  (+41 vs GPU)")
