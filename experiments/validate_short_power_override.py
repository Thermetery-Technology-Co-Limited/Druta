# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded short-v30 power override trial. Default is read-only; --run applies and undoes once."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from druta import client_power_runtime as client  # noqa: E402
from druta import registry_overrides as ro  # noqa: E402
from druta import vbios_fields  # noqa: E402
from druta.nvbackend import GPU  # noqa: E402


def project_image(image, inspection, binding):
    tables = [t for t in inspection.get("tables", ()) if t.get("name") == "power"]
    if len(tables) != 1:
        raise ValueError("Expected one power table")
    table = tables[0]
    if (table["version"], table["header_size"], table["stride"]) != (0x30, 0x2C, 0x47):
        raise ValueError("Unsupported power geometry")
    index = binding["native_index"]
    offset, count = table["offset"], table["count"]
    if type(index) is not int or not 0 <= index < count or offset < 0 or offset + 0x2C + count * 0x47 > len(image):
        raise ValueError("Power table extent or ordinal is invalid")
    row = offset + 0x2C + index * 0x47
    kind = (image[row] & 15) | ((image[row + 0x42] & 15) << 4)
    bounds = struct.unpack_from("<III", image, row + 2)
    if (kind != 1 or image[row] & 0x10 or binding["units"] != "mW"
            or not bounds[0] <= bounds[1] <= bounds[2] <= 0x7fffffff - 1000
            or bounds != (binding["minimum_mw"], binding["rated_mw"], binding["maximum_mw"])):
        raise ValueError("BIOS class, units, or bounds differ from role1")
    expected = bytearray(image)
    struct.pack_into("<I", expected, row + 10, bounds[2] + 1000)
    return bytes(expected), row + 10


def project_client(before):
    result = copy.deepcopy(client._state(before))
    binding = result["binding"]
    legacy = binding.get("layout") == "legacy32"
    ci, ni = binding["client_index"], binding["native_index"]
    if type(ci) is not int or type(ni) is not int or not 0 <= ci < (32 if legacy else 64) or not 0 <= ni < 32:
        raise ValueError("Invalid client/native ordinal")
    maximum = binding["maximum_mw"]
    if type(maximum) is not int or not 0 <= maximum <= 0xffffffff - 1000:
        raise ValueError("Invalid maximum")
    for name, offset in (("info_hex", (8 if legacy else client.INFO_BASE) + ci * client.INFO_STRIDE + 12),
                         ("role_info_hex", 4 + 16),
                         ("native_info_hex", (88 if legacy else client.NATIVE_INFO_BASE)
                          + ni * (228 if legacy else client.NATIVE_INFO_STRIDE) + 16)):
        raw = bytearray.fromhex(result[name])
        if offset + 4 > len(raw) or struct.unpack_from("<I", raw, offset)[0] != maximum:
            raise ValueError("Packet maximum differs from binding")
        struct.pack_into("<I", raw, offset, maximum + 1000)
        result[name] = raw.hex()
    binding["maximum_mw"] += 1000
    return result


