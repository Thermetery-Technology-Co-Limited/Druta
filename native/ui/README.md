# Native frontend

The native Win32/D3D9 application now has a live Session, Daily tuning, a V/F
point editor and owned holds, Monitor, qualified Advanced controls, native I2C
manual commands and a Timings helper workflow. **Focused** and
**Classic / Verbose** share drafts, snapshots, command queue and Undo. Druta
opens in Verbose by default. Choose **Skin → Focused** on the Verbose
menu bar, or use the layout selector in Focused to switch back. Classic uses a
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
  Power arbitration diagnostics are available through explicit Refresh.
- Daily provides core/memory and additional clock/voltage offsets, configured
  board-power request, fan target/Auto, voltage boost and the independently
  identified NVVDD core-current request. Qualified rail limits have a separate
  Focused page and remain beside the limits in Classic. Each uses its own capabilities,
  fresh capture, existing C ABI setter and independent readback. Missing unrelated telemetry
  does not disable a qualified control. Stock/Auto can have a separate capability
  from Apply; an explicit escape action can report that prior state was
  unavailable for Undo.
  XOC is an explicit session mode. The power slider uses the adapter's
  validated request range. With a supported live route, XOC extends that
  range to the **1,999 W software ceiling** or a higher reported BIOS maximum.
  Turning XOC off keeps the active request visible and restores the adapter's
  nominal edit range. Apply or Stock can return to nominal through the owning
  route. A saved profile can capture or match the outside request without a
  write. A profile that changes the board request requires its previous value
  to be restorable under the current XOC setting. The GUI also exposes Max it
  and Reset controls.
  Board-power changes leave unpinned current policies to the driver. After an
  NVML or v30 client-power SET, the board control reapplies eligible explicit
  session pins if the driver changed them. The v40 client-power path verifies
  that peer control bytes stayed unchanged.
  Max it captures
  Undo before applying the normal board maximum, fan/boost targets, a planned
  ramp and an owned V/F hold. Reset reports individual outcomes and any state it
  could not capture. The live Reset/Undo retest restored all 127 curve requests
  and all 19 power-policy requests on the tested adapter.
- Controls start unlocked. An explicit lock choice stays in effect while switching
  adapters or layouts in the same application. Draft edits never write; use the
  selected row's Apply or Stock/Auto. Daily sliders and their value boxes use
  whole numbers in the displayed unit. Configured requests remain separate from
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
- This standalone Load action accepts Python schema-2 `scope: tuning_action` profiles of kind
  `memory`, `boost`, clock `domain` or NVVDD `rail_offset`. Other Python
  scopes/kinds, full-tune/combined profiles and unknown native fields are
  refused by this action. Use the Profile catalog for the broader import scopes below.

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
Focused shows corrected watts for uniform ratios and a capacity-weighted
estimate for mixed ones; Classic Daily shows raw watts, with the corrected value
in the tooltip and on Monitor. Raw driver watts are always used for power Apply.

### Firmware clock controls

**Daily tuning** in Focused and **Control → Clock offsets** in Classic read
the selected card’s table automatically. The editor is also in **Advanced**.
Pascal exposes per-state GPC, XBAR and SYS / Video ceilings in MHz, up to 200%
of the original clock-program ceiling for each domain (subject to the field's
encoding limit). The shared XBAR and SYS ratios range from 1–200% of GPC.
Each ratio row estimates its P0 target using
the fastest graphics V/F point at or below the selected GPU’s effective NVVDD
limit. Staging a ratio, core offset or curve edit updates the preview. The
performance-state selector controls the separate firmware ceilings.
Supported Kepler and Maxwell performance tables expose independent per-state
MHz targets for GPC, XBAR and SYS.
Review lists all staged edits before Apply refreshes the selected adapter.
Once the edited image is loaded, Druta removes its temporary registry records.
The editor reads back the applied values when it reopens. Further edits keep
the other session settings. Restore returns to the image captured before the
first Apply, and Verify closes a saved session after a normal driver restart.

