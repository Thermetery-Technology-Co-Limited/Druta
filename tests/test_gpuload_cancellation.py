# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cancellation before a voltage callback never starts a provisional write."""
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import gpuload


class LoadCancellationTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.gpu = SimpleNamespace(slot=lambda: "0000:02:00.0",
                                   read=Mock(return_value={"mem": 3505, "pstate": 0}))
        self.callback = Mock(return_value="captured")

    def load(self, make):
        load = make.return_value
        load.error = ""
        load.workload_ended.is_set.return_value = False
        load.done.is_set.return_value = False
        load.wait_started.return_value = True
        load.stats = {}
        return load

    def run_load(self):
        return gpuload.induce(self.gpu, on_settled=self.callback,
                              cancelled=self.cancel.is_set, poll=0)

    def test_cancelled_before_start_never_creates_a_load(self):
        self.cancel.set()
        with patch("druta.gpuload.BandwidthLoad") as make:
            result = self.run_load()
            make.assert_not_called()
        self.assertIn("cancelled", result["error"])
        self.callback.assert_not_called()

    def test_cancelled_during_startup_stops_and_joins_without_callback(self):
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            load.wait_started.side_effect = lambda **kw: self.cancel.set() or False
            result = self.run_load()
            load.stop.assert_called_once()
            load.join.assert_called_once()
            self.assertLessEqual(load.wait_started.call_args.kwargs["timeout"], 0.1)
        self.assertIn("cancelled", result["error"])
        self.callback.assert_not_called()
        self.gpu.read.assert_not_called()

    def test_cancelled_while_settling_never_enters_callback(self):
        def read():
            self.cancel.set()
            return {"mem": 3505, "pstate": 0}
        self.gpu.read.side_effect = read
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            result = self.run_load()
            load.stop.assert_called_once()
            load.join.assert_called_once()
        self.assertIn("cancelled", result["error"])
        self.callback.assert_not_called()

    def test_cancelled_after_last_read_still_prevents_callback(self):
        count = 0
        def read():
            nonlocal count
            count += 1
            if count == 4:
                self.cancel.set()
            return {"mem": 3505, "pstate": 0}
        self.gpu.read.side_effect = read
        with patch("druta.gpuload.BandwidthLoad") as make:
            self.load(make)
            result = self.run_load()
        self.assertTrue(result["settled"])
        self.assertIn("cancelled", result["error"])
        self.callback.assert_not_called()

    def test_started_callback_finishes_restoration_before_load_cleanup(self):
        steps = []
        def callback():
            try:
                steps.append("write")
                self.cancel.set()
            finally:
                steps.append("restored")
            return "restored"
        self.callback = callback
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            load.stop.side_effect = lambda: steps.append("stop")
            load.join.side_effect = lambda **kw: steps.append("join")
            result = self.run_load()
        self.assertEqual(steps, ["write", "restored", "stop", "join"])
        self.assertEqual(result["result"], "restored")

    def test_expired_successful_load_cannot_enter_voltage_callback(self):
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            load.done.is_set.return_value = True
            result = self.run_load()
            load.stop.assert_called_once()
            load.join.assert_called_once()
        self.assertIn("ended before the callback", result["error"])
        self.callback.assert_not_called()

    def test_settlement_timeout_never_enters_voltage_callback(self):
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            result = gpuload.induce(self.gpu, settle_timeout=0,
                                    on_settled=self.callback)
            load.stop.assert_called_once()
            load.join.assert_called_once()
        self.assertFalse(result["settled"])
        self.assertIn("did not settle", result["error"])
        self.callback.assert_not_called()

    def test_load_expiring_after_final_read_cannot_enter_voltage_callback(self):
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            count = 0
            def read():
                nonlocal count
                count += 1
                if count == 4:
                    load.done.is_set.return_value = True
                return {"mem": 3505, "pstate": 0}
            self.gpu.read.side_effect = read
            result = self.run_load()
        self.assertIn("ended before the callback", result["error"])
        self.callback.assert_not_called()

    def test_expiry_during_callback_invalidates_result_after_restoration(self):
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            steps = []
            def callback():
                try:
                    steps.append("write")
                    load.done.is_set.return_value = True
                finally:
                    steps.append("restored")
                return "restored"
            self.callback = callback
            load.stop.side_effect = lambda: steps.append("stop")
            result = self.run_load()
        self.assertEqual(steps, ["write", "restored", "stop"])
        self.assertEqual(result["result"], "restored")
        self.assertIn("ended before the callback finished", result["error"])

    def test_deadline_reported_at_join_invalidates_callback_result(self):
        with patch("druta.gpuload.BandwidthLoad") as make:
            load = self.load(make)
            load.join.side_effect = lambda **kw: load.stats.update(hit_deadline=True)
            result = self.run_load()
        self.callback.assert_called_once()
        self.assertEqual(result["result"], "captured")
        self.assertIn("hard duration limit", result["error"])


