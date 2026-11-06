# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later

"""Session logs, crash capture and a one-pass self-check.

The Windows 7 and Vista builds are run on machines where nobody can attach a
debugger, so every failure has to leave a written trail:

* :func:`start_session_log` opens a per-run log before the UI is imported.
  It records startup stages, every uncaught exception (main and worker
  threads), and, through :mod:`faulthandler`, the Python stack of a native
  crash such as an access violation inside a driver call.
* :func:`self_check` runs independent read-only probes (runtime, loaded
  runtime DLLs, Windows exports Druta binds, every Druta module import,
  NVIDIA interfaces, recent Windows crash events and the renderer) and writes
  each result as soon as it is known. One run therefore reports every
  problem it can reach instead of stopping at the first.

Nothing here changes GPU state. NVIDIA probes only enumerate. Text written to
a file has the Windows user name, profile path and computer name replaced, so
a log can be shared as is.
"""
from __future__ import annotations

import faulthandler
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
import traceback

LOG_KEEP = 20
_state = {"file": None, "path": None, "hooks": False}

# (DLL, export, what Druta does when it is missing). Every export bound
# through ctypes by the application, so a single run shows all capability
# gaps of the current Windows image rather than the first one hit.
WINDOWS_EXPORTS = (
    ("kernel32", "GetTickCount64", "required by startup recovery"),
    ("kernel32", "GetSystemDirectoryW", "required for driver library lookup"),
    ("kernel32", "CreateMutexW", "required for single-instance checks"),
    ("kernel32", "GetProcessTimes", "required for startup recovery"),
    ("kernel32", "GetSystemTimeAsFileTime", "required for startup recovery"),
    ("kernel32", "AttachConsole", "command-line output only"),
    ("kernel32", "VirtualProtect", "transport capture"),
    ("kernel32", "FlushInstructionCache", "transport capture"),
    ("user32", "GetDpiForSystem", "Windows 10+; falls back to GetDeviceCaps"),
    ("user32", "SetProcessDPIAware", "fallback DPI awareness"),
    ("user32", "GetDC", "fallback DPI query"),
    ("user32", "SetWindowLongPtrW", "window handling"),
    ("user32", "SendMessageTimeoutW", "window handling"),
    ("user32", "CreateWindowExW", "shutdown notification window"),
    ("user32", "MessageBoxW", "error dialogs"),
    ("shcore", "SetProcessDpiAwareness", "Windows 8.1+; falls back to user32"),
    ("gdi32", "GetDeviceCaps", "fallback DPI query"),
    ("gdi32", "D3DKMTEnumAdapters2", "Windows 8+; current-policy transport "
                                     "is reported unsupported without it"),
    ("gdi32", "D3DKMTQueryAdapterInfo", "adapter identity for driver escapes"),
    ("gdi32", "D3DKMTEscape", "driver escapes"),
    ("gdi32", "D3DKMTCloseAdapter", "driver escapes"),
    ("comdlg32", "GetOpenFileNameW", "file dialogs"),
    ("comdlg32", "GetSaveFileNameW", "file dialogs"),
    ("shell32", "IsUserAnAdmin", "administrator detection"),
    ("shell32", "SHGetFolderPathW", "folder lookup"),
    ("setupapi", "SetupDiGetClassDevsW", "GPU driver restart"),
    ("setupapi", "SetupDiGetDeviceRegistryPropertyW", "GPU driver restart"),
    ("cfgmgr32", "CM_Locate_DevNodeW", "GPU driver restart"),
    ("cfgmgr32", "CM_Get_DevNode_Status", "GPU driver restart"),
    ("advapi32", "OpenSCManagerW", "nvtune service status"),
    ("advapi32", "QueryServiceStatus", "nvtune service status"),
    ("dwmapi", "DwmIsCompositionEnabled", "renderer prerequisite"),
    ("d3d11", "D3D11CreateDevice", "renderer prerequisite"),
    ("dxgi", "CreateDXGIFactory", "renderer prerequisite"),
    ("dxgi", "CreateDXGIFactory1", "renderer prerequisite"),
)

RUNTIME_MODULES = ("ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                   "msvcp140.dll", "d3dcompiler_47.dll", "d3d11.dll",
                   "dxgi.dll", "nvapi64.dll", "nvml.dll")


# ---------------------------------------------------------------------------
# redaction

