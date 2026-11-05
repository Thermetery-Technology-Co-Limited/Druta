# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Collect a read-only Ampere voltage replay inventory for a remote review."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

SCRIPT_SCHEMA = "ampere-voltage-inventory-handoff-v1"
SLOT_RE = re.compile(r"(?:[0-9A-Fa-f]{4}|[0-9A-Fa-f]{8}):[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7]\Z")
SOURCE_FILES = (
    "src/druta/nvbackend.py",
    "src/druta/registry_overrides.py",
    "src/druta/voltage_bios_runtime.py",
    "src/druta/voltage_request_replay.py",
    "src/druta/voltage_request_gate.py",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / "AGENTS.md").is_file() and (candidate / "src" / "druta").is_dir():
            return candidate
    raise RuntimeError("Could not locate the Druta repository from this script path")


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"bytes_hex": raw.hex(), "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return [_jsonable(item) for item in sorted(value, key=repr)]
    return {"repr": repr(value), "type": type(value).__name__}


def _error(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "message": str(exc)}


def _attempt(function):
    try:
        return {"ok": True, "value": _jsonable(function())}
    except Exception as exc:
        return {"ok": False, "error": _error(exc)}


def _source_state(root: Path, script_path: Path) -> dict:
    state = {"repository_name": root.name, "commit": None, "worktree_status": None, "files_sha256": {}}
    for label, args in (
        ("commit", ["git", "rev-parse", "HEAD"]),
        ("worktree_status", ["git", "status", "--short", "--untracked-files=all"]),
    ):
        try:
            result = subprocess.run(
                args,
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode == 0:
                value = result.stdout.rstrip("\r\n")
                state[label] = value if value else ("" if label == "worktree_status" else None)
            else:
                state[label] = {"error": result.stderr.strip(), "returncode": result.returncode}
        except Exception as exc:
            state[label] = {"error": _error(exc)}

    paths = [*SOURCE_FILES, "AGENTS.md", "CLAUDE.md"]
    try:
        relative_script = script_path.resolve().relative_to(root.resolve()).as_posix()
        paths.append(relative_script)
    except ValueError:
        pass
    for relative in paths:
        path = root / Path(relative)
        try:
            state["files_sha256"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception as exc:
            state["files_sha256"][relative] = {"error": _error(exc)}
    return state


def _pci_identity(gpu) -> dict:
    def api_identity(api):
        return {
            "ok": bool(getattr(api, "ok", False)),
            "selected": _jsonable(getattr(api, "selected", None)),
            "error": str(getattr(api, "err_detail", "") or ""),
        }

    static = getattr(gpu, "static", {}) or {}
    return {
        "requested_slot": str(getattr(gpu, "requested_slot", "") or ""),
        "selected_slot": str(gpu.slot() or ""),
        "pairing_error": str(getattr(gpu, "pairing_error", "") or ""),
        "nvapi": api_identity(gpu.nvapi),
        "nvml": api_identity(gpu.nvml),
        "static_identity": _jsonable({key: static.get(key) for key in ("slot", "uuid", "name", "vbios", "driver")}),
    }


def _target_dict(target) -> dict:
    return {"slot": target.slot, "instance_id": target.instance_id, "driver_key": target.driver_key}


def _bracket(gpu, registry_overrides) -> dict:
    result = {"captured_at_utc": _utc_now()}
    try:
        target = registry_overrides.bind_target(gpu)
        result["target"] = {"ok": True, "value": _target_dict(target)}
        result["managed_registry"] = _attempt(lambda: registry_overrides.WindowsRegistry(target).snapshot())
    except Exception as exc:
        result["target"] = {"ok": False, "error": _error(exc)}
        result["managed_registry"] = {"ok": False, "not_attempted": "Windows target binding failed"}

    try:
        image = registry_overrides.read_vbios(gpu)
        result["vbios_image"] = {
            "ok": True,
            "byte_length": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
            "image_hex": image.hex(),
        }
    except Exception as exc:
        result["vbios_image"] = {"ok": False, "error": _error(exc)}
    return result


def _reader_result(function, *, returns_pair: bool) -> dict:
    try:
        value = function()
        if returns_pair and isinstance(value, tuple) and len(value) == 2:
            observation, error = value
            return {"ok": error is None, "observation": _jsonable(observation), "error": _jsonable(error)}
        return {"ok": True, "value": _jsonable(value)}
    except Exception as exc:
        return {"ok": False, "error": _error(exc)}


def _read_round(gpu, voltage_bios_runtime, voltage_request_replay, gate) -> dict:
    result = {"started_at_utc": _utc_now()}
    result["voltage_bios_runtime"] = _reader_result(lambda: voltage_bios_runtime.read(gpu), returns_pair=True)
    result["voltage_request_replay"] = _reader_result(lambda: voltage_request_replay.read(gpu), returns_pair=True)
    result["voltage_request_gate"] = _reader_result(gate.read, returns_pair=False)
    result["finished_at_utc"] = _utc_now()
    return result


def _same_ok_value(left: dict, right: dict):
    if not left.get("ok") or not right.get("ok"):
        return None
    return left.get("value") == right.get("value")


def _comparison(before: dict, final: dict) -> dict:
    return {
        "target_exact": _same_ok_value(before.get("target", {}), final.get("target", {})),
        "managed_registry_exact": _same_ok_value(before.get("managed_registry", {}), final.get("managed_registry", {})),
        "vbios_sha256_and_length_equal": (
            before["vbios_image"].get("sha256") == final["vbios_image"].get("sha256")
            and before["vbios_image"].get("byte_length") == final["vbios_image"].get("byte_length")
            if before.get("vbios_image", {}).get("ok") and final.get("vbios_image", {}).get("ok")
            else None
        ),
    }


def collect(slot: str, output_name: str) -> dict:
    root = _repo_root()
    script_path = Path(__file__).resolve()
    payload = {
        "schema": SCRIPT_SCHEMA,
        "created_at_utc": _utc_now(),
        "output_file_name": output_name,
        "scope": {
            "mode": "read-only inventory; no voltage request replay or configuration change",
            "selected_slot_argument": slot,
            "managed_registry_scope": "Only values accepted by registry_overrides.managed_name under the bound display-driver key are captured.",
            "reader_calls": [
                "registry_overrides.bind_target",
                "registry_overrides.WindowsRegistry.snapshot",
                "registry_overrides.read_vbios",
                "voltage_bios_runtime.read",
                "voltage_request_replay.read",
                "VoltageRequestGate.read",
            ],
            "operations_not_called": [
                "voltage_request_replay.replay or prepare",
                "VoltageRequestGate.set_enabled or any setter",
                "registry write/delete/replace",
                "driver restart, workload, or process stop",
            ],
            "read_failure_note": "Each reader records its own returned error or exception; missing observations are not treated as an empty inventory.",
        },
        "source_before": _source_state(root, script_path),
        "gpu_initialization": None,
        "pci_identity_before": None,
        "before": None,
        "read_rounds": [],
        "pci_identity_after": None,
        "final": None,
        "comparison": None,
        "source_after": None,
    }

    try:
        sys.path.insert(0, str(root / "src"))
        import druta.registry_overrides as registry_overrides
        import druta.voltage_bios_runtime as voltage_bios_runtime
        import druta.voltage_request_gate as voltage_request_gate
        import druta.voltage_request_replay as voltage_request_replay
        from druta.nvbackend import GPU

        gpu = GPU(slot)
        payload["gpu_initialization"] = {"ok": True}
    except Exception as exc:
        payload["gpu_initialization"] = {"ok": False, "error": _error(exc), "traceback": traceback.format_exc()}
        payload["source_after"] = _source_state(root, script_path)
        return payload

    payload["pci_identity_before"] = _attempt(lambda: _pci_identity(gpu))
    payload["before"] = _bracket(gpu, registry_overrides)
    try:
        gate = voltage_request_gate.VoltageRequestGate(gpu)
        for index in range(2):
            round_result = _read_round(gpu, voltage_bios_runtime, voltage_request_replay, gate)
            round_result["round"] = index + 1
            payload["read_rounds"].append(round_result)
    except Exception as exc:
        payload["read_round_setup_error"] = _error(exc)
    payload["pci_identity_after"] = _attempt(lambda: _pci_identity(gpu))
    payload["final"] = _bracket(gpu, registry_overrides)
    payload["comparison"] = _comparison(payload["before"], payload["final"])
    payload["source_after"] = _source_state(root, script_path)
    return payload


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Collect a read-only voltage inventory for remote review.")
    parser.add_argument("--gpu", required=True, help="Explicit PCI slot, for example 0000:01:00.0")
    parser.add_argument("--out", required=True, help="New JSON output file; parent directory must already exist")
    args = parser.parse_args(argv)
    if not SLOT_RE.fullmatch(args.gpu.strip()):
        parser.error("--gpu must be an explicit PCI slot such as 0000:01:00.0")
    out = Path(args.out).expanduser()
    if not out.is_absolute():
        out = Path.cwd() / out
    if not out.parent.is_dir():
        parser.error("--out parent directory must already exist")
    if out.exists():
        parser.error("--out must name a new file; existing files are never overwritten")
    return args.gpu.strip(), out


def main(argv=None) -> int:
    slot, output = _parse_args(argv)
    payload = collect(slot, output.name)
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
    except FileExistsError:
        raise SystemExit("Refusing to overwrite an existing output file") from None
    print(json.dumps({"output": str(output), "schema": SCRIPT_SCHEMA, "read_rounds": len(payload["read_rounds"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
