"""List every DLL a finished bundle expects Windows to provide.

The Windows loader reports only the first missing dependency, and only in a
dialog (for example ``api-ms-win-core-path-l1-1-0.dll`` from a CPython that
needs Windows 8+). This tool reads the import and delay-import tables of every
PE file in a bundle at build time and writes ``Druta-imports.txt``. On the
target machine ``Druta-Diagnose.cmd`` checks each listed DLL, so one run names
every missing system dependency before Python is even loaded.

Format, one per line: ``dll-name|importer[,importer...]``. Lines starting with
``#`` are comments. DLLs shipped anywhere in the bundle are omitted.

Usage: python pe_import_manifest.py --bundle dist/Druta-Win7-Portable
"""
import argparse
from pathlib import Path
import sys

PE_SUFFIXES = {".exe", ".dll", ".pyd"}


def _imports(path):
    import pefile
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
        ])
        names = set()
        for attribute in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attribute, []) or []:
                dll = entry.dll.decode("ascii", "replace") if isinstance(entry.dll, bytes) else str(entry.dll)
                names.add(dll.lower())
        return names, pe.FILE_HEADER.Machine
    finally:
        pe.close()


def collect(bundle):
    """Return ({system_dll: sorted importers}, [unreadable files], machines)."""
    bundle = Path(bundle)
    files = [p for p in sorted(bundle.rglob("*")) if p.is_file() and p.suffix.lower() in PE_SUFFIXES]
    shipped = {p.name.lower() for p in files}
    needed, unreadable, machines = {}, [], {}
    for path in files:
        relative = path.relative_to(bundle).as_posix()
        try:
            names, machine = _imports(path)
        except Exception as e:  # a data file with a PE suffix, or a corrupt PE
            unreadable.append(f"{relative}: {type(e).__name__}: {e}")
            continue
        machines[relative] = machine
        for name in names - shipped:
            needed.setdefault(name, set()).add(relative)
    return ({name: sorted(importers) for name, importers in sorted(needed.items())},
            unreadable, machines)


def write_manifest(bundle, output=None):
    bundle = Path(bundle)
    output = Path(output) if output else bundle / "Druta-imports.txt"
    needed, unreadable, machines = collect(bundle)
    lines = ["# DLLs this bundle expects Windows to provide (generated at build time).",
             "# Checked on the target by Druta-Diagnose.cmd. dll|importers"]
    lines += [f"# unreadable: {entry}" for entry in unreadable]
    non_x64 = sorted(name for name, machine in machines.items() if machine != 0x8664)
    lines += [f"# not x64: {name}" for name in non_x64]
    lines += [f"{name}|{','.join(importers)}" for name, importers in needed.items()]
    # CRLF: read by cmd.exe's FOR /F on Windows Vista and 7.
    output.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii", "replace"))
    return output, needed, unreadable, non_x64


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        parser.error(f"not a directory: {args.bundle}")
    output, needed, unreadable, non_x64 = write_manifest(args.bundle, args.output)
    apisets = [name for name in needed if name.startswith(("api-ms-win-", "ext-ms-win-"))]
    print(f"{output}: {len(needed)} system DLL(s) expected from Windows")
    for name in apisets:
        print(f"  API set not shipped, verify on target: {name} <- {', '.join(needed[name])}")
    for entry in unreadable:
        print(f"  unreadable PE: {entry}")
    for name in non_x64:
        print(f"  not an x64 image: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
