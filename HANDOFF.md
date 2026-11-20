# C++ rewrite handoff: real-machine validation

Updated 2026-11-10. Branch: **`cpp/core`**. Implementation commit:
**`f7895ba52301fee1b384ce4751fd3045b886c2ff`**; subsequent handoff edits are on the
same branch. No PR or release has been created.

The existing Python UI can now use the C++ backend explicitly. G4d–G4i are
implemented: paired NVAPI/NVML companions, the complete UI-facing surface,
public readers and request envelopes, differential tests, DLL packaging and
read-only backend comparison. **Python is still the default.** The next work
is Windows build/runtime and GPU validation, not changing the default yet.

## 1. Get the branch and prepare Windows

The quickest full-application route is a **64-bit Windows 10/11** machine with
an NVIDIA GPU, 64-bit Python 3.12, Git, CMake 3.21 or newer, and Visual Studio
2022 Build Tools with **Desktop development with C++** and a Windows SDK.
Use Windows PowerShell from the repository directory for the commands below.
The native core's older-OS targets are a separate check; see section 7.

For a fresh checkout:

```powershell
git clone --branch cpp/core https://github.com/Thermetery-Technology-Co-Limited/Druta.git Druta-cpp
Set-Location .\Druta-cpp
git rev-parse HEAD
```

For an existing clean checkout, fetch and switch to `cpp/core`, then
`git pull --ff-only`. Preserve any local edits before switching; do not reset
or overwrite an existing working tree to follow this handoff.

```powershell
py -3.12 -m venv .venv
$env:PATH = "$((Resolve-Path .\.venv\Scripts).Path);$env:PATH"
Get-Command python
python --version
python .\src\druta\native_packaging.py --bits
python -m pip install -r .\requirements.txt
python -m pip install pytest setuptools wheel build
$env:CMAKE_GENERATOR = 'Visual Studio 17 2022'
Remove-Item Env:DRUTA_BACKEND -ErrorAction SilentlyContinue
Remove-Item Env:DRUTA_NATIVE_LIB -ErrorAction SilentlyContinue
```

The bitness command must print **64** and `Get-Command python` must point into
this checkout's `.venv`. Setting this shell's PATH avoids needing to activate
the venv through an execution-policy change; it also ensures `build.ps1` uses
the right interpreter. Keep this PowerShell window for the remaining commands.

