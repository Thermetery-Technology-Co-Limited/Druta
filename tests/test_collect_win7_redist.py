"""Build-helper validation and output ownership; no SDK installation or GPU."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import collect_win7_redist as collector


class CollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ucrt = self.root / "ucrt"
        self.ucrt.mkdir()
        for name in collector.UCRT_NAMES:
            (self.ucrt / name).write_bytes(name.encode("ascii"))
        (self.root / "d3d").mkdir()
        self.compiler = self.root / "d3d" / collector.D3D_NAME
        self.compiler.write_bytes(b"fixture shader compiler")
        self.license = self.root / "sdk-license"
        self.license.mkdir()
        (self.license / "sdk_license.rtf").write_bytes(b"fixture license")
        self.output = self.root / "output"

        def metadata(path, version):
            data = path.read_bytes()
            return data, {"name": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                          "version": list(version), "source": str(path)}

        metadata_patch = patch.object(collector, "_dll_metadata", side_effect=metadata)
        hash_patch = patch.object(collector, "D3D_SHA256",
                                 hashlib.sha256(self.compiler.read_bytes()).hexdigest())
        metadata_patch.start()
        hash_patch.start()
        self.addCleanup(metadata_patch.stop)
        self.addCleanup(hash_patch.stop)

    def collect(self):
        return collector.collect(self.ucrt, self.compiler, [self.license], self.output)

    def test_complete_collection_and_identical_rerun(self):
        manifest = self.collect()
        self.assertEqual(manifest["format"], 1)
        self.assertEqual(len(manifest["files"]), 43)
        self.assertEqual(len(manifest["licenses"]), 1)
        for record in manifest["files"] + manifest["licenses"]:
            self.assertEqual(hashlib.sha256((self.output / record["name"]).read_bytes()).hexdigest(),
                             record["sha256"])
        self.assertEqual(self.collect(), manifest)

    def test_missing_forwarder_fails_before_output_is_created(self):
        (self.ucrt / "api-ms-win-crt-runtime-l1-1-0.dll").unlink()
        with self.assertRaisesRegex(ValueError, "complete 42-DLL"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_wrong_shader_hash_fails_before_output_is_created(self):
        with patch.object(collector, "D3D_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "SHA256 differs"):
                self.collect()
        self.assertFalse(self.output.exists())

    def test_unrelated_output_is_never_overwritten(self):
        self.output.mkdir()
        held = self.output / "keep.txt"
        held.write_bytes(b"user content")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            self.collect()
        self.assertEqual(held.read_bytes(), b"user content")
        self.assertEqual(list(self.output.iterdir()), [held])

    def test_changed_prior_collection_is_never_overwritten(self):
        self.collect()
        held = self.output / "ucrtbase.dll"
        held.write_bytes(b"changed after collection")
        with self.assertRaisesRegex(ValueError, "new or empty"):
            self.collect()
        self.assertEqual(held.read_bytes(), b"changed after collection")

    def test_system_directory_is_not_a_runtime_source(self):
        path = self.root / "System32"
        path.mkdir()
        with self.assertRaisesRegex(ValueError, "system path"):
            collector._non_system_path(path)


class PeValidationTests(unittest.TestCase):
    def test_wrong_architecture_or_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ucrtbase.dll"
            path.write_bytes(b"fixture PE")
            for machine, build, message in [(0x14c, 19041, "x64"),
                                             (0x8664, 28000, "Untested DLL version")]:
                pe = SimpleNamespace(
                    FILE_HEADER=SimpleNamespace(Machine=machine, Characteristics=0x2000),
                    VS_FIXEDFILEINFO=[SimpleNamespace(FileVersionMS=10 << 16,
                                                      FileVersionLS=(build << 16) | 5609)],
                    close=Mock())
                with self.subTest(machine=machine, build=build):
                    with patch.object(collector.pefile, "PE", return_value=pe):
                        with self.assertRaisesRegex(ValueError, message):
                            collector._dll_metadata(path, collector.UCRT_VERSION)
                    pe.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
