# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every backend object comes from gpu_backend, and ``--backend`` is parsed (design G4g, (f) item 6).

Three groups:

- a static lint over ``src/druta`` (parsed with ``ast``, so comments and strings do not count):
  no ``GPU(...)``, ``nvbackend.GPU(...)`` or ``native.Gpu(...)`` construction outside the factory,
  the backends and ``tools/``, and no ``.close()`` on ``self.gpu`` or ``fresh``;
- the command-line handling of ``--backend X`` / ``--backend=X`` in druta's ``main`` and in the
  legacy Tk ``app.main`` and the ``__main__`` helpers;
- with ``DRUTA_BACKEND`` unset, the factory is asked for its default and builds ``nvbackend.GPU``.

No driver or library is touched. Every test leaves ``os.environ``, ``sys.argv`` and the factory's pin
as it found them.
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from druta import druta, gpu_backend, nvbackend

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "druta"
_MISSING = object()


# ---- isolation -----------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """A clean factory, DRUTA_BACKEND absent and a neutral argv; all restored at teardown.

    setenv-then-delenv makes monkeypatch record the original value (or absence) of DRUTA_BACKEND,
    so a write by consume_argv inside the test is undone too.
    """
    before = os.environ.get(gpu_backend.ENV, _MISSING)
    gpu_backend.reset_for_tests()
    monkeypatch.setenv(gpu_backend.ENV, "python")
    monkeypatch.delenv(gpu_backend.ENV)
    monkeypatch.setattr(sys, "argv", ["druta"])
    yield
    gpu_backend.reset_for_tests()
    monkeypatch.undo()
    assert os.environ.get(gpu_backend.ENV, _MISSING) == before


# ---- (a) lint ------------------------------------------------------------------------------------

_EXEMPT_FILES = {"gpu_backend.py", "nvbackend.py"}


def _exempt(path):
    relative = path.relative_to(PACKAGE)
    if relative.parts[0] == "tools":
        return True
    name = relative.name
    return name in _EXEMPT_FILES or (len(relative.parts) == 1 and name.startswith("native"))


def _is_construction(func):
    """GPU(...), <anything>.GPU(...) or native.Gpu(...)."""
    if isinstance(func, ast.Name):
        return func.id == "GPU"
    if isinstance(func, ast.Attribute):
        if func.attr == "GPU":
            return True
        return func.attr == "Gpu" and isinstance(func.value, ast.Name) and func.value.id == "native"
    return False


def _is_forbidden_close(func):
    """self.gpu.close() or fresh.close()."""
    if not (isinstance(func, ast.Attribute) and func.attr == "close"):
        return False
    target = func.value
    if isinstance(target, ast.Name):
        return target.id == "fresh"
    return (isinstance(target, ast.Attribute) and target.attr == "gpu"
            and isinstance(target.value, ast.Name) and target.value.id == "self")


def violations(source, filename="<source>"):
    """(line, text) for every forbidden construction or close in one module's source."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Call):
            if _is_construction(node.func):
                found.append((node.lineno, "construction " + ast.unparse(node)))
            elif _is_forbidden_close(node.func):
                found.append((node.lineno, "close " + ast.unparse(node)))
    return found


def test_no_backend_construction_or_close_outside_the_factory():
    problems = []
    scanned = 0
    for path in sorted(PACKAGE.rglob("*.py")):
        if _exempt(path):
            continue
        scanned += 1
        for line, text in violations(path.read_text(encoding="utf-8-sig"), str(path)):
            problems.append(f"{path.relative_to(ROOT)}:{line}: {text}")
    assert scanned > 50, "the lint scanned too few modules; is the package path right?"
    assert not problems, "build backend objects with gpu_backend.open_gpu:\n" + "\n".join(problems)


def test_the_lint_ignores_comments_strings_and_class_attribute_use():
    source = '''
# gpu = GPU(slot) in a comment
"""docstring: nvbackend.GPU(slot) and native.Gpu(slot) and self.gpu.close()"""
text = "fresh.close() GPU(slot)"
bound = GPU.VOLT_LIMIT_MIN_MV
peak = GPU.peak_info([])
handle.close()
'''
    assert violations(source) == []


