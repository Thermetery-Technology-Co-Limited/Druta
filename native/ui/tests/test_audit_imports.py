# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""UI import policy checks real PE bytes, including hidden delay dependencies."""

import importlib.util
import json
from pathlib import Path
import struct

import pytest

from tests.test_native_dll_imports import _Area, _dll, build_pe


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "audit_imports.py"
spec = importlib.util.spec_from_file_location("ui_audit_imports", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def image(*, machine="x86_64", imports=None, delayed=None, delay_attributes=1,
          os_version=(6, 0), subsystem_version=(6, 0)):
    directories = {}
    blob = b""
    if delayed:
        dll, function = delayed
        area = _Area(0x3000)
        area.put(bytes(64), 4)  # One delay descriptor plus the all-zero terminator.
        name = area.put(dll.encode("ascii") + b"\0")
        hint = area.put(b"\0\0" + function.encode("ascii") + b"\0")
        fmt = "<Q" if machine == "x86_64" else "<I"
        handle = area.put(bytes(struct.calcsize(fmt)), 8)
        names = area.put(struct.pack(fmt, hint) + struct.pack(fmt, 0), 8)
        iat = area.put(struct.pack(fmt, hint) + struct.pack(fmt, 0), 8)
        struct.pack_into("<8I", area.data, 0, delay_attributes, name, handle, iat, names, 0, 0, 0)
        directories[13] = (0x3000, 64)
        blob = bytes(area.data)
    data = bytearray(build_pe(
        machine=machine, imports=imports or [_dll("KERNEL32.dll", ["GetProcAddress", "Sleep"]),
                                            _dll("D3D9.dll", ["Direct3DCreate9"])],
        os_version=os_version, subsystem_version=subsystem_version,
        directories=directories, other_tail=blob))
    struct.pack_into("<H", data, 0x58 + 68, 2)  # Windows GUI.
    return bytes(data)


@pytest.mark.parametrize("machine", ["i686", "x86_64"])
def test_accepts_system_dependencies_and_understood_delay_imports(machine):
    result = audit.audit_bytes(image(machine=machine, delayed=("USER32.dll", "MessageBoxW")))
    assert result["passed"], result["violations"]
    assert result["imports"][-1] == {"kind": "delay", "dll": "USER32.dll", "functions": ["MessageBoxW"]}
    assert "not proof" in result["limitation"]
    assert "incomplete" in result["limitation"]


@pytest.mark.parametrize("dll", ["nvapi64.dll", "nvml.dll", "drutacore.dll", "python314.dll",
                                  "vcruntime140.dll", "ucrtbase.dll", "api-ms-win-core-path-l1-1-0.dll"])
def test_rejects_tuning_python_and_runtime_dependencies(dll):
    result = audit.audit_bytes(image(imports=[_dll(dll, ["SomeExport"])]))
    assert not result["passed"]
    assert any(dll in violation for violation in result["violations"])


def test_forbidden_dependency_cannot_hide_in_delay_import_table():
    result = audit.audit_bytes(image(delayed=("drutacore.dll", "dc_gpu_open")))
    assert not result["passed"]
    assert "unexpected dependency: drutacore.dll (delay import)" in result["violations"]


@pytest.mark.parametrize("delay", [False, True])
def test_rejects_static_newer_dpi_import_even_from_an_allowed_system_dll(delay):
    data = (image(delayed=("USER32.dll", "GetDpiForWindow")) if delay else
            image(imports=[_dll("USER32.dll", ["GetDpiForWindow"])]))
    result = audit.audit_bytes(data)
    assert not result["passed"]
    assert any("GetDpiForWindow" in violation for violation in result["violations"])


@pytest.mark.parametrize("field", ["os_version", "subsystem_version"])
def test_rejects_newer_header_floor(field):
    result = audit.audit_bytes(image(**{field: (6, 1)}))
    assert not result["passed"]
    assert any("exceeds Windows 6.0" in violation for violation in result["violations"])


def test_rejects_unknown_delay_descriptor_and_truncated_image():
    result = audit.audit_bytes(image(delayed=("USER32.dll", "MessageBoxW"), delay_attributes=0))
    assert not result["passed"]
    assert any("RVA-based" in violation for violation in result["violations"])
    result = audit.audit_bytes(image()[:300])
    assert not result["passed"]


def test_cli_report_contains_exact_image_hash_and_qualification_limit(tmp_path):
    executable, report = tmp_path / "preview.exe", tmp_path / "imports.json"
    executable.write_bytes(image())
    assert audit.main([str(executable), "--report", str(report)]) == 0
    recorded = json.loads(report.read_text(encoding="utf-8"))
    assert recorded["sha256"] == audit.audit_bytes(executable.read_bytes())["sha256"]
    assert recorded["passed"] is True
    assert "static CRT" in recorded["limitation"]
    executable.write_bytes(b"invalid")
    assert audit.main([str(executable), "--report", str(report)]) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["passed"] is False
