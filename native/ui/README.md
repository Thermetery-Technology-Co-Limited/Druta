# Native frontend

The native Win32/D3D9 application now has a live Session, Daily tuning, a V/F
point editor, Monitor and qualified Advanced controls. **Focused** and
**Classic / Verbose** share drafts, snapshots, command queue and Undo; changing
presentation never opens another GPU or applies a draft. The original fixture
shell remains available with `--preview`.

This is an implemented development milestone in
[the UI/UX plan](../DESIGN-NATIVE-UI.md), with Windows 10 evidence below. Full
Python frontend parity and Vista/7 qualification remain open.

## Live controls and profiles

- Daily provides core/memory offsets, configured board-power request, fan
  target/Auto and voltage boost. Each uses its own capabilities, fresh capture,
  existing C ABI setter and independent readback. Missing unrelated telemetry
  does not disable a qualified control. Stock/Auto can have a separate capability
  from Apply; an explicit escape action can report that prior state was
  unavailable for Undo.
  XOC is an explicit session mode; Max it and Reset all remain pending.
- Draft edits never write. Unlock controls, then use the selected row's Apply
  or Stock/Auto. Memory dragging uses whole MHz; numeric entry retains fractional
  values in the adapter-reported scale. Configured requests remain separate from
  live readings, enforced power and unavailable/stale values.
- **Undo hardware write** restores captured state, including the full prior
  V/F table for core-offset writes and understood peer policies/pins for coupled
  power actions. Fan Undo restores requested levels and policies, not merely
  observed RPM. Partial or uncertain outcomes remain in the operation log.
- **V/F curve** opens a large resizable popup: drag a point or type its absolute
  physical frequency and use Set MHz. Current curve, draft and a fresh
  operating point are distinct. The current adapter's verified frequency divisor
  converts physical MHz to raw delta units. Undo edit and Revert are local;
  Apply/Reset carry a complete expected curve and divisor. A stale or changed
  curve retains edits and requires a fresh read/explicit rebase. The expanded
  plot uses large outlined points, vertical dragging and WASD movement; Shift
  moves three steps and Ctrl+Z/Ctrl+Y undo or redo local edits. De-flatten, ramp
  and rephase stage plans through the pure C ABI. Prediction uses the core's
  software model and reported grid-derived bound; it is not a measurement.
- Global **Save profile...** and **Load profile...** use explicit Windows file
  dialogs. Save reads qualified Daily settings and reports omissions. Load
  requires unlock, validates target/units/current capabilities, captures aggregate
  Undo and reports partial failure. Native `druta-native-daily` version 1 can
  store scalar core/memory/boost, the board request and a representable fan
  policy/target. Version 2 also supports qualified advanced offsets. Profiles
  exclude individual power policies, rail limits and the full V/F point table;
  no profile enables a session gate automatically. A saved core scalar cannot
  restore a sculpted curve; live core-write Undo is separate.
- Import accepts only Python schema-2 `scope: tuning_action` profiles of kind
  `memory`, `boost`, clock `domain` or NVVDD `rail_offset`. Other Python
  scopes/kinds, full-tune/combined profiles and unknown native fields are
  refused. Broader existing-format import is pending.

**Advanced** exposes independently qualified clock-domain, NVVDD and MSVDD
offsets, with an explicit XOC session gate where required, plus available power
policy and current-request rows. Rail-limit fields show an absolute estimate
from the signed request plus captured first-read base; Initial restores the
captured request, not a verified factory default. A separate Rail limits session
mode gates writes; selected-field Undo does not replay opaque rail packets.
Missing fields and transient reads remain distinct from unsupported controls.

The risk indicator follows the existing combined score: XOC adds 1, an enabled
writable rail-limit path adds 1, and an enabled identified writable I2C path
adds 2. Stock is neutral, 1 amber, 2 red and 3 or more crimson; unknown state
is shown separately. The native I2C writer is not ported, so it is inactive;
the indicator does not probe the bus to choose a color. Drafts and active
session gates are distinct.

Profiles are UTF-8 JSON, limited to 1 MiB, with duplicate-key rejection and
same-directory flushed temporary-file replacement. Save has no tuning side
effect. No profile loads at startup or after driver reset. Ordinary tuning stays
session-only; Reconnect closes/reopens without replay. Saving a file does not
authorize automatic application.

The typed CoreClient uses `drutacore`'s C ABI, including the fresh
`dc_gpu_clock_offset_range_get` reader. Python's native facade and standalone
experiment path remain available; no Python runtime is required by the EXE.
Standalone Python and the UI own separate sessions, so a handle mutex is not
cross-process tuning ownership.

## Build on Windows

