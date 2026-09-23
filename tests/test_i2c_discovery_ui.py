"""Opt-in, selected-GPU I2C regulator discovery UI behavior."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from druta import druta, railctl
from druta.druta import Druta
from tests.test_arch_ui_regressions import FakeUiTest


class I2cDiscoveryUiTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.nvapi = SimpleNamespace(ok=True)
        self.gpu = SimpleNamespace(nvapi=self.nvapi, arch=lambda: 10)
        self.app.gpu = self.gpu
        self.app._lock = threading.Lock()
        self.app._gpu_gen = 3
        self.app._closing = False
        self.app.rail = None
        self.app._rail_candidates = []
        self.app._i2c_recovery_for = None
        self.app._i2c_verified = False
        self.app._i2c_verified_for = None
        self.app._i2c_busy = False
        self.app._i2c_scan_busy = False
        self.app._i2c_scan_thread = None
        self.app._i2c_scan_cancel = threading.Event()
        self.app._i2c_scan_token = 0
        self.app._i2c_scan_result = None
        self.app._i2c_scan_started = 0.0
        self.app._i2c_scan_status = ""
        self.app._i2c_scan_work = (0, 0, "")
        self.app._i2c_discovery_complete = False
        self.app._profile_pending = None
        self.app._profile_applying = False
        self.app.refresh_i2c_candidates = Mock()

    def test_launch_constructor_does_not_scan_i2c(self):
        gpu = SimpleNamespace()
        with patch.object(druta, "GPU", return_value=gpu), \
                patch.object(druta, "enumerate_gpus", return_value=[]), \
                patch.object(druta.railctl, "discover") as discover:
            app = Druta()
        discover.assert_not_called()
        self.assertIsNone(app.rail)
        self.assertFalse(app._i2c_discovery_complete)

    def test_checking_i2c_requests_scan_and_unchecking_cancels_it(self):
        self.values["i2c_regulator_header"] = False
        self.values["i2c_mode"] = True
        self.values["risk_banner"] = ""
        self.app._risk_themes = {}
        self.app._rail_writable = set()
        self.app.gpu.read_volt_rail_limits = Mock(return_value={})
        self.app.ov_carryover = Mock()
        self.app.refresh_current_limits = Mock()
        self.app.sync_slider_ranges = Mock()
        self.app.start_i2c_discovery = Mock(return_value=True)
        self.app.on_i2c_mode(app_data=True)
        self.app.start_i2c_discovery.assert_called_once_with()
        self.ui.configure_item.assert_any_call("i2c_regulator_header", default_open=True)
        self.assertTrue(self.values["i2c_mode"])
        self.assertEqual(self.app.risk_features(), set())

        self.app.start_i2c_discovery.reset_mock()
        self.app._i2c_scan_busy = True
        cancel = self.app._i2c_scan_cancel
        self.app.on_i2c_mode(app_data=False)
        self.app.start_i2c_discovery.assert_not_called()
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.app._i2c_discovery_complete)
        self.app.refresh_i2c_candidates.assert_called_once_with()

    def test_worker_completion_publishes_candidates_and_progress_without_writes(self):
        candidate = SimpleNamespace()
        self.app._i2c_scan_busy = True
        self.app._i2c_scan_token = 7
        self.values["i2c_mode"] = True
        with patch.object(druta.railctl, "discover", return_value=[candidate]) as discover:
            self.app._i2c_discovery_worker(7, 3, self.gpu, self.nvapi, 10,
                                            threading.Event())
        discover.assert_called_once()
        progress = discover.call_args.kwargs["progress"]
        progress(2, 5, "NCP4206 port 2, 0x20")
        self.assertEqual(self.app._i2c_scan_work, (2, 5, "NCP4206 port 2, 0x20"))
        self.app.poll_i2c_discovery()
        self.assertTrue(self.app._i2c_discovery_complete)
        self.assertIs(self.app.rail, candidate)
        self.assertEqual(self.app._rail_candidates, [candidate])
        self.assertFalse(self.app._i2c_scan_busy)
        self.app.refresh_i2c_candidates.assert_called_once_with()

    def test_scan_error_and_stale_gpu_result_are_not_published(self):
        self.values["i2c_mode"] = True
        self.app._i2c_scan_busy = True
        self.app._i2c_scan_token = 8
        with patch.object(druta.railctl, "discover", side_effect=RuntimeError("bus lost")):
            self.app._i2c_discovery_worker(8, 3, self.gpu, self.nvapi, 10,
                                            threading.Event())
        self.app.poll_i2c_discovery()
        self.assertFalse(self.app._i2c_discovery_complete)
        self.assertIsNone(self.app.rail)
        self.assertIn("bus lost", self.app.log.call_args.args[0])

        old = SimpleNamespace()
        self.app._i2c_scan_busy = True
        self.app._i2c_scan_token = 9
        self.app._i2c_scan_result = (9, 3, old, False, [SimpleNamespace()], None)
        self.app._gpu_gen = 4
        self.app.poll_i2c_discovery()
        self.assertFalse(self.app._i2c_discovery_complete)
        self.assertIsNone(self.app.rail)

    def test_cancelling_worker_stays_reserved_until_its_result_is_polled(self):
        self.app._i2c_scan_busy = True
        self.app._i2c_scan_token = 11
        worker = Mock()
        worker.is_alive.return_value = True
        self.app._i2c_scan_thread = worker
        self.app.cancel_i2c_discovery(clear=True)
        self.assertTrue(self.app._i2c_scan_cancel.is_set())
        self.assertTrue(self.app._i2c_scan_busy)
        self.assertIs(self.app._i2c_scan_thread, worker)
        self.assertFalse(self.app.start_i2c_discovery())

        self.app._i2c_scan_result = (11, 3, self.gpu, True, [], None)
        self.app.poll_i2c_discovery()
        self.assertFalse(self.app._i2c_scan_busy)
        self.assertIsNone(self.app._i2c_scan_thread)

    def test_i2c_gate_and_profiles_require_completed_manual_detection(self):
        ok, reason = self.app.i2c_gate()
        self.assertFalse(ok)
        self.assertIn("detection has not completed", reason)

        self.app.guard = Mock(return_value=True)
        self.app.profile_failure = Mock()
        with patch.object(druta.profiles, "load", return_value={"i2c": {"port": 1}}), \
                patch.object(druta.profiles, "preflight") as preflight:
            self.app.load_profile("I2C tune")
        preflight.assert_not_called()
        self.app.profile_failure.assert_called_once()
        self.assertIn("detection has not completed", self.app.profile_failure.call_args.args[0])

    def test_discovery_reports_real_probe_units_and_stops_at_next_boundary(self):
        events = []
        ncp = SimpleNamespace(present=lambda: False,
                              discovery_diagnostics={"model": None})
        nvapi = SimpleNamespace(ok=True, selected={})
        with patch.object(railctl, "load_profiles", return_value=[]), \
                patch("druta.controllers.ncp4206.NCP4206", return_value=ncp) as probe, \
                patch("druta.controllers.nct3933.DISCOVERY_PORTS", ()), \
                patch("druta.controllers.ncp4206.DISCOVERY_PORTS", (2,)), \
                patch("druta.controllers.ncp4206.DISCOVERY_ADDRESSES", (0x20, 0x21)):
            railctl.discover(nvapi, progress=lambda *event: events.append(event),
                             cancelled=lambda: len(events) >= 2)
        self.assertEqual(events[0], (0, 2, "Preparing controller probes"))
        self.assertEqual(events[1], (1, 2, "NCP4206 port 2, 0x20"))
        probe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
