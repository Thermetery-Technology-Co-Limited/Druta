"""Fail closed before copying changed or incomplete optional driver packages."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from tools import package_vista_nvtune as packager


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "package"
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "Druta.exe").write_bytes(b"Druta fixture")
        for name in packager.PAYLOAD_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode("ascii"))
        with zipfile.ZipFile(str(self.source / "licenses/nvtune/source.zip"), "w") as archive:
            archive.writestr("nvtune-source/LICENSE", "license fixture")
            archive.writestr("nvtune-source/tool/nvtune.c", "source fixture")
        self.manifest = {
            "format": 1,
            "source": {"repository": "https://github.com/sebastianmarrufo/nvtune",
                       "revision": "a" * 40, "archive": "licenses/nvtune/source.zip"},
            "files": [{"name": name, "sha256": hashlib.sha256((self.source / name).read_bytes()).hexdigest()}
                      for name in sorted(packager.PAYLOAD_FILES)],
        }
        self.save()
        pe = MagicMock()
        pe.__enter__.return_value = SimpleNamespace(
            FILE_HEADER=SimpleNamespace(Machine=0x8664),
            OPTIONAL_HEADER=SimpleNamespace(MajorSubsystemVersion=6, MinorSubsystemVersion=0))
        mocked = patch.object(packager.pefile, "PE", return_value=pe)
        mocked.start()
        self.addCleanup(mocked.stop)

    def save(self):
        (self.source / packager.MANIFEST).write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_complete_package_preserves_exact_bytes_and_can_repeat(self):
        self.assertEqual(packager.collect(self.source, self.bundle), self.manifest)
        for name in packager.PAYLOAD_FILES | {packager.MANIFEST}:
            self.assertEqual((self.source / name).read_bytes(), (self.bundle / name).read_bytes())
        self.assertEqual(packager.collect(self.source, self.bundle), self.manifest)

    def test_changed_driver_cannot_copy_any_payload(self):
        (self.source / "nvtune-driver/nvtunedrv.sys").write_bytes(b"changed driver")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            packager.collect(self.source, self.bundle)
        self.assertEqual([path.name for path in self.bundle.iterdir()], ["Druta.exe"])

    def test_missing_source_archive_cannot_copy_any_payload(self):
        self.manifest["files"] = [entry for entry in self.manifest["files"] if entry["name"] != "licenses/nvtune/source.zip"]
        self.save()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            packager.collect(self.source, self.bundle)
        self.assertEqual([path.name for path in self.bundle.iterdir()], ["Druta.exe"])

    def test_unrelated_existing_cli_is_not_overwritten(self):
        (self.bundle / "nvtune.exe").write_bytes(b"existing CLI")
        with self.assertRaisesRegex(ValueError, "Existing different"):
            packager.collect(self.source, self.bundle)
        self.assertEqual((self.bundle / "nvtune.exe").read_bytes(), b"existing CLI")
        self.assertFalse((self.bundle / "nvtune-driver").exists())


if __name__ == "__main__":
    unittest.main()
