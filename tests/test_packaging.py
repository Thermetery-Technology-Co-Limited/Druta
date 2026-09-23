# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build real distributions in a temporary checkout; never access GPU hardware."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("setuptools"), "install the dev extra")
class DistributionAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.work = Path(cls.temporary.name).resolve()
        cls.source = cls.work / "source"
        cls.source.mkdir()
        # Include only public manifest inputs, not checkout state or profiles.
        for line in (ROOT / "MANIFEST.in").read_text().splitlines():
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            if parts[0] == "include":
                paths = [p for pattern in parts[1:] for p in ROOT.glob(pattern)]
            elif parts[0] == "recursive-include":
                paths = [p for pattern in parts[2:]
                         for p in (ROOT / parts[1]).rglob(pattern)]
            else:
                raise AssertionError(f"Unsupported fixture directive: {line}")
            for path in paths:
                destination = cls.source / path.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        cls.expected_assets = {
            "COPYING": (ROOT / "COPYING").read_bytes(),
            "THIRD-PARTY-NOTICES.md": (ROOT / "THIRD-PARTY-NOTICES.md").read_bytes(),
        }
        for suffix in ("*.toml", "*.md"):
            cls.expected_assets.update({
                path.relative_to(ROOT).as_posix(): path.read_bytes()
                for path in (ROOT / "i2c").glob(suffix)
            })
        # New public profiles must be picked up without editing setup.py.
        cls.expected_assets["i2c/packaging-extra.toml"] = b"# packaging fixture\n"
        (cls.source / "i2c/packaging-extra.toml").write_bytes(
            cls.expected_assets["i2c/packaging-extra.toml"])
        cls.output = cls.work / "packages"
        cls.output.mkdir()
        cls.run_build(cls.source, "build_sdist")
        cls.run_build(cls.source, "build_wheel")
        cls.direct_wheel = next(cls.output.glob("*.whl"))
        cls.sdist = next(cls.output.glob("*.tar.gz"))

    @classmethod
    def run_build(cls, source, command):
        result = subprocess.run(
            [sys.executable, "-c",
             f"from setuptools.build_meta import {command}; {command}({str(cls.output)!r})"],
            cwd=source, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def assert_wheel_assets(self, wheel):
        with zipfile.ZipFile(wheel) as archive:
            for name, expected in self.expected_assets.items():
                self.assertEqual(archive.read("druta/_data/" + name), expected, name)
            self.assertIn("druta/__main__.py", archive.namelist())

    def test_wheel_contains_canonical_assets_and_runtime_entrypoint(self):
        self.assert_wheel_assets(self.direct_wheel)

    def test_sdist_can_rebuild_a_wheel_with_the_same_assets(self):
        unpacked = self.work / "unpacked"
        unpacked.mkdir()
        with tarfile.open(self.sdist) as archive:
            for member in archive.getmembers():
                self.assertTrue((unpacked / member.name).resolve().is_relative_to(unpacked))
            # The archive was just built locally and all member paths were checked.
            archive.extractall(unpacked)
        source = next(unpacked.iterdir())
        self.assertTrue((source / "druta.py").is_file())
        self.assertTrue((source / "src/run_druta.py").is_file())
        self.assertTrue((source / "tests/__init__.py").is_file())
        self.assertTrue((source / "build.ps1").is_file())
        self.run_build(source, "build_wheel")
        self.assert_wheel_assets(self.direct_wheel)

    def test_installed_wheel_reads_assets_away_from_checkout(self):
        installed = self.work / "installed"
        installed.mkdir()
        with zipfile.ZipFile(self.direct_wheel) as archive:
            archive.extractall(installed)
        env = dict(os.environ, PYTHONPATH=str(installed))
        subprocess.run(
            [sys.executable, "-c",
             "from pathlib import Path; from druta.paths import resource_path; "
             "assert 'GNU GENERAL PUBLIC LICENSE' in Path(resource_path('COPYING')).read_text(); "
             "assert Path(resource_path('THIRD-PARTY-NOTICES.md')).is_file(); "
             "assert Path(resource_path('i2c')).joinpath('packaging-extra.toml').is_file()"],
            cwd=self.work, env=env, check=True, capture_output=True, text=True, timeout=15)


if __name__ == "__main__":
    unittest.main()