On Vista and later, the Pascal XBAR/SYS ratio review offers **Use recovery
boot for this Apply**, initially unchecked. **View recovery script** opens
`Druta-Recovery-Boot.cmd` in Notepad. Leave the Apply warning open and run
the script yourself as administrator to arm it. Confirm the GPU shown.
Windows selects a one-time Safe Mode entry and Druta registers recovery for
that account at sign-in. Setup preserves the normal default loader and does
not write boot code. Keep the account password and any BitLocker recovery key
available. Following a freeze, sign in as the original user and approve
cleanup before restarting normally. XP Professional x64 uses manual Safe Mode
recovery.

If a clock edit leaves the display black, a separate helper can clean up the
staged registry records while Windows is responsive. After a hard freeze, use
the same Windows account in Safe Mode. From an administrator command prompt
in the Druta folder, run `druta-ui.exe --recover-clock-overrides` to find the
saved PCI slot, then run `druta-ui.exe --recover-clock-overrides <PCI-slot>`.
Once cleanup is confirmed, boot normally. The command uses exact recorded
values and the Windows device binding, and requires no working NVIDIA display.

### Pascal firmware memory timing

In either skin, open **Timings → BIOS memory timing → Read P0 BIOS timings**.
Keep the selected GPU at P0 and a stable memory clock for the reading. Enter
one or more encoded values, choose **Review BIOS timing changes**, then
**Apply timings and refresh selected GPU**. The native editor uses temporary
driver records and works independently of nvtune and Python. It can edit 22
named timing fields across CONFIG0–5, including row timings, refresh, write
recovery and bus turnaround. tCL is read-only.

Druta reads the six controller words twice. Each editable field must agree
with the current firmware row. A masked field or an unavailable optional word
does not prevent editing other matched fields. An ambiguous row or a row
shared with another memory-clock band cannot be edited. Apply changes only
the selected bits and checks the reviewed controller words after restart.

If the GPU idles after the refresh, return it to the reviewed P0 memory clock
and choose **Verify P0 timing override**. **Restore original firmware settings
and refresh GPU** restores the original image. The firmware control is
available on Pascal when the adapter supplies the matching ROM layout and a
verifiable P0 controller reading.

On the MSI GTX 1050 Ti (GP107, driver 581.80), 19 timings were edited
individually at P0 and 3504 MHz. The controller confirmed each requested
change, including tRFC at 193 → 194 → 193 and tREFI at 6 → 5 → 6. Across the
six controller words, only the selected bits moved. All six words and the
original running image were restored before the next edit. tCL read 22 throughout.
tCCDL and tCCDS were ineligible because the controller and BIOS row differed.
[All 19 hardware trials](../../experiments/gp107-p0-individual-bios-timings-20261120.json)

An earlier tFAW trial at the same operating point measured 28 → 29 → 28
cycles, bringing the total to 20 verified timings on this 1050 Ti. The RTX
3070 Ti loaded its edited image and reported tFAW at 20 cycles throughout.
[tFAW trial records](../../experiments/firmware-tfaw-20261119.json)

## Native I2C controller path

On 64-bit processes, the core can use the NVAPI Ex I2C transport for explicit
controller discovery and reads. Native family paths cover MP2888 manual/coarse
offset recipes, MP29816, NCT3933, uP9512R, NCP4206 and iCX through their separately
qualified protocols. A fingerprint is a candidate, not a unique model
identification or a physical rail assignment. Scan and Read are user actions,
not ordinary telemetry polls. The panel offers IC discovery, friendly candidate
selection and compiled recipes; manual port/address entry is under Advanced routing. An
explicit scan selects a sole candidate; ambiguous results require selection.
Nominal request presets come from the qualified live command range/grid and
only stage a target. They are neither measured voltages nor factory defaults.
An implemented family does not qualify every recipe or layout; unknown methods
remain unavailable. The transport retains its nonvolatile/password command refusals.
The 32-bit Ex packet path is not qualified and declines these I2C operations;
that limitation does not disable the rest of the core.

Mutating requests require the I2C session gate and the selected recipe's current
identity, configuration, limits and verification checks. Scan, selection and
presets never verify or apply automatically. Recipes that require a low-clock
clamp own that request explicitly and restore controller state before releasing
it; independently qualified recipes do not acquire an unrelated clamp. Matching
no-effect profile paths retain their source-backed exemptions. Release restores
only owned requests. Failed or uncertain restoration retains recovery and blocks
conflicting selection/close until resolved. Controller readback is not an
independent physical-voltage measurement, and mock protocol coverage is not live
qualification of every family.

