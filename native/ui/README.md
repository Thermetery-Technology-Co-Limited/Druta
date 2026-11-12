# Native frontend

The native Win32/D3D9 application now has a live Session, Daily tuning, a V/F
point editor and owned holds, Monitor, qualified Advanced controls, native I2C
manual commands and a Timings helper workflow. **Focused** and
**Classic** share drafts, snapshots, command queue and Undo. Classic uses a
composition based on Druta 1.9.1; it is more than a verbose-text option. Both
presentations retain the resizable V/F editor popup; Classic also embeds the
editable curve below the tuning controls, as in the Python interface. Opening
the separate window moves the shared editor there until it closes. Changing
presentation never opens another GPU or applies a draft. The fixture shell remains available with
`--preview`.

This is an implemented development milestone in
[the UI/UX plan](../DESIGN-NATIVE-UI.md), with Windows 10 evidence below. Full
Python frontend parity and Vista/7 qualification remain open.

## Live controls and profiles

- **Clocks → Frequency locks / legacy P0** shows current-adapter lockable lists,
  explicit minimum/maximum and lock-max requests, and independently qualified
  legacy P0 plus maximum fan. Requests snap down to the top-memory clock table;
  its maximum is not the V/F boost ceiling. Owned handover is checked before
  acquisition. Release never blindly resets a foreign range; uncertain writes
  retain recovery. P0 release leaves fan policy unchanged; fan Undo/Auto remain
  explicit. Clock ownership alone adds no periodic maintenance while paused.
- **Timings** supports several local fields, a whole-selection preview and Apply,
  issued-field Undo, and a separate captured-initial restore. Native profiles
  save understood fields with staged overrides; loading only stages them.
  Fresh helper identity, GPU identity, qualified operating band and selected
  geometry must match. Unknown register bits are preserved, and an externally
  changed selected value refuses Undo. This does not qualify physical timing
  writes on an untested adapter.
- **Advanced → PERF diagnostics** explicitly captures limits, controller and
  policy groups. Each group reports its own status and retains stale last-good
  results after read failure. Opening the page does not poll these groups.
  Power arbitration diagnostics are still pending.
- Daily provides core/memory and additional clock/voltage offsets, configured
  board-power request, fan target/Auto, voltage boost and the independently
  identified NVVDD core-current request. Qualified rail limits have a separate
  Focused page and remain beside the limits in Classic. Each uses its own capabilities,
  fresh capture, existing C ABI setter and independent readback. Missing unrelated telemetry
  does not disable a qualified control. Stock/Auto can have a separate capability
  from Apply; an explicit escape action can report that prior state was
  unavailable for Undo.
  XOC is an explicit session mode. Its power slider can show the core's
  published **1,999 W software request envelope**; each Apply still checks the
  adapter's fresh validated power route and bounds, which can be narrower. The
  GUI now also exposes explicit Max it and Reset controls actions. Max it captures
  Undo before applying the normal board maximum, fan/boost targets, a planned
  ramp and an owned V/F hold. Reset reports individual outcomes and any state it
  could not capture. The live Reset/Undo retest restored all 127 curve requests
  and all 19 power-policy requests on the tested adapter.
- Controls start unlocked. An explicit lock choice stays in effect while switching
  adapters or layouts in the same application. Draft edits never write; use the
  selected row's Apply or Stock/Auto. Memory dragging uses whole MHz; numeric entry retains fractional
  values in the adapter-reported scale. Configured requests remain separate from
  live readings, enforced power and unavailable/stale values.
- **Undo hardware write** restores captured state, including the full prior
  V/F table for core-offset writes and explicitly pinned policies for coupled
  power actions. Unpinned peer requests remain driver-derived. Fan Undo restores requested levels and policies, not merely
  observed RPM. Partial or uncertain outcomes remain in the operation log.
- **V/F curve** is integrated in Classic and opens a large resizable popup in
  Focused (also available from Classic): drag a point or type its absolute
  physical frequency and use Set MHz. Current curve, draft and a fresh
  operating point are distinct. The current adapter's verified frequency divisor
  converts physical MHz to raw delta units. Undo edit and Revert are local;
  Apply/Reset carry a complete expected curve and divisor. A stale or changed
  curve retains edits and requires a fresh read/explicit rebase. The expanded
  plot uses large outlined points, vertical dragging and WASD movement; Shift
  moves three steps and Ctrl+Z/Ctrl+Y undo or redo local edits. De-flatten, ramp
  and rephase stage plans through the pure C ABI. Prediction uses the core's
  software model and reported grid-derived bound; it is not a measurement.
- **Hold selected point** owns a V/F hold, optionally with maintained headroom.
  Release restores owned state; uncertain restoration retains recovery. Release
  stays reachable while locked or disconnected, and required maintenance runs
  independently of ordinary drawing/telemetry Pause. Draft edits are not applied
  by Hold or Max it.
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

