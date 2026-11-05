# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fresh-process rail-request capture and one durable replay attempt.

The transaction owner journals and holds its adapter lock before launching.
This worker never retries SET or restarts a GPU. Request, attempt and result
files are exclusive, bounded records retained for recovery after a timeout.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from . import startup
from . import voltage_request_replay as replay

MAX_BYTES = 256 * 1024
TIMEOUT = 55
_PREFIX = "voltage-replay-"
_RESULT_KEYS = {"attempted", "status", "before", "desired", "after", "write_error", "readback_error", "verified"}


class WorkerError(RuntimeError):
    def __init__(self, kind, message, *, attempted=False, uncertain=False, result=None, paths=None, outcome=None):
        super().__init__(message)
        self.kind = kind
        self.attempted, self.uncertain = attempted, uncertain
        self.result, self.paths, self.outcome = result, paths, outcome


def _ro():
    # registry_overrides integrates this module; do not import it at module load.
    from . import registry_overrides

    return registry_overrides


def _target(value):
    ro = _ro()
    if isinstance(value, ro.Target):
        value = {key: getattr(value, key) for key in ("slot", "instance_id", "driver_key")}
    if not isinstance(value, dict) or set(value) != {"slot", "instance_id", "driver_key"}:
        raise WorkerError("identity", "Malformed replay worker target")
    if any(type(v) is not str or not v or len(v) > 4096 or any(ord(c) < 32 for c in v) for v in value.values()):
        raise WorkerError("identity", "Replay worker target contains invalid text")
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", value["slot"]):
        raise WorkerError("identity", "Replay worker needs a canonical PCI slot")
    parts = value["instance_id"].split("\\")
    if len(parts) != 3 or parts[0].upper() != "PCI" or not parts[2]:
        raise WorkerError("identity", "Replay worker needs a PCI device instance")
    hardware = parts[1].upper()
    if not hardware.startswith("VEN_10DE&") or not all(
        re.search(pattern, hardware)
        for pattern in (
            r"(?:^|&)DEV_[0-9A-F]{4}(?:&|$)",
            r"(?:^|&)SUBSYS_[0-9A-F]{8}(?:&|$)",
        )
    ):
        raise WorkerError("identity", "Replay worker needs an NVIDIA device and subsystem identity")
    if not re.fullmatch(
        r"SYSTEM\\CurrentControlSet\\Control\\Class\\\{4d36e968-e325-11ce-bfc1-08002be10318\}\\[0-9]{4}",
        value["driver_key"],
        re.I,
    ):
        raise WorkerError("identity", "Replay worker driver key is not an adapter software key")
    return ro.Target(**value)


def _target_dict(target):
    return {key: getattr(target, key) for key in ("slot", "instance_id", "driver_key")}


def _state(state, target):
    try:
        state = replay.validate(state)
    except Exception as exc:
        raise WorkerError(getattr(exc, "kind", "malformed"), str(exc)) from exc
    if not state["complete"]:
        raise WorkerError("unsupported", "Replay requires every independently present rail")
    identity = state["identity"]
    hardware = target.instance_id.split("\\")[1].upper()
    if identity["slot"] != target.slot or not all(
        re.search(r"(?:^|&)" + token + r"(?:&|$)", hardware)
        for token in (f"DEV_{identity['device_id']:04X}", f"SUBSYS_{identity['subsystem_id']:08X}")
    ):
        raise WorkerError("identity", "Replay observation differs from the Windows target")
    return state


