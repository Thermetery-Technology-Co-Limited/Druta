# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build-time selection of the Windows core; never loads a driver or a DLL."""
from __future__ import annotations

import struct
from pathlib import Path


def windows_machine(path):
    """Read a DLL's PE machine and reject truncated/non-DLL inputs before freezing."""
    path = Path(path)
    with path.open("rb") as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError(f"Not a Windows DLL: {path}")
        offset = struct.unpack_from("<I", dos, 60)[0]
        if offset < 64 or offset > path.stat().st_size - 26:
            raise ValueError(f"Invalid PE header: {path}")
        stream.seek(offset)
        header = stream.read(26)
    if len(header) != 26 or header[:4] != b"PE\0\0":
        raise ValueError(f"Invalid PE header: {path}")
    machine, = struct.unpack_from("<H", header, 4)
    optional_size, characteristics, magic = struct.unpack_from("<HHH", header, 20)
    expected = {0x014C: 0x010B, 0x8664: 0x020B}.get(machine)
    if (expected is None or magic != expected or not characteristics & 0x2000
            or optional_size < 2 or offset + 24 + optional_size > path.stat().st_size):
        raise ValueError(f"Expected an x86 or x64 PE DLL: {path}")
    return machine


def native_binary(root, override=None, *, bits=None):
    """Select the core matching the Python/PyInstaller process, or fail with build guidance.

    An explicit DRUTA_NATIVE_LIB is authoritative: a bad file never silently
    falls through to another build. Default candidates are CMake outputs only.
    """
    root = Path(root).resolve()
    bits = struct.calcsize("P") * 8 if bits is None else bits
    if bits not in (32, 64):
        raise ValueError(f"Unsupported Python architecture: {bits} bits")
    machine = 0x8664 if bits == 64 else 0x014C
    if override:
        candidates = [Path(override).expanduser().resolve()]
    else:
        build = root / "native" / "build"
        candidates = [build / f"msvc-{bits}" / config / "drutacore.dll"
                      for config in ("Release", "RelWithDebInfo", "")]
        candidates += [build / "msvc" / config / "drutacore.dll"
                       for config in ("Release", "RelWithDebInfo", "")]
        candidates += [build / ("mingw-x86_64" if bits == 64 else "mingw-i686") / "drutacore.dll"]
    errors = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            if windows_machine(path) != machine:
                raise ValueError(f"Native core does not match {bits}-bit Python: {path}")
        except ValueError as exc:
            if override:
                raise
            errors.append(str(exc))
            continue
        return path
    detail = "; ".join(errors)
    raise FileNotFoundError(
        f"No {bits}-bit drutacore.dll to bundle. Run build.ps1 (CMake required), "
        "or set DRUTA_NATIVE_LIB to an architecture-matched CMake build."
        + (f" {detail}" if detail else ""))


def main(argv=None):
    """File entry point for PowerShell builds, avoiding native -c quote handling."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bits", action="store_true", help="print this Python process's bitness")
    parser.add_argument("root", nargs="?")
    parser.add_argument("library", nargs="?")
    args = parser.parse_args(argv)
    if args.bits:
        if args.root is not None or args.library is not None:
            parser.error("--bits takes no paths")
        print(struct.calcsize("P") * 8)
        return 0
    if args.root is None or args.library is None:
        parser.error("provide the repository root and native DLL path")
    try:
        print(native_binary(args.root, args.library))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
