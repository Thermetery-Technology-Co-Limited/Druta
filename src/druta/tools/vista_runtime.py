"""Assemble and verify the separately source-built Windows Vista runtime.

Build-time only. No downloads, installation, or changes to the input Python.
The native builds and their small source patches are documented in VISTA.md.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pefile


SOURCES = {
    "cpython": "3d8993a744813c5144851da5347d7b4b1885f234",
    "pyinstaller": "7f2ae63f703ae27955722eac4891678b546d513a",
    "dearpygui": "719d068967ace64eb9ff4171f2052a0799fd6c35",
}
RUNTIME_FILES = (
    "python38.dll",
    "Lib/site-packages/dearpygui/_dearpygui.pyd",
    "Lib/site-packages/PyInstaller/bootloader/Windows-64bit-intel/run.exe",
    "Lib/site-packages/PyInstaller/bootloader/Windows-64bit-intel/runw.exe",
)
MANIFEST = "druta-vista-runtime.json"
PATCHES = Path(__file__).resolve().parent / "vista"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_native(path):
    """Reject the known load-time blockers, not just a misleading PE minimum."""
    with pefile.PE(str(path)) as pe:
        if pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError("Expected x64 PE: " + str(path))
        if path.suffix.lower() == ".exe" and (
                pe.OPTIONAL_HEADER.MajorSubsystemVersion,
                pe.OPTIONAL_HEADER.MinorSubsystemVersion) > (6, 0):
            raise ValueError("Executable requires a newer Windows loader: " + str(path))
        imports = [(entry.dll.decode("ascii"), item.name.decode("ascii"))
                   for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
                   for item in entry.imports if item.name]
        for library, function in imports:
            if library.lower() == "kernel32.dll" and (
                    function.startswith("K32") or function == "GetActiveProcessorCount"):
                raise ValueError("Vista cannot load {}!{} in {}".format(library, function, path))
        return imports


def validate_runtime(directory):
    directory = Path(directory).resolve(strict=True)
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("sources") != SOURCES:
        raise ValueError("Unsupported Vista runtime provenance")
    if set(manifest.get("files", {})) != set(RUNTIME_FILES):
        raise ValueError("Incomplete Vista runtime manifest")
    for name, expected in manifest["files"].items():
        path = directory / name
        if sha256(path) != expected:
            raise ValueError("Vista runtime hash mismatch: " + name)
        inspect_native(path)
    expected_patches = {name + "-vista.patch": sha256(PATCHES / (name + "-vista.patch"))
                        for name in SOURCES}
    if manifest.get("patches") != expected_patches:
        raise ValueError("Vista runtime was assembled with different source patches")
    return manifest


def assemble(python_directory, source_directories, extension, output):
    python_directory = Path(python_directory).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Use a new output directory; existing Python environments are never overwritten")
    if output == python_directory or python_directory in output.parents:
        raise ValueError("Output must be outside the input Python environment")
    provenance = {}
    for name, source in source_directories.items():
        source = Path(source).resolve(strict=True)
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if revision != SOURCES[name]:
            raise ValueError("Unexpected {} revision: {}".format(name, revision))
        # Require precisely the published patch, including changes in nested
        # third-party files. This also catches dirty/unrelated source edits.
        diff_command = ["git", "-C", str(source), "diff", "--binary", "--", "."]
        if name == "pyinstaller":
            # Upstream tracks its prebuilt bootloaders; waf intentionally
            # replaces these four output binaries while building our sources.
            diff_command += [":(exclude)PyInstaller/bootloader/Windows-64bit-intel/" + filename
                             for filename in ("run.exe", "runw.exe", "run_d.exe", "runw_d.exe")]
        actual_diff = subprocess.check_output(diff_command)
        expected_diff = (PATCHES / (name + "-vista.patch")).read_bytes()
        if actual_diff.replace(b"\r\n", b"\n") != expected_diff.replace(b"\r\n", b"\n"):
            raise ValueError("Source differs from published Vista patch: " + name)
        provenance[name] = source
    binary_sources = {
        RUNTIME_FILES[0]: provenance["cpython"] / "PCbuild/amd64/python38.dll",
        RUNTIME_FILES[1]: Path(extension).resolve(strict=True),
    }
    for name in RUNTIME_FILES[2:]:
        binary_sources[name] = provenance["pyinstaller"] / Path(name).relative_to("Lib/site-packages")
    for path in binary_sources.values():
        inspect_native(path)
    baseline = subprocess.check_output([str(python_directory / "python.exe"), "-c",
        "import sys; print('.'.join(map(str, sys.version_info[:3]))); print(sys.maxsize > 2**32)"], text=True)
    if baseline.splitlines() != ["3.8.10", "True"]:
        raise ValueError("Input must be the pinned CPython 3.8.10 x64 build environment")
    shutil.copytree(str(python_directory), str(output), ignore=shutil.ignore_patterns("__pycache__"))
    for name, source in binary_sources.items():
        shutil.copy2(str(source), str(output / name))
    manifest = {
        "format": 1, "sources": SOURCES,
        "files": {name: sha256(output / name) for name in RUNTIME_FILES},
        "patches": {name + "-vista.patch": sha256(PATCHES / (name + "-vista.patch"))
                    for name in SOURCES},
    }
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validate_runtime(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="Validate an already assembled runtime")
    parser.add_argument("--python-directory", type=Path)
    for name in SOURCES:
        parser.add_argument("--" + name + "-source", type=Path)
    parser.add_argument("--dearpygui-extension", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = validate_runtime(args.verify)
    else:
        directories = {name: getattr(args, name + "_source") for name in SOURCES}
        if not all(directories.values()) or not all((args.python_directory, args.dearpygui_extension, args.output)):
            parser.error("Assembly requires all source directories, the built extension, input Python and a new output directory")
        result = assemble(args.python_directory, directories, args.dearpygui_extension, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