def _image_hash(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WorkerError("malformed", "Replay needs an exact canonical image SHA256")
    return value


def _paths(ident):
    if type(ident) is not str or not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise WorkerError("malformed", "Invalid replay worker job identifier")
    directory = _ro().state_directory().resolve()
    stem = _PREFIX + ident
    return {kind: directory / (stem + "." + kind + ".json") for kind in ("request", "attempt", "result")}


def _encoded(value):
    raw = (json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise WorkerError("malformed", "Replay worker record exceeds its bounded size")
    return raw


def _write_new(path, value):
    raw = _encoded(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    # An incomplete record remains on write/fsync failure. It must never be
    # overwritten or mistaken for proof that SET did not run.
    with path.open("xb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(raw).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_record(path):
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise WorkerError("malformed", "Replay worker record exceeds its bounded size")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda text: (_ for _ in ()).throw(ValueError(text)),
        )
    except (ValueError, UnicodeError) as exc:
        raise WorkerError("malformed", "Replay worker record is not canonical JSON") from exc
    if not isinstance(value, dict):
        raise WorkerError("malformed", "Replay worker record must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def _request(value):
    keys = {"schema", "job_id", "mode", "target"}
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise WorkerError("malformed", "Malformed replay worker request")
    mode = value.get("mode")
    if mode == "apply":
        keys |= {"saved", "expected_image_sha256"}
    elif mode != "capture":
        raise WorkerError("malformed", "Unknown replay worker operation")
    if set(value) != keys:
        raise WorkerError("malformed", "Replay worker request has unexpected fields")
    target = _target(value["target"])
    paths = _paths(value["job_id"])
    if mode == "apply":
        _state(value["saved"], target)
        _image_hash(value["expected_image_sha256"])
    return target, paths


def _attempt(request, request_hash):
    return {
        "schema": 1,
        "job_id": request["job_id"],
        "mode": "apply",
        "target": request["target"],
        "request_sha256": request_hash,
        "expected_image_sha256": request["expected_image_sha256"],
        "state": "attempted",
    }


def _result(payload, saved, target):
    if not isinstance(payload, dict) or set(payload) != _RESULT_KEYS:
        raise WorkerError("malformed", "Replay result is missing its bounded captures")
    if (
        payload["attempted"] is not True
        or payload["verified"] is not True
        or type(payload["status"]) is not int
        or payload["status"] != 0
        or payload["write_error"] is not None
        or payload["readback_error"] is not None
    ):
        raise WorkerError("malformed", "Replay result does not establish successful readback")
    before, desired, after = (_state(payload[key], target) for key in ("before", "desired", "after"))
    if replay._opaque(before) != replay._opaque(saved):
        raise WorkerError("identity", "Replay result changed the saved routing or opaque state")
    offsets = {
        i: replay._six(bytes.fromhex(raw), replay.controls.ROW_OFFSET + int(i) * replay.controls.ROW_STRIDE)
        for i, raw in saved["public"].items()
    }
    projected = replay.project(before, offsets_uv=offsets, boost_percent=saved["boost_percent"])
    if desired != projected or replay._requested(after) != replay._requested(projected):
        raise WorkerError("malformed", "Replay result does not match the exact saved requests")
    return copy.deepcopy(payload)


def run_worker(input_path, result_path):
    """Private child entry; reject reused paths before touching the adapter."""
    input_path, result_path = Path(input_path).resolve(), Path(result_path).resolve()
    match = re.fullmatch(re.escape(_PREFIX) + r"([0-9a-f]{32})\.request\.json", input_path.name)
    if not match:
        raise WorkerError("malformed", "Invalid replay worker input path")
    paths = _paths(match[1])
    if input_path != paths["request"] or result_path != paths["result"]:
        raise WorkerError("malformed", "Replay worker paths are outside their assigned job")
    if result_path.exists() or paths["attempt"].exists():
        raise WorkerError("malformed", "Replay worker job has already been used")
    request, request_hash = _read_record(input_path)
    target, expected_paths = _request(request)
    if expected_paths != paths:
        raise WorkerError("malformed", "Replay worker filename and request job differ")
    outcome = {
        "schema": 1,
        "job_id": request["job_id"],
        "mode": request["mode"],
        "target": request["target"],
        "request_sha256": request_hash,
        "attempted": False,
        "ok": False,
        "kind": None,
        "error": None,
        "payload": None,
    }
    try:
        from .nvbackend import GPU

        gpu = GPU(target.slot)
        ro = _ro()
        if ro.bind_target(gpu) != target:
            raise WorkerError("identity", "Fresh replay worker identifies another adapter")
        if request["mode"] == "capture":
            # Keep typed unsupported distinct from transient/malformed failures;
            # the public tuple helper intentionally flattens error messages.
            payload = _state(replay.read_checked(gpu), target)
        else:
            if hashlib.sha256(ro.read_vbios(gpu)).hexdigest() != request["expected_image_sha256"]:
                raise WorkerError("identity", "Fresh running image differs before voltage replay")
            if ro.bind_target(gpu) != target:
                raise WorkerError("identity", "Replay target changed during image verification")
            _write_new(paths["attempt"], _attempt(request, request_hash))
            # From here failure is potentially after a write unless the replay
            # module explicitly reports a pre-SET rejection.
            outcome["attempted"] = True
            try:
                payload = replay.replay(gpu, request["saved"])
            except BaseException as exc:
                outcome["attempted"] = bool(getattr(exc, "attempted", True))
                outcome["payload"] = getattr(exc, "result", None)
                raise
            outcome["payload"] = payload
            payload = _result(payload, request["saved"], target)
        if ro.bind_target(gpu) != target:
            raise WorkerError("identity", "Replay worker target changed after capture")
        outcome.update(ok=True, payload=payload)
    except BaseException as exc:
        outcome.update(kind=getattr(exc, "kind", "failure"), error=f"{type(exc).__name__}: {exc}")
    _write_new(result_path, outcome)
    return 0 if outcome["ok"] else 1


def worker_cli(argv):
    parser = argparse.ArgumentParser(prog="Druta --voltage-replay-worker")
    parser.add_argument("input")
    parser.add_argument("result")
    args = parser.parse_args(argv)
    return run_worker(args.input, args.result)


def _launch(target, mode, *, saved=None, expected_image_sha256=None):
    target = _target(target)
    request = {"schema": 1, "job_id": uuid.uuid4().hex, "mode": mode, "target": _target_dict(target)}
    if mode == "apply":
        request.update(saved=_state(saved, target), expected_image_sha256=_image_hash(expected_image_sha256))
    _, paths = _request(request)
    if any(path.exists() for path in paths.values()):
        raise WorkerError("malformed", "Replay worker job paths already exist")
    strings = {key: str(value) for key, value in paths.items()}
    try:
        request_hash = _write_new(paths["request"], request)
    except Exception as exc:
        raise WorkerError(
            getattr(exc, "kind", "failure"), f"Cannot save replay worker request: {exc}", paths=strings
        ) from exc
    launched = False
    process = None
    try:
        command = startup.application_command(
            "--voltage-replay-worker", strings["request"], strings["result"], windowed=True
        )
        launched = True
        process = subprocess.run(
            command, capture_output=True, timeout=TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        outcome, _ = _read_record(paths["result"])
        expected = {
            "schema",
            "job_id",
            "mode",
            "target",
            "request_sha256",
            "attempted",
            "ok",
            "kind",
            "error",
            "payload",
        }
        if (
            set(outcome) != expected
            or type(outcome["schema"]) is not int
            or outcome["schema"] != 1
            or outcome["job_id"] != request["job_id"]
            or outcome["mode"] != mode
            or outcome["target"] != request["target"]
            or outcome["request_sha256"] != request_hash
            or type(outcome["attempted"]) is not bool
            or type(outcome["ok"]) is not bool
        ):
            raise WorkerError("malformed", "Replay worker result does not bind its request")
        if mode == "capture" and (outcome["attempted"] or paths["attempt"].exists()):
            raise WorkerError("malformed", "Read-only worker reported a write attempt")
        if outcome["attempted"]:
            marker, _ = _read_record(paths["attempt"])
            if marker != _attempt(request, request_hash):
                raise WorkerError("malformed", "Replay worker attempt marker is not its request")
        if not outcome["ok"]:
            if type(outcome["kind"]) is not str or not outcome["kind"] or type(outcome["error"]) is not str:
                raise WorkerError("malformed", "Replay worker failure has no typed error")
            raise WorkerError(
                outcome["kind"],
                outcome["error"],
                attempted=outcome["attempted"],
                uncertain=outcome["attempted"],
                result=outcome["payload"],
                paths=strings,
                outcome=outcome,
            )
        if process.returncode != 0 or outcome["kind"] is not None or outcome["error"] is not None:
            raise WorkerError("malformed", "Replay worker success disagrees with its process outcome")
        if mode == "capture":
            return _state(outcome["payload"], target)
        if outcome["attempted"] is not True:
            raise WorkerError("malformed", "Replay worker success has no durable write attempt")
        result = _result(outcome["payload"], request["saved"], target)
        result["worker_paths"] = strings
        return result
    except WorkerError as exc:
        if exc.paths is not None:
            raise
        raise WorkerError(
            exc.kind,
            str(exc),
            attempted=paths["attempt"].exists(),
            uncertain=mode == "apply" and launched,
            paths=strings,
        ) from exc
    except BaseException as exc:
        attempted = paths["attempt"].exists()
        # OSError raised by process creation establishes that no child began;
        # a missing result after a returned process is an uncertain outcome.
        spawn_failed = process is None and isinstance(exc, OSError) and not attempted
        uncertain = mode == "apply" and launched and not spawn_failed
        if not isinstance(exc, Exception):
            exc.attempted, exc.uncertain, exc.paths = attempted, uncertain, strings
            raise
        raise WorkerError(
            "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "failure",
            f"Fresh voltage worker failed ({type(exc).__name__}): {exc}",
            attempted=attempted,
            uncertain=uncertain,
            paths=strings,
        ) from exc


def capture(target):
    """Return a complete, validated observation from one read-only child."""
    return _launch(target, "capture")


def apply(target, saved, expected_image_sha256):
    """One fresh-process replay with exact image binding and no write retry."""
    return _launch(target, "apply", saved=saved, expected_image_sha256=expected_image_sha256)