def serial(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists() or not out.parent.is_dir():
        parser.error("--out must be a new directory with an existing parent")
    out.mkdir()
    report = {"state": "capturing", "attempts": [], "errors": [], "run": args.run,
              "scope": "Complete client state and ROM projection; broader snapshot peer equality is not asserted."}

    def save():
        (out / "journal.json").write_text(json.dumps(report, default=serial, indent=2) + "\n", encoding="utf-8")

    def capture(label):
        item = {"phase": label}
        report["attempts"].append(item)
        try:
            item["snapshot"] = ro.fresh_snapshot(args.slot)
            item["client"] = client.ClientPowerController(GPU(args.slot)).read()
            return item
        except BaseException:
            item["traceback"] = traceback.format_exc()
            raise
        finally:
            save()

    def verify_capture(label, image_expected, client_expected):
        for attempt in range(8):
            try:
                item = capture(label)
                if (item["snapshot"]["target"] != target or item["snapshot"]["image"] != image_expected
                        or client._state(item["client"]) != client_expected):
                    raise RuntimeError("ROM or complete client state differs from projection")
                return
            except Exception:
                report["attempts"][-1]["verification_traceback"] = traceback.format_exc()
                save()
                if attempt == 7:
                    raise
                time.sleep(0.5)

    receipt_path = None
    try:
        sources = [Path(__file__), ROOT / "src/druta/vbios_fields.py", ROOT / "src/druta/power_runtime.py",
                   ROOT / "src/druta/client_power_runtime.py", ROOT / "src/druta/registry_overrides.py"]
        report["source_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in sources}
        report["source_commit"] = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                                  capture_output=True, text=True, check=True).stdout.strip()
        before = capture("before")
        snap, observed = before["snapshot"], before["client"]
        target, image = snap["target"], snap["image"]
        if image is None or ro.bind_target(GPU(args.slot)) != target or observed["identity"]["slot"] != target.slot:
            raise RuntimeError("Target or image differs")
        registry = ro.WindowsRegistry(target).snapshot()
        if registry or ro.active_receipt(target):
            raise RuntimeError("Trial requires empty registry and no active receipt")
        binding = observed["binding"]
        inspection = vbios_fields.inspect(image)
        expected_image, max_offset = project_image(image, inspection, binding)
        index = binding["native_index"]
        fields = {f["key"]: f for f in inspection["fields"]}
        field = fields[f"power.{index}.maximum"]
        if (field["runtime_binding"]["runtime_type"] != 0 or field["channel"] !=
                bytes.fromhex(observed["role_info_hex"])[9]):
            raise RuntimeError("BIOS/native class or channel differs")
        changes = {f"power.{index}.maximum": binding["maximum_mw"] + 1000}
        contracts = ro.policy_contracts(GPU(args.slot), inspection, changes)
        plan = vbios_fields.plan(image, changes)
        planned = bytearray(image)
        for patch in plan["patches"]:
            at = patch["offset"]
            raw = bytes.fromhex(patch["before"])
            if planned[at:at + len(raw)] != raw:
                raise RuntimeError("Plan preimage mismatch")
            planned[at:at + len(raw)] = bytes.fromhex(patch["after"])
        if bytes(planned) != expected_image:
            raise RuntimeError("Plan differs from independent maximum-only projection")
        expected_client = project_client(observed)
        report.update(target=target, registry=registry, changes=changes, policy_contracts=contracts, max_offset=max_offset,
                      baseline_sha256=hashlib.sha256(image).hexdigest(), plan=plan, state="prepared")
        save()
        if args.run:
            receipt = ro.prepare(GPU(args.slot), changes)
            receipt_path = receipt["path"]
            report["receipt_path"] = receipt_path
            durable = ro.load_receipt(receipt_path)
            if (durable != receipt or durable["target"] != dataclasses.asdict(target) or durable["before"] != registry
                    or durable["plan"] != plan or durable["policy_contracts"] != contracts):
                raise RuntimeError("Durable receipt differs from independent preflight")
            report["state"] = "applying"
            save()
            report["apply"] = ro.execute(durable)
            save()
            if not report["apply"]["ok"] or report["apply"].get("runtime_verified_fields") != list(changes):
                raise RuntimeError("Apply failed")
            verify_capture("active", expected_image, expected_client)
            report["state"] = "active-verified"
        else:
            report["state"] = "read-only-complete"
    except BaseException:
        report["errors"].append(traceback.format_exc())
        if receipt_path is None:
            report["state"] = "failed"
    finally:
        if receipt_path is not None:
            try:
                durable = ro.load_receipt(receipt_path)
                active = ro.active_receipt(target)
                if active is not None:
                    if Path(active["path"]).resolve() != Path(receipt_path).resolve():
                        raise RuntimeError("Active receipt belongs to another transaction")
                    report["undo"] = ro.execute(durable, restore=True)
                    save()
                    if not report["undo"]["ok"] or report["undo"].get("runtime_verified_fields") != list(changes):
                        raise RuntimeError("Undo failed; retain recovery receipt")
                verify_capture("restored", image, client._state(observed))
                if ro.active_receipt(target) is not None or ro.WindowsRegistry(target).snapshot() != registry:
                    raise RuntimeError("Restored state differs from baseline")
                report["state"] = "restored"
            except BaseException:
                report["errors"].append(traceback.format_exc())
                report["state"] = "recovery-required"
        save()
    print(json.dumps({"state": report["state"], "journal": str(out / "journal.json"), "errors": len(report["errors"])}))
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
