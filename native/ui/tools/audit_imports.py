#!/usr/bin/env python3
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded PE import/header audit for the native UI preview, separate from the core.

This checks the UI's Windows 6.0 header floor, expected system dependencies,
and selected known newer static imports. It does not certify every imported
system function or prove Vista/7 startup, CRT compatibility or renderer support.
The core's stricter XP import policy is neither reused as policy nor modified.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


_CORE_PATH = Path(__file__).resolve().parents[2] / "tools" / "check_imports.py"
_SPEC = importlib.util.spec_from_file_location("druta_ui_core_pe_reader", _CORE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
pe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = pe
_SPEC.loader.exec_module(pe)

SYSTEM_DLLS = frozenset({
    "kernel32.dll", "user32.dll", "gdi32.dll", "d3d9.dll", "dwmapi.dll",
    "imm32.dll", "shell32.dll", "comdlg32.dll", "advapi32.dll", "msvcrt.dll",
})

# Newer DPI conveniences must be bound conditionally by the Win32 backend.
# This deliberately is not a complete allowlist of every Windows system API.
NEWER_STATIC_IMPORTS = frozenset({
    "AdjustWindowRectExForDpi", "AreDpiAwarenessContextsEqual",
    "EnableNonClientDpiScaling", "GetAwarenessFromDpiAwarenessContext",
    "GetDpiForSystem", "GetDpiForWindow", "GetSystemMetricsForDpi",
    "GetThreadDpiAwarenessContext", "GetWindowDpiAwarenessContext",
    "IsValidDpiAwarenessContext", "SetProcessDpiAwarenessContext",
    "SetThreadDpiAwarenessContext", "LogicalToPhysicalPointForPerMonitorDPI",
    "PhysicalToLogicalPointForPerMonitorDPI",
    "GetSystemTimePreciseAsFileTime", "GetThreadDescription", "SetThreadDescription",
    "WaitOnAddress", "WakeByAddressAll", "WakeByAddressSingle",
})

LIMITATION = (
    "Passing audits PE headers, dependency DLLs and selected newer static APIs only. "
    "The system-function API floor audit is incomplete. This is not proof of "
    "Vista/7 launch, static CRT compatibility, fonts, device reset or rendering."
)


def _delay_imports(reader, image, optional):
    wide = image.machine == "x86_64"
    count_at = optional + (108 if wide else 92)
    count = reader.unpack("<I", count_at)[0]
    if count <= pe.DIR_DELAY_IMPORT:
        return []
    rva, size = reader.unpack("<II", count_at + 4 + pe.DIR_DELAY_IMPORT * 8)
    if not (rva or size):
        return []
    if not rva:
        raise pe.MalformedError("delay-import directory has size but no RVA")
    imports = []
    thunk_format, thunk_size = ("<Q", 8) if wide else ("<I", 4)
    ordinal_flag = 1 << (63 if wide else 31)
    for number in range(pe.MAX_DESCRIPTORS):
        at = reader.offset(rva + number * 32, 32, f"delay-import descriptor {number}")
        row = reader.unpack("<8I", at)
        if not any(row):
            if number == 0:
                raise pe.MalformedError("delay-import directory is empty")
            return imports
        attributes, name_rva, module_handle, iat, names, bound, unload, stamp = row
        if attributes != 1:
            raise pe.MalformedError("delay imports must use understood RVA-based descriptors (attributes=1)")
        if not name_rva or not module_handle or not iat or not names:
            raise pe.MalformedError(f"delay-import descriptor {number} lacks a required name/handle/thunk RVA")
        dll = reader.name(name_rva, f"delay DLL name {number}")
        if bound or stamp:
            image.problems.append(f"{dll}: bound delay imports are not audited")
        # The module-handle storage may be zero-filled BSS. Only the name
        # lookup table must be file-backed for this import inventory audit.
        functions = []
        imports.append({"kind": "delay", "dll": dll, "functions": functions})
        for slot in range(pe.MAX_THUNKS + 1):
            if slot == pe.MAX_THUNKS:
                raise pe.MalformedError(f"{dll}: too many delay import thunks")
            value = reader.unpack(thunk_format, reader.offset(
                names + slot * thunk_size, thunk_size, f"{dll} delay thunk {slot}"))[0]
            if value == 0:
                break
            if value & ordinal_flag:
                image.problems.append(f"{dll}: delay import by ordinal {value & 0xffff} is not audited")
                continue
            if value >> 31:
                raise pe.MalformedError(f"{dll}: delay thunk is not a valid name RVA")
            functions.append(reader.name(value + 2, f"{dll} delay import name {slot}"))
        if not functions:
            image.problems.append(f"{dll}: no delay function imported by name")
    raise pe.MalformedError("too many delay-import descriptors")


def audit_bytes(data: bytes) -> dict:
    """Return a serializable result; malformed or unaudited input fails closed."""
    image = pe.Image()
    reader = pe._Reader(data)
    delay = []
    subsystem = None
    try:
        pe._read(reader, image)
        optional = reader.unpack("<I", 0x3C)[0] + 24
        subsystem = reader.unpack("<H", optional + 68)[0]
        delay = _delay_imports(reader, image, optional)
        # The core deliberately refuses all delay imports. Remove only that
        # diagnostic after our own parser has successfully audited the table.
        image.problems = [problem for problem in image.problems
                          if not problem.startswith("delay-import directory present")]
    except pe.MalformedError as error:
        image.problems.append(str(error))
    imports = [{"kind": "normal", "dll": dll, "functions": functions}
               for dll, functions in image.imports] + delay
    violations = list(image.problems)
    for name, version in (("operating system", image.os_version),
                          ("subsystem", image.subsystem_version)):
        if version is not None and version > (6, 0):
            violations.append(f"{name} header {version[0]}.{version[1]} exceeds Windows 6.0")
    if subsystem is not None and subsystem != 2:
        violations.append(f"PE subsystem {subsystem} is not Windows GUI (2)")
    for dependency in imports:
        dll = dependency["dll"]
        if dll.lower() not in SYSTEM_DLLS:
            violations.append(f"unexpected dependency: {dll} ({dependency['kind']} import)")
        for function in dependency["functions"]:
            if function in NEWER_STATIC_IMPORTS:
                violations.append(f"newer API must be dynamically resolved: {dll}!{function}")
    violations = list(dict.fromkeys(violations))
    return {
        "format": 1, "sha256": hashlib.sha256(data).hexdigest(),
        "machine": image.machine, "os_version": image.os_version,
        "subsystem_version": image.subsystem_version, "subsystem": subsystem,
        "imports": imports, "passed": not violations, "violations": violations,
        "limitation": LIMITATION,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--report", type=Path, help="write the full audited import inventory as JSON")
    args = parser.parse_args(argv)
    try:
        result = audit_bytes(args.executable.read_bytes())
        result["executable"] = str(args.executable.resolve())
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"UI import audit: {error}", file=sys.stderr)
        return 2
    print(f"{'PASS' if result['passed'] else 'FAIL'}: {args.executable}")
    for dependency in result["imports"]:
        print(f"  {dependency['kind']}: {dependency['dll']} ({len(dependency['functions'])} functions)")
    for violation in result["violations"]:
        print("  VIOLATION: " + violation)
    print(LIMITATION)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
