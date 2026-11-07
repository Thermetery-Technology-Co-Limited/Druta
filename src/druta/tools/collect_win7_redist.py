"""Collect the tested app-local Windows 7 runtimes from explicit SDK sources.

Build-time helper only: requires pefile, performs no downloads or installation,
and never copies DLLs from Windows system directories. End users do not run it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import pefile


UCRT_VERSION = (10, 0, 19041, 5609)
D3D_VERSION = (10, 0, 28000, 2705)
D3D_NAME = "d3dcompiler_47.dll"
D3D_SHA256 = "3e56c1a0867aa04cbca948b3664fcac5d51275865cd044a45a3fd6ce636ee914"
UCRT_NAMES = frozenset("""
api-ms-win-core-console-l1-1-0.dll
api-ms-win-core-console-l1-2-0.dll
api-ms-win-core-datetime-l1-1-0.dll
api-ms-win-core-debug-l1-1-0.dll
api-ms-win-core-errorhandling-l1-1-0.dll
api-ms-win-core-file-l1-1-0.dll
api-ms-win-core-file-l1-2-0.dll
api-ms-win-core-file-l2-1-0.dll
api-ms-win-core-handle-l1-1-0.dll
api-ms-win-core-heap-l1-1-0.dll
api-ms-win-core-interlocked-l1-1-0.dll
api-ms-win-core-libraryloader-l1-1-0.dll
api-ms-win-core-localization-l1-2-0.dll
api-ms-win-core-memory-l1-1-0.dll
api-ms-win-core-namedpipe-l1-1-0.dll
api-ms-win-core-processenvironment-l1-1-0.dll
api-ms-win-core-processthreads-l1-1-1.dll
api-ms-win-core-processthreads-l1-1-0.dll
api-ms-win-core-profile-l1-1-0.dll
api-ms-win-core-rtlsupport-l1-1-0.dll
api-ms-win-core-string-l1-1-0.dll
api-ms-win-core-synch-l1-1-0.dll
api-ms-win-core-synch-l1-2-0.dll
api-ms-win-core-sysinfo-l1-1-0.dll
api-ms-win-core-timezone-l1-1-0.dll
api-ms-win-core-util-l1-1-0.dll
api-ms-win-crt-conio-l1-1-0.dll
api-ms-win-crt-convert-l1-1-0.dll
api-ms-win-crt-environment-l1-1-0.dll
api-ms-win-crt-filesystem-l1-1-0.dll
api-ms-win-crt-heap-l1-1-0.dll
api-ms-win-crt-locale-l1-1-0.dll
api-ms-win-crt-math-l1-1-0.dll
api-ms-win-crt-multibyte-l1-1-0.dll
api-ms-win-crt-private-l1-1-0.dll
api-ms-win-crt-process-l1-1-0.dll
api-ms-win-crt-runtime-l1-1-0.dll
api-ms-win-crt-stdio-l1-1-0.dll
api-ms-win-crt-string-l1-1-0.dll
api-ms-win-crt-time-l1-1-0.dll
api-ms-win-crt-utility-l1-1-0.dll
ucrtbase.dll
""".split())


def _within(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _non_system_path(value, must_exist=True):
    path = Path(value).expanduser().resolve(strict=must_exist)
    if any(part.lower() in ("system32", "syswow64", "winsxs")
           for part in path.parts):
        raise ValueError("Use an SDK redist directory, not a system path: " + str(path))
    for variable in ("SystemRoot", "windir"):
        if os.environ.get(variable) and _within(path, Path(os.environ[variable]).resolve()):
            raise ValueError("Windows system paths are not permitted: " + str(path))
    return path


def _dll_metadata(path, expected_version):
    # Read once: the bytes hashed, inspected and later copied are identical.
    data = path.read_bytes()
    try:
        pe = pefile.PE(data=data)
        try:
            if pe.FILE_HEADER.Machine != 0x8664:
                raise ValueError("Expected an x64 DLL: " + str(path))
            if not pe.FILE_HEADER.Characteristics & 0x2000:
                raise ValueError("Expected a DLL, not an executable: " + str(path))
            versions = getattr(pe, "VS_FIXEDFILEINFO", [])
            if not versions:
                raise ValueError("DLL has no fixed file version: " + str(path))
            info = versions[0]
            version = (info.FileVersionMS >> 16, info.FileVersionMS & 0xffff,
                       info.FileVersionLS >> 16, info.FileVersionLS & 0xffff)
        finally:
            pe.close()
    except pefile.PEFormatError as error:
        raise ValueError("Invalid PE DLL {}: {}".format(path, error))
    if version != expected_version:
        raise ValueError("Untested DLL version {} in {}; expected {}".format(
            ".".join(map(str, version)), path, ".".join(map(str, expected_version))))
    return data, {"name": path.name.lower(),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "version": list(version), "source": str(path)}


def collect(ucrt_directory, d3d_compiler, sdk_license_directories, output):
    ucrt = _non_system_path(ucrt_directory)
    compiler = _non_system_path(d3d_compiler)
    licenses = [_non_system_path(path) for path in sdk_license_directories]
    destination = _non_system_path(output, must_exist=False)
    if not ucrt.is_dir():
        raise ValueError("UCRT source must be a directory: " + str(ucrt))
    if not compiler.is_file() or compiler.name.lower() != D3D_NAME:
        raise ValueError("Shader compiler source must be named " + D3D_NAME)
    if any(_within(destination, source) for source in [ucrt, compiler.parent] + licenses):
        raise ValueError("Output must be separate from the SDK source directories")

    dlls = [path for path in ucrt.iterdir() if path.suffix.lower() == ".dll"]
    found = {path.name.lower() for path in dlls}
    if found != UCRT_NAMES or len(dlls) != len(UCRT_NAMES):
        raise ValueError("Expected the complete 42-DLL SDK 19041 x64 UCRT set. "
                         "Missing: {}; unexpected: {}".format(
                             ", ".join(sorted(UCRT_NAMES - found)) or "none",
                             ", ".join(sorted(found - UCRT_NAMES)) or "none"))

    manifest = {"format": 1, "files": [], "licenses": []}
    payload = {}
    for path in sorted(dlls, key=lambda item: item.name.lower()):
        source = _non_system_path(path)
        if source.parent != ucrt:
            raise ValueError("All UCRT DLLs must come from one SDK directory: " + str(path))
        data, record = _dll_metadata(source, UCRT_VERSION)
        manifest["files"].append(record)
        payload[record["name"]] = data

    data, record = _dll_metadata(compiler, D3D_VERSION)
    if record["sha256"] != D3D_SHA256:
        raise ValueError("D3DCompiler_47 SHA256 differs from the tested Microsoft SDK redist")
    manifest["files"].append(record)
    payload[record["name"]] = data

    if not licenses:
        raise ValueError("At least one SDK license directory is required")
    for directory in sorted(set(licenses)):
        if not directory.is_dir():
            raise ValueError("SDK license source must be a directory: " + str(directory))
        selected = [path for path in sorted(directory.rglob("*")) if path.is_file()
                    and any(word in path.name.lower() for word in ("license", "notice", "redist"))]
        if not any(path.name.lower() == "sdk_license.rtf" for path in selected):
            raise ValueError("SDK license directory must contain sdk_license.rtf: " + str(directory))
        for path in selected:
            source = _non_system_path(path)
            if not _within(source, directory):
                raise ValueError("SDK license source points outside its directory: " + str(path))
            name = (Path("licenses/Microsoft-Windows-SDK") / directory.name
                    / path.relative_to(directory)).as_posix()
            if name in payload:
                raise ValueError("SDK license directories have colliding names: " + name)
            data = source.read_bytes()
            payload[name] = data
            manifest["licenses"].append({"name": name,
                                         "sha256": hashlib.sha256(data).hexdigest(),
                                         "source": str(source)})

    payload["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("Output is not a directory: " + str(destination))
        entries = list(destination.iterdir())
        if entries:
            existing = {path.relative_to(destination).as_posix(): path
                        for path in destination.rglob("*") if path.is_file()}
            expected_tree = set(payload)
            for name in payload:
                expected_tree.update(parent.as_posix() for parent in Path(name).parents
                                     if parent != Path("."))
            # Only an identical completed collection is reusable. Never replace
            # existing files, partial collections or unrelated output content.
            if ({path.relative_to(destination).as_posix()
                 for path in destination.rglob("*")} == expected_tree
                    and set(existing) == set(payload)
                    and all(not path.is_symlink() and path.read_bytes() == payload[name]
                            for name, path in existing.items())
                    and all(not path.is_symlink() for path in destination.rglob("*"))):
                return manifest
            raise ValueError("Output is not an identical existing collection; "
                             "choose a new or empty output directory: " + str(destination))

    # Validate every source before creating output. Write the completion manifest
    # last, so an interrupted copy cannot be mistaken for a complete collection.
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in payload.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ucrt-directory", required=True, type=Path)
    parser.add_argument("--d3d-compiler", required=True, type=Path)
    parser.add_argument("--sdk-license-directory", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = collect(args.ucrt_directory, args.d3d_compiler,
                           args.sdk_license_directory, args.output)
    except (OSError, ValueError) as error:
        print("Runtime collection failed: " + str(error), file=sys.stderr)
        return 1
    print("Collected {} DLLs and {} license/notice files in {}".format(
        len(manifest["files"]), len(manifest["licenses"]), args.output.resolve()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
