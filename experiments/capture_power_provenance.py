# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Private, read-only power provenance capture on a selected adapter.

Run from a Druta checkout: python this_script.py --slot PCI_SLOT --out NEW_DIRECTORY.
No power SET, BIOS write, registry write, restart, or workload is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rows(image, inspection):
    result = []
    for table in inspection.get("tables", []):
        if table.get("name") != "power":
            continue
        offset, header, stride, count = (table[k] for k in ("offset", "header_size", "stride", "count"))
        if min(offset, header, stride, count) < 0 or offset + header + stride * count > len(image):
            raise ValueError("Power table exceeds captured image")
        for index in range(count):
            start = offset + header + index * stride
            raw = image[start : start + stride]
            version = table["version"]
            if version == 0x30 and stride >= 0x43:
                full_class = (raw[0] & 15) | ((raw[0x42] & 15) << 4)
            elif version == 0x40:
                full_class = raw[0]
            else:
                full_class = None
            result.append(
                {
                    "index": index,
                    "full_encoded_class": full_class,
                    "first_byte": raw[0],
                    "extended_class_byte": raw[0x42] if version == 0x30 and stride >= 0x43 else None,
                    "row_hex": raw.hex(),
                }
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True, help="PCI slot accepted by Druta GPU")
    parser.add_argument("--out", required=True, type=Path, help="New private output directory")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Druta checkout root (defaults to the script's parent checkout)",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not (repo / "src" / "druta").is_dir():
        parser.error("--repo must contain src/druta")
    out = args.out.resolve()
    if out.exists() or not out.parent.is_dir():
        parser.error("--out must name a new directory with an existing parent")
    out.mkdir()
    report = {
        "scope": "read-only production getters; no SET/restart/registry write/workload",
        "state": "incomplete",
        "errors": {},
    }
    try:
        sys.path.insert(0, str(repo / "src"))
        from druta import client_power_runtime as client
        from druta import power_range_baseline as baseline
        from druta import registry_overrides as overrides
        from druta import vbios_fields
        from druta.nvbackend import GPU

        sources = (
            "client_power_runtime.py",
            "power_range_baseline.py",
            "registry_overrides.py",
            "vbios_fields.py",
            "nvbackend.py",
        )
        report["source_sha256"] = {name: sha((repo / "src" / "druta" / name).read_bytes()) for name in sources}
        report["source_sha256"]["capture_power_provenance.py"] = sha(Path(__file__).read_bytes())
        try:
            report["source_commit"] = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            report["errors"]["source_commit"] = str(exc)
        gpu = GPU(args.slot)
        target = overrides.bind_target(gpu)
        report["target"] = asdict(target)
        power = client.ClientPowerController(gpu)
        metadata = power.read_metadata()
        write_json(out / "metadata.json", metadata)
        report["identity"] = metadata["identity"]
        report["binding"] = metadata["binding"]
        for name in ("info_hex", "role_info_hex", "native_info_hex"):
            raw = bytes.fromhex(metadata[name])
            (out / (name.removesuffix("_hex") + ".bin")).write_bytes(raw)
            report.setdefault("packet_sizes", {})[name] = len(raw)
        try:
            complete = power.read()
            write_json(out / "complete.json", complete)
            for name in ("info_hex", "control_hex", "role_info_hex", "native_info_hex", "native_control_hex"):
                raw = bytes.fromhex(complete[name])
                (out / ("complete-" + name.removesuffix("_hex") + ".bin")).write_bytes(raw)
                report["packet_sizes"]["complete_" + name] = len(raw)
            report["complete_identity_matches_metadata"] = complete["identity"] == metadata["identity"]
            report["complete_binding_matches_metadata"] = complete["binding"] == metadata["binding"]
        except Exception as exc:
            report["errors"]["complete_read"] = str(exc)
        registry = overrides.WindowsRegistry(target).snapshot()
        write_json(out / "registry-private.json", registry)
        image = overrides.read_vbios(gpu)
        (out / "live-image-private.rom").write_bytes(image)
        inspect = vbios_fields.inspect(image)
        write_json(out / "inspection.json", inspect)
        write_json(out / "power-rows.json", rows(image, inspect))
        report["live_image_sha256"] = sha(image)
        report["bios_identity"] = inspect.get("identity")
        report["power_tables"] = [t for t in inspect.get("tables", []) if t.get("name") == "power"]
        try:
            original, source = baseline._original(target, registry, image)
            report["provenance"] = {"source": source, "original_image_sha256": sha(original)}
            original_inspection = vbios_fields.inspect(original)
            (out / "original-image-private.rom").write_bytes(original)
            write_json(out / "original-inspection.json", original_inspection)
            write_json(out / "original-power-rows.json", rows(original, original_inspection))
        except Exception as exc:
            report["provenance"] = {"source": "unavailable", "reason": str(exc)}
        report["product_original_range"] = baseline.read(gpu, metadata)
        if overrides.bind_target(gpu) != target:
            raise RuntimeError("Selected adapter changed during capture")
        report["state"] = "complete"
    except Exception as exc:
        report["errors"]["capture"] = str(exc)
    write_json(out / "result.json", report)
    print(
        json.dumps(
            {
                "state": report["state"],
                "result": str(out / "result.json"),
                "product_original_range": report.get("product_original_range"),
                "errors": report["errors"],
            },
            indent=2,
        )
    )
    if report["state"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
