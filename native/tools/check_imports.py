#!/usr/bin/env python3
# Druta - a monitor and tuner for NVIDIA GPUs.
# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""Import and header audit for the Windows binaries of the native core.

The Windows loader refuses a PE image before any of its code runs when one
imported function is missing from the DLL named for it, or when the image's
header asks for a newer Windows. This script reads each image itself (no
external tool: the PE headers, the section table and the import directory,
following every RVA through the section table as the loader does) and checks
it against native/tools/xp-imports.txt:

  * only the DLLs that file names, and only the functions it lists for the
    image's machine (kernel32.dll from Microsoft's documentation and an export
    history; msvcrt.dll from two secondary sources; see the file);
  * Major/MinorOperatingSystemVersion and Major/MinorSubsystemVersion no higher
    than the file's limit for the machine.

It fails closed: anything it cannot read, or reads as unusual, is a violation,
never a skip (not a PE image, truncated, an unknown machine, no import
directory, an RVA that maps to no section's file data, a partly-zero import
descriptor, a bound descriptor, a bound-import or delay-import directory, an
import by ordinal, a DLL with no functions, a name that is not printable
ASCII). Passing proves only that the import tables and header versions are
within the allowlist; it does not prove that the image starts on XP.

Import descriptors are read until an all-zero descriptor, ignoring the
directory's size field. The loader stops earlier, at the first descriptor
whose Name or FirstThunk is zero; any such descriptor that is not all zero is
a violation here. So every descriptor the loader processes is read and judged:
no later descriptor can hide behind a terminator the audit honours and the
loader does not.

Usage:
  check_imports.py [--allowlist FILE] [--quiet] PATH...
PATH is a .dll/.exe or a directory, searched (not recursively) for them.
Exit status 0 = clean, 1 = violation, 2 = usage or I/O error.
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ALLOWLIST = os.path.join(HERE, "xp-imports.txt")

MACHINES = {0x014C: "i686", 0x8664: "x86_64"}
MAGIC = {"i686": 0x10B, "x86_64": 0x20B}
DIR_IMPORT, DIR_BOUND_IMPORT, DIR_DELAY_IMPORT = 1, 11, 13
MAX_DESCRIPTORS = 4096
MAX_THUNKS = 65536
MAX_NAME = 512


class MalformedError(Exception):
    """The image cannot be read as a well-formed PE image."""


@dataclass
class Image:
    """What a PE image imports, by descriptor, and its header versions."""

    machine: str | None = None
    imports: list[tuple[str, list[str]]] = field(default_factory=list)  # (DLL, functions) per descriptor
    os_version: tuple[int, int] | None = None
    subsystem_version: tuple[int, int] | None = None
    problems: list[str] = field(default_factory=list)  # violations found while reading


@dataclass
class _Section:
    name: str
    virtual_address: int
    mapped: int  # bytes the loader maps
    raw_offset: int
    file_backed: int  # bytes of the mapping that come from the file


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.sections: list[_Section] = []

    def unpack(self, fmt: str, offset: int):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(self.data):
            raise MalformedError(
                f"truncated: {size} bytes at file offset {offset:#x} run past the end ({len(self.data):#x})"
            )
        return struct.unpack_from(fmt, self.data, offset)

    def offset(self, rva: int, length: int, what: str) -> int:
        """File offset of `length` bytes at `rva`, which must lie in one section's file data."""
        for section in self.sections:
            start = section.virtual_address
            if start <= rva < start + section.mapped:
                if rva + length > start + section.file_backed:
                    raise MalformedError(f"{what} at RVA {rva:#x} runs past the file data of section {section.name}")
                return section.raw_offset + (rva - start)
        raise MalformedError(f"{what} at RVA {rva:#x} maps to no section")

    def name(self, rva: int, what: str) -> str:
        start = self.offset(rva, 1, what)
        for section in self.sections:  # the string must end inside the same section's file data
            if section.raw_offset <= start < section.raw_offset + section.file_backed:
                end_limit = min(section.raw_offset + section.file_backed, start + MAX_NAME)
                break
        else:  # pragma: no cover - offset() found a section
            raise MalformedError(f"{what} at RVA {rva:#x} maps to no section")
        end = self.data.find(b"\0", start, end_limit)
        if end < 0:
            raise MalformedError(f"{what} at RVA {rva:#x} is not terminated within {MAX_NAME} bytes of its section")
        raw = self.data[start:end]
        if not raw or any(byte < 0x21 or byte > 0x7E for byte in raw):
            raise MalformedError(f"{what} at RVA {rva:#x} is not a plain printable ASCII name: {raw!r}")
        return raw.decode("ascii")


def read_pe(data: bytes) -> Image:
    """Read the machine, header versions and import descriptors of a PE image."""
    image = Image()
    reader = _Reader(data)
    try:
        _read(reader, image)
    except MalformedError as error:
        image.problems.append(str(error))
    return image


