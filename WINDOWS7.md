# Windows 7 SP1 x64

For a package that also carries the UCRT and shader compiler, see
[the portable Windows 7 build](https://github.com/Thermetery-Technology-Co-Limited/Druta/blob/codex/windows-7-support/PORTABLE-WINDOWS7.md).
The compact distribution described below keeps those as Windows prerequisites.

Use the separate `Druta-dev-win7-x64.zip` distribution. Extract the entire
`Druta-Win7` folder and run `Druta.exe`; keep `_internal` beside it.
Python does not need to be installed to run the bundle. The regular Windows
build uses Python 3.14 and cannot run on Windows 7.

## Prerequisites

- Windows 7 **SP1**, **64-bit**. This is not a 32-bit build.
- Windows loader update **KB2533623**, or an update that supersedes it
  (for example **KB3063858**).
- Universal C Runtime **KB2999226**, or a superseding update.
- Direct3D shader compiler **KB4019990** (`D3DCompiler_47.dll`).
- A working Direct3D 11 graphics driver supporting feature level **10_0** or
  newer. For hardware rendering in a VM, install its vendor's guest display
  driver and enable 3D acceleration.
- For monitoring/tuning: an NVIDIA card with a Windows 7 driver for that card.
  Windows 7 support in Druta does not add Windows 7 drivers for newer GPUs.

The bundle includes a matched Visual C++ 2019 runtime. It deliberately does
not bundle this build host's system UCRT or shader compiler. Windows updates
can be obtained from the [Microsoft Update Catalog](https://www.catalog.update.microsoft.com/).
Install the servicing-stack and SHA-2 updates required by any later updates
or guest drivers before installing those packages.

## Native DLL dependencies

| Provider | DLLs |
| --- | --- |
| Included in the bundle | `python38.dll`, `libffi-7.dll`, Dear PyGui's `_dearpygui.pyd`, and Python's collected `.pyd` extension modules |
| Included, matched VC++ 2019 runtime | `msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll`, all **14.29.30157.0** |
| Windows graphics stack | `d3d11.dll`, `dxgi.dll`, `d3dcompiler_47.dll`, `dwmapi.dll` |
| Windows Universal CRT | `ucrtbase.dll` and the `api-ms-win-crt-*` forwarding DLLs, supplied by KB2999226 or a superseding update |
| Installed NVIDIA driver, for GPU functions | Dynamically loaded `nvapi64.dll` and `nvml.dll`; not redistributed in this bundle |

The remaining imported Windows system libraries are `advapi32.dll`,
`comctl32.dll`, `gdi32.dll`, `imm32.dll`, `iphlpapi.dll`, `kernel32.dll`,
`ole32.dll`, `oleaut32.dll`, `shell32.dll`, `shlwapi.dll`, `user32.dll`,
`version.dll`, and `ws2_32.dll`. These come with Windows 7.
Python `.pyd` files are native DLLs with Python module entry points.
`shcore.dll` and newer DPI functions are optional: Windows 7 uses the
`user32.dll`/`gdi32.dll` fallback instead.

## Build

Build on a current Windows host with PowerShell 5.1 or newer. Use the official
[CPython 3.8.10 x64 installer](https://www.python.org/downloads/release/python-3810/)
and a separate build environment. Install the pinned requirements:

```powershell
& C:\Python38\python.exe -m pip install -r requirements-win7.txt
```

Obtain the x64 `msvcp140.dll`, `vcruntime140.dll`, and `vcruntime140_1.dll`
from Microsoft's [Visual C++ 2019 redistributable](https://aka.ms/vs/16/release/vc_redist.x64.exe),
version **14.29.30157.0**, or its corresponding Visual Studio redist directory.
The included extraction helper checks the official installer's SHA-256 and
extracts only the required DLLs and license without installing the runtime:

```powershell
New-Item -ItemType Directory -Path build -Force
Invoke-WebRequest https://aka.ms/vs/16/release/vc_redist.x64.exe -OutFile build\vc_redist.x64.exe
& C:\Python38\python.exe tools\extract_win7_crt.py build\vc_redist.x64.exe build\vc2019
```

Keep them together in a build-only folder. The build recipe also verifies the
exact DLL versions and architecture and replaces all CRT copies collected by
PyInstaller. Then build:

```powershell
.\build-win7.ps1 -Python C:\Python38\python.exe -CrtDirectory build\vc2019
```

Output: `dist\Druta-dev-win7-x64.zip`. The normal `build.ps1` and
`requirements.txt` continue to build the current-Windows distribution.

The Windows 7 bundle contains CPython 3.8.10, Dear PyGui 2.3.1, Tomli 2.0.1,
PyInstaller 6.16.0's bootloader, and
the matched Microsoft CRT. The `licenses` directory carries the actual
licenses from this build's Python and package distributions;
`BUILD-DEPENDENCIES.txt` records its installed build dependencies. Python 3.8
and Windows 7 are legacy runtimes; the separate build keeps the current build
on its existing Python version.
Druta has no TLS/network feature; the legacy bundle excludes Python's unused
`ssl`, `_ssl`, and `_hashlib` modules and does not redistribute OpenSSL 1.1.1.
Python's built-in hashlib implementations remain available.

To run from source on Windows 7, install CPython 3.8.10 x64 and Microsoft's
Visual C++ 2019 x64 redistributable, then install just the runtime packages:

```powershell
python -m pip install dearpygui==2.3.1 tomli==2.0.1
python druta.py
```

## Shared feature update (2026-09-07)

The draft includes the shared Druta 1.3.0 profile and sign-in workflow,
Kepler/Maxwell controls, automatic NCP4206 and MP2888A discovery and verification,
and the GTX 690 and GTX 745 (GM107 DDR3) clock-domain decoding. The ROM
decoder accepts the observed Maxwell performance-table format, and uncertain
clock identities retain their inferred labels. The GTX 770 cross-check resolves
Kepler domain 16 as XBAR2CLK, 17 as SYS2CLK and 25 as L2C2CLK using
distinct held-P0 ROM matches. Windows 7 retains its Python 3.8,
legacy NVML export selection, TOML parser and DPI fallbacks. Suppressed features
remain unavailable in Druta and therefore not shown; a new label does not
make an unverified register writable.

The sign-in shutdown check normalizes Windows event timestamps from seven
fractional digits to six for Python 3.8 while preserving the full boot ID.
Missing or inconclusive shutdown evidence still prevents automatic loading.

On the Windows 10 build host, the propagated source passed 372 root-level
unit tests and 42 legacy tests with Python 3.8.10. The source startup smoke
rendered three frames; the full-interface fixture rendered Control, Monitor
and Timings (980 items, nine frames) with no worker or subprocess attempts.
These checks do not repeat the Windows 7 guest or physical-GPU validation
recorded below. GPU measurements in `DRIVER-COMPATIBILITY.md` are scoped to
the Windows 10 hardware runs; they are not Windows 7 hardware claims.

## Verification

Run this on the target Windows 7 installation:

```powershell
$reportPath = Join-Path (Get-Location) 'smoke.json'
if (Test-Path -LiteralPath $reportPath) { Remove-Item -LiteralPath $reportPath }
$process = Start-Process .\Druta.exe -ArgumentList "--smoke-test --smoke-output `"$reportPath`"" -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Druta exited with code $($process.ExitCode)." }
if (-not (Test-Path -LiteralPath $reportPath)) { throw 'Druta produced no smoke report.' }
$report = [System.IO.File]::ReadAllText($reportPath)
if ($report -notmatch '"status"\s*:\s*"passed"') { throw 'Druta did not finish the smoke test.' }
Write-Output $report
```

The smoke test imports runtime modules, parses bundled regulator profiles,
and renders a short Dear PyGui window, without opening a GPU or changing
hardware settings. A successful JSON result proves those startup paths work;
it does not validate NVAPI/NVML or GPU tuning on the guest's virtual adapter.
Use a fresh report and check the process exit code: a missing native DLL can
prevent Python from starting and therefore cannot produce a new JSON report.

The developer-only full-interface fixture renders all three real application
tabs with NVIDIA interfaces unavailable, controls locked, and callbacks,
workers and subprocesses disabled. From a source checkout:

```powershell
python tools\smoke_full_ui.py --output full-ui.json --hold-seconds 3
```

The optional hold allows screenshots of each tab; the fixture is visibly
labelled as an offline test and supplies no invented sensor readings.

For a real NVIDIA Windows 7 machine, also check `--list-gpus`, monitor
telemetry, card selection, V/F curve reading, and optional timing-tool
availability. Feature support remains dependent on the installed NVIDIA
driver. Existing control locks and confirmations apply to all write paths.

### Validated configuration and results

Verified on 2026-09-06 UTC. The target was Windows 7 Ultimate SP1 x64,
build **6.1.7601**, in VMware Player 17 with VMware Tools **12.1.0** and
the VMware SVGA 3D **9.17.4.1** driver. A native Direct3D probe successfully
created a hardware device at feature level **11_0**. The rendering tests used
that driver without a WARP override or a modified Dear PyGui binary.

| Environment | Check | Result |
| --- | --- | --- |
| Windows 10 build host, Python 3.8.10 and 3.14.4 | Tests under `tests/` | All 35 tests pass on each interpreter, including 20 nvtune wrapper regressions |
| Windows 10 build host, Python 3.8.10 | Source and frozen startup smoke tests | Both pass |
| Windows 7 SP1 x64 | Application imports, loader APIs, UCRT and Direct3D libraries | All load successfully |
| Windows 7 SP1 x64 | Frozen `Druta.exe --smoke-test` | Exit 0; fresh passed report; one regulator profile and three rendered frames |
| Windows 7 SP1 x64 | Full-interface source fixture | Exit 0; Control, Monitor and Timings render; 967 items and nine frames; no forbidden worker or subprocess attempts |
| Windows 7 SP1 x64 without NVIDIA hardware | GPU enumeration and ordinary startup | Report the missing NVIDIA backend and exit cleanly with code 1 |

The target had KB4490628, KB4474419, KB2999226, KB4019990 and KB2670838
installed. The loader APIs `AddDllDirectory`, `SetDefaultDllDirectories` and
`RemoveDllDirectory` were verified directly. A basic VGA adapter without a
working Direct3D driver cannot render the interface.

These VM checks validate startup, profile parsing and interface rendering.
Physical NVIDIA telemetry, V/F curve access, timing register access and tuning on
Windows 7 still require testing with an NVIDIA card and its Windows 7 driver.
The full-interface fixture deliberately provides no sensor readings and does
not exercise hardware write paths.

## nvtune on Windows 7

nvtune remains a separate installation. Use its Windows 7 x64 build and
matching `nvtunedrv.sys`, then select **Device -> Locate nvtune...** in Druta.
The driver must be installed and running under the signing requirements in
[nvtune's Windows 7 guide](https://github.com/sebastianmarrufo/nvtune/blob/codex/windows-7-support/WINDOWS7.md).
Druta does not install the driver or change test-signing settings.

Timing previews require an nvtune build supporting **`set --dry-run`** and
**`--commit`**. Older upstream builds write on a bare `set`; Druta now always
passes `--dry-run` for previews, checks the successful completion marker,
and refuses an incompatible or incomplete response without retrying a bare
`set`. Approved writes pass `--commit`. Failed commands and missing readback
values are reported as failures rather than hardware rejection.

The tested static mingw-w64 nvtune executable imports only `ADVAPI32.dll`,
`KERNEL32.dll`, `msvcrt.dll` and `SETUPAPI.dll`; it needs no extra GCC or MSVC
runtime DLLs. Its kernel driver imports `ntoskrnl.exe` and `HAL.dll`.
This is separate from Druta's Python, graphics and NVIDIA DLL dependencies.

On the Windows 7 guest, nvtune's help and field-table commands exit 0;
Druta parses all 33 fields. Its explicit preview fails cleanly on the
non-NVIDIA virtual adapter without retrying a write command. The test-signed
driver loaded and passed read-only IOCTL and administrator ACL checks.
All 17 nvtune CLI regression cases pass on the guest using an in-memory
backend. This covers the integration contract and Windows 7 execution;
real NVIDIA timing-register reads, writes and restore remain unverified.

## Compatibility changes

Python 3.8 uses Tomli for regulator profiles (newer Python uses `tomllib`).
Display scaling falls back to the Windows 7 device-context API. NVML loads
from either System32 (DCH drivers) or the Standard driver's
`%ProgramW6432%\NVIDIA Corporation\NVSMI` directory, as documented by
[NVIDIA](https://docs.nvidia.com/deploy/nvml-api/nvml-api-reference.html).

## Legacy-driver update

The current shared Druta fixes have been ported to this branch while retaining
CPython 3.8.10, Tomli, Windows 7 DPI handling, the pinned native runtime and the
explicit nvtune preview/commit contract.

DLL selection is automatic: Druta resolves the actual Windows system and Program
Files directories through Windows APIs, then tries only those installed-driver
locations. The loader update listed above is still required. The package does
not contain NVIDIA DLLs. Available exports and successful reads select older
clock, fan and telemetry paths; PCI enumeration can fall back to the older V2
record without changing the selected physical slot.

The TITAN private voltage-write profiles remain limited to the measured exact
board, VBIOS and driver combinations (472.12 and 580.97). Porting them to Windows 7
does not confirm a new driver or operating-system hardware combination. Ordinary
API controls retain their capability checks. Zero RPM does not hide a functioning
fan controller, including when an RTX is on a water loop.

The port also includes V/F switch and incomplete-curve defenses, Pascal raw-unit
corrections, PCI-targeted CUDA load, per-fan profile restoration, the Additional
Memory Clock Offset label, and the requested 1200/1500 mV normal/XOC software
voltage bounds with +200/+500 mV NVVDD offset bounds. These are UI request ranges,
not claims that the card can reach those voltages.

Both Windows 7 build variants now carry their exact public source under
`source/`, including OS build tools and regression tests. `SOURCE-MANIFEST.json`
records source and executable SHA-256 hashes. Builds reject edits made during
compilation instead of silently shipping mismatched source.

Shared-driver hardware results in [DRIVER-COMPATIBILITY.md](DRIVER-COMPATIBILITY.md)
were measured on Windows 10. Windows 7 VM checks establish runtime and UI
compatibility only; physical NVIDIA tuning on Windows 7 remains a separate
hardware validation requirement.

### Current port: guest verification

The propagated legacy-driver update was verified on 2026-09-06 in the actual
Windows 7 Ultimate SP1 x64 guest (6.1.7601), using CPython 3.8.10 and VMware
SVGA 3D driver 9.17.4.1. The VM is configured for **8192 MB**; the guest reports
**8,589,402,112 bytes** of physical memory. Display scaling is 150%.

| Check | Fresh guest result |
| --- | --- |
| Shared regression suite | 157 tests pass |
| Windows compatibility, nvtune contract and build/package tests | 42 tests pass; pefile was installed for the build-tool tests |
| Compact and portable frozen EXEs | Both exit 0, parse the regulator profile and render three smoke-test frames |
| Source and both frozen variants without NVIDIA hardware | Enumeration and ordinary startup exit 1 with missing-backend diagnostics; NVML reports both trusted driver locations |
| Native x64 portable self-extractor | Extraction exits 0; the extracted EXE passes its three-frame smoke test |
| Full application interface fixture | Control, Monitor and Timings render successfully: 985 items, nine frames, no worker or subprocess attempts |

The rendered Monitor tab was also captured in the guest. The fixture window
extends beyond this guest's 1024 by 768 desktop at 150% scaling; this observation
establishes rendering, not a fit-to-small-desktop layout claim. These updated
results supersede the earlier test counts above. The VM uses a virtual display
adapter, so this run does not validate physical NVIDIA telemetry or tuning.