Daily profile files exclude I2C state. Typed standalone I2C profiles and selected
full-tune/catalog imports use separate fresh preflight, explicit Apply and
issued-only restoration paths described below. Load stages only; no controller
request is automatically applied at startup or after a driver reset.

Profiles are UTF-8 JSON, limited to 1 MiB, with duplicate-key rejection and
same-directory flushed temporary-file replacement. Save has no tuning side
effect. No profile loads at startup or after driver reset. Ordinary tuning stays
session-only; Reconnect closes/reopens without replay. Saving a file does not
authorize automatic application.

## Timings helper

Both presentations expose Timings. Locate an installed `nvtune.exe` with the
native file dialog, then explicitly Read timings (Classic reads once on its own
when a helper is known). The UI shows captured fields
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

## Standalone compatibility on older Windows

The legacy x64 package contains Classic and Focused, the native core,
the timing worker, and the read-only probe. It runs independently of Python.
It targets Windows 7 SP1 x64, Vista SP2 x64, and Windows XP Professional x64
Edition SP2. The optional Python plugin and a 32-bit XP GUI are outside this build.

The standard v143 GUI previously imported `TryAcquireSRWLockExclusive` through
the C++ runtime, which prevented it from loading on Vista. Its subsystem 6.0
header and Vista-era imports also excluded XP x64. The legacy build uses a
pinned copy of YY-Thunks for runtime fallbacks, including synchronization, and
sets the x64 loader floor to NT 5.2. Packaging checks the imports and headers
of all four binaries against x64 XP, Vista and Windows 7 export inventories.
The package has been exercised on Windows 10. First launch, rendering and GPU
control still need verification on each legacy OS.
[YY-Thunks](https://github.com/Chuyu-Team/YY-Thunks/tree/v1.2.3) ·
[Windows lock API requirements](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-tryacquiresrwlockexclusive)

On Windows 7 and Vista with WDDM, start with `druta-probe.exe`. Compare the
reported PCI addresses with the GPU you selected before checking its policies.
On the Windows 10 test rig, GDI display-name lookup found the GTX 1050 Ti at
`01:00.0`. Device-interface lookup also found the headless RTX 3070 Ti at
`02:00.0`. Druta uses these older methods when `D3DKMTEnumAdapters2` is missing.
Old NVIDIA drivers may expose different current and board-power requests.
XP x64 uses XPDM and has no WDDM escape for fine current policies or extended
XOC board power. Other controls discover their own NVAPI and NVML capabilities.

To build the ZIP from an x64 Visual Studio developer prompt:

```powershell
cmake -S native -B native/build/legacy-x64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DDRUTA_BUILD_UI=ON -DDRUTA_LEGACY_X64=ON
cmake --build native/build/legacy-x64 --target druta-legacy-x64-package
```

The output is `native/build/legacy-x64/Druta-native-legacy-x64-Release.zip`.
For an offline build, set `DRUTA_YY_THUNKS_ARCHIVE` to the official
`YY-Thunks-Objs.zip` from v1.2.3. CMake checks its SHA-256 in either mode.

## Build on Windows

Run from the repository root with CMake 3.20+, Visual Studio 2022 C++ tools and
a Windows SDK. Python 3 runs the PE import audit; pytest runs Python tests.
Checked-in dependencies require no build-time downloads: Dear ImGui/ImPlot and
FreeType pins and licenses are in [vendor/README.md](vendor/README.md); nlohmann JSON's MIT
license, pin and checksums are in [vendor/json/README.md](vendor/json/README.md).
Classic draws its text through FreeType, as released 1.9.1 does; Focused keeps
Dear ImGui's built-in rasteriser.

Portions of this software are copyright © 2026 The FreeType Project
(https://freetype.org). All rights reserved.

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
& ./native/build/ui-msvc-x64/Release/druta-ui.exe --preview --focused --scale 1.5 --capture C:/existing-folder/focused.bmp
```

| Option | Behavior |
| --- | --- |
| `--preview` | Fixture layout; opens no GPU handle and performs no tuning or telemetry reads. |
| `--core ABSOLUTE_DLL` | Load the explicitly selected core DLL in live mode. |
| `--gpu PCI_SLOT` | Select by PCI slot; the live adapter picker can change it later. |
| `--width N`, `--height N` | Initial client size in physical pixels, each 640–7680. Focused scales each unspecified dimension from its 1280×800 default at 100% and fits the window to the monitor's work area. |
| `--scale N` | Explicit UI scale from 1 through 3, including fractional values. |
| `--verbose`, `--classic` | Start in the default Verbose (Classic) layout. Without an explicit size it opens at 1544×1300 at 100% scale, clamped to the screen. |
| `--focused` | Start in the Focused layout. |
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
  See the legacy x64 package section above for current build and audit coverage.

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

The preceding observations are historical, adapter-specific qualification.
Subsequent checkpoints below implement Monitor, saving controls and selected
profile composition. Current implementation gaps are additional ROM/runtime
layout qualification, installed-scaffold evidence and the narrow persistent relationship-
weight workflow. Physical timing/controller effects, actual device restart and
Vista/7 execution retain their scoped validation limits. See the current
[completion ledger](validation/PARITY-COMPLETION.md) and [backlog](../../backlog.md).

### Device menus and selected-request profiles

Both presentations expose Device and Help menus. Device can refresh capability
readbacks without changing the page or drafts, capture/copy/save a selected-GPU
report, open a GPU in a second window (Classic: any enumerated card; Focused:
the selected one), and navigate to timing-helper setup. The child window receives the exact core DLL, PCI slot and expected UUID; it
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

### Advanced feedback and voltage policies

Both layouts expose **Power feedback ratios**, **Voltage admission and V/F
policies**, and read-only **Power arbitration**. Classic shows the first as an
inline "Power-policy feedback adjustment" block under Power policies, and keeps
actions that span every family under "Undo, recovery and profiles". Use each panel's Read/Refresh
button to capture current state. Rendering, normal telemetry polling and opening
the application do not acquire these diagnostic tables.

Power feedback edits one policy's upper or lower ratio. 100% is raw 4096; these
are configured feedback ratios rather than measured watts or amps. Apply checks
the displayed field against a fresh read, preserves other fields and verifies
the resulting packet. Undo restores the preceding edit; Restore captured initial
returns that field to its value before this session's first edit. Uncertain
writes keep a selected-field recovery record instead of replaying a saved table.

Voltage admission controls whether driver voltage requests are admitted; it does
not specify a voltage. V/F monotonicity and smoothing change only the selected
host policy flags. Apply, Undo and captured-initial restoration verify the
requests; host readback does not establish their physical effect. Unsupported
groups are independent, and transient failures retain visibly stale last-good
state. These controls write no registry settings and are never reapplied on
startup or after a driver restart.

Power arbitration shows requested and effective policy values and available
host/firmware inputs. Its packets are acquired sequentially, so changing
firmware votes are not an atomic snapshot. Missing or unknown fields remain
unavailable or raw; they do not disable ordinary power controls.

## Profile catalog, discovery and timing diagnostics

**Profile catalog...** (Focused) or **Profiles...** (Classic) opens a shared
workspace. Add saved files, select their included controls, and compose a staged
profile. Preflight checks every component before Apply can issue a write; Apply
repeats that check. Duplicate targets and incompatible adapters are rejected.
The worker verifies the complete selected result and retains grouped Undo,
including partial failures. Capture and Save never apply settings.

Supported catalog formats are native daily/advanced controls, V/F curves,
selected rail/power policies, timing fields and iCX, plus Python tuning-action,
rail-limit and coupled power-action profiles. Python power imports retain the
distinction between requested rows and explicit pins. Unsupported Python scopes
are named in the catalog. iCX applies separately because it owns its controller
and clock lifecycle. Timing-plus-memory compositions qualify their initial band,
apply timing fields before memory offsets, and restore memory requests before
timing Undo. Board-only Python power snapshots
without qualified peer-preserving semantics are refused before writes.

I2C Connect / Scan can remember a UUID-bound route and recipe under the local
Druta state directory. This stores a discovery hint only: connection still probes
and reads the controller, and does not restore gates, verification or requests.
Full scan bypasses hints. Progress and Cancel remain available while discovery
runs; cancellation preserves the established selection and stops at a read-only
boundary. Partial candidates remain visibly unverified.

Explicit timing capture also attempts the helper's read-only partition snapshot.
Partition words are compared only after matching target, broadcast words and
operating state. Optional failures retain a matching last capture as stale.
Cycle and nanosecond displays require understood field metadata, a stable clock
and the current adapter's memory divisor; split fields and RFC/WL do not acquire
misleading nanosecond values. Missing evidence remains raw or unavailable.

## Voltage tables and temporary timing capture

Advanced contains independent voltage request tables and device switch delays.
Read is explicit. The request table shows stored signed requests, including the
two VCRIT fields; these are not measured voltages or verified physical thresholds.
Apply rechecks the current target, complete request inventory and admission gate,
preserves unrelated fields, and verifies the full result. Undo, captured-initial
Restore and recovery remain explicit. Selected native profiles save typed values;
Load stages a draft and never enables a gate or applies settings automatically.

Semantic REL/constants now have a production current-ROM graph, independent
policy and retained-request provider for the understood layouts. Complete loaded
graph identity must match before a role becomes writable; reading numeric VFE
ordinals alone is insufficient. The tested 3070 Ti selector layout remains
unqualified in both this port and the Python reference. Installed-scaffold offset
evidence is still pending. Device delays and ordinary rail requests retain their
independent capability paths. See the [twentieth checkpoint](validation/2026-11-13-parity-twentieth-increment.md)
for the qualified scope, tests and read-only hardware results.

**Capture at performance band** validates the selected timing helper and uses
qualified current P0 first. With tuning unlocked it can acquire a temporary
legacy/V/F hold and wait up to six seconds. Otherwise, or after verified cleanup
of an unsuccessful hold, it can run an optional CUDA copy workload on the exact
selected PCI device. Capture occurs while the workload is active. Locked controls
permit the workload but never a tuning hold. Qualified P0/P2 uses the current
adapter's existing top-band checks; below-band registers remain raw diagnostics.

The optional `druta-ui-timing-cuda.exe` must remain beside `druta-ui.exe`; the
normal UI build includes it. The child loads the installed CUDA driver from the
Windows system directory; no CUDA toolkit is needed. Missing exports or helper
files produce a specific failure without disabling ordinary timing reads.
One 60-second capture deadline covers workload and timing-helper waits; owned
process termination has a separate bounded cleanup grace. An unconfirmed exit
retains recovery and blocks conflicting controls. Parent driver/kernel calls
cannot be forcibly preempted by this process deadline.

Cancel, Pause and close signal cancellation immediately. Only this capture's
workload and temporary hold are released; fan, board power, curve shape and rail
headroom are unchanged. Resume does not restart a workload. **Auto-capture on
stable P0 entry** uses ordinary telemetry only and never starts CUDA or a hold.
Classic's "Auto-capture at P0" is armed by default and gated like Python: an entry
seen while a read runs is used up, a captured top-band clock is skipped, and
automatic reads are 30 s apart.

The [nineteenth checkpoint](validation/2026-11-13-parity-nineteenth-increment.md)
records the integrated build, 118 passing suites and 22 renderer phases. On the
documented Windows 10 adapter, the live fallback captured stable P2 at 9427 MHz
below its qualified top band and correctly retained diagnostic-only registers;
the exact-BDF worker also passed owned cleanup. This is not qualified timing-write
evidence, a lower-P2-band equivalence claim, or validation on other adapters/OSes.

**Policy workspace...** offers labels tied to the adapter and policy binding,
plus selected maximum previews. It lists skipped policies and uses fresh selected
requests and grouped Undo on explicit Apply. Labels persist as display metadata
only. Connector presets and free text propagate to fresh same-channel bindings.
Explicit Import Python labels accepts matching UUID/policy/channel/type entries;
unmatched and unknown native metadata is preserved. Current-per-wire colors use
reported current divided by the selected connector's wire count, with the even
split assumption labelled. They are not measurements of individual wires.


## Power saving, clock gating and recovery

Both skins expose Power saving and Clock gating / PCIe from Advanced and the
Clocks menu. Read is explicit; opening, rendering and ordinary telemetry never
capture those tables. Each family reports its own capabilities. The interface
preserves unknown request fields, stale readings and uncertain ownership.

GR/individual gating and DIFR hold counted or profiler requests. Release frees
only this application's owned request; it never replays another application's
state. Idle thresholds and adaptive power have separate semantics: aggregate
enabled state is not ownership, and Allow does not restore idle thresholds.
Power-saving profiles stage individual requests; each Apply has its own Undo.
Clock profiles preflight the complete selection and retain typed partial Undo.
A driver restart starts a fresh session and receives no tuning/profile requests.

Panic / restart GPU (Classic: "Panic Button (PnP Reset, Deeper than
Shift+Ctrl+B)") verifies one selected NVIDIA PCI display device through
SetupAPI. A separate helper waits for Druta to exit, restarts exactly that PnP
instance, records the result, and opens a fresh Druta. The restart handoff drains
the admitted worker call and exits without
attempting restoration of uncertain tuning requests. If the old process cannot
finish, the helper times out without a restart or second window. No system reboot
is issued. Device discovery runs off the renderer thread; the selected session
is checked again before handoff. Reboot-required and uncertain results remain
visible in a bounded recovery receipt.

Native source qualification and physical testing are distinct. The current
3070 Ti exposes GR/MS and ELCG/BLCG/SLCG inspection; idle/adaptive and PCIe target
layouts remain outside the traced support on that adapter. New writes, their
physical effects, actual PnP restart and Vista/7 execution have not been qualified
by this checkpoint. See the completion ledger for remaining firmware providers
and the persistent relationship-weight configuration exception.

## Selected profiles and timing exchange

Clocks opens the voltage-policy and power-feedback panels in either skin.
Save captures current selected requests; Load stages a draft for explicit Apply.
Admission uses its own transaction, while monotonicity and smoothing may be
selected together. Feedback profiles preserve raw U4.12 values, preflight the
whole selected range, order coupled lower/upper changes, and retain grouped
Undo for any issued writes. Loaded policy metadata is not silently replaced by
later readings. Reload or discard a draft when its policy binding changes.

The profile catalog now saves and loads individual fan requests, including
mixed manual/automatic policies, and imports released Python fan profiles.
Undo matches current fan IDs and verifies restoration before releasing its
receipt. It preserves the recorded request when restoration is partial or the
adapter changes. Automatic fan target fluctuations are not manual setpoints.

Timings accepts released Python timing profiles, nvtune-profile-1, and flat
fields exchanges using fresh helper metadata and a qualified current operating
band. Aliases are resolved only by that helper; raw register/partition snapshots
and mixed unrelated tuning scopes are refused. Catalog timing and ordinary or
additional memory offsets use whole preflight, timing-first Apply, fresh final
selected-field verification and memory-first Undo. Their original values remain
bound through dispatch; a changed original cannot become a new Undo baseline.
Other outstanding profile adapters and composition
restrictions remain listed in [the completion ledger](validation/PARITY-COMPLETION.md).

The catalog also composes voltage admission/V/F flags and power-feedback
profiles. Select the fields from each file, then Preflight and Apply. Disjoint
feedback bounds are checked together and ordered to preserve a valid range;
V/F flags share one selected write. Admission is enabled first or disabled
last. Final readback covers every selected field, and grouped Undo retains
only requests that were issued, including uncertain partial writes.

The standalone voltage-request, device switch-delay, low-power and PCIe-speed
editors now accept their released Python profiles. Load checks saved identity,
units and selected metadata against current adapter evidence and stages local
drafts. Voltage requests and device switch delays can also be selected in
the catalog alongside native voltage request/limit profiles. Catalog capture
selectors open the appropriate Advanced panel in either skin without reading
or applying a control. Selected low-power and clock/PCIe profiles also compose
in the catalog, with capture selectors navigating to their Advanced panels in
either skin without a read or write. Catalog Apply is explicit and session-only.

Saving and clock transactions retain typed issued-prefix recovery and previous
Undo; final verification includes selected no-op fields and recovery checks all
candidates before restoring any. Adaptive requests with unknown prior state are
refused. Adaptive plus idle profiles apply adaptive first and the selected idle
thresholds last; Undo restores their original typed requests in that order.
Exclusive GR/individual or GR/DIFR handovers release the witnessed owner before
acquiring the next. Simultaneous overlapping ownership cannot be qualified.
Qualified ELCG/SLCG opposite modes release and verify the old request before
acquiring the new one. BLCG exposes only Disable and Release.
Ownership epochs prevent stale Undo after release and reacquisition; typed
handover retains earlier catalog history and panel Undo/recover uses the matching
receipt. Unknown allocation or FREE outcomes leave cleanup unresolved and block
further candidate control or repeated FREE; only guaranteed non-dispatch allows
retry. No implicit reset is issued. Independently resolved PCIe recovery is not
revived by later Undo. Adaptive recovery remains pending if restoration reads
fail, and an allocated GR object whose request was not dispatched is cleaned up
once.

Catalog voltage composition checks all selected fields first, projects an
explicitly selected admission enable during preflight, and checks actual
admission when writing. Admission enable is ordered first and disable last.
Final verification includes selected no-op fields; issued-only typed receipts
retain partial recovery and previous Undo. Profiles never apply on restart.

The catalog also imports selected controls from unscoped Python schema-2 full
tunes. It retains the complete source alongside an explicit target selection.
Indexed curves bind to fresh current geometry; the old format contains no
historical point voltages. Memory uses saved driver units on the same memory,
then checks the current scale and grid. Per-fan policies, raw/estimated rail
requests and clock/voltage offsets use their existing qualified writers.
Coupled power preserves explicit pins and freshly derived unpinned requests
after board and named-current changes. The whole selection is preflighted
before any write, and partial failure retains grouped Undo. Full-tune NCT3933
current-DAC settings apply as a separately selected scope after explicit live
controller selection and I2C opt-in. The released recipe hash, route, identity
and saved configuration must qualify; configuration is never written. Exact
output bytes and their issued originals are retained for Apply and Undo.
Selected full-tune imports also retain uP9512R's five offsets and enable,
NCP4206's raw VID command and enable, and exact coarse MP2888/MP29816 offsets.
They validate the released recipe, fresh route/configuration and representable
units; they do not flatten the saved state to a scalar voltage. Qualified legacy
controller requests compose in voltage/controller-before-clock order and restore
before ordinary grouped Undo. iCX composes last with compatible ordinary requests
and restores first; timing and a second controller remain incompatible with its
retained floor clamp. See [fourteenth](validation/2026-11-13-parity-fourteenth-increment.md),
[fifteenth](validation/2026-11-13-parity-fifteenth-increment.md),
[sixteenth](validation/2026-11-13-parity-sixteenth-increment.md) and
[seventeenth](validation/2026-11-13-parity-seventeenth-increment.md) evidence.

Full-tune power names are separately selected display metadata with fresh
adapter/policy/channel/type matching, Unicode validation and immutable UI-thread
completion delivery. Selected-edit conflicts are preserved, chained metadata
Undo is independent of hardware failure, and failed note-file saves remain
visibly unsaved with explicit retry. Metadata-only operations do not require
tuning unlock or working ordinary telemetry when paired identity remains valid.
See the [eighteenth checkpoint](validation/2026-11-13-parity-eighteenth-increment.md).
Unknown or unqualified selected source scopes still refuse before effects; they
are never silently discarded. None of these imports authorizes startup/reset replay.

The voltage table section in both skins includes **Read installed offsets**.
It recognizes an existing Python scaffold receipt against the current adapter,
ROM, variable inventory and loaded equation packets, then displays the current
signed requests. Saved receipt values are historical. Inspection performs no
tuning writes; selected offset Apply, Undo and profiles remain pending. Missing
or unreadable receipts do not disable independent voltage request controls.
See the [twenty-first checkpoint](validation/2026-11-13-parity-twenty-first-increment.md).
