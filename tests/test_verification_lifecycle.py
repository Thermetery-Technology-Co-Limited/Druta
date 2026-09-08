# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verification owns temporary writes until restoration, using fake hardware only."""
from pathlib import Path
import threading
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from druta import druta
from druta import railctl
from druta.druta import Druta
from tests import test_ncp4206


def offset_rail():
    with (Path(__file__).resolve().parents[1] / "i2c/rtx2080ti-mp2888a.toml").open("rb") as file:
        recipe = railctl.Profile(tomllib.load(file), "mock recipe")
    rail = railctl.Rail(recipe, None)
    rail.present = Mock(return_value=True)
    rail.regs = {recipe.wreg: 32}  # +200 mV, a valid entry setting under XOC.
    rail.read = lambda reg, width: rail.regs.get(reg)
    rail.read_vout = lambda: 1100.0
    rail.writes = []

    def write(reg, value, width):
        rail.writes.append((reg, value, width))
        rail.regs[reg] = value
        return True

    rail._raw_write = write
    rail.enable_xoc(railctl.XOC_CONFIRM)
    return rail


def bare_app():
    app = Druta.__new__(Druta)
    app.gpu = SimpleNamespace(available=lambda: True, status_line=lambda: "mock",
                              read_vcore_mv=lambda: 1200)
    app.rail = test_ncp4206.NCPTests().rail()
    app._gpu_gen = 1
    app._i2c_busy = False
    app._i2c_verified = False
    app._i2c_verified_for = app._i2c_recovery_for = None
    app._profile_pending = None
    app._profile_applying = False
    app._startup_manager = app._startup_request = None
    app.log = Mock()
    app.guard = Mock(return_value=True)
    app.i2c_gate = Mock(return_value=(True, ""))
    app.sync_lock_ui = Mock()
    return app


class ControllerCancellationTests(unittest.TestCase):
    def test_mp_cancel_before_dispatch_writes_nothing(self):
        rail = offset_rail()
        result = rail.verify(acknowledged=True, cancelled=lambda: True)
        self.assertFalse(result[0])
        self.assertIn("cancelled", result[1])
        self.assertEqual(rail.writes, [])

    def test_mp_cancel_after_write_restores_exact_entry(self):
        rail = offset_rail()
        cancel = threading.Event()
        rail._sample = Mock(return_value=(0, 1))
        with patch("druta.railctl.time.sleep", side_effect=lambda _: cancel.set()):
            result = rail.verify(acknowledged=True, ref=lambda: 1000,
                                 cancelled=cancel.is_set)
        self.assertFalse(result[0])
        self.assertIn("cancelled", result[1])
        self.assertEqual(rail.regs[rail.p.wreg], 32)
        self.assertEqual([w[1] for w in rail.writes], [33, 32])
        self.assertTrue(rail._verification_restore_ok)

    def test_mp_mode_change_does_not_revoke_entry_restore_policy(self):
        for entry_xoc, entry_raw in ((True, 32), (False, 0)):
            with self.subTest(entry_xoc=entry_xoc):
                rail = offset_rail()
                rail.xoc = entry_xoc
                rail.regs[rail.p.wreg] = entry_raw
                calls = []

                def sample(**_kwargs):
                    calls.append(True)
                    if len(calls) == 2:
                        rail.xoc = not entry_xoc
                    return 0, 1

                rail._sample = sample
                with patch("druta.railctl.time.sleep"):
                    ok, message, _ = rail.verify(acknowledged=True, ref=lambda: 1000)
                self.assertFalse(ok)
                self.assertIn("XOC mode changed", message)
                self.assertIn("Restored to", message)
                self.assertEqual(rail.xoc, not entry_xoc)
                self.assertEqual(rail.regs[rail.p.wreg], entry_raw)
                self.assertEqual(len(rail.writes), 2)  # One rung, one restore.
                self.assertTrue(rail._verification_restore_ok)

    def test_mp_restore_still_requires_identity_when_entry_policy_is_retained(self):
        rail = offset_rail()
        cancel = threading.Event()
        rail._sample = Mock(return_value=(0, 1))

        def interrupt(_seconds):
            rail.disable_xoc()
            rail.present.return_value = False
            cancel.set()

        with patch("druta.railctl.time.sleep", side_effect=interrupt):
            ok, message, _ = rail.verify(acknowledged=True, ref=lambda: 1000,
                                        cancelled=cancel.is_set)
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED", message)
        self.assertIn("cancelled", message)
        self.assertFalse(rail._verification_restore_ok)
        self.assertIn("identity", rail._verification_restore_error)
        self.assertEqual(len(rail.writes), 1)

    def test_ncp_cancel_during_provisional_voltage_restores_auto_and_command(self):
        rail = test_ncp4206.NCPTests().rail()
        original = dict(rail.regs)
        cancel = threading.Event()

        def voltage():
            if rail.regs[0xd3] & 8:
                cancel.set()
            return 1200

        rail.read_vout = voltage
        with patch("druta.ncp4206.time.sleep"):
            ok, message, _ = rail.verify(acknowledged=True, cancelled=cancel.is_set)
        self.assertFalse(ok)
        self.assertIn("cancelled", message)
        self.assertEqual(rail.regs, original)
        self.assertTrue(rail._verification_restore_ok)

    def test_ncp_cancel_in_baseline_never_writes(self):
        rail = test_ncp4206.NCPTests().rail()
        cancel = threading.Event()
        with patch("druta.ncp4206.time.sleep", side_effect=lambda _: cancel.set()):
            ok, message, _ = rail.verify(acknowledged=True, cancelled=cancel.is_set)
        self.assertFalse(ok)
        self.assertIn("nothing written", message)
        self.assertEqual(rail.calls, [])

    def test_ncp_cancel_preserves_failed_restore(self):
        rail = test_ncp4206.NCPTests().rail()
        cancel = threading.Event()

        def voltage():
            if rail.regs[0xd3] & 8:
                cancel.set()
            return 1200

        rail.read_vout = voltage
        restore = rail.restore_control
        rail.restore_control = Mock(side_effect=lambda state, **kwargs:
                                    (False, "identity disappeared")
                                    if kwargs.get("recovery") else restore(state, **kwargs))
        with patch("druta.ncp4206.time.sleep"):
            ok, message, _ = rail.verify(acknowledged=True, cancelled=cancel.is_set)
        self.assertFalse(ok)
        self.assertIn("restoration failed: identity disappeared", message)
        self.assertIn("cancelled", message)
        self.assertFalse(rail._verification_restore_ok)


