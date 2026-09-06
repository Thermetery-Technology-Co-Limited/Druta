"""Prevent reuse or mixing of the Win7 UCRT set that fails to load on Vista."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import collect_vista_redist as vista


class VistaCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ucrt = self.root / "ucrt"
        self.ucrt.mkdir()
        self.hashes = {}
        for name in vista.UCRT_NAMES:
            data = name.encode("ascii")
            (self.ucrt / name).write_bytes(data)
            self.hashes[name] = hashlib.sha256(data).hexdigest()
        (self.root / "d3d").mkdir()
        self.compiler = self.root / "d3d" / vista.common.D3D_NAME
        self.compiler.write_bytes(b"shader compiler fixture")
        self.licenses = self.root / "sdk-license"
        self.licenses.mkdir()
        (self.licenses / "sdk_license.rtf").write_bytes(b"license fixture")
        self.output = self.root / "output"

        def metadata(path, version):
            data = path.read_bytes()
            return data, {"name": path.name, "version": list(version),
                          "sha256": hashlib.sha256(data).hexdigest(), "source": str(path)}

        patches = [patch.object(vista, "pinned_hashes", return_value=self.hashes),
                   patch.object(vista.common, "_dll_metadata", side_effect=metadata),
                   patch.object(vista.common, "D3D_SHA256", hashlib.sha256(self.compiler.read_bytes()).hexdigest())]
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def collect(self):
        return vista.collect(self.ucrt, self.compiler, [self.licenses], self.output)

    def test_complete_vista_set_can_be_collected_and_revalidated(self):
        manifest = self.collect()
        self.assertEqual(len(manifest["files"]), 42)
        self.assertEqual(manifest["target"], "windows-vista-sp2-x64")
        self.assertEqual(vista.validate(self.output), manifest)
        self.assertEqual(self.collect(), manifest)

    def test_win7_extra_forwarder_is_rejected_before_copy(self):
        (self.ucrt / "api-ms-win-core-console-l1-2-0.dll").write_bytes(b"newer forwarder")
        with self.assertRaisesRegex(ValueError, "complete 41-DLL SDK 10240"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_changed_dll_is_rejected_even_with_a_matching_version(self):
        (self.ucrt / "ucrtbase.dll").write_bytes(b"different build with the same version")
        with self.assertRaisesRegex(ValueError, "UCRT SHA256 differs"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_spec_validation_rejects_a_win7_manifest(self):
        manifest = self.collect()
        manifest.pop("target")
        (self.output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "complete 41-DLL SDK 10240"):
            vista.validate(self.output)

    def test_spec_validation_rejects_post_collection_replacement(self):
        self.collect()
        (self.output / "ucrtbase.dll").write_bytes(b"replaced after collection")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            vista.validate(self.output)


if __name__ == "__main__":
    unittest.main()
