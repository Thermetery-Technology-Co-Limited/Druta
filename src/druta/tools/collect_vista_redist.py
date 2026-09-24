"""Collect the exact SDK 10240 UCRT set proven to load on Vista SP2 x64.

The newer SDK 19041 set remains the Windows 7 recipe, but fails to load on
Vista. Copy the entire matched 41-DLL set; do not mix forwarder generations.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

try:
    from . import collect_win7_redist as common
except ImportError:
    import collect_win7_redist as common


UCRT_VERSION = (10, 0, 10240, 16384)
TARGET = "windows-vista-sp2-x64"
UCRT_NAMES = common.UCRT_NAMES - {"api-ms-win-core-console-l1-2-0.dll"}
PIN_FILE = Path(__file__).resolve().parent / "vista/ucrt-10240.json"


def pinned_hashes():
    pin = json.loads(PIN_FILE.read_text(encoding="utf-8"))
    if tuple(pin.get("version", [])) != UCRT_VERSION or set(pin.get("sha256", {})) != UCRT_NAMES:
        raise ValueError("Incomplete SDK 10240 identity record")
    return pin["sha256"]


def validate(directory):
    """Spec-time check: manifest claims cannot substitute a newer UCRT set."""
    directory = Path(directory).resolve(strict=True)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    expected = dict(pinned_hashes(), **{common.D3D_NAME: common.D3D_SHA256})
    entries = manifest.get("files", [])
    if (manifest.get("format") != 1 or manifest.get("target") != TARGET or
            len(entries) != len(expected) or
            {entry.get("name") for entry in entries} != set(expected)):
        raise ValueError("Vista requires its complete 41-DLL SDK 10240 UCRT set and shader compiler")
    for entry in entries:
        name = entry["name"]
        path = (directory / name).resolve(strict=True)
        if path.parent != directory:
            raise ValueError("Vista runtime path escapes its directory")
        version = common.D3D_VERSION if name == common.D3D_NAME else UCRT_VERSION
        if (entry.get("version") != list(version) or entry.get("sha256") != expected[name] or
                hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]):
            raise ValueError("Vista runtime identity mismatch: " + name)
    return manifest


def collect(ucrt_directory, d3d_compiler, sdk_license_directories, output):
    manifest = common.collect(ucrt_directory, d3d_compiler, sdk_license_directories, output,
                              ucrt_version=UCRT_VERSION, ucrt_names=UCRT_NAMES,
                              ucrt_hashes=pinned_hashes(), target=TARGET)
    validate(output)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ucrt-directory", required=True, type=Path)
    parser.add_argument("--d3d-compiler", required=True, type=Path)
    parser.add_argument("--sdk-license-directory", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = collect(args.ucrt_directory, args.d3d_compiler, args.sdk_license_directory, args.output)
    except (OSError, ValueError) as error:
        print("Vista runtime collection failed: " + str(error), file=sys.stderr)
        return 1
    print("Collected {} DLLs and {} license/notice files in {}".format(
        len(manifest["files"]), len(manifest["licenses"]), args.output.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
