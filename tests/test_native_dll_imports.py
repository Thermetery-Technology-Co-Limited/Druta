# Copyright (C) 2026 Thermetery Technology Co Limited
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Windows binaries must import only what Windows XP has, and say so in their headers.

native/tools/check_imports.py reads every MinGW-built binary itself (PE headers,
section table, import directory) and judges it against native/tools/xp-imports.txt;
native/build.sh and the CMake build run it. These tests build small PE32 and
PE32+ images in memory, one per rule, and show each rule failing on its cause and
passing again once the cause is removed; then they run the audit on the real
builds and compare its reading with binutils' `objdump -p` where objdump exists.
"""

import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "native", "tools", "check_imports.py")
ALLOWLIST = os.path.join(ROOT, "native", "tools", "xp-imports.txt")
SHIPPED = ("drutacore.dll", "druta-probe.exe", "drutacore-selftest.exe")
HARNESS = "druta-runtime-imports-test.exe"
VISTA_ONLY = (
    "GetThreadId",
    "InitializeConditionVariable",
    "SleepConditionVariableCS",
    "WakeAllConditionVariable",
    "WakeConditionVariable",
    "GetTickCount64",
)


def _load_script():
    spec = importlib.util.spec_from_file_location("check_imports", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


check = _load_script()


def _allowlist():
    with open(ALLOWLIST, encoding="utf-8") as handle:
        return check.parse_allowlist(handle.read())


ALLOW = _allowlist()

# ---- a PE32 / PE32+ image builder ------------------------------------------------------------

MACHINE_CODE = {"i686": 0x014C, "x86_64": 0x8664}
IDATA_RVA, OTHER_RVA = 0x1000, 0x3000
FILE_ALIGN = 0x200


class _Area:
    """The bytes of one section; `put` appends a blob and returns its RVA."""

    def __init__(self, rva):
        self.rva = rva
        self.data = bytearray()

    def put(self, blob, align=2):
        while len(self.data) % align:
            self.data.append(0)
        at = self.rva + len(self.data)
        self.data += blob
        return at


class Raw(int):
    """A thunk value written as it is (neither a name nor an ordinal)."""


def _dll(name, functions, **options):
    """One import descriptor: functions are names (str), ordinals (int) or Raw thunk values."""
    return dict(name=name, functions=functions, **options)


def build_pe(
    machine="i686",
    imports=None,
    os_version=(5, 1),
    subsystem_version=(5, 1),
    machine_code=None,
    magic=None,
    directories=None,
    directory_count=16,
    terminator=(0, 0, 0, 0, 0),
    after_terminator=(),
    other_virtual_extra=0,
    other_tail=b"",
):
    """A minimal image: headers, `.idata` at RVA 0x1000 holding the import
    directory, and `.other` at RVA 0x3000 for what a descriptor places there
    (options name_in, thunks_in, hint_in = "other"; other_tail goes first there).

    Descriptor options: oft (False: OriginalFirstThunk 0, names read from
    FirstThunk), stamp, forwarder, name_rva / first_thunk (override the RVAs).
    other_virtual_extra makes `.other` that much larger in memory than in the file."""
    if imports is None:
        imports = [_dll("KERNEL32.dll", ["GetProcAddress", "Sleep"]), _dll("msvcrt.dll", ["malloc"])]
    wide = machine == "x86_64"
    thunk = "<Q" if wide else "<I"
    flag = 1 << (63 if wide else 31)
    areas = {"idata": _Area(IDATA_RVA), "other": _Area(OTHER_RVA)}
    if other_tail:
        areas["other"].put(other_tail, 1)
    descriptors_at = areas["idata"].put(bytes(20 * (len(imports) + 1 + len(after_terminator))), 4)
    rows = []
    for spec in imports:
        name_rva = areas[spec.get("name_in", "idata")].put(spec["name"].encode("latin-1") + b"\0")
        entries = []
        for function in spec["functions"]:
            if isinstance(function, Raw):
                entries.append(int(function))
            elif isinstance(function, int):
                entries.append(flag | function)
            else:
                hint_area = areas[spec.get("hint_in", "idata")]
                entries.append(hint_area.put(b"\x00\x00" + function.encode("latin-1") + b"\0"))
        table = b"".join(struct.pack(thunk, entry) for entry in [*entries, 0])
        lookup = areas[spec.get("thunks_in", "idata")].put(table, 8)
        first = areas["idata"].put(table, 8)
        rows.append(
            (
                lookup if spec.get("oft", True) else 0,
                spec.get("stamp", 0),
                spec.get("forwarder", 0),
                spec.get("name_rva", name_rva),
                spec.get("first_thunk", first),
            )
        )
    rows.append(terminator)
    rows.extend(after_terminator)
    for number, row in enumerate(rows):
        struct.pack_into("<IIIII", areas["idata"].data, descriptors_at - IDATA_RVA + 20 * number, *row)

    optional_size = (112 if wide else 96) + 8 * directory_count
    directory = {1: (descriptors_at, 20 * len(rows))}
    directory.update(directories or {})
    assert 0x58 + optional_size + 40 * len(areas) <= FILE_ALIGN
    raw_offsets, raw_sizes = {}, {}
    position = FILE_ALIGN
    for key, area in areas.items():
        raw_offsets[key] = position
        raw_sizes[key] = -(-max(len(area.data), 1) // FILE_ALIGN) * FILE_ALIGN
        position += raw_sizes[key]
    image = bytearray(position)
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x40)
    image[0x40:0x44] = b"PE\0\0"
    code = MACHINE_CODE[machine] if machine_code is None else machine_code
    struct.pack_into("<HHIIIHH", image, 0x44, code, len(areas), 0, 0, 0, optional_size, 0x2022 if wide else 0x2102)
    optional = 0x58
    struct.pack_into("<H", image, optional, (0x20B if wide else 0x10B) if magic is None else magic)
    struct.pack_into("<HH", image, optional + 40, *os_version)
    struct.pack_into("<HH", image, optional + 48, *subsystem_version)
    count_at = optional + (108 if wide else 92)
    struct.pack_into("<I", image, count_at, directory_count)
    for index, (rva, size) in directory.items():
        if index < directory_count:
            struct.pack_into("<II", image, count_at + 4 + 8 * index, rva, size)
    table = optional + optional_size
    for number, (key, area) in enumerate(areas.items()):
        virtual = max(len(area.data), 1)
        raw = raw_sizes[key]
        if key == "other" and other_virtual_extra:
            virtual, raw = virtual + other_virtual_extra, len(area.data)
        header = struct.pack("<8sIIII", ("." + key).encode(), virtual, area.rva, raw, raw_offsets[key])
        image[table + 40 * number : table + 40 * number + len(header)] = header
        image[raw_offsets[key] : raw_offsets[key] + len(area.data)] = area.data
    return bytes(image)


def _audit(data):
    return check.audit(check.read_pe(data), ALLOW)


def _with(data, offset, blob):
    return data[:offset] + blob + data[offset + len(blob) :]


def test_the_builder_makes_clean_images_on_both_machines():
    for machine, version in (("i686", (5, 1)), ("x86_64", (5, 2))):
        image = check.read_pe(build_pe(machine, os_version=version, subsystem_version=version))
        assert image.problems == []
        assert image.machine == machine
        assert image.os_version == version
        assert image.subsystem_version == version
        assert image.imports == [("KERNEL32.dll", ["GetProcAddress", "Sleep"]), ("msvcrt.dll", ["malloc"])]
        assert check.audit(image, ALLOW) == []


# ---- one case per rule: (cause, the same image without it, what the violation says) ----------

KERNEL32_ONLY = [_dll("KERNEL32.dll", ["Sleep"])]
NAME_OUTSIDE_IDATA = [_dll("KERNEL32.dll", ["Sleep"]), _dll("USER32.dll", ["MessageBoxW"], name_in="other")]
INT_OUTSIDE_IDATA = [_dll("KERNEL32.dll", ["Sleep", "GetThreadId"], thunks_in="other", hint_in="other")]


def _cases():
    clean = build_pe()
    clean64 = build_pe("x86_64", os_version=(5, 2), subsystem_version=(5, 2))
    yield "not MZ", _with(clean, 0, b"ZM"), clean, "no MZ header"
    yield "no PE signature", _with(clean, 0x40, b"PX\0\0"), clean, "no PE signature"
    yield "truncated header", clean[:0x60], clean, "truncated"
    yield "truncated section", clean[: FILE_ALIGN + 0x10], clean, "runs past the end of the file"
    yield "unknown machine", build_pe(machine_code=0x01C4), clean, "neither i386 nor x86-64"
    yield "magic of the other machine", build_pe("x86_64", magic=0x10B), clean64, "does not match machine"
    yield "too many directories", build_pe(directory_count=17), clean, "data directories do not fit"
    yield "no import directory", build_pe(directories={1: (0, 0)}), clean, "no import directory"
    yield "too few directories", build_pe(directory_count=1), clean, "no import directory"
    yield "empty import directory", build_pe(imports=[]), clean, "import directory is empty"
    yield "import directory in no section", build_pe(directories={1: (0x9000, 40)}), clean, "maps to no section"
    yield (
        "name RVA past the file data",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"], name_rva=OTHER_RVA + 0x10)], other_virtual_extra=0x100),
        build_pe(imports=KERNEL32_ONLY, other_virtual_extra=0x100),
        "runs past the file data",
    )
    yield (
        "name RVA in no section",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"], name_rva=0x8000)]),
        build_pe(imports=KERNEL32_ONLY),
        "maps to no section",
    )
    yield (
        "thunk RVA in no section",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"], first_thunk=0x8000, oft=False)]),
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"], oft=False)]),
        "maps to no section",
    )
    yield (
        "descriptor without a name",
        build_pe(imports=KERNEL32_ONLY, terminator=(IDATA_RVA, 0, 0, 0, IDATA_RVA)),
        build_pe(imports=KERNEL32_ONLY),
        "has no name",
    )
    yield (
        "descriptor without FirstThunk",
        build_pe(imports=KERNEL32_ONLY, terminator=(IDATA_RVA, 0, 0, IDATA_RVA, 0)),
        build_pe(imports=KERNEL32_ONLY),
        "has no FirstThunk",
    )
    yield (
        "descriptor with only a TimeDateStamp",
        build_pe(imports=KERNEL32_ONLY, terminator=(0, 1, 0, 0, 0)),
        build_pe(imports=KERNEL32_ONLY),
        "is not all zero",
    )
    yield (
        "DLL with no functions",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"]), _dll("msvcrt.dll", [])]),
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"]), _dll("msvcrt.dll", ["malloc"])]),
        "msvcrt.dll: no function imported by name",
    )
    yield (
        "ordinal import on i686",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep", 17])]),
        build_pe(imports=KERNEL32_ONLY),
        "KERNEL32.dll: import by ordinal 17: not audited",
    )
    yield (
        "ordinal import on x86_64",
        build_pe("x86_64", imports=[_dll("KERNEL32.dll", ["Sleep", 17])]),
        build_pe("x86_64", imports=KERNEL32_ONLY),
        "KERNEL32.dll: import by ordinal 17: not audited",
    )
    yield (
        "thunk neither ordinal nor name RVA",
        build_pe("x86_64", imports=[_dll("KERNEL32.dll", [Raw(1 << 62)])]),
        build_pe("x86_64", imports=KERNEL32_ONLY),
        "neither an ordinal nor a name RVA",
    )
    yield (
        "bound descriptor (TimeDateStamp)",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"], stamp=0xFFFFFFFF)]),
        build_pe(imports=KERNEL32_ONLY),
        "KERNEL32.dll: bound import descriptor",
    )
    yield (
        "bound descriptor (ForwarderChain)",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"], forwarder=0xFFFFFFFF)]),
        build_pe(imports=KERNEL32_ONLY),
        "KERNEL32.dll: bound import descriptor",
    )
    yield "bound-import directory", build_pe(directories={11: (0x1F0, 0x20)}), clean, "bound-import directory present"
    yield "delay-import directory", build_pe(directories={13: (OTHER_RVA, 0x40)}), clean, "delay-import directory"
    yield (
        "control character in a function name",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sl\x01eep"])]),
        build_pe(imports=KERNEL32_ONLY),
        "not a plain printable ASCII name",
    )
    yield (
        "non-ASCII DLL name",
        build_pe(imports=[_dll("KERNEL32\xe9.dll", ["Sleep"])]),
        build_pe(imports=KERNEL32_ONLY),
        "not a plain printable ASCII name",
    )
    yield (
        "empty function name",
        build_pe(imports=[_dll("KERNEL32.dll", [""])]),
        build_pe(imports=KERNEL32_ONLY),
        "not a plain printable ASCII name",
    )
    yield (
        "name not terminated in its section",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"], name_rva=OTHER_RVA)], other_tail=b"K" * 600),
        build_pe(imports=KERNEL32_ONLY, other_tail=b"K" * 600),
        "is not terminated",
    )
    # The two holes of the objdump-text reading: a DLL name, or a whole name
    # table, placed outside .idata. Both are followed and judged here.
    yield (
        "forbidden DLL whose name lies outside .idata",
        build_pe(imports=NAME_OUTSIDE_IDATA),
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"]), _dll("msvcrt.dll", ["malloc"], name_in="other")]),
        "DLL not allowed: USER32.dll",
    )
    yield (
        "forbidden function whose name table lies outside .idata",
        build_pe(imports=INT_OUTSIDE_IDATA),
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep", "GetProcAddress"], thunks_in="other", hint_in="other")]),
        "KERNEL32.dll!GetThreadId is not in the allowlist for i686",
    )
    yield (
        "forbidden function read through FirstThunk (OriginalFirstThunk 0)",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep", "GetThreadId"], oft=False)]),
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep", "GetProcAddress"], oft=False)]),
        "KERNEL32.dll!GetThreadId is not in the allowlist for i686",
    )
    yield (
        "forbidden x86_64 function",
        build_pe("x86_64", imports=[_dll("KERNEL32.dll", ["RtlUnwindEx", "GetTickCount64"])]),
        build_pe("x86_64", imports=[_dll("KERNEL32.dll", ["RtlUnwindEx", "GetTickCount"])]),
        "KERNEL32.dll!GetTickCount64 is not in the allowlist for x86_64",
    )
    yield (
        "x86_64-only name on i686",
        build_pe(imports=[_dll("KERNEL32.dll", ["RtlUnwindEx"])]),
        build_pe("x86_64", imports=[_dll("KERNEL32.dll", ["RtlUnwindEx"])]),
        "KERNEL32.dll!RtlUnwindEx is not in the allowlist for i686",
    )
    yield (
        "msvcrt function outside the allowlist",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"]), _dll("msvcrt.dll", ["malloc", "strcpy_s"])]),
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"]), _dll("msvcrt.dll", ["malloc", "strlen"])]),
        "msvcrt.dll!strcpy_s is not in the allowlist for i686",
    )
    yield (
        "msvcrt name of the other machine",
        build_pe("x86_64", imports=[_dll("msvcrt.dll", ["_iob"])]),
        build_pe("x86_64", imports=[_dll("msvcrt.dll", ["__iob_func"])]),
        "msvcrt.dll!_iob is not in the allowlist for x86_64",
    )
    yield (
        "another DLL",
        build_pe(imports=[_dll("KERNEL32.dll", ["Sleep"]), _dll("ADVAPI32.dll", ["RegOpenKeyExW"])]),
        build_pe(imports=KERNEL32_ONLY),
        "DLL not allowed: ADVAPI32.dll",
    )


CASES = list(_cases())


@pytest.mark.parametrize(("bad", "good", "expected"), [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_each_rule_fails_on_its_cause_and_passes_without_it(bad, good, expected):
    violations = _audit(bad)
    assert any(expected in violation for violation in violations), violations
    assert _audit(good) == []


def test_the_ordinal_is_reported_in_decimal_on_both_machines():
    for machine in ("i686", "x86_64"):
        image = check.read_pe(build_pe(machine, imports=[_dll("KERNEL32.dll", ["Sleep", 17, 0x1234])]))
        assert image.problems == [
            "KERNEL32.dll: import by ordinal 17: not audited",
            "KERNEL32.dll: import by ordinal 4660: not audited",
        ]
        assert image.imports == [("KERNEL32.dll", ["Sleep"])]


def test_pe32_plus_thunks_are_read_as_64_bit():
    image = check.read_pe(build_pe("x86_64", imports=[_dll("KERNEL32.dll", ["Sleep", "GetProcAddress", "TlsAlloc"])]))
    assert image.problems == []
    assert image.imports == [("KERNEL32.dll", ["Sleep", "GetProcAddress", "TlsAlloc"])]


def test_names_are_read_through_the_section_they_lie_in():
    image = check.read_pe(build_pe(imports=NAME_OUTSIDE_IDATA))
    assert image.imports == [("KERNEL32.dll", ["Sleep"]), ("USER32.dll", ["MessageBoxW"])]
    image = check.read_pe(build_pe(imports=INT_OUTSIDE_IDATA))
    assert image.imports == [("KERNEL32.dll", ["Sleep", "GetThreadId"])]


def test_reading_stops_at_the_first_all_zero_descriptor():
    # The loader stops there or earlier (at the first Name or FirstThunk of
    # zero; a descriptor zero in only one of them is a violation), so a
    # descriptor after it is never loaded and is not read.
    after = [(IDATA_RVA, 0, 0, IDATA_RVA, IDATA_RVA)]
    image = check.read_pe(build_pe(imports=KERNEL32_ONLY, after_terminator=after))
    assert image.problems == []
    assert image.imports == [("KERNEL32.dll", ["Sleep"])]


@pytest.mark.parametrize("machine", ["i686", "x86_64"])
@pytest.mark.parametrize("field", ["os_version", "subsystem_version"])
def test_header_versions_at_below_and_above_the_limit(machine, field):
    limit = ALLOW.version_limit[machine]
    below = (limit[0], limit[1] - 1) if limit[1] else (limit[0] - 1, 9)
    label = "operating system" if field == "os_version" else "subsystem"
    for version, ok in ((below, True), (limit, True), ((limit[0], limit[1] + 1), False), ((6, 0), False)):
        versions = {"os_version": limit, "subsystem_version": limit, field: version}
        violations = _audit(build_pe(machine, **versions))
        if ok:
            assert violations == [], version
        else:
            assert violations == [
                f"{label} version {version[0]}.{version[1]} is above {limit[0]}.{limit[1]}, the limit for {machine}"
            ]


# ---- the allowlist file -----------------------------------------------------------------------


def test_shipped_limits_are_xp_and_xp_x64():
    assert ALLOW.version_limit == {"i686": (5, 1), "x86_64": (5, 2)}


def test_shipped_allowlist_judges_kernel32_and_msvcrt_and_keeps_vista_functions_out():
    assert {key[0] for key in ALLOW.judged} == {"kernel32.dll", "msvcrt.dll"}
    for machine in check.MACHINES.values():
        kernel32 = ALLOW.allowed("KERNEL32.dll", machine)
        assert "GetProcAddress" in kernel32
        assert not kernel32 & set(VISTA_ONLY)
        assert "malloc" in ALLOW.allowed("msvcrt.dll", machine)
    assert "_iob" in ALLOW.allowed("msvcrt.dll", "i686") - ALLOW.allowed("msvcrt.dll", "x86_64")
    assert "__iob_func" in ALLOW.allowed("msvcrt.dll", "x86_64") - ALLOW.allowed("msvcrt.dll", "i686")


LIMITS = "[header-version-limit]\ni686 5.1\nx86_64 5.2\n"


@pytest.mark.parametrize(
    "text",
    [
        "GetProcAddress\n" + LIMITS,  # outside a section
        LIMITS + "[KERNEL32.dll arm64]\nSleep\n",  # unknown machine
        LIMITS + "[KERNEL32.dll]\nnot a name\n",
        LIMITS + "[KERNEL32.dll x86_64 i686]\nSleep\n",
        "[KERNEL32.dll]\nSleep\n",  # no limits
        "[header-version-limit]\ni686 5.1\n",  # one machine missing
        LIMITS + "i686 5.1\n",  # a machine twice
        "[header-version-limit]\ni686 5.1\nx86_64 5\n",
        "[header-version-limit]\ni686 5.1\narm64 6.2\nx86_64 5.2\n",
        "[header-version-limit i686]\ni686 5.1\nx86_64 5.2\n",
    ],
)
def test_malformed_allowlists_are_refused(text):
    with pytest.raises(ValueError):
        check.parse_allowlist(text)


def test_the_command_exit_status(tmp_path):
    (tmp_path / "clean.dll").write_bytes(build_pe())
    (tmp_path / "bad.exe").write_bytes(build_pe(imports=INT_OUTSIDE_IDATA))
    (tmp_path / "allow.txt").write_text("[KERNEL32.dll]\nSleep\n", encoding="utf-8")

    def run(*args):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, check=False)

    assert run(str(tmp_path / "clean.dll")).returncode == 0
    done = run(str(tmp_path))
    assert done.returncode == 1
    assert "VIOLATION: KERNEL32.dll!GetThreadId is not in the allowlist for i686" in done.stdout
    assert "1 of 2 binaries clean" in done.stdout
    assert run("--allowlist", str(tmp_path / "allow.txt"), str(tmp_path / "clean.dll")).returncode == 2  # no limits
    assert run(str(tmp_path / "missing.dll")).returncode == 2
    assert run(str(tmp_path / "allow.txt")).returncode == 1  # not a PE image: a violation, not a skip


# ---- the real builds --------------------------------------------------------------------------


def _build_dir(config):
    directory = os.path.join(ROOT, "native", "build", config)
    if not all(os.path.isfile(os.path.join(directory, name)) for name in SHIPPED):
        pytest.skip(f"{config} not built (native/build.sh {config})")
    return directory


def _binaries(directory):
    names = [*SHIPPED, HARNESS] if os.path.isfile(os.path.join(directory, HARNESS)) else list(SHIPPED)
    return [os.path.join(directory, name) for name in names]


def _rebuild(config):
    return f"\nIf these binaries predate the current sources, rebuild them: native/build.sh {config}"


@pytest.mark.parametrize("config", ["mingw-i686", "mingw-x86_64"])
def test_built_windows_binaries_pass_the_audit(config):
    directory = _build_dir(config)
    binaries = _binaries(directory)
    done = subprocess.run([sys.executable, SCRIPT, *binaries], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr + _rebuild(config)
    machine = config.split("-", 1)[1]
    for path in binaries:
        with open(path, "rb") as handle:
            image = check.read_pe(handle.read())
        assert image.machine == machine, path + _rebuild(config)
        # CMakeLists.txt links 5.1 into 32-bit and 5.2 into 64-bit images: XP and XP x64 exactly.
        expected = (5, 1) if machine == "i686" else (5, 2)
        assert (image.os_version, image.subsystem_version) == (expected, expected), path + _rebuild(config)
        imported = {name for _, functions in image.imports for name in functions}
        assert not imported & set(VISTA_ONLY), f"{path}: {sorted(imported & set(VISTA_ONLY))}" + _rebuild(config)


def _objdump():
    for name in ("x86_64-w64-mingw32-objdump", "i686-w64-mingw32-objdump", "objdump"):
        if shutil.which(name):
            return name
    return None


def _objdump_imports(listing):
    """(DLL, functions) per descriptor, from the import tables of `objdump -p`."""
    imports, inside = [], False
    for line in listing.splitlines():
        if line.startswith("The Import Tables"):
            inside = True
            continue
        if inside and line[:1] not in ("", " ", "\t"):
            break  # the next part of the listing
        found = re.match(r"\s*DLL Name: (\S+)$", line)
        if inside and found:
            imports.append((found.group(1), []))
            continue
        # Binutils 2.44 adds an Ordinal column (<none> for named imports) and
        # prints the hint in hexadecimal; older versions use a decimal hint.
        found = re.match(r"\s*[0-9a-f]+\s+(?:<none>\s+)?[0-9a-f]+\s+(\S+)$", line)
        if inside and found and imports:
            imports[-1][1].append(found.group(1))
    return imports


@pytest.mark.parametrize("row", ["\t168490  148  CloseHandle",
                                  "\t00168290  <none>  0094  CloseHandle"])
def test_objdump_import_rows_before_and_after_the_ordinal_column(row):
    listing = ("The Import Tables (interpreted .idata section contents)\n"
               "\tDLL Name: KERNEL32.dll\n" + row + "\n"
               "The Export Tables\n\t168490  148  NotAnImport\n")
    assert _objdump_imports(listing) == [("KERNEL32.dll", ["CloseHandle"])]


@pytest.mark.parametrize("config", ["mingw-i686", "mingw-x86_64"])
def test_the_reading_agrees_with_objdump(config):
    directory = _build_dir(config)
    objdump = _objdump()
    if not objdump:
        pytest.skip("binutils objdump not installed: no second reading to compare with")
    for path in _binaries(directory):
        listing = subprocess.run([objdump, "-p", path], capture_output=True, text=True, check=True).stdout
        with open(path, "rb") as handle:
            image = check.read_pe(handle.read())
        assert image.problems == [], path + _rebuild(config)
        assert image.imports == _objdump_imports(listing), path
        assert sum(len(functions) for _, functions in image.imports) > 50, path