Run from the repository root with CMake 3.20+, Visual Studio 2022 C++ tools and
a Windows SDK. Python 3 runs the PE import audit; pytest runs Python tests.
Checked-in dependencies require no build-time downloads: Dear ImGui/ImPlot pins
and licenses are in [vendor/README.md](vendor/README.md); nlohmann JSON's MIT
license, pin and checksums are in [vendor/json/README.md](vendor/json/README.md).

Build the current core and UI together:

```powershell
cmake -S native -B native/build/ui-integrated -G "Visual Studio 17 2022" -A x64 -DDRUTA_BUILD_UI=ON
cmake --build native/build/ui-integrated --config Release
ctest --test-dir native/build/ui-integrated -C Release -R "^druta-ui-" --output-on-failure
& ./native/build/ui-integrated/ui/Release/druta-ui.exe
```

The integrated `druta-ui-core-stage` target copies that build's `drutacore.dll`
beside the EXE. Licenses are staged there too. `DRUTA_BUILD_UI` defaults to
`OFF`; ordinary core builds and their separate XP policy remain unchanged.

A standalone UI build can stage an explicitly selected DLL:

```powershell
cmake -S native/ui -B native/build/ui-msvc-x64 -G "Visual Studio 17 2022" -A x64 -DDRUTA_UI_CORE_LIBRARY=C:/absolute/path/drutacore.dll
cmake --build native/build/ui-msvc-x64 --config Release
ctest --test-dir native/build/ui-msvc-x64 -C Release --output-on-failure
```

Alternatively omit `DRUTA_UI_CORE_LIBRARY` and launch with
`--core C:/absolute/path/drutacore.dll`, or use `--preview` without a core DLL.
Live launch normally loads only `drutacore.dll` beside the EXE; it does not
search the working directory for another same-named DLL. Missing DLLs or exports
are reported as unavailable, without substituting fixture readings.

If CMake selects the wrong Python, add
`-DPython3_EXECUTABLE=C:/path/to/python.exe`. The default build runs the UI PE
audit and writes `imports.json` beside the EXE. To run it explicitly:

```powershell
python native/ui/tools/audit_imports.py native/build/ui-integrated/ui/Release/druta-ui.exe --report native/build/ui-integrated/ui/Release/imports.json
python -m pytest -q native/ui/tests/test_audit_imports.py
```

## Run and capture

```powershell
& ./native/build/ui-integrated/ui/Release/druta-ui.exe --page curve
& ./native/build/ui-integrated/ui/Release/druta-ui.exe --classic --page monitor --width 1366 --height 768
& ./native/build/ui-msvc-x64/Release/druta-ui.exe --core C:/absolute/path/drutacore.dll
& ./native/build/ui-msvc-x64/Release/druta-ui.exe --preview --scale 1.5 --capture C:/existing-folder/focused.bmp
```

| Option | Behavior |
| --- | --- |
| `--preview` | Fixture layout; opens no GPU handle and performs no tuning or telemetry reads. |
| `--core ABSOLUTE_DLL` | Load the explicitly selected core DLL in live mode. |
| `--gpu PCI_SLOT` | Select by PCI slot; the live adapter picker can change it later. |
| `--width N`, `--height N` | Initial client pixels, each 640–7680. |
| `--scale N` | Explicit UI scale from 1 through 3, including fractional values. |
| `--classic` | Start in Classic / Verbose instead of Focused. |
| `--page daily\|curve\|advanced\|monitor` | Choose the initial page. |
| `--capture FILE.bmp` | Save the rendered view and exit; parent directory must exist. Live mode reads the GPU. |
| `--smoke-test EXISTING_DIRECTORY` | Fixture renderer smoke, BMPs and `smoke.json`, then exit. |
| `--live-smoke-test EXISTING_DIRECTORY` | Same bounded phases with completed live reads; no tuning commands. |

For a live smoke run that waits and exposes the exit code:

```powershell
New-Item -ItemType Directory -Force native/build/ui-live-smoke | Out-Null
$outputDir = (Resolve-Path native/build/ui-live-smoke).Path
$run = Start-Process ./native/build/ui-integrated/ui/Release/druta-ui.exe -ArgumentList @('--live-smoke-test', ('"' + $outputDir + '"')) -Wait -PassThru
$run.ExitCode
```

Smoke counters separate completed live reads from fixture ticks. The sequence
covers presentations/pages, retained drafts, resize/device resets, ordinary and
continuous minimization, Pause and injected hotkey restoration. It does not
prove physical keyboard/mouse input or GPU-engine utilization.

## Refresh and renderer behavior

**Always refresh** starts off. Interaction targets 60 FPS; after 0.75 seconds
without activity, visible rendering targets 10 FPS. Ordinary minimization stops
drawing, telemetry and ordinary capability retries. Always refresh keeps drawing
and telemetry running while minimized. Explicit **Pause** takes precedence.

