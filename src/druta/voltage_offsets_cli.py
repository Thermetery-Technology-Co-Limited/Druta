# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit PCI-slot command-line access to the live voltage-offset engine."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_OFFSET = re.compile(r"([0-9]+):(?i:(REL|ALT|OV|VMIN))=([+-]?[0-9]+)\Z")
_PROFILE_KEYS = {"schema", "slot", "values"}
_RESULT_KEYS = ("slot", "values", "policy", "saved_values", "unsupported")


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, notify, **kwargs):
        self._notify = notify
        super().__init__(*args, **kwargs)

    def _print_message(self, message, file=None):
        if not message:
            return
        stream = file if file is not None else sys.stdout
        if stream is None:
            self._notify(message.rstrip())
            return
        try:
            stream.write(message)
        except (AttributeError, OSError, ValueError):
            self._notify(message.rstrip())


def _parser(notify):
    parser = _ArgumentParser(
        prog="Druta --voltage-offsets",
        description="Read and change app-owned signed-microvolt VFE policy offsets for one explicit GPU slot.",
        epilog="enable creates zero-value scaffold heads and performs a GPU PnP restart; Windows does not reboot.",
        allow_abbrev=False,
        notify=notify,
    )
    parser.add_argument("--gpu", required=True, metavar="SLOT", help="explicit PCI slot, for example 0000:01:00.0")
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=lambda **kwargs: _ArgumentParser(notify=notify, **kwargs),
    )
    enable_parser = commands.add_parser(
        "enable",
        help="install zero-value scaffold heads and restart the selected GPU (Windows does not reboot)",
    )
    enable_parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    enable_parser.add_argument("--reopen", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("read", help="read current offsets and reported policy")
    set_parser = commands.add_parser("set", help="set one or more signed-microvolt policy offsets")
    set_parser.add_argument(
        "--offset", action="append", required=True, metavar="RAIL:ROLE=SIGNED_UV",
        help="repeatable, for example 0:REL=-5000",
    )
    capture_parser = commands.add_parser("capture", help="capture current offsets as a replay profile")
    capture_parser.add_argument("--out", metavar="PROFILE", help="optional JSON profile output path")
    replay_parser = commands.add_parser("replay", help="replay a previously captured profile")
    replay_parser.add_argument("--profile", required=True, metavar="PROFILE")
    return parser


def _engine():
    # The live engine and its driver-facing dependencies load only for this CLI.
    from . import voltage_offsets_live

    return voltage_offsets_live


def _wait_for_parent(pid):
    from . import devicerecovery

    devicerecovery.wait_for_exit(pid)


def _reopen_gui(slot):
    from . import startup

    command = startup.application_command("--gpu", slot, windowed=True)
    return subprocess.Popen(command, close_fds=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def launch_enable(slot):
    """Launch zero-head activation after this GUI exits, then reopen its GPU."""
    from . import startup

    command = startup.application_command(
        "--voltage-offsets", "--gpu", slot, "enable",
        "--parent-pid", str(os.getpid()), "--reopen", windowed=True,
    )
    return subprocess.Popen(command, close_fds=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _enable_helper(slot, parent_pid, notify):
    try:
        _wait_for_parent(parent_pid)
    except Exception as exc:
        notify(f"Voltage-offset helper could not confirm parent exit; activation was not attempted: {exc}")
        return 1

    activation_error = None
    try:
        _engine().enable(slot)
    except Exception as exc:
        activation_error = exc

    reopen_error = None
    try:
        _reopen_gui(slot)
    except Exception as exc:
        reopen_error = exc

    if activation_error is not None:
        message = f"Voltage-offset activation failed: {activation_error}"
        if reopen_error is not None:
            message += f"\nCould not reopen Druta: {reopen_error}"
        notify(message)
        return 1
    if reopen_error is not None:
        notify(f"Voltage offsets were enabled, but Druta could not reopen: {reopen_error}")
        return 1
    return 0


def _parse_offsets(parser, items):
    values = {}
    for item in items:
        match = _OFFSET.fullmatch(item)
        if match is None:
            parser.error(f"invalid --offset {item!r}; expected RAIL:REL|ALT|OV|VMIN=SIGNED_UV")
        rail, role, raw_value = match.groups()
        key = f"voltage.{int(rail)}.{role.upper()}.offset"
        if key in values:
            parser.error(f"duplicate voltage offset {key}")
        value = int(raw_value)
        if not -(2**31) <= value <= 2**31 - 1:
            parser.error(f"{key} must be a signed 32-bit microvolt value")
        values[key] = value
    return values


def _profile_path(parser, path_text, *, output):
    path = Path(path_text).expanduser().absolute()
    if not path.parent.is_dir():
        parser.error(f"profile parent directory does not exist: {path.parent}")
    if not output and not path.is_file():
        parser.error(f"profile file does not exist: {path}")
    return path


def _read_profile(parser, path, slot):
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read profile: {exc}")
    if (
        not isinstance(profile, dict)
        or set(profile) != _PROFILE_KEYS
        or type(profile.get("schema")) is not int
        or profile["schema"] != 1
        or profile.get("slot") != slot
        or not isinstance(profile.get("values"), dict)
        or not profile["values"]
    ):
        parser.error("profile must be a schema-1 capture for the explicitly selected slot")
    return profile["values"]


def _validate_values(parser, values, *, allow_empty):
    if not isinstance(values, dict) or (not allow_empty and not values):
        parser.error("profile values must be a non-empty offset mapping")
    result = {}
    for key, value in values.items():
        if (
            not isinstance(key, str)
            or re.fullmatch(r"voltage\.[0-9]+\.(?:REL|ALT|OV|VMIN)\.offset", key) is None
            or type(value) is not int
            or not -(2**31) <= value <= 2**31 - 1
        ):
            parser.error("profile values must map voltage.<rail>.<REL|ALT|OV|VMIN>.offset to signed 32-bit microvolts")
        result[key] = value
    return result


def _write_profile(path, profile):
    from . import startup

    startup.atomic_json(path, profile)


def _print_json(value, notify):
    message = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if sys.stdout is None:
        notify(message)
        return
    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    except (AttributeError, OSError, ValueError):
        notify(message)


def _public_result(result):
    if not isinstance(result, dict):
        raise ValueError("Voltage-offset engine returned an invalid result")
    return {key: result[key] for key in _RESULT_KEYS if key in result}


def cli(argv, notify):
    parser = _parser(notify)
    args = parser.parse_args(argv)
    helper_mode = False
    if args.command == "enable":
        helper_mode = args.parent_pid is not None or args.reopen
        if helper_mode and (args.parent_pid is None or not args.reopen):
            parser.error("enable helper requires both --parent-pid and --reopen")
        if args.parent_pid is not None and args.parent_pid <= 0:
            parser.error("--parent-pid must be positive")
    elif hasattr(args, "parent_pid") or hasattr(args, "reopen"):
        parser.error("parent wait/reopen flags are only valid for enable")
    slot = args.gpu
    if helper_mode:
        return _enable_helper(slot, args.parent_pid, notify)
    offsets = _parse_offsets(parser, args.offset) if args.command == "set" else None
    output_path = None
    profile_values = None
    if args.command == "capture" and args.out:
        output_path = _profile_path(parser, args.out, output=True)
    elif args.command == "replay":
        profile_path = _profile_path(parser, args.profile, output=False)
        profile_values = _validate_values(parser, _read_profile(parser, profile_path, slot), allow_empty=False)

    try:
        engine = _engine()
        if args.command == "enable":
            result = engine.enable(slot)
            payload = {"command": args.command, **_public_result(result)}
        elif args.command == "read":
            result = engine.read(slot)
            payload = {"command": args.command, **_public_result(result)}
        elif args.command == "set":
            result = engine.set_offsets(slot, offsets)
            payload = {"command": args.command, **_public_result(result)}
        elif args.command == "capture":
            values = _validate_values(parser, engine.capture(slot), allow_empty=True)
            payload = {"command": args.command, "slot": slot, "values": values}
            if output_path is not None:
                _write_profile(output_path, {"schema": 1, "slot": slot, "values": values})
                payload["profile"] = str(output_path)
        else:
            result = engine.replay(slot, profile_values)
            payload = {"command": args.command, **_public_result(result)}
        _print_json(payload, notify)
        return 0
    except Exception as exc:
        _print_json({"command": args.command, "error": str(exc)}, notify)
        return 1