**Focused Daily** exposes independently qualified Crossbar, SYS, Video and LTC
offsets. **Advanced** contains additional memory offset and available
power-policy/current-request rows; **Rails** contains NVVDD/MSVDD offsets and
rail-limit fields. XOC gates remain explicit where required. Rail-limit fields show an absolute estimate
from the signed request plus captured first-read base; Initial restores the
captured request, not a verified factory default. A separate Rail limits session
mode gates writes; selected-field Undo does not replay opaque rail packets.
Missing fields and transient reads remain distinct from unsupported controls.

The risk indicator uses an additive score: XOC adds 1, an enabled writable
rail-limit path adds 2, and an enabled identified writable I2C path adds 3.
With controls unlocked, 0 is green, 1 yellow, 2 amber, 3 red and 4+ crimson.
A locked zero-score session is neutral; locking editing does not release or
hide an active controller request. Unknown state is shown separately.
The indicator uses the established session gate and
controller state; it does not probe the bus to choose a color. Drafts and active
session gates are distinct.

Focused offsets retain the same drafts and write gates as Classic when
navigating Daily, Advanced and Rails. The **Shunt mod**
button beside Board power opens the released resistance-based display
correction and parallel-resistor calculator. **Use for all rails** fills the
effective resistances; **Copy value** copies the numeric mOhm result. Original
resistances stay unchanged, and neither button saves. Explicit Save uses
`%LOCALAPPDATA%/Thermetery/Druta/shuntmod.json`, compatible with Python.
Connector layout and starting resistances are templates, not detected values.
Uniform ratios show corrected watts; mixed ratios show a capacity-weighted
estimate. Raw driver watts remain visible and are always used for power Apply.

## Native I2C controller path

On 64-bit processes, the core can use the NVAPI Ex I2C transport for explicit
controller discovery and reads. The current native writer supports the MP2888A
candidate's manual PMBus command (`0x21`) and its mode bit in `0xE1`; a
fingerprint is a candidate, not a unique model identification or a physical
rail assignment. Scan and Read are user actions, not ordinary telemetry polls.
The panel offers IC discovery, friendly candidate selection and the compiled
manual PMBus recipe; manual port/address entry is under Advanced routing. An
explicit scan selects a sole candidate; ambiguous results require selection.
Nominal request presets come from the qualified live command range/grid and
only stage a target. They are neither measured voltages nor factory defaults.
Other legacy controller families and the MP2888A VID-offset method remain
unimplemented in the native UI. The transport refuses the existing nonvolatile and password command blocklist.
The 32-bit Ex packet path is not qualified and declines these I2C operations;
that limitation does not disable the rest of the core.

Writing requires the I2C session gate and a successful response Verify. The
explicit Verify button can arm the selected controller and verify in one action;
scan, candidate/recipe selection and preset selection never verify or apply
automatically. Verify
owns a driver-enumerated low-clock clamp while it performs a bounded A–B–A
controller-response test, and restores the captured controller state before
releasing its clamp. Apply likewise keeps the clamp until Release restores the
owned controller request. A failed or uncertain restoration retains recovery
and prevents switching GPUs or closing the session until it can be resolved.
Controller readback is not an independent physical-voltage measurement. I2C
settings are excluded from native profiles and are never automatically applied
at startup, after a driver reset, or by loading a profile.

Profiles are UTF-8 JSON, limited to 1 MiB, with duplicate-key rejection and
same-directory flushed temporary-file replacement. Save has no tuning side
effect. No profile loads at startup or after driver reset. Ordinary tuning stays
session-only; Reconnect closes/reopens without replay. Saving a file does not
authorize automatic application.

## Timings helper

Both presentations expose Timings. Locate an installed `nvtune.exe` with the
native file dialog, then explicitly Read timings. The UI shows captured fields
and stages one selected cycle value; Preview creates the backend's canonical
plan before explicit Apply. Stable top-band capture and supported field checks
remain required. Timing Undo is separate from ordinary tuning Undo and remains
visible if a later capture fails. The helper is a separate process, not a
bundled or automatically executed tuning dependency. Capture/preview have live
evidence below; timing writes and restoration still need hardware qualification.

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
| `--classic` | Start in the 1.9.1-inspired Classic composition instead of Focused. |
| `--page daily\|curve\|advanced\|monitor\|timings` | Choose the initial page. |
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
pacing; an owned hold's required maintenance continues while ordinary refresh is
paused or minimized.

Interactive waits use an optional high-resolution one-shot timer with an
ordinary waitable-timer fallback. Idle windows use coarse waits; paused windows
wait for messages. No `timeBeginPeriod` request is made. Direct3D 9's software
vertex-processing fallback is still a D3D9 renderer. Border dragging currently
pauses submission inside the Win32 move/size modal loop.

## Qualification checkpoint