@pytest.mark.parametrize("line", [
    "gpu = GPU(slot)",
    "gpu = nvbackend.GPU(slot)",
    "gpu = nb.GPU()",
    "gpu = native.Gpu(slot)",
    "self.gpu.close()",
    "fresh.close()",
])
def test_the_lint_catches_each_forbidden_form(line):
    assert len(violations(line + "\n")) == 1


def pydll_uses(source, filename="<source>"):
    """Lines that name PyDLL / pydll (attribute or bare name, any call or reference)."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        name = (node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name)
                else None)
        if name in ("PyDLL", "pydll"):
            found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.ImportFrom) and node.module == "ctypes":
            found.extend((node.lineno, f"from ctypes import {alias.name}") for alias in node.names
                         if alias.name in ("PyDLL", "pydll"))
    return found


def test_no_pydll_anywhere_in_the_package():
    """The native library must be a ctypes.CDLL: PyDLL keeps the GIL while a lock export blocks."""
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for line, text in pydll_uses(path.read_text(encoding="utf-8-sig"), str(path)):
            problems.append(f"{path.relative_to(ROOT)}:{line}: {text}")
    assert not problems, "load libraries with ctypes.CDLL, never PyDLL:\n" + "\n".join(problems)


@pytest.mark.parametrize("line", ["lib = ctypes.PyDLL(path)", "lib = ctypes.pydll.LoadLibrary(path)",
                                  "from ctypes import PyDLL", "loader = PyDLL"])
def test_the_pydll_lint_catches_each_form(line):
    assert pydll_uses(line + "\n")


def test_the_pydll_lint_ignores_cdll_comments_and_strings():
    assert pydll_uses("lib = ctypes.CDLL(path)  # not PyDLL\ntext = 'ctypes.PyDLL'\n") == []


def test_the_exemptions_are_exactly_the_backends_the_factory_and_tools():
    assert _exempt(PACKAGE / "gpu_backend.py")
    assert _exempt(PACKAGE / "nvbackend.py")
    assert _exempt(PACKAGE / "native.py")
    assert _exempt(PACKAGE / "native_rail_limits.py")
    assert _exempt(PACKAGE / "tools" / "i2c_discover.py")
    assert not _exempt(PACKAGE / "druta.py")
    assert not _exempt(PACKAGE / "voltage_offsets_live.py")


# ---- (b) druta main ------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch):
    """druta.main with no app, no Startup and a recorded _tell; open_gpu must not run."""
    tell = Mock()
    monkeypatch.setattr(druta, "_tell", tell)
    monkeypatch.setattr(druta, "Druta", Mock(side_effect=AssertionError("must not construct the app")))
    monkeypatch.setattr(druta, "open_gpu", Mock(side_effect=AssertionError("must not construct a GPU")))
    return tell


@pytest.mark.parametrize("flag", [["--backend", "python"], ["--backend=python"],
                                  ["--backend", "NATIVE"], ["--backend=auto"]])
def test_druta_main_strips_the_flag_before_the_option_loop(cli, flag):
    assert druta.main([*flag, "--version"]) == 0
    cli.assert_called_once_with(f"Druta {druta.__version__}")
    assert os.environ[gpu_backend.ENV] == flag[-1].split("=")[-1].lower()


def test_druta_main_accepts_the_flag_after_other_options(cli):
    assert druta.main(["--version", "--backend", "python"]) == 0
    assert os.environ[gpu_backend.ENV] == "python"


@pytest.mark.parametrize("argv", [["--backend", "bogus"], ["--backend=bogus"],
                                  ["--version", "--backend", "bogus"], ["--backend"], ["--backend="]])
def test_druta_main_rejects_a_bad_value_with_code_2(cli, argv):
    assert druta.main(argv) == 2
    message = cli.call_args.args[0]
    assert "auto, native or python" in message
    assert gpu_backend.ENV not in os.environ


def test_a_bad_environment_value_does_not_block_help_or_version(cli, monkeypatch):
    monkeypatch.setenv(gpu_backend.ENV, "bogus")
    assert druta.main(["--help"]) == 0
    assert "--backend auto|native|python" in cli.call_args.args[0]
    assert druta.main(["--version"]) == 0
    assert cli.call_args.args[0] == f"Druta {druta.__version__}"


@pytest.mark.parametrize(("command", "module", "function"), [
    ("--registry-overrides", "registry_overrides", "cli"),
    ("--registry-recover", "registry_overrides", "recovery_cli"),
    ("--registry-snapshot", "registry_overrides", "snapshot_cli"),
    ("--voltage-offsets", "voltage_offsets_cli", "cli"),
    ("--voltage-replay-worker", "voltage_replay_worker", "worker_cli"),
    ("--restart-gpu", "devicerecovery", "cli"),
])
def test_a_bad_environment_value_does_not_block_sub_commands(cli, monkeypatch, command, module, function):
    """Registry recovery and --restart-gpu (the panic / reset path) need no backend and must still run."""
    monkeypatch.setenv(gpu_backend.ENV, "bogus")
    handler = Mock(return_value=0)
    monkeypatch.setattr(importlib.import_module(f"druta.{module}"), function, handler)
    assert druta.main([command, "0000:01:00.0"]) == 0
    assert handler.call_args.args[0] == ["0000:01:00.0"]


def test_a_bad_environment_value_refuses_list_gpus_with_code_2(cli, monkeypatch):
    monkeypatch.setenv(gpu_backend.ENV, "bogus")
    monkeypatch.setattr(druta, "enumerate_gpus", Mock(side_effect=AssertionError("must not enumerate")))
    assert druta.main(["--list-gpus"]) == 2
    assert cli.call_args.args[0] == "DRUTA_BACKEND must be auto, native or python"


def test_a_bad_environment_value_refuses_the_app_start_with_code_2(cli, monkeypatch):
    monkeypatch.setenv(gpu_backend.ENV, "bogus")
    startup_factory = Mock(side_effect=AssertionError("must not begin a startup session"))
    monkeypatch.setattr(druta.startup, "Startup", startup_factory)
    assert druta.main(["--gpu", "0000:01:00.0"]) == 2
    assert cli.call_args.args[0] == "DRUTA_BACKEND must be auto, native or python"
    startup_factory.assert_not_called()


def test_druta_help_names_the_flag(cli):
    assert druta.main(["--help"]) == 0
    assert "--backend auto|native|python" in cli.call_args.args[0]


@pytest.mark.parametrize(("command", "module", "function"), [
    ("--registry-overrides", "registry_overrides", "cli"),
    ("--registry-recover", "registry_overrides", "recovery_cli"),
    ("--registry-snapshot", "registry_overrides", "snapshot_cli"),
    ("--voltage-offsets", "voltage_offsets_cli", "cli"),
    ("--voltage-replay-worker", "voltage_replay_worker", "worker_cli"),
])
@pytest.mark.parametrize("where", ["before", "inside", "equals"])
def test_sub_commands_get_their_own_arguments_unchanged(cli, monkeypatch, command, module, function, where):
    target = importlib.import_module(f"druta.{module}")
    handler = Mock(return_value=0)
    monkeypatch.setattr(target, function, handler)
    own = ["0000:01:00.0", "--gpu", "x", "--flag=1"]
    if where == "before":
        argv = ["--backend", "python", command, *own]
    elif where == "inside":
        argv = [command, own[0], "--backend", "python", *own[1:]]
    else:
        argv = [command, *own[:2], "--backend=python", *own[2:]]
    assert druta.main(argv) == 0
    handler.assert_called_once()
    assert handler.call_args.args[0] == own
    assert os.environ[gpu_backend.ENV] == "python"


def test_restart_gpu_sub_command_gets_its_own_arguments(cli, monkeypatch):
    handler = Mock(return_value=0)
    monkeypatch.setattr(druta.devicerecovery, "cli", handler)
    assert druta.main(["--restart-gpu", "--backend=python", "0000:01:00.0"]) == 0
    assert handler.call_args.args[0] == ["0000:01:00.0"]


def test_list_gpus_goes_through_the_factory(cli, monkeypatch):
    calls = []

    def enumerate_gpus():
        calls.append("enumerate")
        return [{"slot": "0000:01:00.0", "name": "Fake", "has_nvapi": True}]

    monkeypatch.setattr(gpu_backend, "_python_module", lambda: SimpleNamespace(enumerate_gpus=enumerate_gpus))
    monkeypatch.setattr(gpu_backend, "_native_module", Mock(side_effect=AssertionError("python never loads native")))
    assert druta.main(["--backend", "python", "--list-gpus"]) == 0
    assert calls == ["enumerate"]
    cli.assert_called_once_with("0000:01:00.0  Fake")
    assert gpu_backend.choice().kind == "python"


def test_list_gpus_reports_a_refused_backend(cli, monkeypatch):
    monkeypatch.setattr(druta, "enumerate_gpus", Mock(side_effect=gpu_backend.BackendRefused("no library")))
    assert druta.main(["--list-gpus"]) == 1
    assert "no library" in cli.call_args.args[0]


def test_a_refused_backend_at_app_construction_is_a_message_and_code_1(cli, monkeypatch):
    manager = SimpleNamespace(begin=Mock(return_value=None), watch_shutdown=Mock(), close=Mock(), lock=None)
    monkeypatch.setattr(druta.startup, "Startup", Mock(return_value=manager))
    monkeypatch.setattr(druta, "Druta", Mock(side_effect=gpu_backend.BackendRefused("native surface incomplete: x")))
    assert druta.main(["--backend", "native"]) == 1
    assert "native surface incomplete: x" in cli.call_args.args[0]
    manager.close.assert_called_once_with(clean=False)


def test_python_m_druta_help_and_bogus_backend(tmp_path):
    environment = {k: v for k, v in os.environ.items() if k != gpu_backend.ENV}
    environment["PYTHONPATH"] = str(ROOT / "src")
    shown = subprocess.run([sys.executable, "-m", "druta", "--backend", "python", "--help"],
                           cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
    assert shown.returncode == 0, shown.stderr
    assert "--backend auto|native|python" in shown.stdout
    bogus = subprocess.run([sys.executable, "-m", "druta", "--backend", "bogus"],
                           cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
    assert bogus.returncode == 2
    assert "auto, native or python" in bogus.stdout
    environment[gpu_backend.ENV] = "bogus"
    helped = subprocess.run([sys.executable, "-m", "druta", "--help"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
    assert helped.returncode == 0, helped.stdout
    assert "--backend auto|native|python" in helped.stdout


@pytest.mark.parametrize("module", ["druta.gpuload", "druta.timings"])
def test_helper_mains_reject_a_bogus_backend_before_any_work(tmp_path, module):
    environment = {k: v for k, v in os.environ.items() if k != gpu_backend.ENV}
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run([sys.executable, "-m", module, "--backend=bogus"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "auto, native or python" in result.stderr


# ---- (b) the legacy Tk app -----------------------------------------------------------------------

def _fake_tk_module(name):
    module = types.ModuleType(name)

    def missing(attribute):
        if attribute.startswith("__"):
            raise AttributeError(attribute)
        return type(attribute, (), {})

    module.__getattr__ = missing
    return module


@contextlib.contextmanager
def tk_module(monkeypatch, name):
    """Import druta.<name> without a display or a real tkinter; forget it afterwards if it was new."""
    qualified = f"druta.{name}"
    existing = sys.modules.get(qualified)
    if existing is not None:
        yield existing
        return
    tk = _fake_tk_module("tkinter")
    tk.ttk, tk.messagebox = _fake_tk_module("tkinter.ttk"), _fake_tk_module("tkinter.messagebox")
    for module_name, stub in (("tkinter", tk), ("tkinter.ttk", tk.ttk), ("tkinter.messagebox", tk.messagebox)):
        if module_name not in sys.modules:
            monkeypatch.setitem(sys.modules, module_name, stub)
    module = importlib.import_module(qualified)
    try:
        yield module
    finally:
        sys.modules.pop(qualified, None)
        import druta as package
        if getattr(package, name, None) is module:
            delattr(package, name)


@pytest.fixture
def legacy_app(monkeypatch):
    """druta.app with the App class, tk and messagebox replaced; the app class itself is never built."""
    with tk_module(monkeypatch, "app") as module:
        monkeypatch.setattr(module, "App", Mock())
        monkeypatch.setattr(module, "tk", Mock())
        monkeypatch.setattr(module, "messagebox", Mock())
        yield module


@pytest.mark.parametrize("flag", [["--backend", "python"], ["--backend=python"]])
def test_app_main_strips_the_flag_before_its_argv_tests(legacy_app, monkeypatch, flag):
    gpu = SimpleNamespace(available=Mock(return_value=True))
    opened = Mock(return_value=gpu)
    monkeypatch.setattr(legacy_app, "open_gpu", opened)
    monkeypatch.setattr(sys, "argv", ["app", *flag, "--no-dpi", "--gpu", "0000:01:00.0"])
    legacy_app.main()
    assert sys.argv == ["app", "--no-dpi", "--gpu", "0000:01:00.0"]
    assert os.environ[gpu_backend.ENV] == "python"
    opened.assert_called_once_with("0000:01:00.0")
    legacy_app.App.assert_called_once_with(gpu)


def test_app_main_rejects_a_bad_value_with_code_2(legacy_app, monkeypatch, capsys):
    monkeypatch.setattr(legacy_app, "open_gpu", Mock(side_effect=AssertionError("must not construct a GPU")))
    monkeypatch.setattr(sys, "argv", ["app", "--backend", "bogus", "--no-dpi"])
    with pytest.raises(SystemExit) as exited:
        legacy_app.main()
    assert exited.value.code == 2
    assert "auto, native or python" in capsys.readouterr().err
    assert sys.argv == ["app", "--backend", "bogus", "--no-dpi"]


def test_app_main_reports_a_bad_environment_value_and_exits_2(legacy_app, monkeypatch, capsys):
    monkeypatch.setenv(gpu_backend.ENV, "bogus")
    monkeypatch.setattr(legacy_app, "open_gpu", Mock(side_effect=AssertionError("must not construct a GPU")))
    monkeypatch.setattr(sys, "argv", ["app", "--no-dpi"])
    with pytest.raises(SystemExit) as exited:
        legacy_app.main()
    assert exited.value.code == 2
    assert "DRUTA_BACKEND must be auto, native or python" in capsys.readouterr().err
    title, text = legacy_app.messagebox.showerror.call_args.args
    assert "DRUTA_BACKEND must be auto, native or python" in text
    legacy_app.App.assert_not_called()


def test_app_main_shows_a_refused_backend_and_builds_no_app(legacy_app, monkeypatch):
    monkeypatch.setattr(legacy_app, "open_gpu",
                        Mock(side_effect=gpu_backend.BackendRefused("native surface incomplete: x")))
    monkeypatch.setattr(sys, "argv", ["app", "--backend", "native", "--no-dpi"])
    assert legacy_app.main() is None
    title, text = legacy_app.messagebox.showerror.call_args.args
    assert title == "Thermetery Druta"
    assert "GPU backend refused" in text and "native surface incomplete: x" in text
    legacy_app.App.assert_not_called()


def test_app_main_builds_through_the_factory_by_default(legacy_app, monkeypatch):
    built = []

    class FakeGPU:
        def __init__(self, slot):
            built.append(slot)

        def available(self):
            return True

    monkeypatch.setattr(nvbackend, "GPU", FakeGPU)
    monkeypatch.setattr(gpu_backend, "_native_module", Mock(side_effect=AssertionError("python never loads native")))
    monkeypatch.setattr(sys, "argv", ["app", "--no-dpi"])
    legacy_app.main()
    assert built == [nvbackend.slot_from_argv(["app", "--no-dpi"])]
    assert gpu_backend.choice().source == "default"


# ---- (c) the default builds nvbackend.GPU --------------------------------------------------------

def test_without_druta_backend_the_factory_default_builds_nvbackend_gpu(monkeypatch):
    built = []

    class FakeGPU:
        def __init__(self, slot):
            built.append(slot)

    monkeypatch.setattr(nvbackend, "GPU", FakeGPU)
    monkeypatch.setattr(nvbackend, "enumerate_gpus", lambda: [{"slot": "s"}])
    monkeypatch.setattr(gpu_backend, "_native_module", Mock(side_effect=AssertionError("python never loads native")))
    assert druta.open_gpu is gpu_backend.open_gpu
    assert druta.enumerate_gpus is gpu_backend.enumerate_gpus
    gpu = druta.open_gpu("0000:01:00.0")
    assert isinstance(gpu, FakeGPU) and built == ["0000:01:00.0"]
    assert druta.enumerate_gpus() == [{"slot": "s"}]
    pinned = gpu_backend.choice()
    assert (pinned.kind, pinned.requested, pinned.source) == ("python", "python", "default")
    assert gpu_backend.describe() == "backend: Python (requested python via default)"


def test_the_app_constructor_uses_the_factory(monkeypatch):
    """Druta.__init__ asks the factory (default kind) and the factory's enumeration, nothing else."""
    sentinel = RuntimeError("stop after the backend calls")
    order = []

    class FakeGPU:
        def __init__(self, slot):
            order.append(("GPU", slot))

    def enumerate_gpus():
        order.append("enumerate")
        raise sentinel

    monkeypatch.setattr(nvbackend, "GPU", FakeGPU)
    monkeypatch.setattr(nvbackend, "enumerate_gpus", enumerate_gpus)
    monkeypatch.setattr(gpu_backend, "_native_module", Mock(side_effect=AssertionError("python never loads native")))
    with pytest.raises(RuntimeError) as raised:
        druta.Druta("0000:02:00.0")
    assert raised.value is sentinel
    assert order == [("GPU", "0000:02:00.0"), "enumerate"]
    assert gpu_backend.choice().kind == "python"


