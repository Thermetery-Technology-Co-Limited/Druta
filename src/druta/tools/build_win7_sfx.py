"""Wrap the portable directory in an x64 extraction-only 7-Zip executable.

Build helper, not an installer: the resulting EXE extracts files to a directory
chosen by the user. It never launches Druta or changes Windows settings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pefile


SOURCE_SHA256 = "9cbde5099c6deb73691b0579063da5827522ccbbcba3f0020fd04e8c8c16c0d4"
SOURCE_URL = "https://github.com/ip7z/7zip/releases/download/26.03/7z2603-src.tar.xz"
SYSTEM_IMPORTS = {"kernel32.dll", "user32.dll", "shell32.dll", "ole32.dll",
                  "oleaut32.dll", "advapi32.dll", "msvcrt.dll"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(bundle, seven_zip, stub, source, output):
    bundle, seven_zip, stub, source = [p.resolve(strict=True)
                                      for p in (bundle, seven_zip, stub, source)]
    output = output.resolve()
    if output.suffix.lower() != ".exe" or bundle in output.parents:
        raise ValueError("Output must be an EXE outside the portable bundle")
    for name in ("Druta.exe", "python38.dll", "ucrtbase.dll", "d3dcompiler_47.dll"):
        if not (bundle / name).is_file():
            raise ValueError("Expected flat portable bundle: missing " + name)
    if sha256(source) != SOURCE_SHA256:
        raise ValueError("Expected the unmodified official 7-Zip 26.03 source archive")
    with pefile.PE(str(stub)) as pe:
        if pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError("SFX module must be native x64; the stock 32-bit stub needs WOW64")
        header = pe.OPTIONAL_HEADER
        if (header.Subsystem != 2 or
                (header.MajorSubsystemVersion, header.MinorSubsystemVersion) > (6, 1)):
            raise ValueError("SFX must be a Windows 7-compatible GUI executable")
        imports = {entry.dll.decode("ascii").lower()
                   for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])}
        if not imports or imports - SYSTEM_IMPORTS:
            raise ValueError("SFX has unexpected runtime dependencies: " + repr(sorted(imports)))
    command = seven_zip / "7z.exe"
    for path in (command, seven_zip / "7z.dll", seven_zip / "License.txt"):
        if not path.is_file():
            raise ValueError("Missing 7-Zip build tool file: " + str(path))

    notices = bundle / "licenses" / "7-Zip"
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seven_zip / "License.txt", notices / "License.txt")
    shutil.copyfile(source, notices / "7z2603-src.tar.xz")
    (notices / "BUILD.txt").write_text(
        "Unmodified 7-Zip 26.03 SFXWin source: " + SOURCE_URL + "\n"
        "The full source archive and license are included in this directory.\n"
        "Built from an x64 MSVC/WDK command prompt, in CPP/7zip/Bundles/SFXWin:\n"
        "set _CL_=/GS\n"
        'nmake /nologo PLATFORM=x64 "CFLAGS_COMMON=-D_WIN32_WINNT=0x0601 '
        '-DWINVER=0x0601 -DZ7_WIN32_WINNT_MIN=0x0601" '
        '"LFLAGS=/SUBSYSTEM:WINDOWS,6.01 /DYNAMICBASE /FIXED:NO" "MY_FIXED="\n'
        "The vendor build uses /MT. This SFX extracts files only; no automatic launch.\n"
        "Replace the SFX stub to relink this archive with a modified 7-Zip build.\n",
        encoding="utf-8")
    # 7z 'a' updates existing archives; remove only our explicitly named output
    # file so an older payload cannot leave stale files in the new distribution.
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    subprocess.run([str(command), "a", "-t7z", "-mx=7", "-mmt=2",
                    "-sfx" + str(stub), str(output), bundle.name],
                   cwd=str(bundle.parent), check=True)
    subprocess.run([str(command), "t", str(output)], check=True)
    report = {"status": "passed", "output": str(output),
              "size": output.stat().st_size, "sha256": sha256(output),
              "stub_sha256": sha256(stub), "source_sha256": SOURCE_SHA256,
              "source_url": SOURCE_URL, "stub_imports": sorted(imports),
              "mode": "extract only; does not launch or install"}
    output.with_suffix(".manifest.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--seven-zip-directory", type=Path, required=True)
    parser.add_argument("--sfx-module", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build(args.bundle, args.seven_zip_directory, args.sfx_module,
                       args.source_archive, args.output)
    except (OSError, ValueError, pefile.PEFormatError, subprocess.CalledProcessError) as exc:
        parser.exit(1, "SFX build failed: " + str(exc) + "\n")
    print(json.dumps(report, indent=2))