**2026-11-12, Windows 10 x64 build 19045, RTX 3070 Ti:** integrated Release work
uses MSVC 19.44.35230 / SDK 10.0.26100.0. The following ordinary tuning
round trips are configured-request/readback and restoration results for this
adapter/configuration, not physical-effect qualification or constants for
other boards or GPUs of the same generation.
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
- The original live-increment Release checks were **203 view, 171 client, 92 curve-model, 61 Session, 36 profile
  I/O, 48 refresh-policy and 16 PE-policy checks**, plus **57 targeted Python
  compatibility tests**. Fake-backend tests exercise alternate identities,
  partial capabilities, stale/failed reads, failures and manual Undo.
- Focused live Daily at 1280×800 was rendered with the real adapter identity
  and all five rows visible. Headless checks cover the five rows at 1024×768,
  both presentations and the pages implemented at that checkpoint. This does not establish physical
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

On that RTX 3070 Ti/configuration, a native I2C probe found an MP2888A
candidate at port **1**, address **0x20**, initially reporting **676 mV**. The
nominal 900 → 925 → 900 mV controller-response Verify passed and restored its
captured entry before releasing the clock clamp.

The existing Python Max it path with the C++ backend provided the multimeter
baseline: fan 100%, power request 500 W, boost 100% and a 1,093.75 mV V/F
request, with **1.11 V** on the meter. The later native manual-command test at
**405 MHz** produced the following observations:

| Nominal request | Controller report | User's meter reading |
| --- | --- | --- |
| 1,100 mV, first Apply | 1,066 mV | 1.06 V, truncated; excluded from the quantitative comparison |
| 1,125 mV | 1,091 mV | 1.097 V |
| 1,100 mV, return | 1,067 mV | 1.071 V |
| 1,125 mV, repeat | 1,091 mV | 1.097 V |

The repeatable meter change was **26 mV** for a **25 mV** nominal command
change. Release verified the captured controller registers before releasing
the clock clamp; afterward the controller reported **676 mV** at **210 MHz**
and the meter read **675 mV**. The helper exited with no ownership remaining.
This qualifies the observed response of this candidate route and operating
point in this session only. A nominal command is not the measured voltage, and
these readings establish no factory default, gain, offset or behavior for
another board or controller.

A read-only core power-bounds probe on this adapter reported normal
**100,000–500,000 mW** and XOC **1–1,999,000 mW**. Those are the observed
request bounds for this route, not universal hardware limits.

### Later native-control checkpoint

The later Windows 10 / RTX 3070 Ti session added the following scoped evidence;
the earlier fixtures and meter observations above remain historical records.
Raw results and remaining retests are maintained in
[validation/2026-11-12-native-controls.md](validation/2026-11-12-native-controls.md).

| Action | Latest scoped result |
| --- | --- |
| Coupled power | Board request 1,999 → 500 W and core-current request 483.014 → 270 A changed related policy requests; Undo restored the captured state. |
| Native Max it | 500 W, fan/boost 100%, ramp floor 1,000 mV and cap/held point 1,093.75 mV with headroom; maintenance and Undo passed. |
| Reset controls | Reset reached 310 W, 243 A, memory offset 0, boost 0 and fan Auto. After correcting the curve geometry check, native Undo restored all 127 curve voltage/delta entries, all 19 policy requests and the scalar requests exactly. No hold or Undo remained. |
| Native Timings | The selected installed nvtune v1.0.2-alpha helper captured timings and previewed RC 96 → 255 cycles. No timing write was issued. |

These are configured-request/lifecycle observations for the tested adapter and
operating point, not additional physical-voltage qualification or constants for
another board.

Remaining replacement work includes timing-write/restoration qualification,
other I2C controller methods/devices,
registry/VBIOS relationship overrides, power saving, configurable
Monitor workflows, full profiles and Vista/7 qualification. Keep the Python
frontend and experiment facade available during migration. See the
[plan checkpoint](../DESIGN-NATIVE-UI.md#continuation-checkpoint-live-daily-actions-and-manual-curve-edits)
for the implementation boundary.

### Device menus and selected-request profiles

Both presentations expose Device and Help menus. Device can refresh capability
readbacks without changing the page or drafts, capture/copy/save a selected-GPU
report, open that GPU in a second window, and navigate to timing-helper setup.
The child window receives the exact core DLL, PCI slot and expected UUID; it
does not receive tuning settings. Reconnect preserves that constraint. Help
shows shortcuts, About and the actual licence/guide files bundled beside the EXE.

**File → Rail / power profiles** opens the manual selected-request workspace.
Refresh captures the adapter's understood fields. Choose which current board,
policy and rail-limit requests to save. Load previews the saved selection;
Apply rechecks the whole selection against fresh identity, units, bindings,
gates and bounds. Selected policy values are explicit session pins. Board power
runs first, followed by selected policies and rail fields, with readback and a
grouped Undo on partial failure. An unrelated pin changed afterward is preserved
by refusing Undo until that conflict is resolved. Nothing applies automatically.

Rail request deltas and estimated absolute voltages are labelled separately.
An unavailable unrelated rail does not prevent saving a qualified selection.
This workspace is separate from daily and V/F profiles; full-profile composition,
PnP panic/restart and the richer timing workflows remain tracked in the
[completion ledger](validation/PARITY-COMPLETION.md).
