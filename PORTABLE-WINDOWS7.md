# Portable Windows 7 x64 distribution

`Druta-dev-win7-x64-portable.exe` is one file to distribute. Run it to extract
`Druta-Win7-Portable`, then run `Druta.exe` inside that folder. The extractor
does not install software, launch Druta, or change Windows settings.
Keep the entire extracted folder together. A ZIP of the portable application
is also available as `Druta-dev-win7-x64-portable.zip`.

This build carries CPython 3.8.10, Dear PyGui, Tomli, the matched VC++ 2019
runtime, the complete 42-DLL SDK x64 UCRT set, and `D3DCompiler_47.dll`.
Python, .NET, the VC redistributable, the UCRT update and the shader-compiler
update do not need separate installation for this payload. Microsoft permits
[app-local UCRT deployment](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment)
and [side-by-side shader compiler deployment](https://learn.microsoft.com/en-us/windows/win32/directx-sdk--august-2009-).
Before Windows 8 the UCRT belongs beside the main EXE, so this distribution
uses a flat layout instead of `_internal`.

## What the Windows image must still provide

- Windows 7 SP1 x64 and the loader functions/flags from KB2533623 or a
  superseding update such as KB3063858. CPython's extension loader needs them.
- Core Windows libraries, including USER32/GDI32, COMCTL32, ADVAPI32, SHELL32,
  SHLWAPI, VERSION, OLE32/OLEAUT32, WS2_32, IPHLPAPI and RPCRT4.
- `d3d11.dll`, `dxgi.dll` and `dwmapi.dll`, plus a working Direct3D 11 display
  driver supporting feature level 10_0 or newer. Removing `dwmapi.dll` is
  different from disabling desktop composition.
- For GPU functions, a supported NVIDIA card and its matching Windows 7
  driver, including its NVAPI/NVML components.
- For memory timing functions, the separate nvtune tool and kernel driver,
  installed and signed as described in [WINDOWS7.md](WINDOWS7.md).

This is self-contained for the application runtimes, not for arbitrary
removed Windows components. It does not need WOW64: Druta, its DLLs and the
self-extractor are native x64. A particular stripped XOC image still needs
testing; missing Windows APIs or graphics components cannot be restored by
placing random DLLs beside the EXE.

## Validation

On Windows 7 Ultimate SP1 x64 (6.1.7601) the portable frozen application
exited 0 and rendered three smoke-test frames. Live module enumeration of
that actual process confirmed that UCRT, all three VC runtime DLLs and
`D3DCompiler_47.dll` loaded from the portable application directory.
The exact SDK shader compiler also loaded by absolute path and compiled a
minimal shader successfully. The x64 extraction stub extracted a nested
sample archive with exact file hashes and exit status 0.

These tests used the existing updated Windows 7 VM without removing system
DLLs or updates. They establish use of the bundled runtimes; they do not
certify every stripped image or physical NVIDIA timing operation.

## Rebuild the portable payload

Use the Python, pinned packages and matched CRT from [WINDOWS7.md](WINDOWS7.md).
Collect the tested redistributables from installed SDK redist directories:

```powershell
$kits = 'C:\Program Files (x86)\Windows Kits\10'
python tools\collect_win7_redist.py --ucrt-directory "$kits\Redist\10.0.19041.0\ucrt\DLLs\x64" --d3d-compiler "$kits\Redist\D3D\x64\d3dcompiler_47.dll" --sdk-license-directory "$kits\Licenses\10.0.19041.0" --sdk-license-directory "$kits\Licenses\10.0.28000.0" --output build\portable-redist
.\build-win7.ps1 -Python C:\Python38\python.exe -CrtDirectory build\vc2019 -PortableRuntimeDirectory build\portable-redist
```

The helper pins UCRT 10.0.19041.5609 and shader compiler 10.0.28000.2705,
checks x64 PE files and the complete DLL set, and records versions, sources
and SHA256 hashes. It rejects Windows system directories, untested versions,
and nonidentical existing output. SDK license files are preserved. The spec
rechecks the manifest hashes before packaging. The compact build command
without `-PortableRuntimeDirectory` retains its existing behavior.

## Rebuild the single distributable EXE

Obtain 7-Zip 26.03 and its exact source archive from the
[official downloads](https://www.7-zip.org/download.html). Its stock `7z.sfx`
is 32-bit even in the x64 MSI, so compile SFXWin for x64 instead. From an x64
MSVC/WDK developer prompt, inside the unmodified source's
`CPP\7zip\Bundles\SFXWin` directory:

```bat
set _CL_=/GS
nmake /nologo PLATFORM=x64 "CFLAGS_COMMON=-D_WIN32_WINNT=0x0601 -DWINVER=0x0601 -DZ7_WIN32_WINNT_MIN=0x0601" "LFLAGS=/SUBSYSTEM:WINDOWS,6.01 /DYNAMICBASE /FIXED:NO" "MY_FIXED="
```

The tested compiler was MSVC 14.51.36231. The vendor build uses `/MT`; this
stub imports only KERNEL32, USER32, SHELL32, OLE32 and OLEAUT32. Package it:

```powershell
python tools\build_win7_sfx.py --bundle dist\Druta-Win7-Portable --seven-zip-directory C:\build-tools\7-Zip --sfx-module C:\build-tools\7z-x64.sfx --source-archive C:\build-tools\7z2603-src.tar.xz --output dist\Druta-dev-win7-x64-portable.exe
```

The helper checks the stub's x64 architecture, PE minimum and DLL imports,
includes the complete 7-Zip source/license and relink instructions, and
tests archive integrity. It writes an adjacent manifest with artifact and
stub hashes. Use SFXWin, the extraction-only target, not an installer stub.
