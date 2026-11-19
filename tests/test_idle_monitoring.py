# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Paused monitoring stays quiet without losing owned-control maintenance."""

import threading
import unittest
from unittest.mock import Mock

from druta.druta import Druta


class ObservedWake(threading.Event):
    """Let tests observe the worker parked, without guessing a sleep duration."""

    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


class FakeGpu:
    def __init__(self, record=None):
        self.record = record
        self.read = Mock(return_value={"core": 900, "mem": 3004})
        self.retry_clock_capabilities = Mock(return_value=False)
        self.read_rail_live_mv = Mock(return_value=912.5)

    def apply_hold_headroom(self, *args, **kwargs):
        raise AssertionError("paused monitoring must not apply a new hold")

    def hold_headroom_record(self):
        return self.record


class IdleMonitoringTests(unittest.TestCase):
    def make_app(self, *, paused=False, record=None):
        app = Druta.__new__(Druta)
        app._lock = threading.Lock()
        app._stop = threading.Event()
        app._poll_wake = ObservedWake()
        app._monitoring_paused = paused
        app._monitoring_epoch = 0
        app._maintenance_snap = None
        app._gpu_gen = 1
        app._rebuilding = False
        app._snap = {"core": 300, "mem": 405}
        app._snap_err = None
        app._snap_t = 12.0
        app._clock_capability_recovery_token = 0
        app._clock_capability_recovery = None
        app._profile_pending = None
        app._i2c_ui_pending = False
        app.poll_profile_load = Mock()
        app.watch_hold_headroom = Mock()
        app.gpu = FakeGpu(record)
        return app

    def start_worker(self, app):
        worker = threading.Thread(target=app.poll_loop, daemon=True)
        worker.start()
        self.addCleanup(self.stop_worker, app, worker)
        return worker

    def stop_worker(self, app, worker):
        app._stop.set()
        app._poll_wake.set()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "telemetry worker did not stop")

    def test_transition_discards_maintenance_and_resume_requests_fresh_sample(self):
        app = self.make_app()
        app._maintenance_snap = (1, app.gpu, 0, {"nvvdd_live_mv": 900}, 13.0)
        app.set_monitoring_paused(True)
        self.assertTrue(app._monitoring_paused)
        paused_epoch = app._monitoring_epoch
        self.assertGreater(paused_epoch, 0)
        self.assertIsNone(app._maintenance_snap)

        app.set_monitoring_paused(True)
        self.assertEqual(app._monitoring_epoch, paused_epoch)
        app._poll_wake.clear()
        app.set_monitoring_paused(False)
        self.assertFalse(app._monitoring_paused)
        self.assertGreater(app._monitoring_epoch, paused_epoch)
        self.assertTrue(app._poll_wake.is_set())
        self.assertTrue(app._snap_err, "old snapshot must not be presented as fresh")
        self.assertEqual(app._snap, {"core": 300, "mem": 405})

    def test_paused_without_owned_headroom_does_not_read_gpu_or_retry_probes(self):
        app = self.make_app(paused=True)
        self.start_worker(app)
        self.assertTrue(app._poll_wake.waiting.wait(2), "worker did not park")
        app.gpu.read.assert_not_called()
        app.gpu.read_rail_live_mv.assert_not_called()
        app.gpu.retry_clock_capabilities.assert_not_called()
        self.assertEqual(app._snap_t, 12.0)

    def test_paused_owned_headroom_reads_only_its_rail_and_preserves_full_snapshot(self):
        app = self.make_app(paused=True, record={"rail": 1})

        def read_rail(rail):
            app._stop.set()
            return 912.5

        app.gpu.read_rail_live_mv.side_effect = read_rail
        worker = self.start_worker(app)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        app.gpu.read_rail_live_mv.assert_called_once_with(1)
        app.gpu.read.assert_not_called()
        app.gpu.retry_clock_capabilities.assert_not_called()
        self.assertEqual(app._snap, {"core": 300, "mem": 405})
        self.assertEqual(app._snap_t, 12.0)
        app.idle_maintenance()
        app.watch_hold_headroom.assert_called_once()
        self.assertEqual(app.watch_hold_headroom.call_args.args[0],
                         {"nvvdd_live_mv": 912.5})

    def test_failed_maintenance_read_does_not_erase_ordinary_snapshot_or_skip_watch(self):
        app = self.make_app(paused=True, record={"rail": 0})

        def read_rail(rail):
            app._stop.set()
            raise RuntimeError("transient rail read failure")

        app.gpu.read_rail_live_mv.side_effect = read_rail
        worker = self.start_worker(app)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(app._snap, {"core": 300, "mem": 405})
        self.assertEqual(app._snap_t, 12.0)
        app.gpu.read.assert_not_called()
        app.idle_maintenance()
        app.watch_hold_headroom.assert_called_once()
        self.assertIsNone(app.watch_hold_headroom.call_args.args[0].get("nvvdd_live_mv"))

    def test_pause_discards_inflight_normal_result_and_error(self):
        for fail in (False, True):
            for resume_before_completion in (False, True):
                with self.subTest(fail=fail, resume=resume_before_completion):
                    app = self.make_app()
                    started, finish = threading.Event(), threading.Event()

                    def read():
                        started.set()
                        if not finish.wait(2):
                            raise TimeoutError("test did not release the read")
                        app._stop.set()
                        if fail:
                            raise RuntimeError("result belongs to before the pause")
                        return {"core": 1800}

                    app.gpu.read.side_effect = read
                    worker = self.start_worker(app)
                    try:
                        self.assertTrue(started.wait(2))
                        app.set_monitoring_paused(True)
                        if resume_before_completion:
                            app.set_monitoring_paused(False)
                        expected_error = app._snap_err
                    finally:
                        finish.set()
                        self.stop_worker(app, worker)
                    self.assertEqual(app._snap, {"core": 300, "mem": 405})
                    self.assertEqual(app._snap_t, 12.0)
                    self.assertEqual(app._snap_err, expected_error)
                    if not resume_before_completion:
                        app.gpu.retry_clock_capabilities.assert_not_called()

    def test_inflight_maintenance_cannot_cross_gpu_switch_or_pause_cycle(self):
        for change in ("gpu", "pause_cycle"):
            with self.subTest(change=change):
                app = self.make_app(paused=True, record={"rail": 0})
                started, finish = threading.Event(), threading.Event()

                def read_rail(rail):
                    started.set()
                    if not finish.wait(2):
                        raise TimeoutError("test did not release the rail read")
                    app._stop.set()
                    return 987.5

                app.gpu.read_rail_live_mv.side_effect = read_rail
                worker = self.start_worker(app)
                try:
                    self.assertTrue(started.wait(2))
                    if change == "gpu":
                        with app._lock:
                            app._gpu_gen += 1
                            app.gpu = FakeGpu({"rail": 0})
                    else:
                        app.set_monitoring_paused(False)
                        app.set_monitoring_paused(True)
                finally:
                    finish.set()
                    self.stop_worker(app, worker)
                self.assertIsNone(app._maintenance_snap)
                self.assertEqual(app._snap, {"core": 300, "mem": 405})

    def test_resume_wakes_worker_and_publishes_fresh_normal_sample(self):
        app = self.make_app(paused=True)
        read_started = threading.Event()

        def read():
            read_started.set()
            app._stop.set()
            return {"core": 1500, "mem": 7000}

        app.gpu.read.side_effect = read
        worker = self.start_worker(app)
        self.assertTrue(app._poll_wake.waiting.wait(2))
        app.set_monitoring_paused(False)
        self.assertTrue(read_started.wait(2), "resume did not wake the polling worker")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(app._snap, {"core": 1500, "mem": 7000})
        self.assertIsNone(app._snap_err)
        self.assertGreater(app._snap_t, 12.0)

    def test_idle_maintenance_does_not_start_headroom_when_no_raise_is_owned(self):
        app = self.make_app(paused=True)
        app.idle_maintenance()
        app.watch_hold_headroom.assert_not_called()
        app.poll_profile_load.assert_not_called()
        app.gpu.read.assert_not_called()
        app.gpu.read_rail_live_mv.assert_not_called()

    def test_idle_maintenance_uses_only_current_session_sample(self):
        for invalid in (None, "generation", "gpu", "epoch"):
            with self.subTest(invalid=invalid):
                app = self.make_app(paused=True, record={"rail": 0})
                sample = {"nvvdd_live_mv": 925.0}
                app._maintenance_snap = (
                    2 if invalid == "generation" else 1,
                    FakeGpu() if invalid == "gpu" else app.gpu,
                    1 if invalid == "epoch" else 0,
                    sample, 99.0,
                )
                app.idle_maintenance()
                app.watch_hold_headroom.assert_called_once()
                observed = app.watch_hold_headroom.call_args.args[0]
                if invalid is None:
                    self.assertEqual(observed, sample)
                else:
                    self.assertIsNone(observed.get("nvvdd_live_mv"))

    def test_idle_maintenance_finishes_pending_profile_and_verification_completion(self):
        for field in ("_profile_pending", "_i2c_ui_pending"):
            with self.subTest(field=field):
                app = self.make_app(paused=True)
                setattr(app, field, True)
                app.idle_maintenance()
                app.poll_profile_load.assert_called_once_with()
                app.watch_hold_headroom.assert_not_called()


if __name__ == "__main__":
    unittest.main()
