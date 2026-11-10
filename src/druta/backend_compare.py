# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only backend snapshots in separate processes, including the windowed EXE.

There is deliberately no setter, refresh, profile replay or reset in this tool.
Live readings are taken sequentially, so differences are observations to review,
not a verdict that either backend is wrong.
"""
from __future__ import annotations

import ctypes
import dataclasses
import difflib
import json
import math
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from . import gpu_backend, startup
from .nvbackend import format_slot, parse_slot

_CHILD_FLAG = "--backend-capture"
_KINDS = ("python", "native")
_TIMEOUT = 120


def _json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    if dataclasses.is_dataclass(value):
        return _json_value(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=repr)
    if isinstance(value, (list, tuple, ctypes.Array)):
        return [_json_value(item) for item in value]
    if isinstance(value, (ctypes.Structure, ctypes.Union)):
        return {field[0]: _json_value(getattr(value, field[0])) for field in value._fields_}
    if isinstance(value, (bytes, bytearray)):
        return {"hex": bytes(value).hex()}
    raise TypeError(f"Unsupported capture value: {type(value).__name__}")


def _identity(card):
    # Driver handles and enumeration indices belong to a process, so they
    # cannot identify a mismatch between the two captures.
    keys = ("slot", "devid", "subsys")
    return {
        "slot": card.slot(), "pairing_error": card.pairing_error,
        "nvapi": {"ok": card.nvapi.ok, "error": card.nvapi.err_detail,
                  "selected": {key: (card.nvapi.selected or {}).get(key) for key in keys}},
        "nvml": {"ok": card.nvml.ok, "error": card.nvml.err_detail,
                 "selected": {key: (card.nvml.selected or {}).get(key) for key in keys}},
    }


def capture(kind, slot):
    """Capture exactly one forced backend in this (normally child) process."""
    from .registry_overrides import bind_target

    report = {"format": 1, "backend": kind, "requested_slot": slot,
              "captured_utc": datetime.now(UTC).isoformat(),
              "status": "failed", "reads": {}, "errors": {}}
    try:
        card = gpu_backend.open_gpu(slot, kind=kind)
        report["selection"] = dataclasses.asdict(gpu_backend.choice())
        report["description"] = gpu_backend.describe()
        if parse_slot(card.slot()) != parse_slot(slot):
            raise ValueError(f"Backend selected {card.slot()!r}, requested {slot!r}")
        if not card.available():
            raise RuntimeError(card.status_line())
        # _Static is lazy on native. Asking for a capability merges its
        # complete table before the same static mapping is recorded on both.
        card.static.get("pl_min_mw")
        sections = (
            ("identity", lambda: _identity(card)),
            ("static", lambda: dict(card.static)),
            ("telemetry", card.read),
            ("vf_curve", card.read_vf_curve),
            ("rail_limits", card.read_volt_rail_limits),
            ("rail_diagnostics", card.volt_rail_diagnostics),
            ("current_limits", card.get_current_limits),
            ("power_policies", card.read_power_policies),
            ("clock_domains", card.read_clock_domains),
            ("pstates20", card._read_pstates20),
            ("bind_target", lambda: bind_target(card)),
        )
        for name, reader in sections:
            try:
                report["reads"][name] = _json_value(reader())
            except Exception as exc:  # one failed getter must not hide the other evidence
                report["errors"][name] = {"type": type(exc).__name__, "message": str(exc)}
        report["status"] = "partial" if report["errors"] else "complete"
    except Exception as exc:
        report["errors"]["backend"] = {"type": type(exc).__name__, "message": str(exc)}
    return report


def _write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
        stream.write("\n")


def capture_cli(argv):
    """Internal child command; communicates through a file, never a message box."""
    if len(argv) != 3 or argv[0] not in _KINDS or parse_slot(argv[1]) is None:
        return 2
    kind, slot, output = argv
    report = capture(kind, slot)
    try:
        _write_json(output, report)
    except OSError:
        return 2
    return 0 if report["status"] == "complete" else 1


def _failed_child(kind, slot, reason):
    return {"format": 1, "backend": kind, "requested_slot": slot, "status": "failed",
            "reads": {}, "errors": {"process": {"type": "ChildFailed", "message": reason}}}


def compare(slot, directory, *, timeout=_TIMEOUT):
    """Save both captures and a diff to a new directory; never open a parent GPU."""
    parsed = parse_slot(slot)
    if parsed is None:
        raise ValueError("Expected a PCI slot, for example 0000:01:00.0")
    slot = format_slot(*parsed)
    directory = Path(directory).resolve()
    directory.mkdir()  # refuse an old result directory, including existing symlinks
    reports = {}
    for kind in _KINDS:
        output = directory / f"{kind}.json"
        command = startup.application_command(_CHILD_FLAG, kind, slot, str(output))
        env = dict(os.environ, **{gpu_backend.ENV: kind})
        reason = None
        try:
            result = subprocess.run(command, env=env, capture_output=True, timeout=timeout,
                                    check=False)
            if result.returncode:
                reason = f"Capture process exited with code {result.returncode}"
        except subprocess.TimeoutExpired:
            reason = f"Capture process timed out after {timeout:g} seconds"
        except OSError as exc:
            reason = f"Cannot start capture process: {exc}"
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
            if (report.get("backend") != kind or report.get("requested_slot") != slot
                    or report.get("format") != 1 or not isinstance(report.get("reads"), dict)
                    or not isinstance(report.get("errors"), dict)
                    or report.get("status") not in ("complete", "partial", "failed")):
                raise ValueError("Capture identity or format mismatch")
            if reason and report["status"] == "complete":
                report["status"] = "failed"
                report["errors"]["process"] = {"type": "ChildFailed", "message": reason}
        except (OSError, ValueError, AttributeError) as exc:
            report = _failed_child(kind, slot, reason or f"Invalid capture output: {exc}")
            # Preserve malformed/truncated evidence rather than overwrite it.
            if output.exists():
                output.rename(directory / f"{kind}.invalid.json")
            _write_json(output, report)
        reports[kind] = report
    left = {"reads": reports["python"]["reads"], "errors": reports["python"]["errors"]}
    right = {"reads": reports["native"]["reads"], "errors": reports["native"]["errors"]}
    differing = sorted(name for name in left["reads"].keys() | right["reads"].keys()
                       if name not in left["reads"] or name not in right["reads"]
                       or left["reads"][name] != right["reads"][name])
    complete = all(report["status"] == "complete" for report in reports.values())
    equal = complete and left == right
    summary = {"format": 1, "slot": slot, "complete": complete, "equal": equal,
               "differing_sections": differing,
               "statuses": {kind: report["status"] for kind, report in reports.items()},
               "errors": {kind: report["errors"] for kind, report in reports.items()},
               "note": "Sequential live readings can differ with operating point; review the captures."}
    _write_json(directory / "comparison.json", summary)
    delta = difflib.unified_diff(json.dumps(left, indent=2, sort_keys=True).splitlines(True),
                                 json.dumps(right, indent=2, sort_keys=True).splitlines(True),
                                 fromfile="python.json (reads/errors)", tofile="native.json (reads/errors)")
    (directory / "diff.txt").write_text("".join(delta) or "No captured differences.\n", encoding="utf-8")
    return summary


def cli(argv, tell=print):
    if len(argv) != 2:
        tell("usage: Druta --backend-compare SLOT NEW_DIRECTORY")
        return 2
    try:
        summary = compare(*argv)
    except (OSError, ValueError, RuntimeError) as exc:
        tell(f"Backend comparison failed: {exc}")
        return 2
    state = "equal" if summary["equal"] else "differences" if summary["complete"] else "incomplete"
    tell(f"Backend comparison: {state}; reports in {Path(argv[1]).resolve()}. " + summary["note"])
    return 0 if summary["equal"] else 1
