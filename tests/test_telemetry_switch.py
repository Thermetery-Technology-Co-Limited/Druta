# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""A delayed driver read must not cross a GPU switch (no hardware needed)."""

import threading
import unittest
from types import SimpleNamespace

from druta.druta import Druta


class TelemetrySwitchTests(unittest.TestCase):
    def run_read(self, *, fail=False, switch=False, round_trip=False,
                 rebuilding=False):
        started, finish = threading.Event(), threading.Event()
        app = Druta.__new__(Druta)
        app._lock = threading.Lock()
        app._stop = threading.Event()
        app._gpu_gen = 1
        app._rebuilding = False
        app._snap = {"core": 300, "mem": 405}
        app._snap_err = None
        app._snap_t = 12.0

        def read():
            started.set()
            if not finish.wait(2):
                raise TimeoutError("test did not release the driver read")
            app._stop.set()
            if fail:
                raise RuntimeError("old GPU stopped responding")
            return {"core": 900, "mem": 3004}

        original = app.gpu = SimpleNamespace(read=read)
        worker = threading.Thread(target=app.poll_loop, daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(2), "driver read did not start")
            if switch:
                with app._lock:
                    app._gpu_gen += 1
                    app.gpu = SimpleNamespace()
                    # A second switch can return to the original object. Its
                    # pre-switch observation is still stale by generation.
                    if round_trip:
                        app._gpu_gen += 1
                        app.gpu = original
            app._rebuilding = rebuilding
        finally:
            finish.set()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "telemetry worker did not stop")
        return app

    def test_previous_gpu_result_and_error_cannot_replace_current_snapshot(self):
        for fail in (False, True):
            for round_trip in (False, True):
                with self.subTest(fail=fail, round_trip=round_trip):
                    app = self.run_read(fail=fail, switch=True,
                                        round_trip=round_trip)
                    self.assertEqual(app._snap, {"core": 300, "mem": 405})
                    self.assertIsNone(app._snap_err)
                    self.assertEqual(app._snap_t, 12.0)

    def test_result_during_rebuild_is_discarded(self):
        app = self.run_read(rebuilding=True)
        self.assertEqual(app._snap, {"core": 300, "mem": 405})
        self.assertEqual(app._snap_t, 12.0)

    def test_current_gpu_result_is_published(self):
        app = self.run_read()
        self.assertEqual(app._snap, {"core": 900, "mem": 3004})
        self.assertIsNone(app._snap_err)
        self.assertGreater(app._snap_t, 12.0)

    def test_current_gpu_error_is_published(self):
        app = self.run_read(fail=True)
        self.assertEqual(app._snap_err, "old GPU stopped responding")
        self.assertEqual(app._snap_t, 12.0)


if __name__ == "__main__":
    unittest.main()
