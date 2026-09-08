"""Windows 7 regressions. These tests never construct a GPU backend."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import wincompat


class DpiTests(unittest.TestCase):
    def libraries(self, modern=None, legacy=144, dc=0x123456789):
        user32 = SimpleNamespace(SetProcessDPIAware=Mock(return_value=1),
                                 GetDC=Mock(return_value=dc),
                                 ReleaseDC=Mock(return_value=1))
        if modern is not None:
            user32.GetDpiForSystem = Mock(return_value=modern)
        gdi32 = SimpleNamespace(GetDeviceCaps=Mock(return_value=legacy))

        def load(name, **kwargs):
            if name == "user32":
                return user32
            if name == "gdi32":
                return gdi32
            raise OSError("shcore does not exist on Windows 7")

        return load, user32, gdi32

    def test_windows_7_uses_desktop_dpi_and_releases_full_width_handle(self):
        load, user32, gdi32 = self.libraries()
        self.assertEqual(wincompat.dpi_scale(load), 1.5)
        user32.SetProcessDPIAware.assert_called_once_with()
        gdi32.GetDeviceCaps.assert_called_once_with(0x123456789, 88)
        user32.ReleaseDC.assert_called_once_with(None, 0x123456789)
        self.assertIs(user32.GetDC.restype, ctypes.c_void_p)
        self.assertEqual(gdi32.GetDeviceCaps.argtypes,
                         [ctypes.c_void_p, ctypes.c_int])
        self.assertEqual(user32.ReleaseDC.argtypes,
                         [ctypes.c_void_p, ctypes.c_void_p])

    def test_modern_dpi_does_not_allocate_a_dc(self):
        load, user32, _ = self.libraries(modern=192)
        self.assertEqual(wincompat.dpi_scale(load), 2.0)
        user32.GetDC.assert_not_called()

    def test_zero_modern_dpi_falls_back(self):
        load, user32, _ = self.libraries(modern=0, legacy=120)
        self.assertEqual(wincompat.dpi_scale(load), 1.25)
        user32.ReleaseDC.assert_called_once()

    def test_null_dc_is_not_queried_or_released(self):
        load, user32, gdi32 = self.libraries(dc=None)
        self.assertEqual(wincompat.dpi_scale(load), 1.0)
        gdi32.GetDeviceCaps.assert_not_called()
        user32.ReleaseDC.assert_not_called()

    def test_failed_caps_read_still_releases_dc(self):
        load, user32, gdi32 = self.libraries()
        gdi32.GetDeviceCaps.side_effect = OSError("display unavailable")
        self.assertEqual(wincompat.dpi_scale(load), 1.0)
        user32.ReleaseDC.assert_called_once_with(None, 0x123456789)

    def test_missing_windows_libraries_use_default_scale(self):
        self.assertEqual(wincompat.dpi_scale(Mock(side_effect=OSError())), 1.0)


class LegacyTomlTests(unittest.TestCase):
    def test_profiles_load_when_stdlib_tomllib_is_unavailable(self):
        try:
            import tomllib as parser
        except ImportError:
            import tomli as parser
        source = Path(__file__).resolve().parents[1] / "railctl.py"
        spec = importlib.util.spec_from_file_location("railctl_legacy", source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"tomllib": None, "tomli": parser}):
            spec.loader.exec_module(module)
        errors = []
        loaded = module.load_profiles(log=lambda text, *_: errors.append(text))
        self.assertTrue(loaded)
        self.assertEqual(errors, [])


class SmokeTests(unittest.TestCase):
    def setUp(self):
        import druta
        self.druta = druta
        self.dpg = MagicMock()
        self.dpg.get_dearpygui_version.return_value = "test-renderer"
        self.dpg.is_dearpygui_running.return_value = True
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = os.path.join(self.tmp.name, "smoke.json")
        for name, value in (("dpg", self.dpg),
                            ("GPU", Mock(side_effect=AssertionError("GPU access"))),
                            ("enumerate_gpus", Mock(side_effect=AssertionError("GPU access"))),
                            ("dpi_scale", Mock(return_value=1.5))):
            patcher = patch.object(druta, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def report(self):
        with open(self.output, encoding="utf-8") as f:
            return json.load(f)

    def test_smoke_renders_three_frames_without_gpu_access_or_modal_output(self):
        with patch.object(self.druta, "_tell") as tell:
            result = self.druta.main(["--smoke-test", "--smoke-output", self.output])
        self.assertEqual(result, 0)
        self.assertEqual(self.report()["status"], "passed")
        self.assertEqual(self.report()["rendered_frames"], 3)
        self.assertEqual(self.dpg.render_dearpygui_frame.call_count, 3)
        self.dpg.destroy_context.assert_called_once()
        tell.assert_not_called()

    def test_renderer_failure_reports_failure_and_cleans_up(self):
        self.dpg.render_dearpygui_frame.side_effect = RuntimeError("D3D unavailable")
        result = self.druta.main(["--smoke-test", "--smoke-output", self.output])
        self.assertEqual(result, 1)
        self.assertEqual(self.report()["status"], "failed")
        self.assertIn("D3D unavailable", self.report()["error"])
        self.dpg.destroy_context.assert_called_once()

    def test_gpu_selection_cannot_be_combined_with_smoke_mode(self):
        with patch.object(self.druta, "_tell"):
            result = self.druta.main(["--smoke-test", "--gpu", "0000:01:00.0"])
        self.assertEqual(result, 2)
        self.dpg.create_context.assert_not_called()

    def test_missing_profiles_fail_before_renderer_initialization(self):
        with patch.object(self.druta.railctl, "load_profiles", return_value=[]):
            result = self.druta.main(["--smoke-test", "--smoke-output", self.output])
        self.assertEqual(result, 1)
        self.assertIn("No I2C profiles", self.report()["error"])
        self.dpg.create_context.assert_not_called()

    def test_unwritable_report_cannot_open_modal_output(self):
        with patch.object(self.druta, "_tell") as tell, \
                patch.object(sys, "stderr", None), patch.object(sys, "stdout", None):
            result = self.druta.main(["--smoke-test", "--smoke-output", self.tmp.name])
        self.assertEqual(result, 1)
        self.dpg.create_context.assert_not_called()
        tell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
