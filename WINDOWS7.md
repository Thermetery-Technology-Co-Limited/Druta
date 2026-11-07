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
& C:\Python38\python.exe src\druta\tools\extract_win7_crt.py build\vc_redist.x64.exe build\vc2019
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

## Shared feature update (2026-09-23)

The draft now carries the shared Druta 1.6.0 source, including its
generation-aware current controls, NCT3933U and MP29816 rail support,
per-adapter I2C discovery and scans, memory timing profiles, GPU recovery and
the public nvtune release support described below. Application modules live in
`src\druta` and tests in `tests`; `python druta.py` still launches a source
checkout. The Windows 7 build helpers are under `src\druta\tools`. The
pip-installable package declares Python 3.11 or newer; the Windows 7 build
packages the same source with CPython 3.8.10 and PyInstaller instead.

Python 3.8 again imports every module the application uses: annotations that
3.8 cannot evaluate are deferred, the nvtune fingerprint falls back from
`hashlib.file_digest` to an equivalent chunked SHA-256, and regulator profiles
keep the Tomli fallback. Newer Python versions keep their existing behavior.

On the Windows 10 build host, the merged source passed 1,204 tests with Python
3.8.10; the three wheel and sdist tests skip below Python 3.11. It passed 1,207
tests with Python 3.14.4, and 1,200 with Python 3.11.5 without pefile, where
the seven runtime-collector tests skip. One real-renderer layout test was not
run on this host. The current, compact Windows 7 and portable Windows 7 builds
were rebuilt from the committed source. Each frozen `Druta.exe --smoke-test`
exited 0, parsed both bundled regulator profiles and rendered three frames.
The full-interface fixture, the Windows 7 guest and the physical-GPU checks
recorded below were not repeated for this update.

A follow-up makes three behaviors match the shared build. An `nvtune fields`
run that exits nonzero but still prints a table is parsed. A native nvtune
preview that exits 0 is accepted without the completion marker or the extra
row checks, and its warnings are classified as in the shared build, as
described under nvtune below. An NVML GPU whose PCI record
cannot be read stays listed with a blank slot instead of being dropped. The
export-based fallbacks for older NVML are unchanged. With these changes the
source passed 1,209 tests with Python 3.8.10 (the same three skips) and 1,212
with Python 3.14.4 on the build host. The compact and portable Windows 7
builds were then rebuilt from commit `8ff7fdd`. Each frozen
`Druta.exe --smoke-test` exited 0, parsed both bundled regulator profiles and
rendered three frames.

## Shared feature update (2026-10-19)