class WorkloadLifetimeTests(unittest.TestCase):
    """Exercise the actual worker and its pre-teardown event without CUDA."""

    @contextmanager
    def mocked_cuda(self, load, *, wait_for_cleanup=False):
        cu = Mock()
        for name in ("cuInit", "cuCtxCreate_v2", "cuMemGetInfo_v2",
                     "cuMemAlloc_v2", "cuCtxDestroy_v2"):
            getattr(cu, name).return_value = 0
        cu.cuDeviceGetName.return_value = 1
        cleanup_entered, release_cleanup = threading.Event(), threading.Event()

        def free(_):
            cleanup_entered.set()
            if not release_cleanup.wait(3):
                raise RuntimeError("mock CUDA cleanup was not released")
            return 0

        cu.cuMemFree_v2.side_effect = free
        load._select_device = Mock(return_value=0)
        load._buf_size = Mock(return_value=1)
        start, stop = load.start, load.stop

        def start_worker():
            start()
            if wait_for_cleanup:
                self.assertTrue(cleanup_entered.wait(3))
                self.assertTrue(load.workload_ended.is_set())
                self.assertFalse(load.done.is_set())

        def stop_worker():
            stop()
            release_cleanup.set()

        load.start, load.stop = start_worker, stop_worker
        try:
            with patch.object(gpuload, "available", return_value=(True, "")), \
                    patch.object(gpuload.ctypes, "WinDLL", return_value=cu), \
                    patch.object(gpuload, "_bind"), \
                    patch.object(gpuload, "BandwidthLoad", return_value=load):
                yield cleanup_entered
        finally:
            stop_worker()
            load.join(timeout=3)
            self.assertFalse(load._thread and load._thread.is_alive())

    def induce(self, callback):
        gpu = SimpleNamespace(slot=lambda: "0000:02:00.0",
                              read=lambda: {"mem": 3505, "pstate": 0})
        return gpuload.induce(gpu, on_settled=callback, poll=0)

    def test_real_hammer_deadline_prevents_callback_during_blocked_cleanup(self):
        load = gpuload.BandwidthLoad(max_seconds=0, slot="0000:02:00.0")
        callback = Mock()
        with self.mocked_cuda(load, wait_for_cleanup=True):
            result = self.induce(callback)
        callback.assert_not_called()
        self.assertEqual(result["stats"]["copies"], 0)
        self.assertTrue(result["stats"]["hit_deadline"])
        self.assertIn("ended before the callback", result["error"])
        self.assertTrue(load.done.is_set())

    def test_subclass_hammer_ending_during_callback_signals_before_cleanup(self):
        finish = threading.Event()

        class CheckedLoad(gpuload.BandwidthLoad):
            def _hammer(self, *args):
                self.started.set()
                if not finish.wait(3):
                    raise RuntimeError("mock workload was not released")

        load = CheckedLoad(slot="0000:02:00.0")
        steps = []
        with self.mocked_cuda(load) as cleanup_entered:
            def callback():
                try:
                    steps.append("write")
                    finish.set()
                    self.assertTrue(cleanup_entered.wait(3))
                    self.assertTrue(load.workload_ended.is_set())
                    self.assertFalse(load.done.is_set())
                finally:
                    steps.append("restored")
                return "restored"
            result = self.induce(callback)
        self.assertEqual(steps, ["write", "restored"])
        self.assertEqual(result["result"], "restored")
        self.assertIn("ended before the callback finished", result["error"])

    def test_caller_stop_after_callback_is_still_successful(self):
        class CheckedLoad(gpuload.BandwidthLoad):
            def _hammer(self, *args):
                self.started.set()
                if not self._stop.wait(3):
                    raise RuntimeError("mock workload was not stopped")

        load = CheckedLoad(slot="0000:02:00.0")
        callback = Mock(return_value="captured under load")
        with self.mocked_cuda(load):
            result = self.induce(callback)
        callback.assert_called_once()
        self.assertEqual(result["error"], "")
        self.assertEqual(result["result"], "captured under load")
        self.assertTrue(load.workload_ended.is_set())
        self.assertTrue(load.done.is_set())


if __name__ == "__main__":
    unittest.main()
