"""Manual, scoped I2C discovery controls without touching a GPU or cache file."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import dearpygui.dearpygui as real_dpg

from druta import druta
from druta.druta import Druta
from tests.test_arch_ui_regressions import FakeUiTest


class I2cScanControlTests(FakeUiTest):
    def setUp(self):
        super().setUp()
        self.app._i2c_busy = False
        self.app._i2c_scan_busy = False
        self.app._profile_pending = None
        self.app._profile_applying = False
        self.app._i2c_controller_choice = "Unknown -- Full Scan"
        self.app._i2c_scan_token = 4
        self.app._i2c_scan_result = None
        self.app._i2c_scan_work = (0, 0, "")
        self.app._i2c_scan_scope = None
        self.app._closing = False
        self.app.rail = None
        self.app._rail_candidates = []
        self.app._lock = threading.Lock()
        self.app._gpu_gen = 2
        self.values["i2c_mode"] = True

    def test_connect_scan_uses_selected_scope_and_saved_route(self):
        self.app.start_i2c_discovery = Mock(return_value=True)
        self.app._i2c_controller_choice = "NCP4206"

        self.assertTrue(self.app.rescan_i2c())
        self.app.start_i2c_discovery.assert_called_once_with(
            controller="NCP4206", prefer_saved=True)

        self.app.start_i2c_discovery.reset_mock()
        self.app._i2c_controller_choice = "Unknown -- Full Scan"
        self.assertTrue(self.app.rescan_i2c())
        self.app.start_i2c_discovery.assert_called_once_with(
            controller=None, prefer_saved=True)

    def test_full_scan_bypasses_cached_route_and_scope(self):
        self.app.start_i2c_discovery = Mock(return_value=True)
        self.app._i2c_controller_choice = "Nuvoton NCT3933U"

        self.assertTrue(self.app.full_scan_i2c())
        self.app.start_i2c_discovery.assert_called_once_with(
            controller=None, prefer_saved=False)

    def test_scope_selection_rejects_busy_or_unknown_values(self):
        with patch.object(druta.railctl, "controller_names", return_value=["NCP4206"]):
            self.app.select_i2c_controller(app_data="NCP4206")
            self.assertEqual(self.app._i2c_controller_choice, "NCP4206")

            self.app._i2c_busy = True
            self.app.select_i2c_controller(app_data="Unknown -- Full Scan")
            self.assertEqual(self.app._i2c_controller_choice, "NCP4206")
            self.assertEqual(self.values["i2c_controller"], "NCP4206")

            self.app._i2c_busy = False
            self.app.select_i2c_controller(app_data="made up")
            self.assertEqual(self.app._i2c_controller_choice, "NCP4206")
            self.assertEqual(self.values["i2c_controller"], "NCP4206")

    def test_saved_route_is_checked_in_scope_then_falls_back_to_scoped_scan(self):
        gpu = SimpleNamespace()
        nvapi = SimpleNamespace()
        candidate = SimpleNamespace()
        cancel = threading.Event()
        route = {"controller": "NCP4206"}
        with patch.object(druta.i2c_cache, "load_route", return_value=route), \
                patch.object(druta.i2c_cache, "reconnect", return_value=[] ) as reconnect, \
                patch.object(druta.railctl, "discover", return_value=[candidate]) as discover:
            self.app._i2c_discovery_worker(4, 2, gpu, nvapi, 10, cancel,
                                            controller="NCP4206", prefer_saved=True)

        reconnect.assert_called_once()
        self.assertEqual(reconnect.call_args.kwargs["controller"], "NCP4206")
        discover.assert_called_once()
        self.assertEqual(discover.call_args.kwargs["controller"], "NCP4206")
        result = self.app._i2c_scan_result
        self.assertEqual(result[6], "scan")
        self.assertIn("could not be verified", result[7][0])

    def test_valid_saved_route_avoids_scan_and_marks_saved_origin(self):
        gpu = SimpleNamespace()
        nvapi = SimpleNamespace()
        candidate = SimpleNamespace()
        route = {"controller": "Nuvoton NCT3933U"}
        with patch.object(druta.i2c_cache, "load_route", return_value=route), \
                patch.object(druta.i2c_cache, "reconnect", return_value=[candidate]), \
                patch.object(druta.railctl, "discover") as discover:
            self.app._i2c_discovery_worker(4, 2, gpu, nvapi, 10, threading.Event(),
                                            controller="Nuvoton NCT3933U", prefer_saved=True)

        discover.assert_not_called()
        self.assertEqual(self.app._i2c_scan_result[6], "saved")

    def test_corrupt_saved_route_logs_a_note_and_falls_back_to_scan(self):
        gpu = SimpleNamespace()
        nvapi = SimpleNamespace()
        candidate = SimpleNamespace(current_dac=True)
        self.app.gpu = gpu
        self.app._i2c_scan_busy = True
        with patch.object(druta.i2c_cache, "load_route",
                          side_effect=ValueError("bad cache record")), \
                patch.object(druta.railctl, "discover", return_value=[candidate]), \
                patch.object(druta.i2c_cache, "remember", return_value=True):
            self.app._i2c_discovery_worker(4, 2, gpu, nvapi, 10, threading.Event())
            self.app.refresh_i2c_candidates = Mock()
            self.app.update_i2c_scan_ui = Mock()
            self.app.poll_i2c_discovery()

        self.assertTrue(self.app._i2c_discovery_complete)
        self.assertIs(self.app.rail, candidate)
        self.assertTrue(any("Saved I2C route unavailable" in call.args[0]
                            for call in self.app.log.call_args_list))


class I2cScanControlsRenderTests(unittest.TestCase):
    def setUp(self):
        real_dpg.create_context()
        self.addCleanup(real_dpg.destroy_context)
        self.app = Druta.__new__(Druta)
        self.app.s = lambda value: value
        self.app._rail_candidates = []
        self.app.rail = None
        self.app._i2c_controller_choice = "Unknown -- Full Scan"
        self.app._i2c_busy = False
        self.app._i2c_scan_busy = False
        self.app._profile_pending = None
        self.app._profile_applying = False
        self.app._i2c_discovery_complete = False
        self.app._i2c_scan_work = (0, 0, "")
        self.app._i2c_scan_status = ""
        self.app.start_i2c_discovery = Mock(return_value=True)
        with real_dpg.window():
            real_dpg.add_checkbox(tag="i2c_mode", default_value=True)
            with patch.object(druta.railctl, "controller_names", return_value=["NCP4206"]):
                self.app.build_i2c_candidates()

    def test_real_dpg_controls_have_expected_labels_and_dispatch_three_args(self):
        self.assertEqual(real_dpg.get_item_configuration("i2c_rescan")["label"],
                         "Connect / Scan")
        self.assertEqual(real_dpg.get_item_configuration("i2c_full_scan")["label"],
                         "Full scan")
        self.assertEqual(real_dpg.get_value("i2c_controller"), "Unknown -- Full Scan")

        # The production loop calls DPG's signature-aware dispatcher. Calling
        # the callback directly with its three queue values would test a path
        # Druta never uses and reject valid zero-argument button callbacks.
        with patch.object(druta.railctl, "controller_names", return_value=["NCP4206"]):
            real_dpg.run_callbacks([
            [real_dpg.get_item_configuration("i2c_rescan")["callback"],
             "i2c_rescan", None, None],
            [real_dpg.get_item_configuration("i2c_full_scan")["callback"],
             "i2c_full_scan", None, None],
            [real_dpg.get_item_configuration("i2c_controller")["callback"],
             "i2c_controller", "NCP4206", None],
            ])

        self.assertEqual(self.app.start_i2c_discovery.call_args_list, [
            call(controller=None, prefer_saved=True),
            call(controller=None, prefer_saved=False),
        ])
        self.assertEqual(self.app._i2c_controller_choice, "NCP4206")


if __name__ == "__main__":
    unittest.main()