The draft now carries the shared Druta 1.8.1 source (main `81094be`, through
#37). From 1.7.0 it gains Ampere rail-limit and clock-domain controls,
voltage-limit headroom for V/F holds and the measured clock on the Monitor
tab. From 1.8.0 it gains the Ada controls, uP9512R I2C voltage offsets, the
EVGA RTX 3090 Ti Kingpin iCX controls and presets, graphics V/F shape
preservation and the Pascal frequency-slider hide. From 1.8.1 it gains the
MP2888A explicit PMBus method beside the existing offset method. Each keeps
main's capability, packet, identity and readback checks. The measurements in
the release notes come from the cards and drivers named there; none of them
is a Windows 7 hardware result. 1.7.0's power-policy sliders and the Ampere
and Ada core-current sliders (from 1.7.0 and 1.8.0) are carried too, but
Windows 7 cannot run them, as described below.

Two new code paths needed Python 3.8 changes. Saving an iCX preset called
`Path.write_text(newline=)`, which is Python 3.10+, so it raised before
writing anything; it now writes through `open()` with the same UTF-8 encoding
and LF newlines. The Turing frequency-lock check passed `strict=True` to
`zip()`, which 3.8 rejects whenever both legacy records are present; the
lengths are checked first, so a plain `zip()` gives the same result. Four new
MP2888A and uP9512R test modules use the Tomli fallback, and the source
package lists the 18 public files main's build now publishes. Every new NVML,
NVAPI and CUDA call goes through the existing export-presence guards, and the
Windows 7 spec needs no new modules, data or excludes.

Three shared features depend on Windows APIs that Windows 7 lacks. None of
them gets a refusal that main does not have:

- **Current limits and power policies.** Every current-limit slider (Pascal,
  Turing and Blackwell since the 1.6.0 port, Ampere and Ada core current since
  1.7.0 and 1.8.0) and 1.7.0's power-policy list read and write the driver's
  policy table through an adapter handle from `gdi32`'s `D3DKMTEnumAdapters2`.
  Windows 7 does not export it, so these controls stay unavailable here and
  report why. Until this update that missing export counted as a failed read:
  every profile saved on those cards was incomplete, so sign-in loading could
  not be enabled, and Reset all always recorded a failed current-limits step.
  It now counts as an unsupported capability, as main does for a policy
  interface it does not understand. Rail limits and V/F hold headroom use a
  different path and are unaffected.
- **MP2888A explicit PMBus on a Turing card.** Before taking its own clock
  clamp, the explicit PMBus method checks that no other client holds a
  frequency lock. On an unlocked Turing card that check needs the RM
  frequency-limit records, which are read through the same `gdi32` export.
  Verify, Apply and Auto recovery for that method are therefore always
  refused on Turing here, and the refusal names the missing export rather
  than the driver's lock state. The offset method does not take a clamp and
  is unaffected, and Ampere cards do not use this channel.
- **GPU device restart (PnP).** It needs Windows 10 version 2004 or later,
  and the recovery helper checks that only after Druta has closed. The Panic
  button and Device menu entry have offered it since the 1.6.0 port; 1.7.0's
  "Voltage limits still raised" dialog, shown after a session ended without
  restoring the headroom it raised, now offers it too. On Windows 7, choosing
  any of them closes Druta, restarts nothing and reopens it. A startup
  profile that was waiting does not run, and the dialog returns. Choose
  **Keep them for now** instead. (Superseded on 2026-11-06: Windows 7 now
  restarts the GPU through SetupAPI; see "GPU restart through SetupAPI".)

On a Windows 10 host, the merged source passed 2,143 tests with stock CPython
3.8.10, where the three wheel and sdist tests skip, and 2,146 with Python
3.14.7. The 3.8.10 run was repeated with a simulated Windows 7 `gdi32`, which
lacks `D3DKMTEnumAdapters2`, with the same result. Under 3.8.10, the source
`druta.py --smoke-test` exited 0, parsed both bundled regulator profiles and
rendered three frames. The full-interface fixture rendered Control, Monitor
and Timings (1,063 items, nine frames) with no worker or subprocess attempts.
The compact and portable Windows 7 packages were not rebuilt for this update;
the packages described above predate it. The Windows 7 guest and
physical-GPU checks were not repeated.

## Shared feature update (2026-11-06): Druta 1.9.1

The draft now carries the shared Druta 1.9.1 source (main `fa6ee83`, through
#79): registry and VBIOS overrides through a verified GPU restart, the live
session controls, extended live power, Pascal core current limits, profiles
and Undo for the new controls, Ampere GA102 table layouts and the Advanced
tab. Each keeps main's capability, packet, identity and readback checks; the
release notes' measurements come from the cards and drivers named there and
are not Windows 7 hardware results.

**Why 1.9.1 itself cannot start here.** The regular 1.9.1 package uses
CPython 3.14. Its `python314.dll` imports `api-ms-win-core-path-l1-1-0.dll`,
an API set that Windows 7 does not have, so the loader stops before any
Druta code runs ("Failed to load Python DLL ... python314.dll"). Running
`src/druta/tools/pe_import_manifest.py` on the published
`Druta-1.9.1-win64.zip` lists exactly that import, plus `api-ms-win-crt-*`
forwarders that this build does not bundle. Windows 7 needs this separate
CPython 3.8 build.

**Python 3.8 changes.** Main's new modules use several 3.9 to 3.11 language
features. Static and test-suite checks found these in the shipped source:

- `zip(..., strict=...)` (3.10) appears in 25 places, including
  verification code that must reject length mismatches. On Python before
  3.10, `druta.legacy_python` now installs a `zip` that implements `strict`
  exactly, before any Druta module loads, instead of editing main's call
  sites.
- `dict | dict` (3.9) in `second_level_clock_gating_live.py` ran at import
  time, so on 3.8 that module could not load. This and one more case in
  `power_integral_profiles.py` now use `{**a, **b}`, as does
  `int.bit_count()` (3.10) in the engine clock-gating panel.
- `BaseException.add_note()` (3.11) is used by seven cancellation handlers
  (voltage, clock-VF, REL, client-power and power-integral rollback). On 3.8
  the call would raise inside the handler and replace the user's
  KeyboardInterrupt before the rollback result was attached. These now use
  `legacy_python.add_note`, which falls back to PEP 678's `__notes__` list.

`src/druta/tools/py38_compat.py` reports every such construct in a source
tree; `tests/test_legacy_diagnostics.py` keeps `src/druta` clean so the next
propagation fails a test instead of failing on the legacy machine. Test-only
uses (parenthesized `with` blocks, `dict |`, `str.removeprefix`) were
rewritten so the whole suite also runs on 3.8.

**Windows 7 behavior of the new controls.** The live PERF, power-saving and
client-power controls read through the same captured RM transport as the
current limits. On Windows 7 `gdi32` lacks `D3DKMTEnumAdapters2`, which this
branch already classifies as an unsupported capability. Those controls
therefore stay unavailable here with that reason; they are not retried as
failed reads.

The registry and VBIOS overrides take effect only through Druta's verified
GPU restart. On Windows 7 that restart now goes through SetupAPI, as
described in the next section, so these overrides can be activated here.
That path has not been run on Windows 7 yet. If Windows refuses the restart
or defers it to a reboot, main's existing rollback of the staged values runs.

## GPU restart through SetupAPI (2026-11-06)

Merged from main's PR #80 (`fix/legacy-device-restart`). `pnputil
/restart-device` exists only from Windows 10 version 2004, so Druta now
picks the restart method from the running Windows. Windows 10 2004 and later
keep PnPUtil. Older Windows, including Windows 7, restarts the same device
instance with SetupAPI's property-change request (`DIF_PROPERTYCHANGE` with
`DICS_PROPCHANGE`), the class-installer path that DevCon's restart also uses.
It is used when every needed SetupAPI export is present and Druta is a native
process; this build is x64, so it is never WOW64.

This makes the Panic button, **Device > Restart GPU device (PnP)**, the
"Voltage limits still raised" restart and registry/VBIOS override activation
available on Windows 7. The same-device check before the restart and the
started-without-problem check after it are unchanged. When Windows sets
`DI_NEEDREBOOT` or `DI_NEEDRESTART`, Druta reports that a reboot is needed
and does not reboot. Recovery receipts record which method ran.

By expectation, not yet by test: a WDDM display adapter on Windows 7 should
restart in place, with the screen blanking briefly. This needs checking on a
Windows 7 machine with an NVIDIA card. The hardware-free tests use a fake
SetupAPI and cover the call sequence, structure sizes, refusals, reboot
flags, a stale target and a hung call.


**Validation of this update.** It was checked on Linux only: the
hardware-free suite on CPython 3.13 and 3.8 matches main's 3.13 baseline
except for tests that need Windows' `ctypes.WinDLL`. No Windows 7 package was
built and nothing was run on Windows 7 or on a physical GPU. The diagnostics
below exist so the first Windows 7 run reports every remaining problem at
once.

## Diagnostics

Every legacy package carries `Druta-Diagnose.cmd` and `Druta-imports.txt`.
On the Windows 7 machine, run `Druta-Diagnose.cmd` from the extracted folder.
It is read-only and writes a `Druta-diagnostics-<time>` folder (or one in
`%TEMP%` when the Druta folder is not writable). Send that whole folder back:

- `summary.txt`: every finding in one place.
- `imports-check.txt`: each DLL that the bundle's EXE, DLL and PYD files
  import from Windows, and whether this machine has it. This names a
  missing-DLL loader failure even when Python cannot start.
  `Druta-imports.txt` is written at build time by
  `src/druta/tools/pe_import_manifest.py`.
- `os.txt`, `hotfixes.txt` (with checks for KB2533623, KB3063858, KB2999226,
  KB2670838 and KB971644), `gpu.txt`, `system-dlls.txt` and
  `bundle-files.txt`.
- `self-check.txt`: `Druta.exe --self-check` runs independent read-only
  probes and writes each result as soon as it is known. It covers the Python
  runtime, the runtime DLLs actually loaded, every Windows export Druta
  binds, an import of every application module, NVML/NVAPI/CUDA enumeration,
  recent Windows crash events for Druta (faulting module and offset) and the
  Direct3D renderer. A failing probe does not stop the next one.
- `logs\`: Druta's session logs. Every run, not only the self-check, writes
  `logs\druta-<time>-<pid>.log` beside `Druta.exe` (else under
  `%LOCALAPPDATA%\Thermetery\Druta\logs` or `%TEMP%\Druta-logs`). Each log has
  the startup stages, every uncaught exception (including worker threads) and,
  through `faulthandler`, the Python stack of a native crash such as an
  access violation inside a driver DLL. The newest 20 logs are kept.

The collector does not record user or computer names: the Python reports
replace the Windows user name, profile path and computer name, and the
script does not run `systeminfo`. PCI device IDs and driver versions are
included.

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
python src\druta\tools\smoke_full_ui.py --output full-ui.json --hold-seconds 3
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

Druta learns the helper's command contract from `nvtune --help` and never
tries a `set` to find out. A build that advertises **`--dry-run`** previews
with it, and a build whose help says everything defaults to a dry run
previews with a bare `set`. As in the shared build, a preview that exits
nonzero fails and is not retried as a bare `set`. A preview that exits 0 is
the plan, with or without nvtune's completion marker. Once a changed register
row has been printed, every later line Druta does not recognize is listed as a
warning, so a commit needs Force. The exception is nvtune's exact `unchanged`
row for a register whose requested fields already hold their values: it is
listed as a note, not a warning. That comes from the fix in PR #27, merged here
ahead of the shared build because nvtune's Windows 7 and Vista builds preview
with `--dry-run`. The public upstream
releases write on a bare `set`, so Druta calculates their read-only preview
from `fields` and `dump --raw` instead. Approved writes use the helper's
advertised commit convention (`--commit` where it exists). Failed commands
and missing readback values are reported as failures rather than hardware
rejection.

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
These guest results predate the help-based contract discovery above, which
has host regression coverage only.

## Compatibility changes

Python 3.8 uses Tomli for regulator profiles (newer Python uses `tomllib`).
Display scaling falls back to the Windows 7 device-context API. NVML loads
from either System32 (DCH drivers) or the Standard driver's
`%ProgramW6432%\NVIDIA Corporation\NVSMI` directory, as documented by
[NVIDIA](https://docs.nvidia.com/deploy/nvml-api/nvml-api-reference.html).

## Legacy-driver update

The current shared Druta fixes have been ported to this branch while retaining
CPython 3.8.10, Tomli, Windows 7 DPI handling and the pinned native runtime.
nvtune previews and commits follow the shared build's help-based contract
described under nvtune above.

DLL selection is automatic: Druta resolves the actual Windows system and Program
Files directories through Windows APIs, then tries only those installed-driver
locations. The loader update listed above is still required. The package does
not contain NVIDIA DLLs. Available exports and successful reads select older
clock, fan and telemetry paths; PCI enumeration can fall back to the older V2
record without changing the selected physical slot.

Private voltage-rail writes follow the shared build: they are offered by GPU
architecture, start from a recognized native packet and are checked by
readback. No driver version is consulted. The TITAN measurements on 472.12 and
580.97 remain recorded in `VOLTAGE-RAILS-TITAN.md` and `VOLTAGE-RAILS-47212.md`.
Porting this to Windows 7 does not confirm a new driver or operating-system
hardware combination. Ordinary
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
