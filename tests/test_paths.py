# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve existing files across the src-layout move without GPU access."""
import importlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from druta import paths, profiles, railctl, shuntmod, startup, timings, timingwrite


class ApplicationPaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.recipe = paths.resource_path("i2c") / "rtx2080ti-mp2888a.toml"
        # Restore the real module constant after all mocked locations are undone.
        self.addCleanup(importlib.reload, profiles)
        self.mock(paths.sys, "frozen", False, create=True)

    def mock(self, obj, name, value, **kwargs):
        replacement = patch.object(obj, name, value, **kwargs)
        replacement.start()
        self.addCleanup(replacement.stop)

    def source(self):
        root = self.base / "checkout"
        package = root / "src" / "druta"
        package.mkdir(parents=True)
        (package / "__init__.py").touch()
        (root / "pyproject.toml").touch()
        self.mock(paths, "__file__", str(package / "paths.py"))
        return root

    def installed(self):
        package = self.base / "venv" / "Lib" / "site-packages" / "druta"
        package.mkdir(parents=True)
        self.mock(paths, "__file__", str(package / "paths.py"))
        self.mock(paths.sys, "executable", str(self.base / "venv" / "Scripts" / "python.exe"))
        return package

    def frozen(self):
        executable = self.base / "portable" / "Druta.exe"
        bundle = executable.parent / "_internal"
        bundle.mkdir(parents=True)
        self.mock(paths.sys, "frozen", True)
        self.mock(paths.sys, "executable", str(executable))
        self.mock(paths.sys, "_MEIPASS", str(bundle), create=True)
        self.mock(paths, "__file__", str(bundle / "druta" / "paths.py"))
        return executable.parent, bundle

    def store_profile(self, directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        state = {"saved_at": "2026-09-07", "saved_ts": 1, "vf_deltas": {"0": 0}}
        (directory / (name + ".json")).write_text(json.dumps(state), encoding="utf-8")
        return state

    def test_source_location_and_assets_are_independent_of_working_directory(self):
        root = self.source()
        unrelated = self.base / "elsewhere"
        unrelated.mkdir()
        for name in ("COPYING", "THIRD-PARTY-NOTICES.md"):
            (root / name).write_text(name, encoding="utf-8")
        before = Path.cwd()
        try:
            os.chdir(unrelated)
            self.assertEqual(paths.source_root(), root)
            self.assertEqual(paths.app_dir(), root)
            self.assertEqual(railctl.profile_dirs(), [str(root / "i2c")])
            for name in ("COPYING", "THIRD-PARTY-NOTICES.md"):
                self.assertEqual(paths.resource_path(name).read_text(encoding="utf-8"), name)
        finally:
            os.chdir(before)

    def test_source_layout_requires_project_markers(self):
        root = self.source()
        (root / "pyproject.toml").unlink()
        self.assertIsNone(paths.source_root())

    def test_source_profiles_and_undo_snapshots_remain_loadable(self):
        root = self.source()
        directory = root / "profiles"
        expected = self.store_profile(directory, "existing-tune")
        self.store_profile(directory, "autosave-existing")
        importlib.reload(profiles)
        self.assertEqual(Path(profiles.DIR), directory)
        self.assertEqual(profiles.load("existing-tune"), expected)
        self.assertEqual({row[0]: row[3] for row in profiles.list_profiles()},
                         {"existing-tune": False, "autosave-existing": True})
        profiles.save("new-tune", expected)
        self.assertTrue((directory / "new-tune.json").is_file())
        self.assertFalse((root / "src" / "druta" / "profiles").exists())

    def test_source_nvtune_discovery_keeps_the_repository_root_first(self):
        root = self.source()
        portable = root / "nvtune.exe"
        portable.touch()  # A file-presence probe only; it is never executed.
        with patch.object(timings, "configured_exe", return_value=None), \
                patch.object(timings, "pinned_exe", return_value=None), \
                patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(timings.find_exe(), str(portable))

    def test_frozen_profiles_keep_the_old_bundle_root(self):
        _, bundle = self.frozen()
        expected = self.store_profile(bundle / "profiles", "existing-tune")
        self.store_profile(bundle / "profiles", "autosave-existing")
        importlib.reload(profiles)
        self.assertEqual(Path(profiles.DIR), bundle / "profiles")
        self.assertEqual(profiles.load("existing-tune"), expected)
        self.assertEqual(len(profiles.list_profiles()), 2)
        self.assertFalse((bundle / "druta" / "profiles").exists())

    def test_frozen_assets_and_portable_executable_use_the_original_locations(self):
        portable, bundle = self.frozen()
        self.assertIsNone(paths.source_root())
        self.assertEqual(paths.app_dir(), portable)
        self.assertEqual(timings._app_dir(), str(portable))
        self.assertEqual(paths.resource_path("COPYING"), bundle / "COPYING")
        self.assertEqual(paths.resource_path("THIRD-PARTY-NOTICES.md"),
                         bundle / "THIRD-PARTY-NOTICES.md")

    def test_frozen_user_i2c_recipe_takes_precedence_over_bundled_copy(self):
        portable, bundle = self.frozen()
        for directory in (portable / "i2c", bundle / "i2c"):
            directory.mkdir()
            shutil.copyfile(self.recipe, directory / "same.toml")
        self.assertEqual(railctl.profile_dirs(),
                         [str(portable / "i2c"), str(bundle / "i2c")])
        loaded = railctl.load_profiles()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(Path(loaded[0].path), portable / "i2c" / "same.toml")
        (portable / "i2c" / "same.toml").unlink()
        self.assertEqual(Path(railctl.load_profiles()[0].path), bundle / "i2c" / "same.toml")

    def test_installed_assets_load_from_package_data(self):
        package = self.installed()
        data = package / "_data"
        (data / "i2c").mkdir(parents=True)
        shutil.copyfile(self.recipe, data / "i2c" / "controller.toml")
        for name in ("COPYING", "THIRD-PARTY-NOTICES.md"):
            (data / name).write_text(name, encoding="utf-8")
            self.assertEqual(paths.resource_path(name).read_text(encoding="utf-8"), name)
        self.assertIsNone(paths.source_root())
        self.assertEqual(railctl.profile_dirs(), [str(data / "i2c")])
        self.assertEqual(len(railctl.load_profiles()), 1)

    def test_installed_profiles_are_writable_and_stable_outside_site_packages(self):
        package = self.installed()
        local = self.base / "user-data"
        directory = local / "Thermetery" / "Druta" / "profiles"
        expected = self.store_profile(directory, "existing-tune")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            importlib.reload(profiles)
            self.assertEqual(Path(profiles.DIR), directory)
            self.assertEqual(profiles.load("existing-tune"), expected)
            profiles.save("new-tune", expected)
            self.assertTrue((directory / "new-tune.json").is_file())
            self.mock(paths.sys, "executable", str(self.base / "other-venv" / "python.exe"))
            self.assertEqual(paths.profile_dir(), directory)
            self.assertFalse((package / "profiles").exists())

    def test_installed_home_fallback_does_not_create_data_on_import(self):
        self.installed()
        home = self.base / "home"
        with patch.dict(os.environ, {"LOCALAPPDATA": ""}), patch.object(Path, "home", return_value=home):
            importlib.reload(profiles)
            self.assertEqual(paths.profile_dir(), home / "Thermetery" / "Druta" / "profiles")
            self.assertFalse(home.exists())

    def test_existing_settings_and_stock_backup_locations_do_not_change(self):
        self.installed()
        local = self.base / "user-data"
        gpu = SimpleNamespace(static={"name": "Example GPU", "vbios": "1.2", "uuid": "GPU-123"})
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            self.assertEqual(Path(timings.config_path()), local / "Thermetery" / "Druta" / "nvtune.json")
            self.assertEqual(Path(timings.legacy_config_path()), local / "TitanTune" / "nvtune.json")
            self.assertEqual(Path(shuntmod.config_path()), local / "Thermetery" / "Druta" / "shuntmod.json")
            self.assertEqual(startup.state_dir(), local / "Druta")
            self.assertEqual(Path(timingwrite.card_backup_path(gpu)),
                             local / "nvtune" / "Example-GPU-1-2-GPU-123.stock.json")
            self.assertEqual(Path(timingwrite.legacy_backup_path(gpu)),
                             local / "nvtune" / "Example-GPU-1-2.stock.json")
        self.assertFalse(local.exists())


if __name__ == "__main__":
    unittest.main()