## 2. Build the application and verify the native library

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -DistDirectory dist-native-test
if ($LASTEXITCODE -ne 0) { throw 'Druta build failed; inspect the build output.' }
$exe = (Resolve-Path .\dist-native-test\Druta\Druta.exe).Path
Get-Item .\dist-native-test\Druta\_internal\drutacore.dll
Get-FileHash .\dist-native-test\Druta\_internal\drutacore.dll -Algorithm SHA256
```

This builds the core from source, checks its architecture, and bundles it.
Expected outputs:

| Output | Purpose |
| --- | --- |
| `dist-native-test/Druta/Druta.exe` | Application to test; keep its entire folder together. |
| `dist-native-test/Druta/_internal/drutacore.dll` | Native core, ABI 2, matching the Python process. |
| `dist-native-test/Druta/source/SOURCE-MANIFEST.json` | Source and native-library hashes, Git revision and architecture. |
| `dist-native-test/Druta/source/HANDOFF.md` | This handoff travels with the source bundle. |
| `dist-native-test/Druta-1.9.1-win64.zip` | Local application/source bundle; version comes from the checkout. |

`build.ps1` builds only the DLL target. Build the other native targets before
running the self-test or probe:

```powershell
cmake --build .\native\build\msvc-64 --config Release
ctest --test-dir .\native\build\msvc-64 -C Release --output-on-failure
```

The self-test should pass. The read-only `druta-probe.exe` is now under
`native/build/msvc-64/Release/`; its report can help diagnose missing exports.

If using a separately cross-built DLL from **this implementation**, pass it
with `-NativeLibrary PATH` to `build.ps1`. Relative paths are repository-relative.
A mismatched or missing DLL is a build failure. Do not reuse an old DLL merely
because its filename or ABI number matches; new exports are also required.

## 3. Identify the card and compare without changing settings

First check both enumeration paths from the console:

```powershell
python .\druta.py --backend python --list-gpus
python .\druta.py --backend native --list-gpus
$slot = '0000:01:00.0'  # Replace with the intended card's exact listed PCI slot.
```

Confirm the intended adapter and slot on both sides. A forced `native` request
must report a load/ABI/surface failure instead of silently selecting Python.
`--version` only displays the backend request; it does **not** load the core
and does not establish that native is active.

Run the packaged read-only comparison. Leave the output directory nonexistent;
the tool creates it, and its parent must already exist.

```powershell
$out = Join-Path (Get-Location).Path ('validation-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$comparison = Start-Process -FilePath $exe -ArgumentList @('--backend-compare', $slot, ('"{0}"' -f $out)) -Wait -PassThru
$comparison.ExitCode
Get-Content (Join-Path $out 'comparison.json')
Get-Content (Join-Path $out 'diff.txt')
(Get-Content (Join-Path $out 'native.json') -Raw | ConvertFrom-Json).selection
```

The EXE is a windowed application and may attach output to the parent console.
If a completion/error dialog appears, dismiss it to let the command finish;
`Start-Process -Wait -PassThru` provides a reliable exit code. Quoting `$out` in
the argument list also supports checkout paths with spaces.

The tool runs each kind in its own process. It writes `python.json`,
`native.json`, `comparison.json` and `diff.txt`; it applies no settings or
profiles. Inspect `native.json` for `selection.kind = native`, `abi = 2`, and
the actual packaged DLL path. Both captures should identify the selected card.

| Exit | Meaning and next action |
| --- | --- |
| 0 | Both captures complete and equal. Continue to the UI checks. |
| 1 | Differences or an incomplete capture. Read statuses/errors and the diff before proceeding. |
| 2 | Invocation/output failure, such as an existing result directory. Correct it and use a new directory. |

Sequential clocks, temperatures, power, timestamps and other live readings can
differ. Review those in their operating context. Wrong identity, unexplained
missing controls, malformed records, backend errors or changed request values
need investigation. A nonzero comparison is not automatically a bug, and two
failed captures are never a passing comparison.

If packaging is the blocker, the source route uses the same implementation and
finds `native/build/msvc-64/Release/drutacore.dll`:

```powershell
python .\druta.py --backend-compare $slot ($out + '-source')
python .\druta.py --backend native --gpu $slot
```

Record whether a result came from the packaged EXE or the source launcher.

## 4. Exercise the UI and supported controls

```powershell
Start-Process -FilePath $exe -ArgumentList @('--backend', 'native', '--gpu', $slot) -Verb RunAs
```

Accept the elevation prompt for the write tests. For the source-launch route,
use an elevated PowerShell with the same venv and slot; otherwise operations
requiring administrator rights can correctly refuse before testing a write.

Confirm the log/About backend line shows **native core**, its path and ABI 2.
Record the starting settings and use small changes within this adapter's
reported normal bounds, with XOC off. Run one writer per card at a time; close
other tuning applications and finish each operation before the next. Where
comparison with Python is needed, restore the captured settings, close the
native instance and launch the same EXE with `--backend python`.

For every supported write, record the request, actual readback and restoration
result. A setter's success text alone is not a hardware result. Mark genuinely
unsupported operations **N/A with the capability reason**; disappearance of a
previously valid control after a transient read is a regression to investigate.

| Check | Expected result |
| --- | --- |
| Idle telemetry and refresh | Updates continue; identity stays on the chosen card; supported controls remain available. |
| Core/memory offsets; private clock-domain offset where offered | Request and readback agree, and Undo restores the captured offsets and affected V/F shape. |
| Board power limit and a supported policy/current limit | Configured request and enforced/derived readings remain distinguished; readback and Undo agree with the selected operation. |
| Fan manual setting, then driver/automatic control | Correct fan/adapter is affected; restoring control succeeds. |
| V/F apply, point lock and their reset/release | Intended rows change; unrelated rows remain intact; release state matches the named reset result. |
| Rail limits and MSVDD, when explicitly enabled and supported | Independent rail capability checks hold; exact selected-field readback works; saved profiles include MSVDD domains written this session. |
| Hold headroom, then restore | The write-ahead recovery record and verified state agree; restoring releases this session's owned change. |
| Save a manual profile, change a supported control, explicitly load, then Undo | Saved settings and the pre-load state both restore through verified paths. Saving alone applies nothing. |
| Reset all | Named steps are reported independently; failed steps remain visible. Rail reset uses this session's first read, not an asserted factory default. |
| Card switch and second window | Identity and controls follow the selected card; failed switching keeps the current card usable; each process keeps its selected backend kind. |
| I2C discovery; registry/voltage-offset panels | Read-only discovery and panel reads use the paired card; errors stay visible. Persistent override writes have separate qualification. |
| Kepler/Maxwell legacy P0, if available | UI control is present, a hold is telemetry-confirmed, and the owned hold can be released. |

Then run several minutes of polling while using read-only I2C discovery and
explicit manual profile loads through the UI. Existing UI busy/refusal behavior
should remain usable; there should be no deadlock, wrong-card access or crash.
Capture the application log. These lines need investigation:

- `native backend: front end requested missing member ...`
- `native backend: internal error reading ...`

After restoring settings, repeat the read-only comparison into a **new** output
directory and retain both before/after captures. Repeated samples describe this
card, driver and operating point, not universal defaults or support gates.

## 5. Recovery and driver-restart check

Prefer the application's selected-device **Restart GPU device** action from
an elevated instance. The CLI alternative below requires an elevated PowerShell
and closed old Druta instances; keep the `$exe` and `$slot` definitions available:

```powershell
Start-Process -FilePath $exe -ArgumentList @('--backend', 'native', '--restart-gpu', $slot)
```

Do not use `-Wait` as proof of restart completion: it may also wait for the
reopened GUI to close. Inspect the recovery result and the reopened adapter's
identity/backend. The driver restart is the panic/reset action: session-only
tuning must **not** automatically reapply. A saved manual profile still needs
an explicit load. Do not enable startup-profile application for this test.
Existing persistent registry/VBIOS overrides are a separate mechanism and are
not removed by this session-only rule.

To return to the established Python backend, close the native instance and run:

```powershell
Start-Process -FilePath $exe -ArgumentList @('--backend', 'python', '--gpu', $slot) -Verb RunAs
```

Switching backends is not a settings reset. Use a verified restore/reset or the
driver restart as appropriate, then inspect the resulting state. Preserve logs
and captures when a write, restore or recovery fails before attempting it again.

## 6. Automated checks and evidence to return

These focused tests use fake driver callbacks, not tuning writes to the installed GPU:

```powershell
$focusedTests = @(
    'tests/test_backend_conformance.py', 'tests/test_native_companions.py',
    'tests/test_native_frontend.py', 'tests/test_native_frontend_abi.py',
    'tests/test_frontend_backends.py', 'tests/test_frontend_ui.py',
    'tests/test_frontend_registry.py', 'tests/test_backend_compare.py',
    'tests/test_packaging.py'
)
python -m pytest -q @focusedTests
```

Linux validation already completed: 200 focused tests, 65 PE/import-audit tests,
Linux and ASan/UBSan builds/self-tests, and x86/x64 MinGW builds with all eight
PE images passing the import audit. The regression comparison to `f01e0dd`
leaves the same 39 pre-existing Linux failures. **Those are not an expected-failure
allowlist for Windows.** Investigate Windows failures on their own evidence.
Actual Windows PowerShell/PyInstaller execution and GPU behavior were not
available in that environment; this handoff is for obtaining that evidence.

Keep one result record per adapter/configuration, for example:

```text
Date/tester:
Git revision (git rev-parse HEAD), local changes (git status --short):
Windows edition/build, Python bitness, compiler/build route:
GPU model, exact PCI slot, device/subsystem IDs, VBIOS, driver:
Packaged or source launch; native DLL path, ABI and SHA-256:
Initial settings, persistent overrides already present, workload/operating point:
Before/after comparison directories and reviewed differences:
Each test: PASS / FAIL / N/A(reason), request, readback, restoration:
Profile/Undo and Reset-all results:
Restart result and evidence that session tuning was not reapplied:
Soak duration/result; missing-member/internal-error log lines:
Reproduction steps and application log for any failure:
```

Return the comparison JSON/diff files, relevant application log/screenshots,
test output, the bundle's `SOURCE-MANIFEST.json`, and a probe report if one was
run. Record any source correction and rebuild before retesting, so the code,
DLL and evidence remain matched. Leave the freshly built native application
running on the intended card for human review after restoration is confirmed.

## 7. Remaining work and legacy machines

- **G4j:** Complete the hardware sequence above on the available generations,
  including a clean missing-member log and concurrency soak. Results are scoped
  to those configurations; do not convert them into device/driver allowlists.
- **G4k:** The default switch to `auto` still needs the owner's hardware sign-off
  and approval. The design also calls for a release as opt-in. Startup-profile
  handling stays Python by default until separately qualified.
- **Legacy Windows:** Start with an architecture-matched, XP-targeted native
  `druta-probe.exe` and retain `druta-probe-report.txt` beside the EXE or in
  `%TEMP%`. The current Python 3.12 GUI setup above is not an XP setup, and an
  ordinary modern MSVC build is not an XP qualification. Static import checks
  do not prove startup on an actual legacy OS. Older-gdi32 adapter openers
  require probe evidence before implementation.
- **Later ports:** A native UI, native GPU-restart implementation and remaining
  Python runtime consumers are not completed by this integration. The current
  UI and restart path remain Python over the selected backend.

Reference: [native README](native/README.md),
[front-end design and acceptance criteria](native/DESIGN-FRONTEND.md),
[transport design](native/DESIGN-TRANSPORT.md), and [manual](MANUAL.md).
Continue on `cpp/core`; do not create a PR unless the owner asks.