def _read(reader: _Reader, image: Image) -> None:
    if reader.data[:2] != b"MZ":
        raise MalformedError("not a PE image (no MZ header)")
    (e_lfanew,) = reader.unpack("<I", 0x3C)
    if reader.unpack("<4s", e_lfanew)[0] != b"PE\0\0":
        raise MalformedError("not a PE image (no PE signature)")
    machine, section_count, _, _, _, optional_size, _ = reader.unpack("<HHIIIHH", e_lfanew + 4)
    image.machine = MACHINES.get(machine)
    if image.machine is None:
        raise MalformedError(f"machine {machine:#06x} is neither i386 nor x86-64")
    optional = e_lfanew + 24
    (magic,) = reader.unpack("<H", optional)
    if magic != MAGIC[image.machine]:
        raise MalformedError(f"optional header magic {magic:#x} does not match machine {image.machine}")
    os_major, os_minor = reader.unpack("<HH", optional + 40)
    sub_major, sub_minor = reader.unpack("<HH", optional + 48)
    image.os_version = (os_major, os_minor)
    image.subsystem_version = (sub_major, sub_minor)
    count_at = optional + (92 if magic == 0x10B else 108)
    (directory_count,) = reader.unpack("<I", count_at)
    directories_at = count_at + 4
    if directory_count > 16 or directories_at + 8 * directory_count > optional + optional_size:
        raise MalformedError(
            f"{directory_count} data directories do not fit the optional header ({optional_size} bytes)"
        )
    directories = [reader.unpack("<II", directories_at + 8 * i) for i in range(directory_count)]

    table = optional + optional_size
    for index in range(section_count):
        raw_name, virtual_size, virtual_address, raw_size, raw_offset = reader.unpack("<8sIIII", table + 40 * index)
        if raw_size and raw_offset + raw_size > len(reader.data):
            raise MalformedError(f"section {index} runs past the end of the file")
        mapped = virtual_size or raw_size
        reader.sections.append(
            _Section(
                raw_name.rstrip(b"\0").decode("latin-1"), virtual_address, mapped, raw_offset, min(mapped, raw_size)
            )
        )

    for index, label in ((DIR_BOUND_IMPORT, "bound-import"), (DIR_DELAY_IMPORT, "delay-import")):
        if index < directory_count and directories[index] != (0, 0):
            rva, size = directories[index]
            image.problems.append(f"{label} directory present (RVA {rva:#x}, size {size:#x}): not audited")
    if directory_count <= DIR_IMPORT or directories[DIR_IMPORT][0] == 0:
        raise MalformedError("no import directory (every binary of the core imports kernel32)")

    wide = image.machine == "x86_64"
    thunk_format, thunk_size = ("<Q", 8) if wide else ("<I", 4)
    ordinal_flag = 1 << (63 if wide else 31)
    descriptor_rva = directories[DIR_IMPORT][0]
    for number in range(MAX_DESCRIPTORS):
        at = reader.offset(descriptor_rva + 20 * number, 20, f"import descriptor {number}")
        original, stamp, forwarder, name_rva, first = reader.unpack("<IIIII", at)
        if not (original or stamp or forwarder or name_rva or first):
            if number == 0:
                raise MalformedError("the import directory is empty (every binary of the core imports kernel32)")
            return
        if not name_rva or not first:
            raise MalformedError(
                f"import descriptor {number} is not all zero but has no "
                f"{'name' if not name_rva else 'FirstThunk'}: the loader stops here, the audit cannot"
            )
        dll = reader.name(name_rva, f"DLL name of import descriptor {number}")
        if stamp or forwarder:
            image.problems.append(
                f"{dll}: bound import descriptor (TimeDateStamp {stamp:#x}, ForwarderChain {forwarder:#x}): not audited"
            )
        functions: list[str] = []
        image.imports.append((dll, functions))
        thunks = original or first
        for slot in range(MAX_THUNKS + 1):
            if slot == MAX_THUNKS:
                raise MalformedError(f"{dll}: more than {MAX_THUNKS} imports")
            (value,) = reader.unpack(
                thunk_format, reader.offset(thunks + thunk_size * slot, thunk_size, f"{dll} thunk {slot}")
            )
            if value == 0:
                break
            if value & ordinal_flag:
                image.problems.append(f"{dll}: import by ordinal {value & 0xFFFF}: not audited")
                continue
            if value >> 31:
                raise MalformedError(f"{dll}: thunk {slot} value {value:#x} is neither an ordinal nor a name RVA")
            functions.append(reader.name(value + 2, f"{dll} import name {slot}"))  # skip the 2-byte hint
        if not functions:
            image.problems.append(f"{dll}: no function imported by name")
    raise MalformedError(f"more than {MAX_DESCRIPTORS} import descriptors")


