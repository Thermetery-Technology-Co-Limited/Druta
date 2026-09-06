# Portable Windows Vista SP2 x64 build

Extract **Druta-dev-vista-x64-portable.zip**, keep the complete
`Druta-Vista-Portable` folder together, and run `Druta.exe` inside it.
Python, Dear PyGui, Tomli, the VC++ runtime, the Universal CRT, and the
Direct3D shader compiler are included. Extracting the package and starting
the application do not automatically install dependencies, trust a driver
certificate or change Windows boot settings.

The Vista package uses the complete **41-DLL UCRT 10.0.10240.16384** set
from the Windows SDK, together with VC++ **14.29.30157.0** and
`D3DCompiler_47.dll` **10.0.28000.2705**. The newer UCRT 19041 set used by
the Windows 7 build fails to load on the Vista test VM. The separate Vista
collector and package checks enforce the exact tested DLL hashes.

This is a separate legacy build. It retains the same Control, Monitor and
Timings interface as the current application. It does not add NVIDIA driver
support for any GPU: use a card and driver that actually work on Vista.
Feature availability depends on the installed NVIDIA driver. Physical GPU
functions remain unverified on Vista; a VMware virtual adapter cannot validate NVIDIA
telemetry, clock/voltage controls, V/F curves or timing-register access.

## Required Windows components

- Windows Vista **Service Pack 2, x64**.
- Direct3D 11 from the Vista Platform Update, **KB971512** (part of KB971644).
  Hardware rendering needs a graphics driver exposing Direct3D feature level
  **10_0** or greater; the explicit WARP option is described below.
  The portable payload includes `D3DCompiler_47.dll`; core Windows
  graphics components such as `d3d11.dll`, `dxgi.dll` and `dwmapi.dll` still
  belong to the Windows image.
