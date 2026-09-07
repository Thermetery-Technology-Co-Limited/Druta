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

## NVIDIA driver compatibility port

This branch includes the current driver compatibility fixes: trusted NVML
locations, optional older NVML exports, PCI-addressed GPU pairing and CUDA
loads, clock/fan fallbacks, V/F curve validation and GPU switching, whole-bin
ramp staging, profile restoration, and the **Additional Memory Clock Offset**
label. The Vista-specific runtime, file dialogs and explicit nvtune
`--dry-run` / `--commit` safeguards remain in place.

NVIDIA states that [Vista support was deprecated in Release 367.xx](https://nvidia.custhelp.com/app/answers/detail/a_id/4373/kw/applications).
Its [365.19 WHQL driver](https://www.nvidia.com/en-us/drivers/details/102379/)
was released on May 13, 2016 and explicitly lists Vista x64; 472.12 is a
Windows 7/8/8.1 release. A Windows 10 test of 472.12 does not establish
Vista driver support. The TITAN RTX and TITAN Xp results in
[DRIVER-COMPATIBILITY.md](DRIVER-COMPATIBILITY.md) concern those measured
cards and drivers on the modern test host, not this Vista package.

The official signed 365.19 package was downloaded and extracted for a
[static DLL export audit](experiments/vista-driver-36519-exports.json).
NVML, NVAPI and CUDA are x64 PE files with minimum subsystem 6.0.
NVML has 130 exports and exposes PCI-info V2, but not V3, indexed fan APIs,
clock-offset APIs or GPU frequency-lock APIs. Druta uses the compatible
PCI record and guards missing functions; unavailable readings stay unknown.
The CUDA functions used by the load worker and NVAPI's query entry point
exist in these files. Export presence does not prove a function works on a
particular GPU. No NVIDIA driver binary is included in the Druta package.

Per-rail voltage writes retain their measured card/VBIOS/driver profiles.
The 365.19 driver has no validated rail-write profile, so unconfirmed TITAN
rail writes remain disabled even when the write toggle is selected. Physical
NVIDIA monitoring, V/F operations, clock/fan fallbacks and timing access on
Vista still require testing with a supported card and its installed driver.

Earlier port validation passed 158 application tests and 54 compatibility/package tests
under the patched Python 3.8.10 runtime on the modern build host. Its source
startup probe and full Control/Monitor/Timings fixture pass with all GPU
backends, workers and subprocesses disabled (985 items, nine rendered frames).
The fresh guest checks and earlier runtime validation are recorded separately below.

## Shared-feature refresh (2026-09-07)

This draft now inherits the shared application through `4f90922` and the
Windows 7 compatibility refresh. It includes complete rail/I2C profile
restoration, guarded sign-in loading, Kepler/Maxwell capability handling,
verified-board P0 hold controls, automatic NCP4206/MP2888A discovery and
session-bound verification, and the GTX 690 ROM/clock-domain decoder. The
[clock evidence](experiments/kepler-gtx690-clock-domains.md) distinguishes
confirmed identities from inferred names and the unresolved XBAR/SYS pair.
Hardware controls retain their driver/capability checks; shared Windows 10
measurements do not establish physical GPU support on Vista.

The sign-in guard now normalizes Windows event timestamps with seven
fractional digits for Python 3.8 while preserving the full boot identifier.
Missing or abnormal shutdown evidence still prevents automatic profile
loading. No startup task was registered during this port validation.

Current validation on the Windows 10 build host, using the patched Vista
Python 3.8.10 runtime: **359 application tests and 57 compatibility/package
tests pass (416 total)**. Source startup renders three frames, and the full
UI fixture renders 980 items across all three tabs and nine frames with no
GPU, worker or subprocess access. Native runtime hashes/import constraints
and the matching-source packaging preflight pass. The native Vista patches,
SDK 10240 runtime, Unicode dialog behavior and optional nvtune packaging are
preserved. These refreshed application sources have not been rerun in the
Vista guest or on physical Vista NVIDIA hardware; earlier guest results below
remain historical evidence.

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

Each portable build includes `source/` beside `Druta.exe`, containing the
exact public working-tree source, tests, profiles and Vista runtime patches.
`source/SOURCE-MANIFEST.json` records each file's SHA-256 and the EXE hash.
The build rejects source changes during compilation. Private probe dumps,
profiles/autosaves, `.git`, build inputs and NVIDIA DLLs are excluded.

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

### Driver-compatibility port: fresh guest results (September 6, 2026)

The updated package was retested on Vista Ultimate SP2 x64 **6.0.6002**,
now assigned **8 GB RAM**, with VMware SVGA driver **8.16.07.0005**.
The 158 application tests and 54 compatibility/package tests passed under
patched Python 3.8.10. Frozen `--smoke-test` exited 0 with a fresh passed
report and three rendered frames. `--list-gpus` and normal startup both
exited 1 with a clear unavailable-NVIDIA result on the VMware adapter.
The full-interface fixture constructed Control, Monitor and Timings and
completed nine frame-loop iterations and all three tab selections, with 983
items at guest DPI scale 1.0 and no forbidden worker/subprocess attempts.
This is a construction/frame-loop smoke result, not a visual full-UI pass.

The guest was signed out during this run. The noninteractive VMware process
session can render with WARP, but it cannot establish interactive hardware
rendering or completed user interactions. Its hardware file-dialog framebuffer
was blank and was rejected by the fixture. The explicit WARP file-dialog
framebuffer passed after 89 frames; visual inspection confirmed
`subfolder-café`, `file-café.txt` and `visible-ascii.txt`. A separate
instrumented full-UI capture produced blank 1×1 images and was rejected as
visual evidence. Current interactive hardware rendering and full-UI visuals
remain unverified in this signed-out session. The
[sanitized validation record](experiments/vista-port-validation-20260906.json)
keeps these results separate.

### Earlier Vista runtime validation

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
| Python compatibility checks | 57 tests under `tests/` and 12 profile checks passed in the guest |
| Unicode file dialog | Hardware window visibly renders; explicit WARP framebuffer shows Unicode folders/filenames, file sizes and dates |
| nvtune development driver | Corrected installer starts the driver from a path with spaces; all eight read-only native driver checks pass |
| nvtune CLI fixture | All 17 cases pass using the in-memory backend |

On this Vista VMware adapter, hardware framebuffer readback remained black
after warm-up even though the visible window rendered correctly. A VMware
desktop capture verified Unicode entries, sizes and dates in that hardware
window; the explicit WARP framebuffer capture also passed. The dialog fixture
now rejects blank captures, so its hardware capture check reports this
limitation rather than a false pass. Neither check claims a completed user
file selection.

The identical portable payload also passed on Windows 7 Ultimate SP1 x64
(6.1.7601): frozen startup, the full UI (985 items), all 69 Python checks,
the hardware Unicode framebuffer capture, and all 17 nvtune CLI cases.
The same nvtune driver and native installer passed all eight read-only checks
on both guests. Temporary services and certificates were removed, the original
boot settings restored, and test signing confirmed off after reboot.
These VM checks do not validate physical NVIDIA telemetry,
clock/voltage tuning, V/F curve changes or timing-register reads and writes.