@dataclass
class Allowlist:
    # (lower-case DLL name, machine or None for every machine) -> allowed functions
    judged: dict[tuple[str, str | None], set[str]]
    version_limit: dict[str, tuple[int, int]]  # machine -> highest OS / subsystem version

    def allows_dll(self, dll: str) -> bool:
        return any(key[0] == dll.lower() for key in self.judged)

    def allowed(self, dll: str, machine: str | None) -> set[str]:
        names = set(self.judged.get((dll.lower(), None), set()))
        if machine:
            names |= self.judged.get((dll.lower(), machine), set())
        return names


VERSION_SECTION = "header-version-limit"


def parse_allowlist(text: str) -> Allowlist:
    """Sections: ``[DLL]`` (every machine), ``[DLL machine]``, and ``[header-version-limit]``."""
    machines = tuple(MACHINES.values())
    judged: dict[tuple[str, str | None], set[str]] = {}
    limits: dict[str, tuple[int, int]] = {}
    section: tuple[str, str | None] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            words = line[1:-1].split()
            if len(words) not in (1, 2) or (len(words) == 2 and words[1] not in machines):
                raise ValueError(f"allowlist line {number}: {line!r} is not [DLL] or [DLL {'|'.join(machines)}]")
            section = (words[0].lower(), words[1] if len(words) == 2 else None)
            if section[0] != VERSION_SECTION:
                judged.setdefault(section, set())
            elif section[1] is not None:
                raise ValueError(f"allowlist line {number}: [{VERSION_SECTION}] takes no machine")
            continue
        if section is None:
            raise ValueError(f"allowlist line {number}: {line!r} is outside a section")
        if section[0] == VERSION_SECTION:
            match = re.fullmatch(r"(\S+)\s+(\d+)\.(\d+)", line)
            if not match or match.group(1) not in machines or match.group(1) in limits:
                raise ValueError(
                    f"allowlist line {number}: {line!r} is not '<machine> <major>.<minor>' once per machine"
                )
            limits[match.group(1)] = (int(match.group(2)), int(match.group(3)))
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_@]*", line):
            raise ValueError(f"allowlist line {number}: {line!r} is not a function name")
        judged[section].add(line)
    missing = [machine for machine in machines if machine not in limits]
    if missing:
        raise ValueError(f"[{VERSION_SECTION}] has no limit for {', '.join(missing)}")
    return Allowlist(judged, limits)


def audit(image: Image, allow: Allowlist) -> list[str]:
    """Return the violations of one image (empty = clean)."""
    violations = list(image.problems)
    if image.machine is None:
        return violations
    limit = allow.version_limit[image.machine]
    for label, version in (("operating system", image.os_version), ("subsystem", image.subsystem_version)):
        if version is not None and version > limit:
            violations.append(
                f"{label} version {version[0]}.{version[1]} is above "
                f"{limit[0]}.{limit[1]}, the limit for {image.machine}"
            )
    for dll, functions in image.imports:
        if not allow.allows_dll(dll):
            violations.append(f"DLL not allowed: {dll}")
            continue
        allowed = allow.allowed(dll, image.machine)
        for name in functions:
            if name not in allowed:
                violations.append(f"{dll}!{name} is not in the allowlist for {image.machine}")
    return violations


def collect(paths: list[str]) -> list[str]:
    binaries: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.lower().endswith((".dll", ".exe")):
                    binaries.append(os.path.join(path, name))
        else:
            binaries.append(path)
    return binaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("paths", nargs="+", help="PE images or directories holding them")
    parser.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--quiet", action="store_true", help="print the DLLs and their import counts, not every function"
    )
    args = parser.parse_args(argv)
    try:
        with open(args.allowlist, encoding="utf-8") as handle:
            allow = parse_allowlist(handle.read())
    except (OSError, ValueError) as error:
        print(f"check_imports: {error}", file=sys.stderr)
        return 2
    binaries = collect(args.paths)
    if not binaries:
        print("check_imports: no .dll or .exe found", file=sys.stderr)
        return 2
    failed = 0
    for binary in binaries:
        try:
            with open(binary, "rb") as handle:
                image = read_pe(handle.read())
        except OSError as error:
            print(f"check_imports: {error}", file=sys.stderr)
            return 2
        violations = audit(image, allow)
        versions = ""
        if image.os_version and image.subsystem_version:
            versions = (
                f", OS {image.os_version[0]}.{image.os_version[1]}, "
                f"subsystem {image.subsystem_version[0]}.{image.subsystem_version[1]}"
            )
        print(f"{binary} ({image.machine or 'unknown machine'}{versions}):")
        for dll, functions in image.imports:
            print(f"  {dll}: {len(functions)} functions")
            if not args.quiet:
                for name in functions:
                    print(f"    {name}")
        for violation in violations:
            print(f"  VIOLATION: {violation}")
        print(f"  {'FAIL' if violations else 'ok'}")
        failed += bool(violations)
    print(
        f"check_imports: {len(binaries) - failed} of {len(binaries)} binaries clean "
        f"(every import and the header versions judged against {os.path.basename(args.allowlist)})"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
