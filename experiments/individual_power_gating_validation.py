# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read individual gating requests; optionally validate one balanced session."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import struct
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "individual_inventory_helpers", Path(__file__).with_name("ampere_voltage_inventory_20261030.py")
)
util = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(util)
SCHEMA = "individual-power-gating-validation-v1"
INDICES = (0, 4, 10, 11, 12, 13, 14, 15)
SOURCE_FILES = ("src/druta/individual_power_gating_live.py", "src/druta/lpwr_runtime.py",
                "src/druta/perf_diagnostics.py", "src/druta/clock_bios_runtime.py",
                "src/druta/nvbackend.py", "experiments/individual_power_gating_validation.py",
                "experiments/ampere_voltage_inventory_20261030.py")


def _source(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _slot(value):
    if not isinstance(value, str) or not util.SLOT_RE.fullmatch(value):
        raise ValueError("Explicit PCI slot required")
    domain, bus, tail = value.lower().split(":")
    device, function = tail.split(".")
    if int(domain, 16) != 0 or int(device, 16) > 31:
        raise ValueError("PCI address outside understood range")
    return f"{int(domain, 16):04x}:{int(bus, 16):02x}:{int(device, 16):02x}.{int(function)}"


def _peer_read(controller, index):
    from druta import lpwr_runtime as lpwr

    result = {}
    for attribute in (0x39, 0x48, 0x49):
        request = lpwr._request(attribute, (index,))
        raw = lpwr._get(controller.reader, controller.identity, lpwr.ATTRIBUTE_COMMAND, lpwr.ATTRIBUTE_SIZE, request)
        available, value = lpwr._attributes(raw, attribute, (index,))[str(index)]
        result[str(attribute)] = {"available": available, "value": value}
        if attribute == 0x39 and (not available or not value):
            break
    return result


def _inventory(controller, peer_read):
    return {str(index): util._attempt(lambda index=index: peer_read(controller, index)) for index in range(22)}


def _peer_checks(before, held, after, cleanup, selected):
    peers = [str(index) for index in range(22) if index != selected]
    verified, changed = True, False
    for key in peers:
        values = []
        for snapshot in (before, held, after, cleanup):
            row = snapshot[key]
            if not row["ok"]:
                verified = False
                continue
            value = row["value"]
            support = value.get("57", {})
            if not support.get("available") or (support.get("value") and not all(
                value.get(str(attr), {}).get("available") for attr in (72, 73)
            )):
                verified = False
                continue
            values.append(value)
        if len(values) < 4:
            verified = False
        if values and any(value != values[0] for value in values[1:]):
            changed = True
    return {"status": "verified" if verified else "unverified", "unchanged": False if changed else True if verified else None}


def _exact_events(events, index):
    if len(events) != 2:
        return False
    for event, value in zip(events, (0, 1), strict=True):
        request = bytearray(0x404)
        struct.pack_into("<IHHHHII", request, 0, 1, 3, index, 0x37, 0, value, 0)
        response = bytearray(request)
        struct.pack_into("<H", response, 10, 1)
        if (event.get("status") != 0 or event.get("request_hex") != request.hex()
                or event.get("response_hex") != response.hex()):
            return False
    return True


def collect(gpu, slot, *, trial, index, controller_factory, source, peer_read=_peer_read, checkpoint=None):
    report = {"schema": SCHEMA, "slot": slot, "trial_requested": trial, "index": index,
              "events": [], "errors": [], "qualified": False, "read_success": False}
    controller = None
    original = gpu._legacy_clk_escape

    def observed(packet, fields):
        if len(packet) < 16 or packet[14] != 0x2080E802:
            return original(packet, fields)
        size = int(packet[15])
        event = {"phase": report.get("phase"), "header_before": list(packet[:17]),
                 "request_hex": ctypes.string_at(ctypes.addressof(packet) + 68, size).hex(),
                 "fields": util._jsonable(fields)}
        try:
            status = original(packet, fields)
            event["status"] = util._jsonable(status)
            return status
        except BaseException as exc:
            event["error"] = util._error(exc)
            raise
        finally:
            event["header_after"] = list(packet[:17])
            event["response_hex"] = ctypes.string_at(ctypes.addressof(packet) + 68, size).hex()
            report["events"].append(event)

    gpu._legacy_clk_escape = observed
    try:
        report["source_before"] = source()
        pci = util._pci_identity(gpu)
        report["pci_before"] = pci
        if pci["pairing_error"] or _slot(pci["requested_slot"]) != slot or _slot(pci["selected_slot"]) != slot:
            raise RuntimeError("Selected PCI identity does not match requested slot")
        selected = index if index is not None else 0
        controller = controller_factory(gpu, selected)
        if _slot(controller.identity["slot"]) != slot:
            raise RuntimeError("Controller identifies another PCI target")
        report["identity"] = util._jsonable(controller.identity)
        report["phase"] = "before"
        report["inventory_before"] = _inventory(controller, peer_read)
        report["selected_read"] = util._attempt(controller.read)
        if trial:
            before = controller.read()
            report["before"] = before
            if not before["support"] or before["reason_masks"][0x48] & 1:
                raise RuntimeError("Selected controller unsupported or reason0 already held")
            if checkpoint:
                checkpoint(report)
            report["phase"] = "hold"
            report["held"] = controller.hold()
            report["inventory_held"] = _inventory(controller, peer_read)
            if checkpoint:
                checkpoint(report)
            report["phase"] = "release"
            report["after"] = controller.release()
            report["inventory_after"] = _inventory(controller, peer_read)
    except BaseException as exc:
        report["errors"].append({"phase": report.get("phase", "initialization"), **util._error(exc)})
    finally:
        if controller is not None:
            report["cleanup"] = util._attempt(controller.close)
            report["after_cleanup"] = util._attempt(controller.read) if trial else None
            if trial:
                report["inventory_after_cleanup"] = _inventory(controller, peer_read)
        report["pci_after"] = util._attempt(lambda: util._pci_identity(gpu))
        report["source_after"] = util._attempt(source)
        gpu._legacy_clk_escape = original
    common = (not report["errors"] and report.get("pci_after", {}).get("ok")
              and report["pci_after"]["value"] == report.get("pci_before")
              and report.get("source_after", {}).get("ok")
              and report["source_after"]["value"] == report.get("source_before")
              and report.get("cleanup", {}).get("ok"))
    report["read_success"] = bool(common and any(row["ok"] for row in report.get("inventory_before", {}).values())
                                  and (index is None or report.get("selected_read", {}).get("ok")))
    if trial and all(key in report for key in ("before", "held", "after", "inventory_held", "inventory_after")):
        before, held, after = (report[key] for key in ("before", "held", "after"))
        expected = dict(before["reason_masks"])
        expected[0x48] |= 1
        report["peer_check"] = _peer_checks(report["inventory_before"], report["inventory_held"],
                                            report["inventory_after"], report["inventory_after_cleanup"], index)
        report["qualified"] = bool(common and _exact_events(report["events"], index) and held["owned_request"]
                                   and held["reason_masks"] == expected and not after["owned_request"]
                                   and after["reason_masks"] == before["reason_masks"]
                                   and report["after_cleanup"]["ok"]
                                   and report["after_cleanup"]["value"]["reason_masks"]
                                   == {str(key): value for key, value in before["reason_masks"].items()}
                                   and report["peer_check"]["unchanged"] is not False)
    return report


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--trial", action="store_true")
    parser.add_argument("--controller", type=int, choices=INDICES)
    args = parser.parse_args(argv)
    if args.trial and args.controller is None:
        parser.error("--trial requires --controller")
    try:
        args.gpu = _slot(args.gpu.strip())
    except ValueError as exc:
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
            stream.seek(0)
            json.dump(util._jsonable(report), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

        try:
            root = util._repo_root()
            sys.path.insert(0, str(root / "src"))
            from druta.individual_power_gating_live import IndividualPowerGatingController
            from druta.nvbackend import GPU

            report = collect(GPU(args.gpu), args.gpu, trial=args.trial, index=args.controller,
                             controller_factory=IndividualPowerGatingController,
                             source=lambda: _source(root), checkpoint=checkpoint)
        except BaseException as exc:
            report = {"schema": SCHEMA, "initialization_error": util._error(exc)}
        checkpoint(report)
    return 0 if report.get("qualified" if args.trial else "read_success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
