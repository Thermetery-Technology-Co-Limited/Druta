"""Python 3.8 shims, the 3.8 compatibility checker and the diagnostics trail."""
import builtins
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from druta import diagnostics, legacy_python
from druta.tools import pe_import_manifest, py38_compat

ROOT = Path(__file__).resolve().parents[1]


class StrictZipTests(unittest.TestCase):
    def test_equal_lengths_and_default_truncation(self):
        z = legacy_python._StrictZip
        self.assertEqual(list(z([1, 2], "ab", strict=True)), [(1, "a"), (2, "b")])
        self.assertEqual(list(z([1, 2, 3], "ab")), [(1, "a"), (2, "b")])
        self.assertEqual(list(z()), [])

    def test_strict_length_mismatch_raises_like_python_310(self):
        z = legacy_python._StrictZip
        with self.assertRaisesRegex(ValueError, "argument 2 is shorter than argument 1"):
            list(z([1, 2], [1], strict=True))
        with self.assertRaisesRegex(ValueError, "argument 2 is longer than argument 1"):
            list(z([1], [1, 2], strict=True))
        with self.assertRaisesRegex(ValueError, "argument 3 is shorter than arguments 1-2"):
            list(z([1, 2], [1, 2], [1], strict=True))

    def test_install_only_before_310_and_only_once(self):
        with patch.object(builtins, "zip", legacy_python._builtin_zip):
            self.assertEqual(legacy_python.install((3, 11)), [])
            self.assertIs(builtins.zip, legacy_python._builtin_zip)
            self.assertEqual(legacy_python.install((3, 8)), ["zip(strict=)"])
            self.assertIs(builtins.zip, legacy_python._StrictZip)
            self.assertEqual(legacy_python.install((3, 8)), [])

    @unittest.skipUnless(sys.version_info < (3, 10), "the shim is only active on 3.8/3.9")
    def test_package_import_installed_the_shim(self):
        self.assertEqual(list(zip("ab", "cd", strict=True)), [("a", "c"), ("b", "d")])


class AddNoteTests(unittest.TestCase):
    def test_uses_native_add_note_when_present(self):
        calls = []
        exc = SimpleNamespace(add_note=calls.append)
        legacy_python.add_note(exc, "restored")
        self.assertEqual(calls, ["restored"])

    def test_fallback_keeps_cancellation_and_pep678_notes(self):
        class Old(BaseException):
            add_note = None  # what a 3.8 exception looks like to getattr

        exc = Old()
        legacy_python.add_note(exc, "rollback verified")
        legacy_python.add_note(exc, 2)
        self.assertEqual(exc.__notes__, ["rollback verified", "2"])


class Py38CheckerTests(unittest.TestCase):
    def issues(self, source):
        return [issue.split(": ", 1)[1] for issue in py38_compat.check_source(source)]

    def test_reports_constructs_that_fail_on_python_38(self):
        source = "\n".join([
            "def f(x: int | None) -> list[int]: pass",
            "x = {} | {'a': 1}",
            "y = 'ab'.removeprefix('a')",
            "z = (5).bit_count()",
            "import tomllib",
            "e = ValueError(); e.add_note('n')",
            "isinstance(1, int | str)",
            "n = (1).to_bytes()",
        ])
        found = " ".join(self.issues(source))
        for text in ("evaluated at definition time", "dict | dict", ".removeprefix()",
                     ".bit_count()", "module tomllib", ".add_note()", "isinstance(x, A | B)",
                     "to_bytes() without length/byteorder"):
            self.assertIn(text, found)

    def test_future_annotations_locals_and_guarded_imports_are_fine(self):
        source = "\n".join([
            "from __future__ import annotations",
            "def f(x: int | None) -> list[int]:",
            "    y: dict[str, int] = {}",
            "    return list(zip(x, x, strict=True))",
            "try:",
            "    import tomllib",
            "except ImportError:",
            "    import tomli as tomllib",
        ])
        self.assertEqual(self.issues(source), [])

    def test_local_annotations_are_not_evaluated_without_future_import(self):
        self.assertEqual(self.issues("def f():\n    y: int | None = None\n"), [])

    def test_shipped_package_stays_compatible_with_the_legacy_runtime(self):
        """Guards every future propagation from main into the legacy branches."""
        issues = py38_compat.check_paths([ROOT / "src" / "druta", ROOT / "src" / "run_druta.py"])
        self.assertEqual(issues, [])