@pytest.mark.parametrize("module_name", ["registry_overrides", "voltage_offsets_live", "voltage_replay_worker"])
def test_helper_modules_reach_the_factory(module_name):
    """The helper modules no longer bind nvbackend.GPU as their constructor (the lint covers the calls)."""
    source = (PACKAGE / f"{module_name}.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                and node.module == "gpu_backend" for alias in node.names}
    assert "open_gpu" in imported


# ---- (b) the helper tools' entry paths -----------------------------------------------------------

class _Stop(Exception):
    """Raised by a stub once the entry path has passed the backend step."""


def run_main_block(module, overrides):
    """Execute ``module``'s ``if __name__ == "__main__":`` body in a copy of its namespace.

    The copy carries ``overrides`` (stubs for the availability checks this host fails), which a
    ``python -m`` run cannot take, while the body itself is the module's own source.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8-sig"))
    blocks = [node for node in tree.body if isinstance(node, ast.If)
              and ast.unparse(node.test) == "__name__ == '__main__'"]
    assert len(blocks) == 1
    code = compile(ast.Module(body=blocks[0].body, type_ignores=[]), module.__file__, "exec")
    namespace = dict(vars(module))
    namespace.update(overrides)
    exec(code, namespace)


def _auto_falls_back(monkeypatch, built):
    """Factory hooks under which auto falls back to Python with a reason; Python builds a recording fake."""
    class Unavailable(ImportError):
        pass

    def load():
        raise Unavailable("native core not loaded: no library here")

    class FakeGPU:
        static = {"mem_clocks": []}

        def __init__(self, slot):
            built.append(slot)

    monkeypatch.setattr(gpu_backend, "_native_module",
                        lambda: SimpleNamespace(NativeUnavailable=Unavailable, load=load))
    monkeypatch.setattr(gpu_backend, "_python_module", lambda: SimpleNamespace(GPU=FakeGPU))


def _gpuload_overrides(**extra):
    stop = Mock(side_effect=_Stop("past the backend step"))
    return {"available": lambda: (True, None), "_read": stop, **extra}


def _timings_overrides(**extra):
    av = SimpleNamespace(ok=True, exe="nvtune.exe", driver="driver", reason="")
    table = SimpleNamespace(fields=[], registers=[], inferred_registers=lambda: [], aliases={})
    return {"available": lambda: av, "field_table": lambda: table,
            "snapshot": Mock(side_effect=_Stop("past the backend step")), **extra}


TOOLS = [("druta.gpuload", _gpuload_overrides), ("druta.timings", _timings_overrides)]


@pytest.mark.parametrize(("name", "overrides"), TOOLS)
def test_tool_reports_an_unknown_environment_value_on_stderr_with_code_2(monkeypatch, capsys, name, overrides):
    module = importlib.import_module(name)
    monkeypatch.setenv(gpu_backend.ENV, "bogus")
    monkeypatch.setattr(sys, "argv", [name])
    checked = Mock(side_effect=AssertionError("must stop before any availability check"))
    with pytest.raises(SystemExit) as exited:
        run_main_block(module, overrides(available=checked))
    assert exited.value.code == 2
    out, err = capsys.readouterr()
    assert "DRUTA_BACKEND must be auto, native or python" in err
    assert "no GPU backend" not in out


@pytest.mark.parametrize(("name", "overrides"), TOOLS)
def test_tool_reports_a_refused_backend_on_stderr_and_stops(monkeypatch, capsys, name, overrides):
    module = importlib.import_module(name)
    monkeypatch.setattr(gpu_backend, "open_gpu", Mock(side_effect=gpu_backend.BackendRefused("surface incomplete")))
    monkeypatch.setattr(sys, "argv", [name, "--backend", "native"])
    stubs = overrides()
    with pytest.raises(SystemExit) as exited:
        run_main_block(module, stubs)
    assert exited.value.code == 1
    out, err = capsys.readouterr()
    assert "GPU backend refused: surface incomplete" in err
    assert "no GPU backend" not in out
    if "snapshot" in stubs:
        stubs["snapshot"].assert_not_called()   # timings never carries on without a GPU object


@pytest.mark.parametrize(("name", "overrides"), TOOLS)
def test_tool_notes_an_auto_fallback_once_when_backend_was_given(monkeypatch, capsys, name, overrides):
    module = importlib.import_module(name)
    built = []
    _auto_falls_back(monkeypatch, built)
    monkeypatch.setattr(sys, "argv", [name, "--backend=auto"])
    with pytest.raises(_Stop):
        run_main_block(module, overrides())
    assert built == [nvbackend.slot_from_argv([name])]
    err = capsys.readouterr().err
    assert err.count("backend: Python (requested auto via --backend; native core not loaded") == 1


@pytest.mark.parametrize(("name", "overrides"), TOOLS)
def test_tool_is_silent_about_the_backend_without_the_flag(monkeypatch, capsys, name, overrides):
    module = importlib.import_module(name)
    built = []
    _auto_falls_back(monkeypatch, built)
    monkeypatch.setenv(gpu_backend.ENV, "auto")
    monkeypatch.setattr(sys, "argv", [name])
    with pytest.raises(_Stop):
        run_main_block(module, overrides())
    assert len(built) == 1
    assert "backend:" not in capsys.readouterr().err


@pytest.fixture
def bench(monkeypatch):
    """druta.bench_move with no Tk variants and the real-app run stubbed down to the backend step."""
    import ctypes
    if not hasattr(ctypes, "windll"):   # bench_move binds user32 at import; nothing here calls it
        monkeypatch.setattr(ctypes, "windll", MagicMock(), raising=False)
    with tk_module(monkeypatch, "bench_move") as module, tk_module(monkeypatch, "app") as legacy:
        monkeypatch.setattr(module, "VARIANTS", [])
        monkeypatch.setattr(legacy, "_enable_dpi_awareness", Mock())
        monkeypatch.setattr(legacy, "_detect_scale", Mock(return_value=1.0))
        monkeypatch.setattr(legacy, "App", Mock(side_effect=_Stop("App built")))
        yield module, legacy


def test_bench_move_strips_the_flag_before_reading_the_window_size(bench, monkeypatch, capsys):
    module, legacy = bench
    opened = Mock(return_value="gpu")
    monkeypatch.setattr(gpu_backend, "open_gpu", opened)
    monkeypatch.setattr(sys, "argv", ["bench_move", "--backend", "python", "800x600"])
    module.main()
    out = capsys.readouterr().out
    assert "window size 800x600" in out
    assert sys.argv == ["bench_move", "800x600"]
    assert os.environ[gpu_backend.ENV] == "python"
    opened.assert_called_once()
    legacy.App.assert_called_once_with("gpu")


@pytest.mark.parametrize("environment, argv", [(None, ["--backend=bogus"]), ("bogus", [])])
def test_bench_move_rejects_an_unknown_value_with_code_2(bench, monkeypatch, capsys, environment, argv):
    module, _ = bench
    if environment:
        monkeypatch.setenv(gpu_backend.ENV, environment)
    monkeypatch.setattr(sys, "argv", ["bench_move", *argv])
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert exited.value.code == 2
    out, err = capsys.readouterr()
    assert "auto, native or python" in err and "window size" not in out


def test_bench_move_reports_a_refused_backend_instead_of_skipping(bench, monkeypatch, capsys):
    module, legacy = bench
    monkeypatch.setattr(gpu_backend, "open_gpu", Mock(side_effect=gpu_backend.BackendRefused("surface incomplete")))
    monkeypatch.setattr(sys, "argv", ["bench_move", "--backend", "native"])
    with pytest.raises(SystemExit) as exited:
        module.main()
    assert exited.value.code == 1
    out, err = capsys.readouterr()
    assert "GPU backend refused: surface incomplete" in err
    assert "skipped" not in out
    legacy.App.assert_not_called()


def test_bench_move_notes_an_auto_fallback_when_backend_was_given(bench, monkeypatch, capsys):
    module, legacy = bench
    built = []
    _auto_falls_back(monkeypatch, built)
    monkeypatch.setattr(sys, "argv", ["bench_move", "--backend", "auto"])
    module.main()   # the stubbed App raises _Stop, which the tool reports as "skipped"
    out, err = capsys.readouterr()
    assert len(built) == 1
    assert err.count("backend: Python (requested auto via --backend; native core not loaded") == 1


# ---- describe() in the app: the log line and Help > About -----------------------------------------

_FALLBACK = gpu_backend.Choice("python", "auto", "DRUTA_BACKEND", None, None, "native core not loaded: none")
_PLAIN = gpu_backend.Choice("python", "python", "default", None, None, None)


def _run_until_first_curve_read(monkeypatch):
    """Druta.run() on a stand-in app with fake DPG, stopped at its first vf_read; returns the log mock."""
    app = MagicMock()
    app.s = lambda value: value
    app.gpu.available.return_value = True
    app.gpu.status_line.return_value = "NVAPI: ok | NVML: ok"
    app.gpu_list = []
    app.KNOB_COLS = [100, 100]
    app.KNOB_COL_PAD = 10
    app._recovery_result = None
    app.vf_read.side_effect = _Stop("first curve read")
    monkeypatch.setattr(druta, "dpg", MagicMock())
    monkeypatch.setattr(druta.threading, "Thread", MagicMock())
    with pytest.raises(_Stop):
        druta.Druta.run(app)
    return app.log


@pytest.mark.parametrize(("pinned", "ok"), [(_FALLBACK, False), (_PLAIN, None)])
def test_run_logs_describe_beside_the_status_line(monkeypatch, pinned, ok):
    monkeypatch.setattr(gpu_backend, "_choice", pinned)
    log = _run_until_first_curve_read(monkeypatch)
    lines = [entry.args for entry in log.call_args_list]
    status = lines.index(("backend: NVAPI: ok | NVML: ok",))
    assert lines[status + 1] == (gpu_backend.describe(), ok)
    assert sum(1 for entry in lines if entry[0] == gpu_backend.describe()) == 1


@pytest.mark.parametrize("pinned", [_FALLBACK, _PLAIN])
def test_about_window_shows_describe_next_to_the_version(monkeypatch, pinned):
    monkeypatch.setattr(gpu_backend, "_choice", pinned)
    ui = MagicMock()
    monkeypatch.setattr(druta, "dpg", ui)
    app = MagicMock()
    app.s = lambda value: value
    app.step_khz.return_value = 15000
    druta.Druta.build_tool_windows(app)
    calls = ui.mock_calls
    about = next(i for i, entry in enumerate(calls)
                 if entry[0] == "window" and entry.kwargs.get("tag") == "win_about")
    following = [entry for entry in calls[about + 1:] if entry[0] == "add_text"]
    assert following[0].args == (f"Thermetery Druta {druta.__version__}",)
    assert following[1].args == (gpu_backend.describe(),)
