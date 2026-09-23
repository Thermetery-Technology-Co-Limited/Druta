# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise package/source launchers and lazy imports without opening a GPU."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from druta import druta, gpuload, paths, startup
from druta.tools import probe_volt_rails, probe_volt_rails_47212


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        temporary = tempfile.TemporaryDirectory(prefix="druta unrelated cwd ")
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.environment = dict(os.environ)
        self.environment.pop("PYTHONPATH", None)

    def assert_version(self, command, environment=None, cwd=None):
        result = subprocess.run(command, cwd=self.directory if cwd is None else cwd,
                                env=self.environment if environment is None else environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Druta " + druta.__version__, result.stdout)

    def test_raw_checkout_bootstrap_runs_from_an_unrelated_directory(self):
        self.assert_version([sys.executable, str(self.root / "druta.py"), "--version"])

    def test_module_entrypoint_from_checkout_root_handles_compatibility_shim(self):
        self.assert_version([sys.executable, "-m", "druta", "--version"], cwd=self.root)

    def test_frozen_source_entrypoint_resolves_package_from_unrelated_cwd(self):
        self.assert_version([sys.executable, str(self.root / "src/run_druta.py"), "--version"])

    def test_compatibility_import_preserves_package_and_submodule_identity(self):
        code = '''
import importlib, pathlib, sys
import druta
from druta import druta as application, startup
original = druta
assert druta is sys.modules['druta']
assert druta.__package__ == 'druta'
assert pathlib.Path(druta.__file__).parent.name == 'druta'
assert application.startup is startup
assert importlib.import_module('druta.druta') is application
assert importlib.reload(druta) is original
assert druta.druta is application
assert druta.startup is startup
assert importlib.import_module('druta') is original
print('package identity preserved')
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=self.root,
                                env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("package identity preserved", result.stdout)

    def test_module_entrypoint_launches_the_application(self):
        environment = dict(self.environment, PYTHONPATH=str(self.root / "src"))
        self.assert_version([sys.executable, "-m", "druta", "--version"], environment)

    def test_second_gpu_command_survives_loss_of_pythonpath_and_original_cwd(self):
        command = druta.Druta.relaunch_argv("0000:02:00.0")
        self.assertEqual(command[-2:], ["--gpu", "0000:02:00.0"])
        self.assertEqual(Path(command[1]), self.root / "druta.py")
        self.assert_version(command[:-2] + ["--version"])

    def test_sign_in_command_survives_loss_of_pythonpath_and_original_cwd(self):
        command = startup.launch_command()
        self.assertEqual(command[-1], "--startup-profile")
        self.assertEqual(Path(command[1]), self.root / "druta.py")
        # pythonw hides stdout; use the same interpreter's console executable
        # and the non-mutating version flag to exercise its exact script target.
        self.assert_version([sys.executable, *command[1:-1], "--version"])

    def test_installed_package_uses_its_module_entrypoint(self):
        with patch("druta.startup.source_root", return_value=None):
            self.assertEqual(startup.application_command("--version"),
                             [sys.executable, "-m", "druta", "--version"])

    def test_frozen_application_uses_its_executable_for_both_launches(self):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", r"C:\portable Druta\Druta.exe"):
            self.assertEqual(druta.Druta.relaunch_argv("0000:02:00.0"),
                             [sys.executable, "--gpu", "0000:02:00.0"])
            self.assertEqual(startup.launch_command(),
                             [sys.executable, "--startup-profile"])

    def test_sign_in_prefers_pythonw_when_it_exists(self):
        with patch("druta.startup.source_root", return_value=None), \
                patch.object(Path, "is_file", return_value=True):
            self.assertEqual(startup.launch_command(),
                             [str(Path(sys.executable).with_name("pythonw.exe")),
                              "-m", "druta", "--startup-profile"])

    def test_missing_source_bootstrap_reports_a_useful_error(self):
        with patch("druta.startup.source_root", return_value=Path(self.directory)):
            with self.assertRaisesRegex(RuntimeError, "source launcher"):
                startup.application_command("--version")

    def test_optional_controller_module_and_resources_are_resolved_in_package(self):
        self.assertIsNotNone(druta.railctl)
        self.assertEqual(druta.railctl.__name__, "druta.railctl")
        self.assertEqual(druta.Druta.resource_path("COPYING"), paths.resource_path("COPYING"))
        self.assertIn("GNU GENERAL PUBLIC LICENSE", druta.Druta.read_licence("COPYING"))


class DiagnosticImportTests(unittest.TestCase):
    def test_legacy_load_probes_resolve_the_packaged_load_class(self):
        # Stop at construction, before any load, clocks, or controller writes.
        gpu = Mock()
        for function in (probe_volt_rails_47212.load_tests,
                         probe_volt_rails_47212.live_field_tests):
            with self.subTest(function=function.__name__), \
                    patch.object(gpuload, "BandwidthLoad", side_effect=RuntimeError("mock load stop")) as load:
                with self.assertRaisesRegex(RuntimeError, "mock load stop"):
                    function(gpu, {}, Mock(), [])
                load.assert_called_once()

    def test_modern_probe_resolves_both_packaged_lazy_imports(self):
        # Both imports precede the first GPU call; stop at that call.
        gpu = Mock()
        gpu.read_vf_curve.side_effect = RuntimeError("mock GPU stop")
        with self.assertRaisesRegex(RuntimeError, "mock GPU stop"):
            probe_volt_rails.test_raised_ceiling(gpu, {}, Mock(), [])
        gpu.read_vf_curve.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