class VerificationLifecycleTests(unittest.TestCase):
    def test_run_waits_for_actual_ncp_restore_before_release_and_context_destroy(self):
        for restore_failed, startup in ((False, False), (True, False), (False, True)):
            with self.subTest(restore_failed=restore_failed, startup=startup):
                self.run_shutdown(restore_failed=restore_failed, startup=startup)

    def run_shutdown(self, *, restore_failed, startup):
        app = bare_app()
        app.gpu_list = []
        app.s = lambda value: value
        app.menu_h = lambda: 20
        app._stop = threading.Event()
        app._lock = threading.Lock()
        app._snap = app._snap_err = app._snap_t = None
        for method in ("load_fonts", "build_ui", "relayout", "poll_loop", "vf_read",
                       "timings_capture", "refresh_profile_list", "clear_once",
                       "set_stale", "drag_watchdog", "update_vf_corner"):
            setattr(app, method, Mock())
        original = dict(app.rail.regs)
        written = threading.Event()
        events = []

        def voltage():
            if app.rail.regs[0xd3] & 8:
                written.set()
                if not app._i2c_cancel.wait(2):
                    raise RuntimeError("shutdown did not cancel the owned verifier")
            return 1200

        app.rail.read_vout = voltage
        restore = app.rail.restore_control

        def record_restore(state, **kwargs):
            if not kwargs.get("recovery"):
                return restore(state, **kwargs)
            events.append("restore")
            return ((False, "injected restore refusal") if restore_failed
                    else restore(state, **kwargs))

        app.rail.restore_control = record_restore
        app.release_on_exit = Mock(side_effect=lambda: events.append("release"))
        running = [True]

        def close_during_verify(*_args, **_kwargs):
            app.verify_i2c_rail()
            self.assertFalse(app._i2c_thread.daemon)
            self.assertTrue(written.wait(2), "verification did not reach its provisional write")
            app.save_profile()
            running[0] = False
            if startup:
                app._profile_pending = ("startup", {}, True)
                raise RuntimeError("startup callback interrupted")

        if startup:
            app._startup_request = {"name": "startup", "profile": {}}
            app.begin_profile_load = close_during_verify
            app._startup_manager = SimpleNamespace(block=Mock(), ending=False)

        ui = MagicMock()
        ui.is_dearpygui_running.side_effect = lambda: running[0]
        ui.does_item_exist.return_value = False
        ui.get_callback_queue.return_value = [[lambda: close_during_verify()]]
        ui.run_callbacks = druta.dpg.run_callbacks

        def destroy():
            self.assertFalse(app._i2c_thread.is_alive())
            self.assertFalse(app._dpg_ready)
            events.append("destroy")

        ui.destroy_context.side_effect = destroy
        with patch("druta.druta.dpg", ui), patch("druta.ncp4206.time.sleep"), \
                patch("druta.druta.gpuload.available", return_value=(True, "")), \
                patch("druta.druta.gpuload.induce", side_effect=lambda _gpu, **kw:
                      {"result": kw["on_settled"]()}), \
                patch("druta.druta._tell") as tell, \
                patch("druta.druta.profiles.capture") as capture:
            if startup:
                with self.assertRaisesRegex(RuntimeError, "startup callback interrupted"):
                    app.run()
            else:
                app.run()
            capture.assert_not_called()
            self.assertEqual(tell.call_count, int(restore_failed))
            if restore_failed:
                self.assertIn("injected restore refusal", tell.call_args.args[0])
        self.assertEqual(events, ["restore", "release", "destroy"])
        self.assertFalse(app._i2c_busy)
        self.assertFalse(app.i2c_verified())
        self.assertIsNone(app._profile_pending)
        self.assertEqual(app.shutdown_is_clean(), not restore_failed)
        if restore_failed:
            self.assertTrue(any("injected restore refusal" in c.args[0]
                                for c in app.log.call_args_list))
        else:
            self.assertEqual(app.rail.regs, original)
        if startup:
            app._startup_manager.block.assert_called_once()

    def test_queued_mode_callback_reverts_ui_without_mutating_controller(self):
        app = bare_app()
        app._i2c_busy = True
        app._i2c_modes = {"unlock": True, "xoc_mode": True,
                          "i2c_mode": True, "vlim_mode": False}
        app.rail.disable_xoc = Mock()
        app.rail.enable_xoc = Mock()
        app.risk_features = Mock(side_effect=AssertionError("read live modes while busy"))
        values = {tag: not value for tag, value in app._i2c_modes.items()}
        with patch("druta.druta.dpg.does_item_exist", return_value=True), \
                patch("druta.druta.dpg.set_value", side_effect=values.__setitem__), \
                patch("druta.druta.dpg.configure_item") as configure:
            app.sync_risk_ui()
        self.assertEqual(values, app._i2c_modes)
        app.rail.disable_xoc.assert_not_called()
        app.rail.enable_xoc.assert_not_called()
        app.risk_features.assert_not_called()
        self.assertTrue(all(c.kwargs["enabled"] is False for c in configure.call_args_list))

    def test_snapshot_callbacks_refuse_busy_pending_and_applying(self):
        for attr, value in (("_i2c_busy", True), ("_profile_pending", ("p", {}, False)),
                            ("_profile_applying", True)):
            with self.subTest(attr=attr):
                app = bare_app()
                setattr(app, attr, value)
                app.show_win = Mock()
                with patch("druta.druta.profiles.capture") as capture, \
                        patch("druta.druta.dpg.get_value") as value_read:
                    app.save_profile()
                    app.open_save_profile()
                capture.assert_not_called()
                value_read.assert_not_called()
                app.show_win.assert_not_called()

    def test_ungated_stock_callbacks_cannot_interrupt_verification(self):
        app = bare_app()
        app._i2c_busy = True
        app.gpu.reset_volt_rail_limits = Mock()
        app.stock_knob("vlim_rel")
        app.apply_vlim_reset()
        app.gpu.reset_volt_rail_limits.assert_not_called()

    def test_timing_writes_and_second_load_cannot_interrupt_verification(self):
        for field, value in (("_i2c_busy", True), ("_profile_pending", ("p", {}, False))):
            with self.subTest(field=field):
                app = bare_app()
                setattr(app, field, value)
                with patch("druta.druta.timingwrite.apply") as write, \
                        patch("druta.druta.timingwrite.restore") as restore, \
                        patch("druta.druta.threading.Thread") as worker:
                    app.tw_apply()
                    app.tw_restore()
                    app.timings_read_p0()
                write.assert_not_called()
                restore.assert_not_called()
                worker.assert_not_called()

    def test_verify_waits_for_an_existing_timing_capture_or_load(self):
        app = bare_app()
        app._tim_busy = True
        app.verify_i2c_rail()
        app.i2c_gate.assert_not_called()
        self.assertFalse(app._i2c_busy)

    def test_cancelled_result_cannot_publish_a_pass_even_if_controller_returns_success(self):
        app = bare_app()
        app._i2c_busy = True
        cancel = threading.Event()
        app.rail.verify = Mock(return_value=(True, "restored", []))

        def induce(_gpu, **kwargs):
            result = kwargs["on_settled"]()
            cancel.set()
            return {"result": result}

        with patch("druta.druta.gpuload.induce", side_effect=induce):
            app._i2c_verify_worker(cancel=cancel)
        self.assertFalse(app.i2c_verified())
        self.assertFalse(app._i2c_busy)

    def test_thread_start_failure_releases_busy_and_does_not_write(self):
        app = bare_app()
        with patch("druta.druta.dpg", MagicMock()), \
                patch("druta.druta.gpuload.available", return_value=(True, "")), \
                patch("druta.druta.threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("no thread resources")
            app.verify_i2c_rail()
        self.assertFalse(app._i2c_busy)
        self.assertIsNone(app._i2c_thread)
        self.assertFalse(app.i2c_verified())
        self.assertEqual(app.rail.calls, [])

    def test_worker_log_failure_cannot_hide_restoration_failure_or_strand_busy(self):
        app = bare_app()
        app._i2c_busy = True

        def verify(**kwargs):
            app.rail._verification_write_attempted = True
            app.rail._verification_restore_ok = False
            app.rail._verification_restore_error = "bus disconnected during restore"
            raise RuntimeError("log sink failed")

        app.rail.verify = verify
        app.log.side_effect = RuntimeError("log sink failed again")
        with patch("druta.druta.gpuload.induce", side_effect=lambda _gpu, **kw:
                   {"result": kw["on_settled"]()}):
            with self.assertRaisesRegex(RuntimeError, "log sink failed again"):
                app._i2c_verify_worker()
        self.assertFalse(app._i2c_busy)
        self.assertFalse(app.i2c_verified())
        self.assertFalse(app.shutdown_is_clean())
        self.assertIn("bus disconnected during restore", app.log.call_args.args[0])

    def test_pending_profile_is_not_applied_after_cancelled_verification(self):
        app = bare_app()
        app._profile_pending = ("pending", {"i2c": {}}, False)
        app._i2c_ui_pending = True
        app.profile_failure = Mock()
        app.finish_profile_load = Mock()
        app.poll_profile_load()
        self.assertIsNone(app._profile_pending)
        app.profile_failure.assert_called_once()
        app.finish_profile_load.assert_not_called()

    def test_busy_lock_sync_disables_controls_and_preserves_the_unlock_checkbox(self):
        app = bare_app()
        app._i2c_busy = True
        app._i2c_modes = {"unlock": True, "xoc_mode": False}
        app._ctl_widgets = ["go_i2crail", "go_fan", "go_core"]
        app.gpu.fan_capabilities = lambda: {"manual": True, "auto": True}
        app.gpu.arch = lambda: druta.GPU.ARCH_TURING
        app.vf_applicable = lambda: False
        values = {"unlock": False, "xoc_mode": True}
        with patch("druta.druta.dpg.does_item_exist", return_value=True), \
                patch("druta.druta.dpg.get_value", side_effect=values.get), \
                patch("druta.druta.dpg.set_value", side_effect=values.__setitem__), \
                patch("druta.druta.dpg.configure_item") as configure:
            Druta.sync_lock_ui(app)
        self.assertTrue(values["unlock"])
        self.assertFalse(values["xoc_mode"])
        for tag in app._ctl_widgets:
            self.assertIn(((tag,), {"enabled": False}),
                          [(c.args, c.kwargs) for c in configure.call_args_list])


class PowerMonitorTests(unittest.TestCase):
    def test_unknown_power_or_limit_is_never_displayed_as_zero(self):
        app = Druta.__new__(Druta)
        app.refresh_domains = Mock()
        app.mem_fmt = lambda value: ("--", "")
        app._bar_band, app._bar_themes = {}, {}
        app.gpu = SimpleNamespace(mem_offset_scale=lambda: (1, "MHz"))
        for active, power, limit in ((False, None, None), (False, None, 200000),
                                      (False, 0, 200000), (False, 50, None),
                                      (True, 50, None), (True, 50, 200000)):
            with self.subTest(active=active, power=power, limit=limit):
                app.shunt = SimpleNamespace(active=active, exact=True, factor=2,
                                             apply=lambda value: value * 2)
                with patch("druta.druta.dpg.set_value") as set_value, \
                        patch("druta.druta.dpg.configure_item") as configure:
                    app.refresh_monitor({"power_w": power, "pl_now_mw": limit})
                values = {c.args[0]: c.args[1] for c in set_value.call_args_list}
                overlay = next(c.kwargs["overlay"] for c in configure.call_args_list
                               if c.args[0] == "bar_tdp")
                if power is None or limit is None:
                    self.assertEqual(overlay, "power / limit unavailable")
                if limit is None:
                    self.assertIn("limit unavailable", values["s_pwr"])
                    self.assertNotIn("limit 0", values["s_pwr"])
                if power is None:
                    self.assertEqual(values["t_pwr"], "--")
                if power == 0:
                    self.assertEqual(overlay, "0%   0 / 200 W")


if __name__ == "__main__":
    unittest.main()