- The DLL-loader update **KB3063858**, or a superseding installed update,
  providing the secure library-search APIs used by CPython 3.8. Microsoft
  provides the [Vista x64 update](https://www.microsoft.com/en-us/download/details.aspx?id=47396).
- The NVIDIA display driver and its NVAPI/NVML libraries for GPU functions.
  These NVIDIA driver components are not bundled.
- For timings, a compatible nvtune executable and its signed, running kernel
  driver. A distribution prepared with `-NvtunePackageDirectory` includes
  nvtune beside Druta and the matching driver tools in `nvtune-driver/`.
  Druta discovers the adjacent executable automatically. Driver installation
  is an explicit separate action. Extraction and application startup do not
  install it or change Windows signing settings. See the included
  `nvtune-driver/README.txt` first. The package includes nvtune's GPL license,
  complete corresponding source ZIP and build manifest under `licenses/nvtune/`.

Microsoft documents [DirectX 11 on Vista through KB971512](https://support.microsoft.com/en-US/Windows/Hardware/Display-Graphics/how-to-install-the-latest-version-of-directx)
and [app-local UCRT deployment](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment).
The flat distribution places the complete SDK UCRT beside `Druta.exe`, as
required before Windows 8. A ZIP is the Vista distributable; the existing
Win7 self-extractor targets Windows 7 and is not used here.

## Why the Windows 7 binaries needed changes

Three narrowly scoped native source patches preserve the existing UI and
Python ABI:

| Component | Vista change |
| --- | --- |
| CPython 3.8.10 | Resolve `GetActiveProcessorCount` dynamically; use `GetSystemInfo` on Vista, which has no processor groups. Windows 7 and newer retain group-aware counting. |
| PyInstaller 6.16.0 bootloader | Compile for Windows 6.0 and `PSAPI_VERSION=1`, linking `psapi.lib`; replace Win7-only `K32` imports with the original PSAPI exports. |
| Dear PyGui 2.3.1 | Use Vista-compatible Win32 directory enumeration in Windows file dialogs; provide an explicit software-rendering option for VM validation. |

Microsoft's API documentation confirms that
[`GetActiveProcessorCount` requires Windows 7](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getactiveprocessorcount)
and [`FindExInfoBasic` is unavailable on Vista](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ne-minwinbase-findex_info_levels).
Only changing a PE version field would leave these failures intact.

The patches are under `tools/vista/` and are included with the distribution
in `licenses/Vista-runtime-changes`. That directory also contains a manifest
recording source revisions, patch hashes and hashes of the changed binaries.
These are Druta-specific modified builds, not upstream-supported Vista
releases of CPython, Dear PyGui or PyInstaller.

## Rebuild

Use a modern Windows build host. First prepare the official CPython 3.8.10
x64 environment with `requirements-vista.txt` and obtain the matched VC2019
**14.29.30157.0** files described in [WINDOWS7.md](WINDOWS7.md).
Collect Vista's exact SDK UCRT set and shader compiler separately:

```powershell
$kits = 'C:\Program Files (x86)\Windows Kits\10'
& C:\Python38\python.exe tools/collect_vista_redist.py --ucrt-directory "$kits\Redist\ucrt\DLLs\x64" --d3d-compiler "$kits\Redist\D3D\x64\d3dcompiler_47.dll" --sdk-license-directory "$kits\Licenses\10.0.19041.0" --sdk-license-directory "$kits\Licenses\10.0.28000.0" --output build/vista/portable-redist
```

Microsoft documents the unversioned SDK `Redist/ucrt/DLLs` directory for
[app-local deployment](https://devblogs.microsoft.com/cppblog/introducing-the-universal-crt/).
The collector requires all 41 matching x64 UCRT files, verifies their versions
and pinned hashes from `tools/vista/ucrt-10240.json`, preserves SDK licenses,
and writes a per-file provenance manifest. It rejects the Windows 7 set,
mixed versions and nonidentical existing output. Keep this collection separate
from `build/win7/portable-redist`.
Do not copy DLLs from the build host's System32 directory.

Clone these exact upstream versions into `build/vista/`:

```powershell
git clone --depth 1 --branch v3.8.10 https://github.com/python/cpython.git build/vista/cpython
git clone --depth 1 --branch zlib-1.2.11 https://github.com/python/cpython-source-deps.git build/vista/cpython/externals/zlib-1.2.11
git clone --depth 1 --branch v6.16.0 https://github.com/pyinstaller/pyinstaller.git build/vista/pyinstaller
git clone --depth 1 --branch v2.3.1 --recurse-submodules https://github.com/hoffstadt/DearPyGui.git build/vista/dearpygui
git -C build/vista/cpython apply "$PWD/tools/vista/cpython-vista.patch"
git -C build/vista/pyinstaller apply "$PWD/tools/vista/pyinstaller-vista.patch"
git -C build/vista/dearpygui apply "$PWD/tools/vista/dearpygui-vista.patch"
```

Build `PCbuild/pythoncore.vcxproj` in an x64 Visual Studio developer prompt.
The recorded build tools are MSVC **14.51.36231**, toolset **v145**
and Windows SDK **10.0.28000.0**. Only the rebuilt `python38.dll` is taken
from that build; its automatically copied modern VC runtime DLLs are not
distributed:

```bat
msbuild build\vista\cpython\PCbuild\pythoncore.vcxproj /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v145 /p:WindowsTargetPlatformVersion=10.0.28000.0 /p:IncludeExternals=true /p:KillPython=false /m /nologo
cd build\vista\pyinstaller\bootloader
C:\Python38\python.exe waf all --target-arch=64bit --no-tests
```

Build the patched Dear PyGui extension with the same CPython 3.8 headers and
import library using the [complete CMake recipe](tools/vista/README.md).
The binary package includes that recipe at `licenses/Vista-runtime-changes/README.md`.
Keep the baseline Python environment intact, then
assemble an isolated Vista build environment:

```powershell
& C:\Python38\python.exe tools/vista_runtime.py --python-directory C:\Python38 --cpython-source build/vista/cpython --pyinstaller-source build/vista/pyinstaller --dearpygui-source build/vista/dearpygui --dearpygui-extension build/vista/dearpygui/cmake-build-local/DearPyGui/_dearpygui.pyd --output build/vista/runtime
.\build-vista.ps1 -Python .\build\vista\runtime\python.exe -CrtDirectory build\vc2019 -PortableRuntimeDirectory build\vista\portable-redist
```

The assembly helper checks exact upstream revisions and patch contents,
rejects Win7-only static imports and newer executable loader minimums, and
never overwrites an existing Python environment. The spec checks the
manifest and native-file hashes again before packaging. The ordinary
Windows and Windows 7 build recipes retain their existing behavior.

To include the optional nvtune CLI and driver tools, add
`-NvtunePackageDirectory C:\build\nvtune-package` to `build-vista.ps1`.
`tools/package_vista_nvtune.py` requires its exact seven payload files plus
`licenses/nvtune/manifest.json`, checks all hashes, the Vista x64 PE minimums
and source provenance, and includes its license and complete source ZIP.
Packaging never executes the CLI, installer or driver.

## Verification

Run a startup probe from an extracted folder on the target OS:

```bat
Druta.exe --smoke-test --smoke-output C:\Druta\vista-smoke.json
```

Use a fresh report path and verify process exit code 0 plus a new report with
`"status": "passed"` and three rendered frames. Missing native DLLs can
prevent Python from starting and therefore produce no new JSON report.
This probe imports the runtime, parses bundled regulator profiles and draws
a real Dear PyGui window without opening a GPU or writing hardware settings.

The full-interface fixture is `tools/smoke_full_ui.py`; it renders all real
application tabs with NVIDIA backends unavailable, controls locked, and
callbacks, worker threads and subprocesses blocked. File-dialog directory
enumeration also needs testing on Vista because it exercises a different
native path from initial frames.

For a VM display driver that cannot create a hardware Direct3D 11 device,
the modified renderer offers an explicit process-local software option:

```bat
set DRUTA_D3D11_WARP=1
Druta.exe --smoke-test --smoke-output C:\Druta\vista-warp.json
```

The exact value `1` requests WARP. It still needs the Vista Direct3D 11
platform update and only changes GUI rendering. It does not emulate an
NVIDIA adapter or validate GPU tuning. Leave the variable unset for the
normal hardware renderer.

### Target VM results

Verified on Windows Vista Ultimate SP2 x64 **6.0.6002**, with VMware Tools
**11.0.6** and the VMware SVGA driver **8.16.07.0005**. The guest received no
Python, VC++ or UCRT installation: these dependencies came from the extracted
portable payloads. Its Windows image already had Direct3D 11 from KB971512
and the secure DLL-loader APIs; a separate KB3063858 installation was not
needed. This does not establish support for Vista images lacking those APIs.

| Check | Result |
| --- | --- |
| App-local native libraries with SDK UCRT 10240 | UCRT, VC++ and patched Python load; `Py_GetVersion` succeeds |
| Frozen startup | Exit 0; fresh passed report and three rendered frames |
| Real full-interface fixture | Control, Monitor and Timings constructed; 983 items, nine frames, no worker or subprocess attempts |
| Python compatibility checks | 52 tests under `tests/` and 12 profile checks passed in the guest |
| Unicode file dialog | Hardware window visibly renders; explicit WARP framebuffer shows Unicode folders/filenames, file sizes and dates |
| nvtune development driver | Corrected installer starts the driver from a path with spaces; all eight read-only native driver checks pass |
| nvtune CLI fixture | All 17 cases pass using the in-memory backend |

The early hardware framebuffer capture could be black before the VM renderer
settled, despite the visible window rendering correctly. The dialog fixture
allows warm-up time before capturing its framebuffer. Its JSON checks the
current path and default filename, while the image verifies listed entries;
it does not claim a completed user file selection.

The added SDK 10240 packaging guards also pass on the build host. Final
Windows 7 regression results for the same complete runtime set are recorded
separately. These VM checks do not validate physical NVIDIA telemetry,
clock/voltage tuning, V/F curve changes or timing-register reads and writes.