class RedactionTests(unittest.TestCase):
    def test_profile_user_and_computer_are_replaced_case_insensitively(self):
        env = {"USERPROFILE": r"C:\Users\Alice", "USERNAME": "Alice", "COMPUTERNAME": "RIG-01"}
        text = r"c:\users\alice\Desktop\Druta.exe on rig-01 by ALICE"
        with patch.object(Path, "home", return_value=Path("/nonexistent-home")):
            self.assertEqual(diagnostics.redact(text, env),
                             r"<user-profile>\Desktop\Druta.exe on <computer> by <user>")

    def test_short_or_empty_values_are_ignored(self):
        with patch.object(Path, "home", return_value=Path("/")):
            self.assertEqual(diagnostics.redact("a b", {"USERNAME": "a", "COMPUTERNAME": ""}), "a b")


class SessionLogTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (sys.excepthook, getattr(threading, "excepthook", None), dict(diagnostics._state))
        diagnostics._state.update(file=None, path=None, hooks=False)

    def tearDown(self):
        diagnostics.stop_session_log()
        sys.excepthook, thread_hook, state = self.saved
        if thread_hook is not None:
            threading.excepthook = thread_hook
        diagnostics._state.clear()
        diagnostics._state.update(state)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unwritable_directory_falls_back_and_header_and_stages_are_written(self):
        blocked = self.tmp / "blocked"
        blocked.write_text("a file, not a directory")
        path = diagnostics.start_session_log(["Druta.exe", "--gpu", "0000:01:00.0"],
                                             [blocked / "logs", self.tmp / "logs"])
        self.assertEqual(path.parent, self.tmp / "logs")
        diagnostics.stage("renderer")
        text = path.read_text(encoding="utf-8")
        for expected in ("session log", "argv: Druta.exe --gpu 0000:01:00.0", "python:",
                         "stage: session log started", "stage: renderer"):
            self.assertIn(expected, text)

    def test_uncaught_main_and_thread_exceptions_are_logged(self):
        path = diagnostics.start_session_log(["Druta.exe"], [self.tmp])
        with patch.object(sys, "stderr", open(os.devnull, "w")):
            try:
                raise RuntimeError("main boom")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
            worker = threading.Thread(target=lambda: 1 / 0, name="poller")
            worker.start()
            worker.join()
        text = path.read_text(encoding="utf-8")
        self.assertIn("uncaught exception (main thread)", text)
        self.assertIn("RuntimeError: main boom", text)
        self.assertIn("uncaught exception (thread poller)", text)
        self.assertIn("ZeroDivisionError", text)

    def test_never_raises_when_no_directory_is_writable(self):
        blocked = self.tmp / "file"
        blocked.write_text("x")
        self.assertIsNone(diagnostics.start_session_log(["Druta.exe"], [blocked / "a", blocked / "b"]))

    def test_old_logs_are_pruned(self):
        for i in range(diagnostics.LOG_KEEP + 5):
            old = self.tmp / f"druta-2000010{i:02d}.log"
            old.write_text("old")
            os.utime(old, (1000 + i, 1000 + i))
        diagnostics.start_session_log(["Druta.exe"], [self.tmp])
        self.assertEqual(len(list(self.tmp.glob("druta-*.log"))), diagnostics.LOG_KEEP)