Restore from the taskbar or **Ctrl+Shift+F11**; if another process owns the
shortcut, taskbar restoration remains available. Restore requests fresh readings;
last-good values are marked stale until replaced. Session reads/commands run on
an ordered worker independently of drawing; polls coalesce, generation checks
reject late results, and close drains work before closing the handle. Plot
arrays are bounded. Required maintenance deadlines remain separate from frame
pacing; owned hold modes are not enabled yet.

Interactive waits use an optional high-resolution one-shot timer with an
ordinary waitable-timer fallback. Idle windows use coarse waits; paused windows
wait for messages. No `timeBeginPeriod` request is made. Direct3D 9's software
vertex-processing fallback is still a D3D9 renderer. Border dragging currently
pauses submission inside the Win32 move/size modal loop.

## Qualification checkpoint

**2026-11-12, Windows 10 x64 build 19045, RTX 3070 Ti:** integrated Release work
uses MSVC 19.44.35230 / SDK 10.0.26100.0. These are configured-request/readback
and restoration results for this adapter/configuration, not physical-effect
qualification or constants for other boards or GPUs of the same generation.
Each helper action reported successful Apply, exact request readback and Undo;
the original scalar requests and full curve were checked afterward.

| Action | Observed round trip |
| --- | --- |
| Memory offset | 32 → 33 → 32 MHz in the reported UI scale; curve unchanged. |
| Core offset | 0 → 15 → 0 MHz; original full curve restored. |
| Board request | 310 → 309 → 310 W; backend verified peer policies/pins. |
| Fan target | 30 → 31 → 30%; captured fan policies restored. |
| Voltage boost | 0 → 1 → 0%; no physical rail-voltage claim. |
| Curve point 0 | Raw delta 0 → -15000 → 0; all 127 deltas and geometry checked. |

- The manual native-profile exercise used raw memory **512 → 528**, with the
  current adapter's reported scale **16**. Load and Undo restored captured
  requests. These scale/raw values are evidence for this configuration only.
- All 11 live renderer phases passed, including four D3D9 resets. Ordinary
  minimized and explicit Pause phases produced **zero frames and zero completed
  reads**; Always refresh while minimized produced **115 frames and 2 completed
  reads over two seconds**. These are renderer resets, not GPU-driver
  restart-persistence tests.
- Release checks: **203 view, 171 client, 92 curve-model, 61 Session, 36 profile
  I/O, 48 refresh-policy and 16 PE-policy checks**, plus **57 targeted Python
  compatibility tests**. Fake-backend tests exercise alternate identities,
  partial capabilities, stale/failed reads, failures and manual Undo.
- Focused live Daily at 1280×800 was rendered with the real adapter identity
  and all five rows visible. Headless checks cover the five rows at 1024×768,
  both presentations and all four live pages. This does not establish physical
  input or full accessibility qualification.
- The independent desktop capture helper failed twice with
  `window capture timed out: timed out waiting on channel`. No physical inputs
  were issued through it. D3D9 BMP captures succeeded; physical numeric entry,
  clicks and shortcut delivery remain unverified.
- Vista SP2 x64 and Windows 7 SP1 x64 remain targets with no actual launch,
  font/DPI, device-loss or shutdown qualification yet. The PE audit checks
  selected imports/headers, not every CRT path or actual legacy execution.
  XP/x86 UI support is not promised; core support remains independent.

The first fixture shell's build, rendered scale review and smoke history remain
in [validation/2026-11-11-smoke.json](validation/2026-11-11-smoke.json). Its sample
ticks and captures are fixture evidence, separate from the live results above.
The live evidence and final build hash are recorded in
[validation/2026-11-12-live.json](validation/2026-11-12-live.json).

The current control-arc evidence is recorded separately in
[validation/2026-11-12-controls.json](validation/2026-11-12-controls.json).
On the tested RTX 3070 Ti, the Crossbar request returned 0 → -1 → 0 MHz;
all surfaced advanced and rail requests and the complete V/F curve were checked
as restored. This is configured-request evidence for that adapter and operating
point, not proof of physical response or behavior on other boards.

Remaining replacement work includes owned holds/headroom/recovery, Max it and
Reset all, I2C implementation and parked physical qualification, registry/VBIOS
relationship overrides, power saving, timings, configurable Monitor workflows,
full profiles and Vista/7 qualification. Keep the Python frontend and experiment
facade available during migration. See the
[plan checkpoint](../DESIGN-NATIVE-UI.md#continuation-checkpoint-live-daily-actions-and-manual-curve-edits)
for the implementation boundary.
