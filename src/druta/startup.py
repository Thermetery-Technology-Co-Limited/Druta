# Druta - Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in profile startup, with a durable attempt marker and Windows boot evidence.

Only this module registers a task; importing or launching Druta never does.
The task runs in the signed-in user's interactive session, without a password.
State is per-user, independent of a portable build's temporary extraction path.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
import xml.etree.ElementTree as ET

from .paths import source_root

NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def state_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Druta"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2, sort_keys=True, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as src:
            value = json.load(src)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except FileNotFoundError:
        return default


def system_tool(name):
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name)


def run_tool(args):
    return subprocess.run(args, capture_output=True, text=True, check=True,
                          timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def parse_boot(start_xml, status_xml):
    """Require an explicit good shutdown/boot from THIS boot's event 20.

    The latest event 12 identifies the OS start. Matching by record order and
    time rejects a stale good event after log clearing, a missing event, and
    boot/resume variants that do not provide sufficient evidence.
    """
    def event(xml, provider, event_id):
        root = ET.fromstring(xml)
        node = root if root.tag == NS + "Event" else root.find(NS + "Event")
        if node is None:
            raise ValueError("Windows boot event is missing")
        system = node.find(NS + "System")
        if (system.find(NS + "Provider").get("Name") != provider
                or system.findtext(NS + "EventID") != str(event_id)):
            raise ValueError("unexpected Windows boot event")
        timestamp = system.find(NS + "TimeCreated").get("SystemTime")
        # Windows uses seven fractional digits; Python accepts and truncates them.
        at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        fields = {n.get("Name"): n.text for n in node.findall(NS + "EventData/" + NS + "Data")}
        return int(system.findtext(NS + "EventRecordID")), timestamp, at, fields
    start = event(start_xml, "Microsoft-Windows-Kernel-General", 12)
    status = event(status_xml, "Microsoft-Windows-Kernel-Boot", 20)
    if status[0] < start[0] or not 0 <= (status[2] - start[2]).total_seconds() <= 120:
        raise ValueError("shutdown evidence does not belong to the current boot")
    good = (status[3].get("LastShutdownGood") == "true"
            and status[3].get("LastBootGood") == "true")
    return {"id": start[1], "clean": good,
            "reason": "" if good else "Windows reports an abnormal previous shutdown or boot"}


def boot_status():
    try:
        def query(provider, event_id):
            xpath = f"*[System[Provider[@Name='{provider}'] and EventID={event_id}]]"
            return run_tool([system_tool("wevtutil.exe"), "qe", "System", "/q:" + xpath,
                             "/rd:true", "/c:1", "/f:xml"]).stdout
        return parse_boot(query("Microsoft-Windows-Kernel-General", 12),
                          query("Microsoft-Windows-Kernel-Boot", 20))
    except Exception as e:
        return {"id": None, "clean": False, "reason": f"cannot confirm a clean Windows shutdown: {e}"}


def current_sid():
    row = next(csv.reader(io.StringIO(run_tool([system_tool("whoami.exe"),
                                               "/user", "/fo", "csv", "/nh"]).stdout)))
    sid = row[-1].strip()
    if not sid.startswith("S-1-") or any(c not in "S0123456789-" for c in sid):
        raise ValueError("cannot identify the signed-in Windows account")
    return sid


def application_command(*arguments, windowed=False):
    """Relaunch this installation without depending on the working directory."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *arguments]
    executable = Path(sys.executable)
    if windowed:
        candidate = executable.with_name("pythonw.exe")
        if candidate.is_file():
            executable = candidate
    root = source_root()
    if root is not None:
        bootstrap = (root / "druta.py").resolve()
        if not bootstrap.is_file():
            raise RuntimeError(f"cannot find the source launcher ({bootstrap})")
        return [str(executable), str(bootstrap), *arguments]
    return [str(executable), "-m", "druta", *arguments]


def launch_command():
    return application_command("--startup-profile", windowed=True)


def task_xml(command, sid):
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    root = ET.Element("Task", {"version": "1.2", "xmlns": namespace})
    def add(parent, tag, text=None, **attrs):
        child = ET.SubElement(parent, tag, attrs)
        child.text = text
        return child
    registration = add(root, "RegistrationInfo")
    add(registration, "Description", "Load the selected Druta profile after a confirmed clean shutdown.")
    trigger = add(add(root, "Triggers"), "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", sid)
    add(trigger, "Delay", "PT30S")
    principal = add(add(root, "Principals"), "Principal", id="User")
    add(principal, "UserId", sid)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "HighestAvailable")
    settings = add(root, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"),
                       ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"),
                       ("ExecutionTimeLimit", "PT0S"), ("Enabled", "true")):
        add(settings, key, value)
    action = add(add(root, "Actions", Context="User"), "Exec")
    add(action, "Command", command[0])
    add(action, "Arguments", subprocess.list2cmdline(command[1:]))
    add(action, "WorkingDirectory", str(Path(command[0]).parent))
    return ET.tostring(root, encoding="unicode")


def register_task(enabled):
    sid = current_sid()
    name = "Druta profile startup " + sid
    if not enabled:
        try:
            run_tool([system_tool("schtasks.exe"), "/Query", "/TN", name])
        except subprocess.CalledProcessError:
            return
        run_tool([system_tool("schtasks.exe"), "/Delete", "/TN", name, "/F"])
        return
    fd, temporary = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as out:
            out.write(task_xml(launch_command(), sid))
        run_tool([system_tool("schtasks.exe"), "/Create", "/TN", name, "/XML", temporary, "/F"])
    finally:
        os.unlink(temporary)


class Startup:
    """One owner across all Druta windows/builds; no crash marker can be erased
    by a second process. A blocked/attempted boot stays blocked after reopening.
    """
    def __init__(self, directory=None, boot_reader=boot_status, registrar=register_task):
        self.directory = Path(directory) if directory is not None else state_dir()
        self.config_path = self.directory / "startup-profile.json"
        self.session_path = self.directory / "startup-session.json"
        self.registrar = registrar
        self.boot_reader = boot_reader
        self.lock = None
        self.config = None
        self.session = {}
        self.reason = ""
        self.boot = None
        self.failed = False
        self.ending = False
        self.listener = None

    def begin(self, automatic=False):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.lock = open(self.directory / "startup.lock", "a+b")
            if self.lock.tell() == 0:
                self.lock.write(b"0")
                self.lock.flush()
            self.lock.seek(0)
            import msvcrt
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if self.lock:
                self.lock.close()
            self.lock = None
            self.reason = "another Druta window owns startup loading; use that window to configure it"
            return None
        try:
            self.config = read_json(self.config_path)
            if self.config and self.config.get("enabled"):
                if (self.config.get("version") != 1 or self.config.get("enabled") is not True
                        or not isinstance(self.config.get("name"), str)
                        or not isinstance(self.config.get("profile"), dict)):
                    raise ValueError("invalid startup profile configuration")
            old = read_json(self.session_path)
            self.boot = self.boot_reader()
            self.session = dict(old or {})
            boot_id = self.boot["id"]
            if self.config and self.config.get("enabled"):
                if not self.boot["clean"]:
                    self.reason = self.boot["reason"]
                elif old is None or old.get("running") is not False:
                    self.reason = "the previous Druta session did not exit normally (or its shutdown record is missing)"
                elif old.get("blocked_boot") == boot_id:
                    self.reason = old.get("reason") or "startup is blocked for this boot"
                if self.reason:
                    self.session.update(blocked_boot=boot_id, reason=self.reason)
            self.session.update(running=True, boot=boot_id)
            self.persist()
            if not self.config or not self.config.get("enabled") or self.reason:
                return None
            if not automatic:
                return None
            if self.session.get("attempted_boot") == boot_id:
                self.reason = "the startup profile was already attempted during this boot"
                return None
            self.session["attempted_boot"] = boot_id
            self.persist()  # committed BEFORE constructing a GPU or issuing a write
            return self.config
        except Exception as e:
            self.reason = f"startup loading disabled: {e}"
            self.failed = True
            return None

    def persist(self):
        atomic_json(self.session_path, self.session)

    def enable(self, name, profile):
        if self.lock is None or self.failed:
            raise ValueError(self.reason or "startup state is unavailable")
        config = {"version": 1, "enabled": True, "name": name,
                  "profile": profile}
        # Save a fixed copy: overwriting the named tune cannot silently change
        # the settings that were approved for unattended application.
        previous = self.config
        atomic_json(self.config_path, config)
        try:
            self.registrar(True)
        except Exception:
            atomic_json(self.config_path, previous or {"enabled": False})
            raise
        self.config = config
        self.session["running"] = True
        self.persist()

    def disable(self):
        if self.lock is None:
            raise ValueError(self.reason)
        # Disable loading first, even when task removal is denied.
        atomic_json(self.config_path, {"version": 1, "enabled": False})
        self.config = {"enabled": False}
        self.registrar(False)

    def block(self, reason):
        self.reason = reason
        self.session.update(blocked_boot=(self.boot or {}).get("id"), reason=reason)
        self.persist()

    def end_session(self, confirmed, flags, idle=True):
        # QUERYENDSESSION alone is not a shutdown; Windows can cancel it.
        # ENDSESSION_CRITICAL (forced termination) is not a clean exit either.
        if not confirmed:
            return
        self.ending = True
        if not idle or flags & 0x40000000:
            self.failed = True  # normal GUI cleanup must not erase this result
        if self.lock and idle and not self.failed and not flags & 0x40000000:
            self.session["running"] = False
            self.persist()

    def watch_shutdown(self, idle):
        if self.lock:
            self.listener = ShutdownListener(lambda confirmed, flags:
                                             self.end_session(confirmed, flags, idle()))
            self.listener.start()

    def close(self, clean=False):
        if self.lock is None:
            return
        try:
            if clean and not self.failed:
                self.session["running"] = False
                self.persist()
        finally:
            if self.listener:
                self.listener.stop()
            self.lock.close()
            self.lock = None


class ShutdownListener:
    """A hidden, process-owned top-level window receives Windows logoff/shutdown.

    Console handlers are insufficient for a console-less app that loads user32.
    This window leaves the toolkit's window procedure alone and never blocks a
    shutdown. Missing notification leaves the durable running marker dirty.
    """
    def __init__(self, callback):
        self.callback = callback
        self.hwnd = None
        self.ready = threading.Event()
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="Druta-shutdown")
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            raise RuntimeError(f"shutdown notification window unavailable: {self.error or 'timeout'}")

    def stop(self):
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, 0x10, 0, 0)  # WM_CLOSE
            self.thread.join(2)

    def _run(self):
        import ctypes as c
        from ctypes import wintypes as w
        user = self.user32 = c.WinDLL("user32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)
        proc_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WindowClass(c.Structure):
            _fields_ = [("style", w.UINT), ("proc", proc_type),
                        ("cls_extra", c.c_int), ("wnd_extra", c.c_int),
                        ("instance", w.HINSTANCE), ("icon", w.HICON),
                        ("cursor", w.HANDLE), ("background", w.HBRUSH),
                        ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]

        user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.DefWindowProcW.restype = c.c_ssize_t
        user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                        c.c_int, c.c_int, c.c_int, c.c_int,
                                        w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p]
        user.CreateWindowExW.restype = w.HWND
        user.RegisterClassW.argtypes = [c.POINTER(WindowClass)]
        user.RegisterClassW.restype = w.ATOM
        user.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
        user.DestroyWindow.argtypes = [w.HWND]
        user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.GetMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
        user.DispatchMessageW.restype = c.c_ssize_t
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel.GetModuleHandleW.restype = w.HMODULE

        @proc_type
        def procedure(hwnd, message, wp, lp):
            if message == 0x11:  # WM_QUERYENDSESSION: respect the user's shutdown
                return 1
            if message == 0x16:  # WM_ENDSESSION: only now is it confirmed
                try:
                    self.callback(bool(wp), lp)
                except Exception:
                    pass  # failure to persist must leave the marker dirty
                return 0
            if message == 0x10:
                user.DestroyWindow(hwnd)
                return 0
            if message == 2:
                user.PostQuitMessage(0)
                return 0
            return user.DefWindowProcW(hwnd, message, wp, lp)

        name = f"DrutaShutdown-{os.getpid()}-{id(self)}"
        instance = kernel.GetModuleHandleW(None)
        wc = WindowClass(proc=procedure, instance=instance, name=name)
        registered = False
        try:
            if not user.RegisterClassW(c.byref(wc)):
                raise c.WinError(c.get_last_error())
            registered = True
            self.hwnd = user.CreateWindowExW(0, name, "Druta shutdown state", 0,
                                            0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise c.WinError(c.get_last_error())
            self.ready.set()
            message = w.MSG()
            while user.GetMessageW(c.byref(message), None, 0, 0) > 0:
                user.DispatchMessageW(c.byref(message))
        except Exception as e:
            self.error = str(e)
            self.ready.set()
        finally:
            self.hwnd = None
            if registered:
                user.UnregisterClassW(name, instance)
