# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a selected-device restart outside the old GPU/renderer process.

The helper waits for the requesting process to exit before touching PnP, then
launches a fresh Druta with a durable result. It never resumes a tuning profile.
"""
import argparse
import ctypes
from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import uuid

from . import startup


def wait_for_exit(pid, timeout_ms=45000):
    """Wait on a process handle, not a sleep followed by an assumed exit."""
    if os.name != "nt" or pid <= 0 or pid == os.getpid():
        raise ValueError("invalid parent process for device recovery")
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: process has already exited
            return
        raise OSError(error, "cannot wait for the previous Druta process")
    try:
        status = kernel.WaitForSingleObject(handle, timeout_ms)
        if status == 0x102:
            raise TimeoutError("previous Druta is still running; GPU was not restarted")
        if status != 0:
            raise OSError(ctypes.get_last_error(), "could not confirm Druta has exited")
    finally:
        kernel.CloseHandle(handle)


def launch_helper(target):
    """Caller closes normally only after this process starts successfully."""
    command = startup.application_command(
        "--restart-gpu", target.slot, "--expected-instance", target.instance_id,
        "--parent-pid", str(os.getpid()), windowed=True)
    return subprocess.Popen(command, close_fds=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run(slot, *, expected_instance=None, parent_pid=None, notify=None):
    """Restart once, record the outcome, and reopen without cached GPU state."""
    from . import devicereset
    log = startup.state_dir() / "device-recovery" / (uuid.uuid4().hex + ".json")
    report = {"slot": slot, "ok": False, "reboot_required": False,
              "message": "", "returncode": None}
    parent_exited = parent_pid is None
    try:
        # Prove the receipt is writable before closing/restarting anything here.
        startup.atomic_json(log, report)
        if parent_pid is not None:
            wait_for_exit(parent_pid)
            parent_exited = True
        target = devicereset.resolve_target(slot)
        if expected_instance is not None and target.instance_id != expected_instance:
            raise ValueError("selected GPU changed since confirmation; no restart issued")
        report["instance_id"] = target.instance_id
        slot = report["slot"] = target.slot
        report.update(asdict(devicereset.restart_target(target)))
    except Exception as exc:
        report["message"] = f"GPU restart failed: {exc}"
    try:
        startup.atomic_json(log, report)
    except OSError as exc:
        if notify:
            notify(f"{report['message']}\nCould not save recovery result: {exc}")
        return 1
    # A stuck old process may still own hardware operations. Do not introduce a
    # second GUI or call any driver interface while its exit is unconfirmed.
    if not parent_exited:
        if notify:
            notify(report["message"] + f"\nDetails: {log}")
        return 1
    try:
        command = startup.application_command(
            "--gpu", slot, "--recovery-result", str(log), windowed=True)
        subprocess.Popen(command, close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, RuntimeError) as exc:
        if notify:
            notify(f"{report['message']}\nCould not reopen Druta: {exc}\nDetails: {log}")
        return 1
    return 0 if report["ok"] and not report["reboot_required"] else 1


def cli(argv, notify):
    parser = argparse.ArgumentParser(prog="Druta --restart-gpu")
    parser.add_argument("slot")
    parser.add_argument("--expected-instance")
    parser.add_argument("--parent-pid", type=int)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        notify("usage: Druta --restart-gpu PCI_SLOT\n"
               "Restarts only that GPU through Windows PnP, then reopens Druta. "
               "Requires administrator rights. Does not reboot the computer.")
        return 2
    return run(args.slot, expected_instance=args.expected_instance,
               parent_pid=args.parent_pid, notify=notify)


def read_result(path, slot):
    """Only accept this helper's bounded, same-card receipt directory."""
    path = Path(path).resolve()
    base = (startup.state_dir() / "device-recovery").resolve()
    if path.parent != base or path.suffix != ".json" or path.stat().st_size > 65536:
        raise ValueError("invalid device recovery receipt")
    report = startup.read_json(path)
    if (not report or report.get("slot") != slot
            or not isinstance(report.get("message"), str)
            or type(report.get("ok")) is not bool
            or type(report.get("reboot_required")) is not bool):
        raise ValueError("device recovery receipt does not match this GPU")
    return report
