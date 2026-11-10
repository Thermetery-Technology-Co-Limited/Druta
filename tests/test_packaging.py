# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build real distributions in a temporary checkout; never access GPU hardware."""
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from druta import native, native_packaging

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
        self.assertTrue((source / "native/CMakeLists.txt").is_file())
        self.assertTrue((source / "native/include/drutacore/drutacore.h").is_file())
        self.assertTrue((source / "native/src/core/abi.cpp").is_file())
        self.assertTrue((source / "native/tools/xp-imports.txt").is_file())
        self.assertTrue((source / "native/cmake/mingw-i686.cmake").is_file())
        self.assertFalse((source / "native/build").exists())
        self.assertTrue((source / "RELEASE-NOTES-1.9.0a.md").is_file())
        self.assertTrue((source / "RELEASE-NOTES-1.9.0b.md").is_file())
        evidence = Path("experiments/ui-recovery-result-qualified-20261029.json")
        self.assertEqual((source / evidence).read_bytes(), (ROOT / evidence).read_bytes())
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


class NativeBundle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def dll(self, relative, bits):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = bytearray(512)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 60, 128)
        data[128:132] = b"PE\0\0"
        struct.pack_into("<H", data, 132, 0x8664 if bits == 64 else 0x014C)
        struct.pack_into("<HHH", data, 148, 240 if bits == 64 else 224, 0x2000,
                         0x020B if bits == 64 else 0x010B)
        path.write_bytes(data)
        return path

    def test_selects_the_matching_cross_build(self):
        x86 = self.dll("native/build/mingw-i686/drutacore.dll", 32)
        x64 = self.dll("native/build/mingw-x86_64/drutacore.dll", 64)
        self.assertEqual(native_packaging.native_binary(self.root, bits=32), x86)
        self.assertEqual(native_packaging.native_binary(self.root, bits=64), x64)

    def test_source_loader_discovers_build_script_release_output(self):
        bits = struct.calcsize("P") * 8
        core = self.dll(f"native/build/msvc-{bits}/Release/drutacore.dll", bits)
        with patch.object(sys, "platform", "win32"), \
                patch("druta.paths.source_root", return_value=self.root):
            self.assertIn(core, native.candidate_paths({}))

    def test_build_helper_runs_as_file_with_space_containing_paths(self):
        # PowerShell 5.1's native argument handling can strip quotes inside
        # Python -c text. The build invokes this file with plain path arguments.
        script = ROOT / "src/druta/native_packaging.py"
        bits = struct.calcsize("P") * 8
        result = subprocess.run([sys.executable, str(script), "--bits"],
                                capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(result.stdout.strip(), str(bits))
        core = self.dll("native build with spaces/drutacore.dll", bits)
        result = subprocess.run([sys.executable, str(script), str(self.root), str(core)],
                                capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(result.stdout.strip(), str(core))

    def test_explicit_core_cannot_fall_through_on_machine_mismatch(self):
        wrong = self.dll("selected/drutacore.dll", 32)
        self.dll("native/build/mingw-x86_64/drutacore.dll", 64)
        with self.assertRaisesRegex(ValueError, "does not match 64-bit"):
            native_packaging.native_binary(self.root, wrong, bits=64)

    def test_missing_or_malformed_core_is_a_build_error(self):
        with self.assertRaisesRegex(FileNotFoundError, "Run build.ps1"):
            native_packaging.native_binary(self.root, bits=64)
        broken = self.root / "drutacore.dll"
        broken.write_bytes(b"not a DLL")
        with self.assertRaisesRegex(ValueError, "Not a Windows DLL"):
            native_packaging.native_binary(self.root, broken, bits=64)
        broken = self.dll("truncated/drutacore.dll", 64)
        broken.write_bytes(broken.read_bytes()[:154])
        with self.assertRaisesRegex(ValueError, "Expected an x86 or x64 PE DLL"):
            native_packaging.windows_machine(broken)

    def test_spec_bundles_validated_core_at_frozen_loader_location(self):
        import types
        core = self.dll("native/build/msvc-64/Release/drutacore.dll", struct.calcsize("P") * 8)
        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hooks.collect_all = lambda name: ([], [], [])
        captured = {}

        def analysis(*args, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(pure=[], scripts=[], binaries=kwargs["binaries"], datas=[])

        namespace = {"SPECPATH": str(ROOT), "Analysis": analysis, "PYZ": lambda *a: None,
                     "EXE": lambda *a, **kw: None, "COLLECT": lambda *a, **kw: None}
        with patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}), \
                patch.dict(os.environ, {"DRUTA_NATIVE_LIB": str(core)}):
            exec(compile((ROOT / "Druta.spec").read_text(), str(ROOT / "Druta.spec"), "exec"), namespace)
        self.assertIn((str(core), "."), captured["binaries"])
        bundle = self.root / "bundle/_internal"
        bundle.mkdir(parents=True)
        bundled = bundle / "drutacore.dll"
        shutil.copy2(core, bundled)
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", str(bundle), create=True), \
                patch.object(sys, "platform", "win32"), \
                patch.object(sys, "executable", str(bundle.parent / "Druta.exe")):
            self.assertIn(bundled, native.candidate_paths({}))


if __name__ == "__main__":
    unittest.main()
