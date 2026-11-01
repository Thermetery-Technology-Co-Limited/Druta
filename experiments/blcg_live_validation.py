# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read BLCG witnesses; --trial performs one Ampere hold/read/release session.

Uses the production controller only. Observed configuration registers are a
partial source-bound witness, not full driver state or physical gating effect.
No hardware modules are imported for --help or offline collector tests.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

_HELPER_PATH = Path(__file__).with_name("ampere_voltage_inventory_20261030.py")
_SPEC = importlib.util.spec_from_file_location("blcg_inventory_helpers", _HELPER_PATH)
util = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(util)
SCHEMA = "blcg-live-validation-v1"
SOURCE_FILES = (
    "src/druta/block_clock_gating_live.py",
    "src/druta/gr_gating_live.py",
    "src/druta/clock_bios_runtime.py",
    "src/druta/lpwr_runtime.py",
    "src/druta/perf_diagnostics.py",
    "src/druta/nvbackend.py",
    "experiments/ampere_voltage_inventory_20261030.py",
)


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _slot(value):
    _require(isinstance(value, str) and util.SLOT_RE.fullmatch(value), "Explicit PCI slot required")
    domain, bus, tail = value.lower().split(":")
    device, function = tail.split(".")
    _require(int(domain, 16) == 0 and int(device, 16) <= 31, "PCI address outside understood range")
    return f"{int(domain, 16):04x}:{int(bus, 16):02x}:{int(device, 16):02x}.{int(function)}"


def _source(root):
    state = util._source_state(root, Path(__file__))
    state["files_sha256"] = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (*SOURCE_FILES, "AGENTS.md", "CLAUDE.md", "experiments/blcg_live_validation.py")
    }
    return state


def _event_kind(words):
    if len(words) == 22 and words[4] == 0x0500002A and words[15] == 0xB2CC:
        return "allocate"
    if len(words) == 20 and words[14] == 0xB0CC0301:
        return "request"
    if len(words) == 18 and words[14] == 0xB0CC0302:
        return "release"
    if len(words) == 16 and words[4] == 0x0500000C:
        return "free"
    return None


def _inventory(state):
    return (state["architecture"], state["layout"], state["registers"].keys(), state["allowed_clear_masks"])