def _identity_values(environ=None):
    environ = os.environ if environ is None else environ
    pairs = []
    for key, label in (("USERPROFILE", "<user-profile>"), ("HOMEPATH", "<user-profile>"),
                       ("USERNAME", "<user>"), ("COMPUTERNAME", "<computer>"),
                       ("USERDOMAIN", "<domain>")):
        value = (environ.get(key) or "").strip()
        if len(value) >= 2 and value not in ("\\",):
            pairs.append((value, label))
    try:
        home = str(Path.home())
        if len(home) > 3:
            pairs.append((home, "<user-profile>"))
    except Exception:
        pass
    # Longest first: a profile path contains the user name.
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def redact(text, environ=None):
    """Replace user/computer identity in ``text`` (case-insensitive)."""
    text = str(text)
    for value, label in _identity_values(environ):
        text = re.sub(re.escape(value), label, text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------------------
# session log

def log_directories(environ=None):
    """Candidate log directories, most useful first."""
    environ = os.environ if environ is None else environ
    out = []
    try:
        from .paths import app_dir
        out.append(Path(app_dir()) / "logs")
    except Exception:
        pass
    local = environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Thermetery" / "Druta" / "logs")
    temp = environ.get("TEMP") or environ.get("TMP")
    if temp:
        out.append(Path(temp) / "Druta-logs")
    return out


def _open_log(directories, name):
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / name
            handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
            return path, handle
        except OSError:
            continue
    return None, None


def _prune(directory, keep=LOG_KEEP):
    try:
        logs = sorted(directory.glob("druta-*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _write(line):
    handle = _state["file"]
    if handle is None:
        return
    try:
        handle.write(redact(line).rstrip("\n") + "\n")
        handle.flush()
    except Exception:
        pass


def stage(name):
    """Record that startup reached ``name``; the last one shown is where it stopped."""
    _write(f"{time.strftime('%H:%M:%S')} stage: {name}")


def log(message):
    _write(f"{time.strftime('%H:%M:%S')} {message}")


def log_exception(where, exc_type=None, exc=None, tb=None):
    if exc_type is None:
        exc_type, exc, tb = sys.exc_info()
    if exc_type is None:
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    notes = getattr(exc, "__notes__", None)
    if isinstance(notes, list) and notes and sys.version_info < (3, 11):
        text += "".join(f"note: {note}\n" for note in notes)
    _write(f"{time.strftime('%H:%M:%S')} uncaught exception ({where}):\n{text}")


def _install_hooks():
    if _state["hooks"]:
        return
    previous_excepthook = sys.excepthook

    def excepthook(exc_type, exc, tb):
        log_exception("main thread", exc_type, exc, tb)
        previous_excepthook(exc_type, exc, tb)

    sys.excepthook = excepthook
    previous_thread_hook = getattr(threading, "excepthook", None)
    if previous_thread_hook is not None:
        def thread_hook(args):
            name = getattr(args.thread, "name", "?")
            log_exception(f"thread {name}", args.exc_type, args.exc_value, args.exc_traceback)
            previous_thread_hook(args)

        threading.excepthook = thread_hook
    _state["hooks"] = True


def _header(argv):
    from . import __version__
    lines = [f"Druta {__version__} session log",
             f"started: {time.strftime('%Y-%m-%d %H:%M:%S')} (pid {os.getpid()})",
             f"argv: {' '.join(argv)}",
             f"python: {sys.version.splitlines()[0]} ({platform.architecture()[0]})",
             f"frozen: {bool(getattr(sys, 'frozen', False))}",
             f"executable: {sys.executable}",
             f"platform: {platform.platform()}"]
    getwinver = getattr(sys, "getwindowsversion", None)
    if getwinver is not None:
        v = getwinver()
        lines.append(f"windows: {v.major}.{v.minor}.{v.build} {v.service_pack}".rstrip())
    try:
        from .legacy_python import install
        lines.append("python shims: " + (", ".join(install()) or "none needed"))
    except Exception as e:
        lines.append(f"python shims: unavailable ({e})")
    return lines


def start_session_log(argv=None, directories=None):
    """Open this run's log, install crash hooks, return the log path or None.

    Never raises: a read-only or full disk must not stop Druta from starting.
    """
    if _state["file"] is not None:
        return _state["path"]
    try:
        argv = list(sys.argv if argv is None else argv)
        name = time.strftime("druta-%Y%m%d-%H%M%S") + f"-{os.getpid()}.log"
        directories = log_directories() if directories is None else directories
        path, handle = _open_log(directories, name)
        if handle is None:
            return None
        _state.update(file=handle, path=path)
        _prune(path.parent)
        for line in _header(argv):
            _write(line)
        try:
            # Native crashes (access violations in a driver DLL, renderer
            # faults) print the Python stack of every thread to this file.
            faulthandler.enable(file=handle, all_threads=True)
            _write("note: on Windows, faulthandler also prints 'Windows fatal exception' for "
                   "exceptions a driver DLL catches itself; the last stack before the log "
                   "ends is the one that stopped Druta")
        except Exception as e:
            _write(f"faulthandler unavailable: {e}")
        _install_hooks()
        stage("session log started")
        return path
    except Exception:
        return None


def stop_session_log(status="exited"):
    handle = _state["file"]
    if handle is None:
        return
    stage(status)
    try:
        faulthandler.disable()
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass
    _state.update(file=None, path=None)


# ---------------------------------------------------------------------------
# self-check

class _Report:
    def __init__(self, path):
        self.path = path
        self.failures = 0
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text("", encoding="utf-8")

    def line(self, text=""):
        text = redact(text)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text + "\n")
        log(text)

    def section(self, title):
        self.line("")
        self.line(f"== {title}")
        stage(f"self-check: {title}")


def _probe_runtime(report):
    from . import __version__
    report.line(f"Druta: {__version__}")
    report.line(f"Python: {sys.version.splitlines()[0]}")
    report.line(f"Architecture: {platform.architecture()[0]}; frozen: {bool(getattr(sys, 'frozen', False))}")
    report.line(f"Executable: {sys.executable}")
    report.line(f"Platform: {platform.platform()}")
    getwinver = getattr(sys, "getwindowsversion", None)
    if getwinver is not None:
        v = getwinver()
        report.line(f"Windows: {v.major}.{v.minor}.{v.build} {v.service_pack}".rstrip())
    from .legacy_python import install
    report.line("Python shims installed: " + (", ".join(install()) or "none needed"))


def _windll(name):
    import ctypes
    return ctypes.WinDLL(name, use_last_error=True)


def _probe_runtime_modules(report):
    import ctypes
    from ctypes import wintypes
    kernel = _windll("kernel32")
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    for name in RUNTIME_MODULES + (f"python{sys.version_info[0]}{sys.version_info[1]}.dll",):
        handle = kernel.GetModuleHandleW(name)
        if not handle:
            report.line(f"{name}: not loaded")
            continue
        buffer = ctypes.create_unicode_buffer(1024)
        kernel.GetModuleFileNameW(handle, buffer, 1024)
        report.line(f"{name}: {buffer.value}")


def _probe_exports(report, loader=None):
    loader = loader or _windll
    loaded = {}
    missing = 0
    for dll, export, purpose in WINDOWS_EXPORTS:
        if dll not in loaded:
            try:
                loaded[dll] = loader(dll)
            except OSError as e:
                loaded[dll] = e
        library = loaded[dll]
        if isinstance(library, OSError):
            state = f"DLL not loadable ({library})"
        else:
            try:
                getattr(library, export)
                state = "present"
            except AttributeError:
                state = "missing"
        if state != "present":
            missing += 1
        report.line(f"{dll}!{export}: {state} - {purpose}")
    report.line(f"{missing} of {len(WINDOWS_EXPORTS)} exports unavailable "
                "(informational: Druta checks each capability where it is used)")


# Developer scripts that open windows, spawn processes or measure the desktop
# when imported. The application never imports them, so a frozen build does
# not contain them; a source checkout does, and they must not run here.
NOT_APPLICATION_MODULES = frozenset({
    "druta.app", "druta.bench_move", "druta.drag_probe", "druta.ping_ui",
    "druta.size_probe", "druta.threshold_probe",
})


def _druta_modules():
    import pkgutil
    import druta
    names = []
    for info in pkgutil.walk_packages(druta.__path__, "druta."):
        if (info.name.startswith("druta.tools") or info.name.endswith("__main__")
                or info.name in NOT_APPLICATION_MODULES):
            continue
        names.append(info.name)
    return sorted(names)


def _probe_imports(report):
    import importlib
    names = _druta_modules()
    if not names:
        raise RuntimeError("the frozen package did not list its modules; nothing was imported")
    failed = 0
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:
            failed += 1
            report.line(f"FAILED {name}: {type(e).__name__}: {e}")
            last = traceback.extract_tb(e.__traceback__)[-1:]
            for frame in last:
                report.line(f"    at {frame.filename}:{frame.lineno} in {frame.name}")
    report.line(f"{len(names) - failed} of {len(names)} Druta modules imported")
    if failed:
        raise RuntimeError(f"{failed} module(s) failed to import")


def _probe_nvidia(report):
    from . import nvbackend
    nvml = nvbackend.Nvml()
    report.line(f"NVML: {'ok' if nvml.ok else 'unavailable'} {getattr(nvml, 'err_detail', '')}".rstrip())
    library = getattr(nvml, "dll_path", "")
    if library:
        report.line(f"NVML library: {library}")
    for gpu in nvml.gpus:
        devid = gpu.get("devid")
        subsys = gpu.get("subsys")
        report.line(f"  NVML GPU {gpu.get('nvml_index')}: {gpu.get('name') or '?'} slot={gpu.get('slot') or '?'}"
                    f" device={'%04X' % devid if devid is not None else '?'}"
                    f" subsystem={'%08X' % subsys if subsys is not None else '?'}")
    nvapi = nvbackend.NvAPI()
    report.line(f"NVAPI: {'ok' if nvapi.ok else 'unavailable'} {getattr(nvapi, 'err_detail', '')}".rstrip())
    for gpu in nvapi.gpus:
        report.line(f"  NVAPI GPU: {gpu.get('name') or '?'} slot={gpu.get('slot') or '?'}")
    try:
        from . import gpuload
        ok, why = gpuload.available()
        report.line(f"CUDA driver (nvcuda.dll): {'ok' if ok else why}")
    except Exception as e:
        report.line(f"CUDA driver check failed: {type(e).__name__}: {e}")
    if not nvml.ok and not nvapi.ok:
        raise RuntimeError("no NVIDIA interface answered")


def _probe_crash_events(report, run=None):
    """Recent 'Application Error' events for Druta: the faulting module and offset."""
    import subprocess
    run = run or subprocess.run
    query = ("*[System[Provider[@Name='Application Error'] or "
             "Provider[@Name='Windows Error Reporting']]]")
    result = run(["wevtutil", "qe", "Application", "/q:" + query, "/c:40", "/rd:true", "/f:text"],
                 capture_output=True, text=True, timeout=30,
                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError(f"wevtutil exit {result.returncode}: {result.stderr.strip()}")
    events = re.split(r"(?m)^Event\[\d+\]:\s*$", result.stdout)
    shown = 0
    for event in events:
        if "druta" not in event.lower() and "python3" not in event.lower():
            continue
        shown += 1
        for line in event.strip().splitlines():
            report.line("  " + line.rstrip())
        report.line("  --")
        if shown >= 5:
            break
    report.line(f"{shown} recent Druta crash event(s) found")


def _probe_renderer(report, output_dir):
    import json
    from . import druta as app
    smoke = None if output_dir is None else str(Path(output_dir) / "renderer-smoke.json")
    code = app._smoke_test(smoke)
    if smoke and Path(smoke).is_file():
        result = json.loads(Path(smoke).read_text(encoding="utf-8"))
        for key in sorted(result):
            report.line(f"{key}: {result[key]}")
    if code != 0:
        raise RuntimeError(f"renderer smoke test exit {code}")


PROBES = (
    ("Runtime", _probe_runtime),
    ("Runtime DLLs loaded in this process", _probe_runtime_modules),
    ("Windows exports Druta binds", _probe_exports),
    ("Druta module imports", _probe_imports),
    ("NVIDIA interfaces (read-only enumeration)", _probe_nvidia),
    ("Recent Windows crash events for Druta", _probe_crash_events),
)


def self_check(output_dir=None, probes=None, renderer=True):
    """Run every probe; return 0 when all passed, 1 otherwise.

    Each probe is independent: a failure is written and the next probe still
    runs. The renderer probe is last because a broken Direct3D stack can
    terminate the process; the session log's faulthandler output then shows
    where, and everything before it is already on disk.
    """
    output_dir = None if output_dir is None else Path(output_dir)
    report = _Report(None if output_dir is None else output_dir / "self-check.txt")
    report.line("Druta self-check (read-only; no GPU settings are changed)")
    report.line(f"session log: {_state['path'] or 'not available'}")
    probes = list(PROBES if probes is None else probes)
    if renderer:
        probes.append(("Renderer (Dear PyGui / Direct3D 11)",
                       lambda r: _probe_renderer(r, output_dir)))
    for title, probe in probes:
        report.section(title)
        started = time.monotonic()
        try:
            probe(report)
            report.line(f"-> passed ({time.monotonic() - started:.1f} s)")
        except Exception as e:
            report.failures += 1
            report.line(f"-> FAILED: {type(e).__name__}: {e}")
            for line in traceback.format_exc().rstrip().splitlines()[-6:]:
                report.line("   " + line)
    report.line("")
    report.line(f"self-check finished: {report.failures} probe(s) failed")
    return 1 if report.failures else 0


def cli(argv):
    """``Druta --self-check [--output DIR]``."""
    argv = list(argv)
    output = None
    if argv[:1] == ["--output"] and len(argv) == 2:
        output = argv[1]
    elif argv:
        sys.stderr and sys.stderr.write("usage: Druta --self-check [--output DIR]\n")
        return 2
    if output is None:
        base = (log_directories() or [Path.cwd()])[0].parent
        output = str(base / time.strftime("Druta-self-check-%Y%m%d-%H%M%S"))
    start_session_log()
    stage("self-check")
    code = self_check(output)
    stop_session_log(f"self-check exit {code}")
    return code
