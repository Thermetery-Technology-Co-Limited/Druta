"""Packaging regressions for incompatible or changed Vista native runtimes."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from tools import vista_runtime


class NativeLoaderTests(unittest.TestCase):
    def native(self, machine=0x8664, version=(6, 0), imports=()):
        pe = MagicMock()
        pe.__enter__.return_value = SimpleNamespace(
            FILE_HEADER=SimpleNamespace(Machine=machine),
            OPTIONAL_HEADER=SimpleNamespace(MajorSubsystemVersion=version[0],
                                            MinorSubsystemVersion=version[1]),
            DIRECTORY_ENTRY_IMPORT=[SimpleNamespace(dll=library.encode("ascii"),
                imports=[SimpleNamespace(name=function.encode("ascii"))])
                for library, function in imports])
        return patch.object(vista_runtime.pefile, "PE", return_value=pe)

    def test_version_field_alone_cannot_hide_win7_static_imports(self):
        for function in ("GetActiveProcessorCount", "K32EnumProcessModules", "K32GetModuleFileNameExW"):
            with self.subTest(function=function), self.native(imports=[("KERNEL32.dll", function)]):
                with self.assertRaisesRegex(ValueError, "Vista cannot load"):
                    vista_runtime.inspect_native(Path("runtime.dll"))

    def test_original_psapi_imports_are_valid(self):
        imports = [("PSAPI.dll", "EnumProcessModules"), ("PSAPI.dll", "GetModuleFileNameExW")]
        with self.native(imports=imports):
            self.assertEqual(vista_runtime.inspect_native(Path("Druta.exe")), imports)

    def test_wrong_architecture_and_newer_executable_minimum_are_rejected(self):
        with self.native(machine=0x14c), self.assertRaisesRegex(ValueError, "x64"):
            vista_runtime.inspect_native(Path("runtime.dll"))
        with self.native(version=(6, 1)), self.assertRaisesRegex(ValueError, "newer Windows"):
            vista_runtime.inspect_native(Path("Druta.exe"))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.files = {}
        for name in vista_runtime.RUNTIME_FILES:
            path = self.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode("ascii"))
            self.files[name] = vista_runtime.sha256(path)
        self.manifest = {"format": 1, "sources": vista_runtime.SOURCES,
                         "files": self.files, "patches": {}}
        self.save()

    def save(self):
        (self.directory / vista_runtime.MANIFEST).write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_changed_binary_is_rejected_before_packaging(self):
        (self.directory / "python38.dll").write_bytes(b"different runtime")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            vista_runtime.validate_runtime(self.directory)

    def test_extra_manifest_path_is_rejected(self):
        self.files["../outside.dll"] = "0" * 64
        self.save()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            vista_runtime.validate_runtime(self.directory)

    def test_existing_python_directory_is_never_overwritten(self):
        held = self.directory / "keep.txt"
        held.write_text("existing environment", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            vista_runtime.assemble(self.directory, {}, None, self.directory)
        self.assertEqual(held.read_text(encoding="utf-8"), "existing environment")


if __name__ == "__main__":
    unittest.main()