def collect(gpu, slot, *, trial, controller_factory, checkpoint, source):
    """Readers/controller are injectable; the wrapper observes and never issues calls."""
    report = {
        "schema": SCHEMA,
        "created_at_utc": util._utc_now(),
        "slot": slot,
        "trial_requested": trial,
        "scope": "Partial source-bound configuration witness; physical effect not measured",
        "events": [],
        "errors": [],
        "before": None,
        "held": None,
        "after": None,
        "cleanup": None,
        "after_cleanup": None,
        "qualified": False,
    }
    controller = None
    original_escape = gpu._legacy_clk_escape

    def observed_escape(packet, fields):
        before = [int(word) for word in packet]
        kind = _event_kind(before)
        if kind is None:
            return original_escape(packet, fields)
        event = {
            "kind": kind,
            "phase": report.get("phase"),
            "packet_before": before,
            "fields": util._jsonable(fields),
            "status": None,
            "error": None,
        }
        try:
            status = original_escape(packet, fields)
            event["status"] = util._jsonable(status)
            return status
        except BaseException as exc:
            event["error"] = util._error(exc)
            raise
        finally:
            event["packet_after"] = [int(word) for word in packet]
            report["events"].append(event)

    gpu._legacy_clk_escape = observed_escape
    try:
        report["source_before"] = source()
        report["pci_identity_before"] = util._pci_identity(gpu)
        pci = report["pci_identity_before"]
        _require(
            not pci["pairing_error"] and _slot(pci["requested_slot"]) == slot == _slot(pci["selected_slot"]),
            "Selected PCI identity does not match requested slot",
        )
        controller = controller_factory(gpu)
        report["controller_identity"] = util._jsonable(controller.identity)
        _require(_slot(controller.identity["slot"]) == slot, "Controller identifies another PCI target")
        report["phase"] = "before"
        report["before"] = controller.read()
        if trial:
            before = report["before"]
            _require(
                before["support"]
                and before["architecture"]["architecture"] == 0x170
                and before["layout"] == "ga10x-blcg",
                "--trial requires the source-supported native170 ga10x-blcg layout",
            )
            masks = before["allowed_clear_masks"]
            _require(
                len(before["registers"]) == len(masks) == 5 and before["registers"].keys() == masks.keys(),
                "--trial requires all five source-defined registers to be valid",
            )
            _require(
                all(value & ~masks[key] == 0 for key, value in before["registers"].items()),
                "--trial baseline contains bits outside the source-defined masks",
            )
            _require(
                any(before["registers"].values()),
                "--trial requires at least one nonzero baseline register for a positive transition",
            )
            _require(not before["owned_request"] and not before["uncertain_outcome"], "Controller is not idle")
            checkpoint(report)
            report["phase"] = "hold"
            report["hold_response"] = controller.hold()
            report["held"] = controller.read()
            report["phase"] = "release"
            report["release_response"] = controller.release()
            report["after"] = controller.read()
    except BaseException as exc:
        report["errors"].append({"phase": report.get("phase", "initialization"), **util._error(exc)})
    finally:
        if controller is not None:
            report["phase"] = "cleanup_own_controller"
            report["cleanup"] = util._attempt(controller.close)
            report["after_cleanup"] = util._attempt(controller.read)
        report["pci_identity_after"] = util._attempt(lambda: util._pci_identity(gpu))
        report["source_after"] = util._attempt(source)
        gpu._legacy_clk_escape = original_escape
        report["phase"] = "finished"

    events = report["events"]
    report["event_counts"] = {
        kind: sum(event["kind"] == kind for event in events) for kind in ("allocate", "request", "release", "free")
    }
    report["checks"] = {
        "no_collector_errors": not report["errors"],
        "cleanup_succeeded": bool(report["cleanup"] and report["cleanup"]["ok"]),
        "cleanup_read_succeeded": bool(report["after_cleanup"] and report["after_cleanup"]["ok"]),
        "pci_identity_unchanged": report["pci_identity_after"]["ok"]
        and report["pci_identity_after"]["value"] == report.get("pci_identity_before"),
        "source_unchanged": report["source_after"]["ok"]
        and report["source_after"]["value"] == report.get("source_before"),
    }
    if trial and all(report[key] is not None for key in ("before", "held", "after")):
        before, held, after = (report[key] for key in ("before", "held", "after"))
        clean = report["after_cleanup"]["value"] if report["after_cleanup"]["ok"] else None
        report["checks"].update(
            {
                "one_bounded_lifecycle": [event["kind"] for event in events]
                == ["allocate", "request", "release", "free"],
                "source_hold_values_observed": bool(held["registers"])
                and all(value == 0 for value in held["registers"].values()),
                "positive_transition": before["registers"] != held["registers"],
                "release_to_baseline": after["registers"] == before["registers"],
                "stable_read_inventory": _inventory(before) == _inventory(held) == _inventory(after),
                "held_ownership_reported": held["owned_request"] is True and held["uncertain_outcome"] is False,
                "released_idle": after["owned_request"] is False and after["uncertain_outcome"] is False,
                "cleanup_matches_baseline": clean is not None
                and _inventory(clean) == _inventory(before)
                and clean["registers"] == before["registers"]
                and clean["owned_request"] is False
                and clean["uncertain_outcome"] is False,
                "blcg_request_selector": all(
                    event["packet_before"][18] == 4 for event in events if event["kind"] == "request"
                ),
                "blcg_release_selector": all(
                    event["packet_before"][17] == 12 for event in events if event["kind"] == "release"
                ),
            }
        )
        report["qualified"] = all(report["checks"].values())
    else:
        report["checks"]["read_only_no_mutation_events"] = not events if not trial else None
    report["read_success"] = bool(report["before"] and all(value is True for value in report["checks"].values()))
    return report


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", required=True, help="Explicit PCI slot, e.g. 0000:01:00.0")
    parser.add_argument("--out", required=True, help="New JSON output in an existing directory")
    parser.add_argument("--trial", action="store_true", help="One native170 hold/read/release trial")
    args = parser.parse_args(argv)
    try:
        args.gpu = _slot(args.gpu.strip())
    except RuntimeError as exc:
        parser.error(str(exc))
    args.out = Path(args.out).expanduser().absolute()
    if not args.out.parent.is_dir() or args.out.exists():
        parser.error("--out must name a new file in an existing directory")
    return args


def main(argv=None):
    args = _parse_args(argv)
    descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:

        def checkpoint(report):
            encoded = json.dumps(util._jsonable(report), indent=2, sort_keys=True) + "\n"
            stream.seek(0)
            stream.write(encoded)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

        try:
            root = util._repo_root()
            sys.path.insert(0, str(root / "src"))
            from druta.block_clock_gating_live import BlockClockGatingController
            from druta.nvbackend import GPU

            report = collect(
                GPU(args.gpu),
                args.gpu,
                trial=args.trial,
                controller_factory=BlockClockGatingController,
                checkpoint=checkpoint,
                source=lambda: _source(root),
            )
        except BaseException as exc:
            report = {"schema": SCHEMA, "initialization_error": util._error(exc)}
        checkpoint(report)
    print(
        json.dumps(
            {"output": str(args.out), "trial_requested": args.trial, "qualified": report.get("qualified", False)}
        )
    )
    return 0 if report.get("qualified" if args.trial else "read_success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
