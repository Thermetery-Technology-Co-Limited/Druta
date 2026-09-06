"""Source packaging rejects edits and retains the executable/source pairing."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import package_source


class SourcePackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.root.mkdir()
        self.bundle = Path(self.tmp.name) / 'bundle'
        self.bundle.mkdir()
        (self.root / 'druta.py').write_bytes(b'print("build source")\n')
        (self.bundle / 'Druta.exe').write_bytes(b'compiled executable fixture')
        self.allowlist = patch.object(package_source, 'FILES', ('druta.py',))
        self.allowlist.start()
        self.addCleanup(self.allowlist.stop)

    def test_exact_source_and_executable_hashes_are_recorded(self):
        before = package_source.snapshot(self.root)
        result = package_source.package(self.root, before, self.bundle)
        self.assertEqual(result['files'], before)
        copied = self.bundle / 'source' / 'druta.py'
        self.assertEqual(copied.read_bytes(), (self.root / 'druta.py').read_bytes())
        self.assertEqual(result['executable_sha256'],
                         hashlib.sha256((self.bundle / 'Druta.exe').read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.bundle / 'source' / 'SOURCE-MANIFEST.json').read_text()), result)

    def test_compile_time_edit_refuses_new_source_package(self):
        before = package_source.snapshot(self.root)
        (self.root / 'druta.py').write_bytes(b'edited during compilation\n')
        with self.assertRaisesRegex(RuntimeError, 'Source changed during compilation'):
            package_source.package(self.root, before, self.bundle)
        self.assertFalse((self.bundle / 'source').exists())

    def test_private_research_and_session_files_are_not_copied(self):
        (self.root / 'private.txt').write_text('private')
        (self.root / 'profiles').mkdir()
        (self.root / 'profiles' / 'autosave-secret.json').write_text('{}')
        before = package_source.snapshot(self.root)
        self.assertEqual([entry['path'] for entry in before], ['druta.py'])
        package_source.package(self.root, before, self.bundle)
        self.assertFalse((self.bundle / 'source' / 'private.txt').exists())
        self.assertFalse((self.bundle / 'source' / 'profiles').exists())


if __name__ == '__main__':
    unittest.main()
