"""Copy an explicitly supplied, hash-verified nvtune package into the Vista ZIP.

Build-time only. This does not run nvtune, install a driver, trust a certificate,
or change Windows test-signing settings.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

import pefile


MANIFEST = "licenses/nvtune/manifest.json"
PAYLOAD_FILES = frozenset((
    "nvtune.exe", "nvtune-driver/nvtunedrv.sys",
    "nvtune-driver/nvtunedrv-cert.cer", "nvtune-driver/install-on-target.cmd",
    "nvtune-driver/README.txt", "licenses/nvtune/COPYING", "licenses/nvtune/source.zip",
))


def collect(source, bundle):
    source = Path(source).resolve(strict=True)
    bundle = Path(bundle).resolve(strict=True)
    if not (bundle / "Druta.exe").is_file():
        raise ValueError("Destination must be the newly built Druta bundle")
    manifest_data = (source / MANIFEST).read_bytes()
    manifest = json.loads(manifest_data.decode("utf-8"))
    entries = manifest.get("files", [])
    if (manifest.get("format") != 1 or len(entries) != len(PAYLOAD_FILES) or
            {entry["name"] for entry in entries} != PAYLOAD_FILES):
        raise ValueError("Incomplete or unsupported nvtune package manifest")
    provenance = manifest.get("source", {})
    if (provenance.get("repository") != "https://github.com/sebastianmarrufo/nvtune" or
            not re.fullmatch(r"[0-9a-f]{40}", provenance.get("revision", "")) or
            provenance.get("archive") != "licenses/nvtune/source.zip"):
        raise ValueError("Missing exact nvtune source provenance")
    payload = {}
    for entry in entries:
        path = (source / entry["name"]).resolve(strict=True)
        if source not in path.parents:
            raise ValueError("nvtune payload path escapes its directory")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("nvtune payload hash mismatch: " + entry["name"])
        payload[entry["name"]] = data
    for name in ("nvtune.exe", "nvtune-driver/nvtunedrv.sys"):
        with pefile.PE(data=payload[name]) as pe:
            if pe.FILE_HEADER.Machine != 0x8664 or (
                    pe.OPTIONAL_HEADER.MajorSubsystemVersion,
                    pe.OPTIONAL_HEADER.MinorSubsystemVersion) > (6, 0):
                raise ValueError("nvtune package requires Vista x64 native binaries")
    with zipfile.ZipFile(str(source / provenance["archive"])) as archive:
        names = archive.namelist()
        if not names or archive.testzip() is not None or not any(Path(name).name in ("COPYING", "LICENSE") for name in names):
            raise ValueError("nvtune source archive is incomplete or corrupt")
        if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name for name in names):
            raise ValueError("Unsafe path in nvtune source archive")
    payload[MANIFEST] = manifest_data
    # Preflight every destination before copying any file. A repeated identical
    # collection is safe; unrelated output must never be overwritten.
    for name, data in payload.items():
        destination = (bundle / name).resolve()
        if bundle not in destination.parents:
            raise ValueError("nvtune output path escapes the Druta bundle")
        if destination.exists() and (not destination.is_file() or destination.read_bytes() != data):
            raise ValueError("Existing different nvtune bundle file: " + name)
    for name, data in payload.items():
        destination = bundle / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.source, args.bundle), indent=2, sort_keys=True))
