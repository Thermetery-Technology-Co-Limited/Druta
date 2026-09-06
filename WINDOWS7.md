# Windows 7 SP1 x64

Use the separate `Druta-dev-win7-x64.zip` distribution. Extract the entire
`Druta-Win7` folder and run `Druta.exe`; keep `_internal` beside it.
Python does not need to be installed to run the bundle. The regular Windows
build uses Python 3.14 and cannot run on Windows 7.

## Prerequisites

- Windows 7 **SP1**, **64-bit**. This is not a 32-bit build.
- Windows loader update **KB2533623**, or an update that supersedes it.
- Universal C Runtime **KB2999226**, or a superseding update.
- Direct3D shader compiler **KB4019990** (`D3DCompiler_47.dll`).
- A working Direct3D 11 graphics driver. A VM needs its vendor's guest display
  driver and 3D acceleration. A basic VGA adapter is not a useful GPU test.
- For monitoring/tuning: an NVIDIA card with a Windows 7 driver for that card.
  Windows 7 support in Druta does not add Windows 7 drivers for newer GPUs.

The bundle includes a matched Visual C++ 2019 runtime. It deliberately does
not bundle this build host's system UCRT or shader compiler. Windows updates
can be obtained from the [Microsoft Update Catalog](https://www.catalog.update.microsoft.com/).
Install the servicing-stack and SHA-2 updates required by any later updates
or guest drivers before installing those packages.

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

Validation results for this change will be recorded here after guest testing.

## Compatibility changes

Python 3.8 uses Tomli for regulator profiles (newer Python uses `tomllib`).
Display scaling falls back to the Windows 7 device-context API. NVML loads
from either System32 (DCH drivers) or the Standard driver's
`%ProgramW6432%\NVIDIA Corporation\NVSMI` directory, as documented by
[NVIDIA](https://docs.nvidia.com/deploy/nvml-api/nvml-api-reference.html).