class SelfCheckTests(unittest.TestCase):
    def test_a_failing_probe_does_not_stop_later_probes(self):
        import tempfile
        out = Path(tempfile.mkdtemp())
        seen = []

        def broken(report):
            seen.append("broken")
            raise OSError("d3d11.dll not loadable")

        def later(report):
            seen.append("later")
            report.line("later probe ran")

        code = diagnostics.self_check(out, probes=[("Broken", broken), ("Later", later)],
                                      renderer=False)
        text = (out / "self-check.txt").read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertEqual(seen, ["broken", "later"])
        self.assertIn("-> FAILED: OSError: d3d11.dll not loadable", text)
        self.assertIn("later probe ran", text)
        self.assertIn("self-check finished: 1 probe(s) failed", text)

    def test_export_probe_reports_missing_dlls_and_exports_without_stopping(self):
        lines = []
        report = SimpleNamespace(line=lines.append)

        def loader(name):
            if name == "shcore":
                raise OSError("not found")
            return SimpleNamespace(GetTickCount64=1, GetDC=1)

        diagnostics._probe_exports(report, loader)
        text = "\n".join(lines)
        self.assertIn("kernel32!GetTickCount64: present", text)
        self.assertIn("shcore!SetProcessDpiAwareness: DLL not loadable", text)
        self.assertIn("user32!GetDpiForSystem: missing", text)

    def test_every_druta_module_imports_on_this_interpreter(self):
        lines = []
        diagnostics._probe_imports(SimpleNamespace(line=lines.append))
        self.assertFalse([line for line in lines if line.startswith("FAILED")], lines)

    def test_crash_event_probe_keeps_only_druta_events(self):
        stdout = ("Event[0]:\n  Description: Faulting application name: Druta.exe\n"
                  "  Faulting module name: nvapi64.dll\n"
                  "Event[1]:\n  Description: Faulting application name: other.exe\n")
        lines = []
        run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        diagnostics._probe_crash_events(SimpleNamespace(line=lines.append), run)
        text = "\n".join(lines)
        self.assertIn("nvapi64.dll", text)
        self.assertNotIn("other.exe", text)
        self.assertIn("1 recent Druta crash event(s) found", text)


class ImportManifestTests(unittest.TestCase):
    def test_shipped_dlls_are_omitted_and_missing_ones_listed_with_importers(self):
        import tempfile
        bundle = Path(tempfile.mkdtemp())
        for name in ("Druta.exe", "python38.dll", "_internal/ucrtbase.dll", "notes.txt", "bad.pyd"):
            (bundle / name).parent.mkdir(parents=True, exist_ok=True)
            (bundle / name).write_bytes(b"x")
        imports = {
            "Druta.exe": ({"python38.dll", "kernel32.dll", "UCRTBASE.dll".lower()}, 0x8664),
            "python38.dll": ({"api-ms-win-core-path-l1-1-0.dll", "kernel32.dll"}, 0x8664),
            "ucrtbase.dll": ({"kernel32.dll"}, 0x14C),
        }

        def fake(path):
            if path.name == "bad.pyd":
                raise ValueError("not a PE")
            return imports[path.name]

        with patch.object(pe_import_manifest, "_imports", fake):
            output, needed, unreadable, non_x64 = pe_import_manifest.write_manifest(bundle)
        self.assertEqual(needed, {"api-ms-win-core-path-l1-1-0.dll": ["python38.dll"],
                                  "kernel32.dll": ["Druta.exe", "_internal/ucrtbase.dll", "python38.dll"]})
        self.assertEqual(non_x64, ["_internal/ucrtbase.dll"])
        self.assertTrue(unreadable[0].startswith("bad.pyd: ValueError"))
        raw = output.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"api-ms-win-core-path-l1-1-0.dll|python38.dll\r\n", raw)


class DiagnoseScriptTests(unittest.TestCase):
    script = ROOT / "Druta-Diagnose.cmd"

    def test_ascii_and_crlf_for_cmd_exe(self):
        raw = self.script.read_bytes()
        raw.decode("ascii")
        if b"\r\n" in raw:  # a checkout honoring .gitattributes
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn("*.cmd text eol=crlf", (ROOT / ".gitattributes").read_text())

    def test_no_closing_parenthesis_inside_a_block_outside_quotes(self):
        """cmd.exe ends an ( ... ) block at the first unquoted ')'."""
        depth = 0
        for number, line in enumerate(self.script.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.lower().startswith("rem ") or stripped.startswith("::"):
                continue
            unquoted = "".join(part for i, part in enumerate(stripped.split('"')) if i % 2 == 0)
            if depth and stripped.startswith(")"):
                depth -= 1
                unquoted = unquoted[1:].strip()
                if unquoted.startswith("else") and unquoted.endswith("("):
                    depth += 1
                continue
            if depth:
                # Only a FOR's "in ( ... )" set may close a parenthesis here;
                # in an ECHO even a balanced "(text)" ends the block.
                if unquoted.lower().startswith("for "):
                    unquoted = unquoted[unquoted.lower().index(" do ") + 4:]
                self.assertNotIn(")", unquoted, f"line {number}: {line}")
            if unquoted.endswith("(") and not unquoted.endswith("^("):
                depth += 1
        self.assertEqual(depth, 0)

    def test_collects_no_user_identity(self):
        text = self.script.read_text().lower()
        for forbidden in ("systeminfo", "%username%", "%computername%", "whoami", "ipconfig"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
