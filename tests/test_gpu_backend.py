# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The backend factory (gpu_backend): selection, pin, overrides, startup-profile rule and log text.

The native module and nvbackend are replaced by fakes, so no library has to be built and no
driver is touched. One class runs the real ``native.load()`` against fake libraries to show that a
missing library, a missing export and an ABI mismatch reach the factory as reasons.
"""
from __future__ import annotations

import ctypes
import os
import sys
import types

import pytest

from druta import gpu_backend


class FakeNativeUnavailable(ImportError):
    pass


class FakeNativeGpu:
    def __init__(self, slot, available=True):
        self.slot, self._available = slot, available

    def available(self):
        return self._available


class FakePythonGpu:
    def __init__(self, slot):
        self.slot = slot


class Calls(list):
    pass


def make_native(*, load_error=None, open_error=None, available=True, enum_error=None):
    calls = Calls()
    native = types.SimpleNamespace(NativeUnavailable=FakeNativeUnavailable, ABI_VERSION=7)
    library = types.SimpleNamespace(path="/opt/drutacore/libdrutacore.so")

    def load():
        calls.append("load")
        if load_error is not None:
            raise load_error
        return library

    def gpu(slot):
        calls.append(("Gpu", slot))
        if open_error is not None:
            raise open_error
        return FakeNativeGpu(slot, available)

    def enumerate_gpus():
        calls.append("enumerate")
        if enum_error is not None:
            raise enum_error
        return [{"slot": "native-slot"}]

    native.load, native.Gpu, native.enumerate_gpus, native.calls = load, gpu, enumerate_gpus, calls
    return native


def make_python():
    calls = Calls()

    def gpu(slot):
        calls.append(("GPU", slot))
        return FakePythonGpu(slot)

    def enumerate_gpus():
        calls.append("enumerate")
        return [{"slot": "python-slot"}]

    return types.SimpleNamespace(GPU=gpu, enumerate_gpus=enumerate_gpus, calls=calls)


def complete(native_module):
    return ()


_MISSING = object()


@pytest.fixture(autouse=True)
def environment_guard():
    """Fail any test that leaves DRUTA_BACKEND different from how it found it (and put it back)."""
    before = os.environ.get(gpu_backend.ENV, _MISSING)
    yield
    after = os.environ.get(gpu_backend.ENV, _MISSING)
    if after != before:
        if before is _MISSING:
            os.environ.pop(gpu_backend.ENV, None)
        else:
            os.environ[gpu_backend.ENV] = before
        pytest.fail(f"{gpu_backend.ENV} leaked out of the test: {before!r} -> {after!r}")


def clear_backend_env(monkeypatch):
    """Remove DRUTA_BACKEND so that monkeypatch restores the original value or absence at teardown.

    delenv alone records nothing when the variable is absent, so a later consume_argv write would
    outlive the test; setenv first makes monkeypatch record the original state.
    """
    monkeypatch.setenv(gpu_backend.ENV, "python")
    monkeypatch.delenv(gpu_backend.ENV)


@pytest.fixture
def env(monkeypatch):
    """A clean factory, a clean environment and argv, and fake backends; returns a setup function."""
    gpu_backend.reset_for_tests()
    clear_backend_env(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["druta"])
    state = types.SimpleNamespace(native=make_native(), python=make_python(), native_imports=0)

    def native_module():
        state.native_imports += 1
        return state.native

    monkeypatch.setattr(gpu_backend, "_native_module", native_module)
    monkeypatch.setattr(gpu_backend, "_python_module", lambda: state.python)
    monkeypatch.setattr(gpu_backend, "surface_gate", complete)

    def setup(**native_options):
        state.native = make_native(**native_options)
        return state

    yield setup
    gpu_backend.reset_for_tests()


# ---- python never touches native ---------------------------------------------------------------

def test_default_is_python_and_never_loads_native(env):
    state = env()
    gpu = gpu_backend.open_gpu("0000:01:00.0")
    assert isinstance(gpu, FakePythonGpu) and gpu.slot == "0000:01:00.0"
    assert state.native_imports == 0 and state.native.calls == []
    assert gpu_backend.choice() == gpu_backend.Choice("python", "python", "default", None, None, None)
    assert gpu_backend.enumerate_gpus() == [{"slot": "python-slot"}]
    assert state.native_imports == 0


@pytest.mark.parametrize("how", ["argument", "environment", "argv"])
def test_explicit_python_never_loads_native(env, monkeypatch, how):
    state = env()
    if how == "argument":
        gpu_backend.open_gpu(kind="python")
    else:
        if how == "environment":
            monkeypatch.setenv(gpu_backend.ENV, "python")
        else:
            gpu_backend.consume_argv(["--backend", "python"])
        gpu_backend.open_gpu()
    gpu_backend.enumerate_gpus()
    assert state.native_imports == 0 and state.native.calls == []
    assert gpu_backend.choice().kind == "python"


def test_python_enumerate_first_never_loads_native(env):
    state = env()
    assert gpu_backend.enumerate_gpus() == [{"slot": "python-slot"}]
    assert state.native_imports == 0
    assert gpu_backend.choice().kind == "python"


def test_import_does_not_load_the_native_library():
    import subprocess
    code = ("import sys; import druta.gpu_backend; "
            "assert 'druta.native' not in sys.modules and 'druta.nvbackend' not in sys.modules, sorted(sys.modules)")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": src},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# ---- the mode x condition matrix ----------------------------------------------------------------

LOAD_REASONS = {
    "library absent": "native core not loaded: /x/libdrutacore.so: not present",
    "ABI mismatch": "native core not loaded: /x/libdrutacore.so: ABI 3, this build needs 7",
    "missing export": "native core not loaded: /x/libdrutacore.so: undefined symbol: dc_gpu_open",
}


@pytest.mark.parametrize("condition", sorted(LOAD_REASONS))
def test_auto_load_failure_uses_python(env, condition):
    state = env(load_error=FakeNativeUnavailable(LOAD_REASONS[condition]))
    gpu = gpu_backend.open_gpu("s", kind="auto")
    assert isinstance(gpu, FakePythonGpu)
    assert state.native.calls == ["load"]  # no construction after a failed load
    assert gpu_backend.choice() == gpu_backend.Choice("python", "auto", "argument", None, None,
                                                      LOAD_REASONS[condition])


@pytest.mark.parametrize("condition", sorted(LOAD_REASONS))
def test_forced_native_load_failure_is_refused(env, condition):
    state = env(load_error=FakeNativeUnavailable(LOAD_REASONS[condition]))
    with pytest.raises(gpu_backend.BackendRefused, match=LOAD_REASONS[condition].replace(".", r"\.")):
        gpu_backend.open_gpu("s", kind="native")
    assert state.python.calls == []
    assert gpu_backend.choice() is None


def test_auto_surface_incomplete_uses_python(env, monkeypatch):
    state = env()
    monkeypatch.setattr(gpu_backend, "surface_gate", lambda native: ["clock_offset_range", "msvdd_offset_domains"])
    gpu = gpu_backend.open_gpu(kind="auto")
    assert isinstance(gpu, FakePythonGpu)
    assert ("Gpu", None) not in state.native.calls
    assert gpu_backend.choice().reason == "native surface incomplete: clock_offset_range, msvdd_offset_domains"


def test_forced_native_surface_incomplete_is_refused(env, monkeypatch):
    state = env()
    monkeypatch.setattr(gpu_backend, "surface_gate", lambda native: ["clock_offset_range"])
    with pytest.raises(gpu_backend.BackendRefused, match="native surface incomplete: clock_offset_range"):
        gpu_backend.open_gpu(kind="native")
    assert state.python.calls == [] and ("Gpu", None) not in state.native.calls


def test_long_surface_list_is_shortened(env, monkeypatch):
    env()
    monkeypatch.setattr(gpu_backend, "surface_gate", lambda native: [f"m{i}" for i in range(20)])
    gpu_backend.open_gpu(kind="auto")
    assert gpu_backend.choice().reason.endswith("m11 and 8 more")


def test_default_surface_gate_reports_incomplete(env, monkeypatch):
    """Until the front-end surface check exists, auto resolves to python and forced native is refused."""
    monkeypatch.setattr(gpu_backend, "surface_gate", gpu_backend.surface_not_provided)
    state = env()
    with pytest.raises(gpu_backend.BackendRefused, match="native surface incomplete: .*not provided yet"):
        gpu_backend.open_gpu(kind="native")
    assert isinstance(gpu_backend.open_gpu(kind="auto"), FakePythonGpu)
    assert gpu_backend.choice().reason.startswith("native surface incomplete: ")
    assert ("Gpu", None) not in state.native.calls


def test_the_gate_is_given_the_native_module(env, monkeypatch):
    state = env()
    seen = []
    monkeypatch.setattr(gpu_backend, "surface_gate", lambda native: seen.append(native) or ())
    gpu_backend.open_gpu(kind="auto")
    assert seen == [state.native]


OPEN_FAILURES = [ValueError("dc_gpu_open: no such slot"), OSError("exception: access violation"),
                 MemoryError("out"), FakeNativeUnavailable("unloaded mid-open")]


@pytest.mark.parametrize("error", OPEN_FAILURES, ids=lambda e: type(e).__name__)
def test_auto_open_failure_uses_python(env, error):
    state = env(open_error=error)
    gpu = gpu_backend.open_gpu("0000:02:00.0", kind="auto")
    assert isinstance(gpu, FakePythonGpu) and gpu.slot == "0000:02:00.0"
    assert state.native.calls == ["load", ("Gpu", "0000:02:00.0")]
    assert gpu_backend.choice().reason == f"native open failed: {type(error).__name__}: {error}"


@pytest.mark.parametrize("error", OPEN_FAILURES, ids=lambda e: type(e).__name__)
def test_forced_native_open_failure_is_refused_never_falls_back(env, error):
    state = env(open_error=error)
    with pytest.raises(gpu_backend.BackendRefused, match="native open failed") as caught:
        gpu_backend.open_gpu("s", kind="native")
    assert caught.value.__cause__ is error
    assert state.python.calls == []
    assert gpu_backend.choice() is None


class UnexpectedError(Exception):
    pass


@pytest.mark.parametrize("kind", ["auto", "native"])
@pytest.mark.parametrize("error", [UnexpectedError("bug"), TypeError("bug"), AttributeError("bug"), RuntimeError("bug")],
                         ids=lambda e: type(e).__name__)
def test_unexpected_open_exception_propagates(env, kind, error):
    state = env(open_error=error)
    with pytest.raises(type(error)) as caught:
        gpu_backend.open_gpu(kind=kind)
    assert caught.value is error
    assert state.python.calls == []
    assert gpu_backend.choice() is None


@pytest.mark.parametrize("kind", ["auto", "native"])
@pytest.mark.parametrize("where", ["load", "open", "gate"])
def test_keyboard_interrupt_propagates(env, monkeypatch, kind, where):
    if where == "load":
        state = env(load_error=KeyboardInterrupt())
    elif where == "open":
        state = env(open_error=KeyboardInterrupt())
    else:
        state = env()

        def gate(native):
            raise KeyboardInterrupt()
        monkeypatch.setattr(gpu_backend, "surface_gate", gate)
    with pytest.raises(KeyboardInterrupt):
        gpu_backend.open_gpu(kind=kind)
    assert state.python.calls == [] and gpu_backend.choice() is None


def test_unexpected_load_exception_propagates(env):
    env(load_error=UnexpectedError("bug in load"))
    with pytest.raises(UnexpectedError):
        gpu_backend.open_gpu(kind="auto")


@pytest.mark.parametrize("kind", ["auto", "native"])
def test_native_success(env, kind):
    state = env()
    gpu = gpu_backend.open_gpu("0000:01:00.0", kind=kind)
    assert isinstance(gpu, FakeNativeGpu) and gpu.slot == "0000:01:00.0"
    assert state.python.calls == []
    assert gpu_backend.choice() == gpu_backend.Choice("native", kind, "argument", "/opt/drutacore/libdrutacore.so",
                                                      7, None)


@pytest.mark.parametrize("kind", ["auto", "native"])
def test_native_open_but_unavailable_stays_native(env, kind):
    state = env(available=False)
    gpu = gpu_backend.open_gpu(kind=kind)
    assert isinstance(gpu, FakeNativeGpu)
    assert state.python.calls == []  # no Python comparison object is built
    assert gpu_backend.choice().kind == "native"
    assert "reports it unavailable" in gpu_backend.choice().reason
    assert "start with --backend python to use the Python backend" in gpu_backend.describe()


# ---- enumerate_gpus as the first call -----------------------------------------------------------

def test_auto_enumerate_first_uses_native_enumeration(env, monkeypatch):
    state = env()
    monkeypatch.setenv(gpu_backend.ENV, "auto")
    assert gpu_backend.enumerate_gpus() == [{"slot": "native-slot"}]
    assert ("Gpu", None) not in state.native.calls  # listing opens no handle
    assert gpu_backend.choice().kind == "native" and gpu_backend.choice().reason is None
    assert isinstance(gpu_backend.open_gpu("s"), FakeNativeGpu)


def test_auto_enumerate_failure_falls_back(env, monkeypatch):
    state = env(enum_error=OSError("fault"))
    monkeypatch.setenv(gpu_backend.ENV, "auto")
    assert gpu_backend.enumerate_gpus() == [{"slot": "python-slot"}]
    assert gpu_backend.choice().reason == "native open failed: OSError: fault"
    assert state.python.calls == ["enumerate"]


def test_forced_native_enumerate_failure_is_refused(env, monkeypatch):
    state = env(enum_error=ValueError("bad"))
    monkeypatch.setenv(gpu_backend.ENV, "native")
    with pytest.raises(gpu_backend.BackendRefused, match="native open failed: ValueError: bad"):
        gpu_backend.enumerate_gpus()
    assert state.python.calls == []


# ---- pinning ------------------------------------------------------------------------------------

def test_pin_follows_the_first_decision(env, monkeypatch):
    state = env()
    monkeypatch.setenv(gpu_backend.ENV, "native")
    first = gpu_backend.open_gpu("a")
    monkeypatch.setenv(gpu_backend.ENV, "python")  # a later environment change does not move the pin
    second = gpu_backend.open_gpu("b")
    assert isinstance(first, FakeNativeGpu) and isinstance(second, FakeNativeGpu)
    assert gpu_backend.enumerate_gpus() == [{"slot": "native-slot"}]
    assert state.python.calls == []
    assert state.native.calls.count("load") == 1


def test_after_pin_errors_propagate_without_fallback(env, monkeypatch):
    state = env()
    gpu_backend.open_gpu(kind="auto")
    assert gpu_backend.choice().kind == "native"
    error = ValueError("card gone")

    def failing(slot):
        raise error
    monkeypatch.setattr(state.native, "Gpu", failing)
    with pytest.raises(ValueError) as caught:
        gpu_backend.open_gpu("b")
    assert caught.value is error and state.python.calls == []


@pytest.mark.parametrize("first, later, allowed", [
    ("python", "python", True), ("python", "native", False), ("python", "auto", False),
    ("native", "native", True), ("native", "python", False), ("native", "auto", False),
    ("auto", "auto", True), ("auto", "native", True), ("auto", "python", False),
])
def test_mismatch_after_pin(env, first, later, allowed):
    env()
    gpu_backend.open_gpu(kind=first)
    if allowed:
        gpu_backend.open_gpu(kind=later)
    else:
        with pytest.raises(gpu_backend.BackendMismatch, match=f"cannot switch to {later}"):
            gpu_backend.open_gpu(kind=later)
    assert gpu_backend.choice().requested == first


def test_auto_resolved_to_python_accepts_python_and_rejects_native(env):
    env(load_error=FakeNativeUnavailable("absent"))
    gpu_backend.open_gpu(kind="auto")
    assert isinstance(gpu_backend.open_gpu(kind="python"), FakePythonGpu)
    assert isinstance(gpu_backend.open_gpu(kind="auto"), FakePythonGpu)
    with pytest.raises(gpu_backend.BackendMismatch):
        gpu_backend.open_gpu(kind="native")


def test_refusal_pins_nothing(env, monkeypatch):
    state = env(load_error=FakeNativeUnavailable("absent"))
    with pytest.raises(gpu_backend.BackendRefused):
        gpu_backend.open_gpu(kind="native")
    assert gpu_backend.choice() is None
    assert isinstance(gpu_backend.open_gpu(kind="python"), FakePythonGpu)
    assert state.python.calls == [("GPU", None)]


def test_concurrent_first_calls_decide_once(env, monkeypatch):
    """The first load blocks until every thread has entered open_gpu: without the lock all of them would load."""
    import threading
    import time
    threads_n = 8
    state = env()
    monkeypatch.setenv(gpu_backend.ENV, "auto")
    entered, entered_lock, all_entered = [0], threading.Lock(), threading.Event()
    real_load, gate_calls, results, errors = state.native.load, [], [], []

    def blocking_load():
        assert all_entered.wait(timeout=5), "not every thread reached open_gpu"
        time.sleep(0.05)  # let the others reach the decision (the lock, or a load of their own)
        return real_load()
    monkeypatch.setattr(state.native, "load", blocking_load)
    monkeypatch.setattr(gpu_backend, "surface_gate", lambda native: gate_calls.append(1) or ())

    def worker():
        with entered_lock:
            entered[0] += 1
            if entered[0] == threads_n:
                all_entered.set()
        try:
            results.append(type(gpu_backend.open_gpu()))
        except BaseException as exc:  # reported below rather than lost in the thread
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert state.native.calls.count("load") == 1 and len(gate_calls) == 1
    assert results == [FakeNativeGpu] * threads_n
    assert gpu_backend.choice().kind == "native"


# ---- precedence and the startup-profile rule ----------------------------------------------------

def test_argument_beats_argv_beats_environment(env, monkeypatch):
    env()
    monkeypatch.setenv(gpu_backend.ENV, "native")
    gpu_backend.open_gpu(kind="python")
    assert gpu_backend.choice().source == "argument" and gpu_backend.choice().kind == "python"


def test_argv_overrides_environment_and_is_named(env, monkeypatch):
    env()
    monkeypatch.setenv(gpu_backend.ENV, "native")
    gpu_backend.consume_argv(["--backend", "python"])
    gpu_backend.open_gpu()
    assert gpu_backend.choice().requested == "python" and gpu_backend.choice().source == "--backend"


def test_environment_source_is_named(env, monkeypatch):
    env()
    monkeypatch.setenv(gpu_backend.ENV, " Native ")
    gpu_backend.open_gpu()
    assert gpu_backend.choice().requested == "native" and gpu_backend.choice().source == gpu_backend.ENV


def test_empty_environment_value_means_unset(env, monkeypatch):
    env()
    monkeypatch.setenv(gpu_backend.ENV, "")
    gpu_backend.open_gpu()
    assert gpu_backend.choice().source == "default"


@pytest.mark.parametrize("value", ["fast", "nvapi", "1"])
def test_unknown_environment_value(env, monkeypatch, value):
    state = env()
    monkeypatch.setenv(gpu_backend.ENV, value)
    with pytest.raises(ValueError, match="^DRUTA_BACKEND must be auto, native or python$"):
        gpu_backend.open_gpu()
    with pytest.raises(ValueError, match="^DRUTA_BACKEND must be auto, native or python$"):
        gpu_backend.enumerate_gpus()
    assert state.native_imports == 0 and state.python.calls == []


def test_unknown_kind_argument(env):
    env()
    with pytest.raises(ValueError, match="^DRUTA_BACKEND must be auto, native or python$"):
        gpu_backend.open_gpu(kind="cuda")


def test_startup_profile_asks_for_python(env, monkeypatch):
    """With the default flipped to auto, --startup-profile still asks for python."""
    state = env()
    monkeypatch.setattr(gpu_backend, "_DEFAULT", "auto")
    monkeypatch.setattr(sys, "argv", ["Druta.exe", "--startup-profile"])
    assert isinstance(gpu_backend.open_gpu(), FakePythonGpu)
    assert gpu_backend.choice() == gpu_backend.Choice("python", "python", "--startup-profile", None, None, None)
    assert state.native_imports == 0


def test_startup_profile_yields_to_an_explicit_request(env, monkeypatch):
    env()
    monkeypatch.setattr(sys, "argv", ["Druta.exe", "--startup-profile"])
    monkeypatch.setenv(gpu_backend.ENV, "native")
    assert isinstance(gpu_backend.open_gpu(), FakeNativeGpu)
    assert gpu_backend.choice().source == gpu_backend.ENV


def test_startup_profile_yields_to_backend_flag(env, monkeypatch):
    env()
    argv = ["Druta.exe", "--startup-profile", "--backend", "auto"]
    monkeypatch.setattr(sys, "argv", argv)
    gpu_backend.consume_argv(sys.argv)
    assert sys.argv == ["Druta.exe", "--startup-profile"]
    assert isinstance(gpu_backend.open_gpu(), FakeNativeGpu)
    assert gpu_backend.choice().source == "--backend"


def test_without_startup_profile_the_default_applies(env, monkeypatch):
    env()
    monkeypatch.setattr(gpu_backend, "_DEFAULT", "auto")
    assert isinstance(gpu_backend.open_gpu(), FakeNativeGpu)
    assert gpu_backend.choice().source == "default" and gpu_backend.choice().requested == "auto"


def test_the_shipped_default_is_python(env):
    env()
    assert gpu_backend._DEFAULT == "python"


# ---- consume_argv -------------------------------------------------------------------------------

@pytest.mark.parametrize("argv, rest, value", [
    (["--backend", "native"], [], "native"),
    (["--backend=auto"], [], "auto"),
    (["--gpu", "0000:01:00.0", "--backend", "python", "--list-gpus"], ["--gpu", "0000:01:00.0", "--list-gpus"],
     "python"),
    (["Druta.exe", "--backend=NATIVE", "--startup-profile"], ["Druta.exe", "--startup-profile"], "native"),
    (["--registry-overrides", "--backend", "auto", "status"], ["--registry-overrides", "status"], "auto"),
    (["--backend", "native", "--backend=python"], [], "python"),
])
def test_consume_argv_strips_and_sets_environment(env, monkeypatch, argv, rest, value):
    env()
    original = argv
    returned = gpu_backend.consume_argv(argv)
    assert returned is original and argv == rest  # in place, for consume_argv(sys.argv)
    assert os.environ[gpu_backend.ENV] == value


def test_consume_argv_without_flag_changes_nothing(env):
    env()
    argv = ["--gpu", "x", "--backends", "--backendx=1"]
    assert gpu_backend.consume_argv(argv) == ["--gpu", "x", "--backends", "--backendx=1"]
    assert gpu_backend.ENV not in os.environ


@pytest.mark.parametrize("argv, message", [
    (["--gpu", "x", "--backend"], "--backend needs a value"),
    (["--backend="], "--backend needs a value"),
    (["--backend", "fast"], "^DRUTA_BACKEND must be auto, native or python$"),
    (["--backend=cuda", "--gpu", "x"], "^DRUTA_BACKEND must be auto, native or python$"),
    (["--backend", "--gpu", "x"], "^DRUTA_BACKEND must be auto, native or python$"),
])
def test_consume_argv_bad_values(env, argv, message):
    env()
    before = list(argv)
    with pytest.raises(ValueError, match=message):
        gpu_backend.consume_argv(argv)
    assert argv == before and gpu_backend.ENV not in os.environ


@pytest.mark.parametrize("argv", [["--backend", "native", "--backend", "fast"], ["--backend=auto", "--backend"]])
def test_consume_argv_is_atomic(env, monkeypatch, argv):
    """A valid value followed by a bad one changes nothing: no environment, no argv change, no --backend record."""
    env()
    before = list(argv)
    with pytest.raises(ValueError):
        gpu_backend.consume_argv(argv)
    assert argv == before and gpu_backend.ENV not in os.environ
    assert gpu_backend.describe() == "backend: not chosen yet (requested python via default)"
    monkeypatch.setenv(gpu_backend.ENV, before[0].partition("=")[2] or before[1])
    gpu_backend.open_gpu()
    assert gpu_backend.choice().source == gpu_backend.ENV


def test_consume_argv_reaches_child_environment(env):
    """Children started through subprocess inherit the request through os.environ."""
    import subprocess
    env()
    gpu_backend.consume_argv(["--backend", "native"])
    out = subprocess.run([sys.executable, "-c", f"import os; print(os.environ[{gpu_backend.ENV!r}])"],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "native"


# ---- describe() ---------------------------------------------------------------------------------

def test_describe_before_any_decision(env, monkeypatch):
    env()
    assert gpu_backend.describe() == "backend: not chosen yet (requested python via default)"
    monkeypatch.setenv(gpu_backend.ENV, "bogus")
    assert gpu_backend.describe() == "backend: not chosen (DRUTA_BACKEND must be auto, native or python)"


def test_describe_python_default(env):
    env()
    gpu_backend.open_gpu()
    assert gpu_backend.describe() == "backend: Python (requested python via default)"


def test_describe_native(env, monkeypatch):
    env()
    monkeypatch.setenv(gpu_backend.ENV, "auto")
    gpu_backend.open_gpu()
    assert gpu_backend.describe() == ("backend: native core (/opt/drutacore/libdrutacore.so, ABI 7; "
                                      "requested auto via DRUTA_BACKEND)")


def test_describe_native_unavailable(env):
    env(available=False)
    gpu_backend.open_gpu(kind="native")
    assert gpu_backend.describe() == (
        "backend: native core (/opt/drutacore/libdrutacore.so, ABI 7; requested native via argument); "
        "the native core opened the card but reports it unavailable: "
        "start with --backend python to use the Python backend")


def test_describe_auto_fallback(env):
    env(load_error=FakeNativeUnavailable("native core not loaded: no candidate location"))
    gpu_backend.consume_argv(["--backend", "auto"])
    gpu_backend.open_gpu()
    assert gpu_backend.describe() == ("backend: Python (requested auto via --backend; "
                                      "native core not loaded: no candidate location)")


def test_describe_open_failure_fallback(env):
    env(open_error=OSError("access violation"))
    gpu_backend.open_gpu(kind="auto")
    assert gpu_backend.describe() == ("backend: Python (requested auto via argument; "
                                      "native open failed: OSError: access violation)")


def test_describe_refusal(env, monkeypatch):
    env()
    monkeypatch.setattr(gpu_backend, "surface_gate", lambda native: ["clock_offset_range"])
    monkeypatch.setenv(gpu_backend.ENV, "native")
    with pytest.raises(gpu_backend.BackendRefused):
        gpu_backend.open_gpu()
    assert gpu_backend.describe() == ("backend: native refused (requested native via DRUTA_BACKEND): "
                                      "native surface incomplete: clock_offset_range")


def test_describe_startup_profile(env, monkeypatch):
    env()
    monkeypatch.setattr(sys, "argv", ["Druta.exe", "--startup-profile"])
    gpu_backend.open_gpu()
    assert gpu_backend.describe() == "backend: Python (requested python via --startup-profile)"


def test_choice_is_frozen(env):
    env()
    gpu_backend.open_gpu()
    with pytest.raises(AttributeError):
        gpu_backend.choice().kind = "native"


def test_reset_for_tests_clears_pin_and_argv_record(env):
    env()
    gpu_backend.consume_argv(["--backend", "python"])
    gpu_backend.open_gpu()
    gpu_backend.reset_for_tests()
    assert gpu_backend.choice() is None
    gpu_backend.open_gpu(kind="native")
    assert gpu_backend.choice().kind == "native"


# ---- an OSError from load() is a load failure ---------------------------------------------------

LOAD_OS_ERRORS = [OSError(36, "File name too long"), PermissionError(13, "Permission denied")]


def _first_call(first):
    return gpu_backend.open_gpu("s") if first == "open" else gpu_backend.enumerate_gpus()


@pytest.mark.parametrize("first", ["open", "enumerate"])
@pytest.mark.parametrize("error", LOAD_OS_ERRORS, ids=lambda e: type(e).__name__)
def test_auto_load_oserror_falls_back(env, monkeypatch, first, error):
    state = env(load_error=error)
    monkeypatch.setenv(gpu_backend.ENV, "auto")
    result = _first_call(first)
    assert (isinstance(result, FakePythonGpu) if first == "open" else result == [{"slot": "python-slot"}])
    assert gpu_backend.choice().kind == "python"
    assert gpu_backend.choice().reason == f"native core not loaded: {type(error).__name__}: {error}"
    assert ("Gpu", "s") not in state.native.calls and "enumerate" not in state.native.calls


@pytest.mark.parametrize("first", ["open", "enumerate"])
@pytest.mark.parametrize("error", LOAD_OS_ERRORS, ids=lambda e: type(e).__name__)
def test_forced_native_load_oserror_is_refused(env, monkeypatch, first, error):
    state = env(load_error=error)
    monkeypatch.setenv(gpu_backend.ENV, "native")
    reason = f"native core not loaded: {type(error).__name__}: {error}"
    with pytest.raises(gpu_backend.BackendRefused) as caught:
        _first_call(first)
    assert str(caught.value) == reason
    assert state.python.calls == [] and gpu_backend.choice() is None
    assert gpu_backend.describe() == f"backend: native refused (requested native via DRUTA_BACKEND): {reason}"


# ---- enumerate_gpus as the first call: the rest of the matrix ------------------------------------

@pytest.mark.parametrize("kind", ["auto", "native"])
def test_enumerate_first_native_unavailable(env, monkeypatch, kind):
    state = env(load_error=FakeNativeUnavailable("native core not loaded: no candidate location"))
    monkeypatch.setenv(gpu_backend.ENV, kind)
    if kind == "auto":
        assert gpu_backend.enumerate_gpus() == [{"slot": "python-slot"}]
        assert gpu_backend.choice().reason == "native core not loaded: no candidate location"
    else:
        with pytest.raises(gpu_backend.BackendRefused, match="no candidate location"):
            gpu_backend.enumerate_gpus()
        assert state.python.calls == [] and gpu_backend.choice() is None
    assert "enumerate" not in state.native.calls


@pytest.mark.parametrize("kind", ["auto", "native"])
def test_enumerate_first_surface_incomplete(env, monkeypatch, kind):
    state = env()
    monkeypatch.setenv(gpu_backend.ENV, kind)
    monkeypatch.setattr(gpu_backend, "surface_gate", lambda native: ["clock_offset_range"])
    if kind == "auto":
        assert gpu_backend.enumerate_gpus() == [{"slot": "python-slot"}]
        assert gpu_backend.choice().reason == "native surface incomplete: clock_offset_range"
    else:
        with pytest.raises(gpu_backend.BackendRefused, match="native surface incomplete: clock_offset_range"):
            gpu_backend.enumerate_gpus()
        assert state.python.calls == []
    assert "enumerate" not in state.native.calls


@pytest.mark.parametrize("kind", ["auto", "native"])
@pytest.mark.parametrize("where", ["load", "gate", "enumerate"])
def test_enumerate_first_keyboard_interrupt_propagates(env, monkeypatch, kind, where):
    if where == "load":
        state = env(load_error=KeyboardInterrupt())
    elif where == "enumerate":
        state = env(enum_error=KeyboardInterrupt())
    else:
        state = env()

        def gate(native):
            raise KeyboardInterrupt()
        monkeypatch.setattr(gpu_backend, "surface_gate", gate)
    monkeypatch.setenv(gpu_backend.ENV, kind)
    with pytest.raises(KeyboardInterrupt):
        gpu_backend.enumerate_gpus()
    assert state.python.calls == [] and gpu_backend.choice() is None


@pytest.mark.parametrize("kind", ["auto", "native"])
@pytest.mark.parametrize("error", [UnexpectedError("bug"), TypeError("bug"), RuntimeError("bug")],
                         ids=lambda e: type(e).__name__)
def test_enumerate_first_unexpected_exception_propagates(env, monkeypatch, kind, error):
    state = env(enum_error=error)
    monkeypatch.setenv(gpu_backend.ENV, kind)
    with pytest.raises(type(error)) as caught:
        gpu_backend.enumerate_gpus()
    assert caught.value is error
    assert state.python.calls == [] and gpu_backend.choice() is None


# ---- describe() once a refusal is followed by a pin ----------------------------------------------

def test_describe_after_refusal_then_pin(env, monkeypatch):
    env(load_error=FakeNativeUnavailable("native core not loaded: absent"))
    with pytest.raises(gpu_backend.BackendRefused):
        gpu_backend.open_gpu(kind="native")
    assert gpu_backend.describe().startswith("backend: native refused")
    gpu_backend.open_gpu(kind="python")
    assert gpu_backend.describe() == "backend: Python (requested python via argument)"


def test_describe_after_refusal_then_auto_pin(env):
    env(load_error=FakeNativeUnavailable("native core not loaded: absent"))
    with pytest.raises(gpu_backend.BackendRefused):
        gpu_backend.open_gpu(kind="native")
    gpu_backend.open_gpu(kind="auto")
    assert gpu_backend.describe() == "backend: Python (requested auto via argument; native core not loaded: absent)"


# ---- python in a fresh process never imports the native module -----------------------------------

def test_python_in_a_fresh_process_never_imports_native():
    """The real lazy imports, not the test hooks: a python open and enumeration leave druta.native unimported.

    druta.nvbackend is stubbed in the child, so no driver library is needed.
    """
    import subprocess
    code = "\n".join([
        "import sys, types",
        "import druta",
        "fake = types.ModuleType('druta.nvbackend')",
        "fake.GPU = lambda slot=None: ('python-gpu', slot)",
        "fake.enumerate_gpus = lambda: [{'slot': 'python-slot'}]",
        "sys.modules['druta.nvbackend'] = fake",
        "druta.nvbackend = fake",
        "from druta import gpu_backend",
        "assert gpu_backend.open_gpu('s', kind='python') == ('python-gpu', 's')",
        "assert gpu_backend.enumerate_gpus() == [{'slot': 'python-slot'}]",
        "assert gpu_backend.open_gpu('t') == ('python-gpu', 't')",
        "assert gpu_backend.choice().kind == 'python'",
        "assert 'druta.native' not in sys.modules, 'druta.native was imported'",
        "print('ok')",
    ])
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    child_env = {key: value for key, value in os.environ.items() if key != gpu_backend.ENV}
    child_env["PYTHONPATH"] = src
    result = subprocess.run([sys.executable, "-c", code], env=child_env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ---- the real native.load() reaching the factory ------------------------------------------------

class FakeFunction:
    def __init__(self, result=0):
        self.result = result

    def __call__(self, *args):
        return self.result


class FakeLibrary:
    """A loaded library for native._bind: every export present unless named in ``missing``."""

    def __init__(self, abi, missing=()):
        self._abi, self._missing = abi, set(missing)

    def __getattr__(self, name):
        if name in self._missing:
            raise AttributeError(f"undefined symbol: {name}")
        function = FakeFunction(self._abi if name == "dc_abi_version" else 0)
        object.__setattr__(self, name, function)
        return function


@pytest.fixture
def real_loader(monkeypatch, tmp_path):
    """The real native module with its loader pointed at fake libraries; nothing is dlopen()ed."""
    from druta import native, nvbackend
    gpu_backend.reset_for_tests()
    clear_backend_env(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["druta"])
    monkeypatch.setattr(native, "_library", None)
    monkeypatch.setattr(native, "_error", None)
    monkeypatch.setattr(gpu_backend, "surface_gate", complete)
    python_calls = []
    monkeypatch.setattr(nvbackend, "GPU", lambda slot: python_calls.append(slot) or FakePythonGpu(slot))
    path = tmp_path / "libdrutacore.so"
    path.write_bytes(b"")
    state = types.SimpleNamespace(native=native, path=path, python_calls=python_calls)

    def use(library=None, present=True):
        if not present:
            path.unlink()
        monkeypatch.setattr(native, "candidate_paths", lambda environ=None: [path])
        monkeypatch.setattr(ctypes, "CDLL", lambda name, *a, **k: library)
        return state
    yield use
    gpu_backend.reset_for_tests()


def test_real_loader_library_absent(real_loader):
    state = real_loader(present=False)
    assert isinstance(gpu_backend.open_gpu(kind="auto"), FakePythonGpu)
    assert gpu_backend.choice().reason == f"native core not loaded: {state.path}: not present"
    gpu_backend.reset_for_tests()  # a new process, as far as the factory knows; the loader keeps its error
    with pytest.raises(gpu_backend.BackendRefused, match="not present"):
        gpu_backend.open_gpu(kind="native")


def test_real_loader_abi_mismatch(real_loader):
    state = real_loader(FakeLibrary(abi=state_abi() + 1))
    assert isinstance(gpu_backend.open_gpu(kind="auto"), FakePythonGpu)
    assert f"ABI {state_abi() + 1}, this build needs {state_abi()}" in gpu_backend.choice().reason
    assert state.python_calls == [None]


def test_real_loader_missing_export(real_loader):
    real_loader(FakeLibrary(abi=state_abi(), missing={"dc_gpu_open"}))
    with pytest.raises(gpu_backend.BackendRefused, match="undefined symbol: dc_gpu_open"):
        gpu_backend.open_gpu(kind="native")
    assert gpu_backend.choice() is None


def test_real_loader_success_reaches_construction(real_loader, monkeypatch):
    state = real_loader(FakeLibrary(abi=state_abi()))
    monkeypatch.setattr(state.native, "Gpu", lambda slot: FakeNativeGpu(slot))
    gpu = gpu_backend.open_gpu("s", kind="auto")
    assert isinstance(gpu, FakeNativeGpu)
    assert gpu_backend.choice().library == str(state.path) and gpu_backend.choice().abi == state_abi()


def state_abi():
    from druta import native
    return native.ABI_VERSION
