# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only capture, process isolation and failure reports without GPU hardware."""
import ctypes
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from druta import backend_compare as compare
from druta import gpu_backend

SLOT = "0000:01:00.0"


def test_capture_only_calls_the_documented_readers(monkeypatch):
    calls = []
    half = SimpleNamespace(ok=True, err_detail="", selected={"slot": SLOT, "devid": 1,
                                                           "subsys": 2, "handle": 999})
    card = SimpleNamespace(slot=lambda: SLOT, pairing_error=None, nvapi=half, nvml=half,
                           static={"pl_min_mw": 50_000}, available=lambda: True)
    for name in ("read", "read_vf_curve", "read_volt_rail_limits", "volt_rail_diagnostics",
                 "get_current_limits", "read_power_policies", "read_clock_domains", "_read_pstates20"):
        setattr(card, name, lambda name=name: calls.append(name) or {"value": name})
    opened = Mock(return_value=card)
    monkeypatch.setattr(gpu_backend, "open_gpu", opened)
    monkeypatch.setattr(gpu_backend, "choice", lambda: gpu_backend.Choice("native", "native", "argument",
                                                                       "/core.dll", 2, None))
    monkeypatch.setattr(gpu_backend, "describe", lambda: "selected native")
    from druta import registry_overrides
    monkeypatch.setattr(registry_overrides, "bind_target", lambda given: calls.append("bind_target") or
                        registry_overrides.Target(SLOT, "instance", "driver"))
    result = compare.capture("native", SLOT)
    opened.assert_called_once_with(SLOT, kind="native")
    assert result["status"] == "complete"
    assert calls == ["read", "read_vf_curve", "read_volt_rail_limits", "volt_rail_diagnostics",
                     "get_current_limits", "read_power_policies", "read_clock_domains", "_read_pstates20",
                     "bind_target"]
    assert "handle" not in result["reads"]["identity"]["nvapi"]["selected"]
    assert result["reads"]["bind_target"]["driver_key"] == "driver"

    card.read_vf_curve = Mock(side_effect=ValueError("transient read"))
    result = compare.capture("native", SLOT)
    assert result["status"] == "partial"
    assert result["errors"]["vf_curve"] == {"type": "ValueError", "message": "transient read"}
    assert "clock_domains" in result["reads"]


@pytest.mark.parametrize("selected", ["0000:02:00.0", "0001:01:00.0", "0000:01:00.1"])
def test_capture_wrong_target_stops_before_any_reads(monkeypatch, selected):
    card = SimpleNamespace(slot=lambda: selected, read=Mock())
    monkeypatch.setattr(gpu_backend, "open_gpu", lambda *args, **kwargs: card)
    monkeypatch.setattr(gpu_backend, "choice", lambda: gpu_backend.Choice("python", "python", "argument",
                                                                       None, None, None))
    result = compare.capture("python", SLOT)
    assert result["status"] == "failed"
    assert "selected" in result["errors"]["backend"]["message"]
    card.read.assert_not_called()


def test_serializes_pstate_structures_without_addresses():
    class Row(ctypes.Structure):
        _fields_ = [("value", ctypes.c_uint32), ("flags", ctypes.c_uint32, 2)]
    rows = (Row * 2)(Row(17, 2), Row(19, 1))
    assert compare._json_value(rows) == [{"value": 17, "flags": 2}, {"value": 19, "flags": 1}]
    assert compare._json_value(float("nan")) == {"nonfinite": "nan"}
    with pytest.raises(TypeError, match="Unsupported capture"):
        compare._json_value(object())


def test_comparison_runs_separate_children_even_when_parent_is_pinned(monkeypatch, tmp_path):
    child = tmp_path / "fake child.py"
    child.write_text('''import json, os, sys
_, kind, slot, output = sys.argv[1:]
assert os.environ["DRUTA_BACKEND"] == kind
with open(output, "x") as stream:
    json.dump({"format": 1, "backend": kind, "requested_slot": slot, "status": "complete",
               "pid": os.getpid(), "reads": {"identity": {"slot": slot}}, "errors": {}}, stream)
''')
    monkeypatch.setattr(compare.startup, "application_command", lambda *args: [sys.executable, str(child), *args])
    monkeypatch.setattr(gpu_backend, "open_gpu", Mock(side_effect=AssertionError("parent must not open GPU")))
    monkeypatch.setattr(gpu_backend, "_choice", gpu_backend.Choice("native", "native", "argument", "x", 2, None))
    monkeypatch.setenv(gpu_backend.ENV, "auto")
    output = tmp_path / "reports"
    result = compare.compare(SLOT, output)
    assert result["equal"]
    python = json.loads((output / "python.json").read_text())
    native = json.loads((output / "native.json").read_text())
    assert python["pid"] != native["pid"] and python["pid"] != os.getpid()
    assert gpu_backend.choice().kind == "native"
    assert os.environ[gpu_backend.ENV] == "auto"
    assert (output / "diff.txt").read_text() == "No captured differences.\n"
    with pytest.raises(FileExistsError):
        compare.compare(SLOT, output)


@pytest.mark.parametrize("failure", ["timeout", "crash", "malformed", "complete_then_crash"])
def test_failed_children_are_not_reported_as_equal(monkeypatch, tmp_path, failure):
    def run(command, **kwargs):
        kind, slot, path = command[-3:]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "malformed":
            from pathlib import Path
            Path(path).write_text("{partial")
        if failure == "complete_then_crash":
            compare._write_json(path, {"format": 1, "backend": kind, "requested_slot": slot,
                                       "status": "complete", "reads": {}, "errors": {}})
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(compare.subprocess, "run", run)
    output = tmp_path / "reports"
    result = compare.compare(SLOT, output, timeout=0.1)
    assert not result["complete"] and not result["equal"]
    assert result["errors"]["python"]["process"]
    assert (output / "python.json").is_file() and (output / "native.json").is_file()
    if failure == "malformed":
        assert (output / "python.invalid.json").read_text() == "{partial"


def test_diff_reports_changed_read_values_and_cli_exit_code(monkeypatch, tmp_path):
    def run(command, **kwargs):
        kind, slot, path = command[-3:]
        compare._write_json(path, {"format": 1, "backend": kind, "requested_slot": slot,
                                   "status": "complete", "reads": {"telemetry": 1 if kind == "python" else 2},
                                   "errors": {}})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(compare.subprocess, "run", run)
    output = tmp_path / "reports"
    tell = Mock()
    assert compare.cli([SLOT, str(output)], tell) == 1
    assert "differences" in tell.call_args.args[0]
    assert json.loads((output / "comparison.json").read_text())["differing_sections"] == ["telemetry"]
    assert "--- python.json" in (output / "diff.txt").read_text()
    assert compare.cli(["bad-slot", str(tmp_path / "bad")], tell) == 2
    assert not (tmp_path / "bad").exists()


def test_child_uses_file_output_and_refuses_overwrite(monkeypatch, tmp_path):
    report = {"status": "complete", "backend": "native"}
    monkeypatch.setattr(compare, "capture", lambda *args: report)
    path = tmp_path / "capture.json"
    assert compare.capture_cli(["native", SLOT, str(path)]) == 0
    assert json.loads(path.read_text()) == report
    assert compare.capture_cli(["native", SLOT, str(path)]) == 2
    assert compare.capture_cli(["auto", SLOT, str(path)]) == 2


def test_main_dispatches_compare_before_startup(monkeypatch):
    from druta import druta
    monkeypatch.setattr(druta.startup, "Startup", Mock(side_effect=AssertionError("no startup manager")))
    monkeypatch.setattr(compare, "cli", Mock(return_value=1))
    monkeypatch.setattr(compare, "capture_cli", Mock(return_value=0))
    assert druta.main(["--backend-compare", SLOT, "new-directory"]) == 1
    compare.cli.assert_called_once_with([SLOT, "new-directory"], druta._tell)
    assert druta.main(["--backend-capture", "native", SLOT, "capture.json"]) == 0


def test_diagnostic_drain_is_native_only(monkeypatch):
    from druta import native_frontend
    drain = Mock(return_value=["native backend: front end requested missing member Gpu.test"])
    monkeypatch.setattr(native_frontend, "drain_missing_members", drain)
    monkeypatch.setattr(gpu_backend, "_choice", None)
    assert gpu_backend.drain_diagnostics() == []
    monkeypatch.setattr(gpu_backend, "_choice", gpu_backend.Choice("python", "python", "argument", None, None, None))
    assert gpu_backend.drain_diagnostics() == []
    drain.assert_not_called()
    monkeypatch.setattr(gpu_backend, "_choice", gpu_backend.Choice("native", "native", "argument", "x", 2, None))
    assert gpu_backend.drain_diagnostics() == drain.return_value
    drain.assert_called_once_with()


def test_real_surface_gate_checks_the_loaded_backend_class():
    # A successful DLL load must not bypass the class surface check.
    assert "clock_offset_range" in gpu_backend._native_frontend_gate(SimpleNamespace(Gpu=object))
