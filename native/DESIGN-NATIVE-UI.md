# Native UI/UX and Python compatibility plan

Initial direction agreed 2026-11-11; live implementation checkpoint 2026-11-12.
Base: `8ceb01896e5a4c20d8b3ba327db1ae16f367ac66`, [PR #83](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/83),
stacked on the first RTX 3070 Ti qualification fixes in PR #82.
This plan covers the new C++ desktop interface. [DESIGN-FRONTEND.md](DESIGN-FRONTEND.md)
records the existing Python-interface/C++-backend integration.

## Compact handoff

- The initial fixture shell remains available with `--preview`. A live Session,
  Daily actions, scoped Undo, manual profiles, staged V/F planning, qualified
  Advanced controls and a bounded native MP2888A I2C workflow are implemented;
  see the continuation checkpoint below.
- Keep Windows Vista and Windows 7 in the first-version target, alongside
  modern Windows. Legacy compatibility is an acceptance requirement, not yet
  a demonstrated result for the new executable.
- Default to **Focused**: Daily, V/F curve, Advanced and Monitor. **Classic**
  uses a Druta 1.9.1-style composition, with the same application state and
  retained V/F editor popup; it is not merely a verbose-text setting.
- Preserve standalone Python experiments through the existing C ABI and Python
  compatibility facade. Attaching Python to the running UI session is undecided.
- Preserve the performance policy shipped in PR #83: adaptive visible rendering;
  **Always refresh** off by default; ordinary minimization pauses rendering and
  telemetry; enabling the toggle keeps both running while minimized; explicit
  **Pause** overrides it. Required active-control maintenance continues.
- Ordinary tuning remains session-only. Driver restart is the user's reset:
  never replay tuning at startup or after restart. Saved profiles load explicitly.
- A multimeter confirmed an I2C response for the tested MP2888A candidate route
  and operating point in this session. Qualification of other routes,
  controllers and configurations remains open.

## Presentations and interaction

**Focused** puts core/memory offsets, board power, fans and a compact live summary
on Daily tuning. Give the V/F editor its own usable workspace. Advanced contains
domain clocks, rails/current/policies, power saving, I2C, registry/BIOS and timing
tools. Monitor provides configurable plots and sensor tables. Profiles and
history remain reachable from every page.

**Classic** recreates the 1.9.1-style composition and familiar grouping,
with bounds, expanded readings, diagnostics and the operation log. Separate
theme from information density internally, even if the initial settings offer two simple presentation presets.
Switching presentation must not reopen a device, write hardware, lose drafts,
change ownership or duplicate command/recovery logic.

Shared interaction rules:

- Keep selected GPU identity and active hold/recovery state visible.
- Use short labels, consistent units and aligned numeric entry. Expand Details
  for provenance, packet information and long explanations.
- Separate editable targets, configured readback and live telemetry. Expand
  mismatches or pending changes; polling never replaces an unfinished edit.
- Show concise action results with requested/actual values, time, scope and
  individual failures available in Details. Failed writes and uncertain
  restoration remain visible in both presentations.
- Keep supported Advanced controls discoverable. A transient read failure
  preserves established capabilities, current valid state and drafts; retry
  reads within a bounded budget. Unsupported capability is a separate state.
- Show current curve, draft and operating point together; explain planned
  effects before Apply. Distinguish Undo edit, Undo hardware write and Release.
- Preserve explicit Apply/Reset behavior. Dragging does not write hardware;
  layout cleanup adds neither speculative SET retries nor blanket prompts.
- Smooth scrolling and transitions must not trigger driver calls per frame or
  present interpolated numbers as measured telemetry.

Review both presentations at 1024×768, 1366×768 and larger sizes; test 100/125/150/200%
scaling, keyboard navigation, exact numeric entry, Unicode paths/text, unavailable values and long errors.

## Rendering and platform baseline

Implemented stack: C++17, Win32, pinned Dear ImGui and ImPlot, with the upstream
Direct3D 9 renderer as the common Vista/7/10/11 candidate. The manual-profile
service also vendors pinned MIT-licensed nlohmann JSON. D3D11 can be evaluated
later; the first version must
not need Vista's D3D11 platform update merely to draw.

Qualification targets are Vista SP2 x64, Windows 7 SP1 x64, Windows 10 x64 and
Windows 11 x64. XP and an x86 UI release remain separate scope decisions;
choosing D3D9 does not establish support for either. Preserve the core's existing
architecture support and XP import audit independently of the UI target.

Audit the compiler/runtime and dependency imports, not just PE subsystem
versions. Resolve optional newer OS functions conditionally with older
fallbacks. Prove launch, fonts, DPI, resize/minimize, device loss/recreation and
shutdown on each target OS before claiming support or substantially expanding
the control inventory. Do not weaken the core DLL's checks for UI imports.

Performance policy:

- Render normally during interaction, then reduce visible idle frame rate with
  immediate input wakeup. Use the current 10 FPS target/0.75 s grace as the first
  baseline; measure it on target systems before treating it as final.
- Update changed/visible controls; avoid unchanged layout work. Keep expensive
  diagnostics on demand. Snapshot processing is independent of painting.
- **Always refresh** starts off. On keeps rendering and telemetry running even
  when minimized. Explicit Pause takes precedence until the window is restored.
- Pause ordinary telemetry, capability retries and drawing together. Finish
  already-started operations and run only an active mode's required maintenance.
  Maintenance deadlines are independent of rendering deadlines.
- Restore through the taskbar or a global shortcut, retain the existing session
  and request fresh readings. Keep old values marked stale until then.
- Measure CPU time and GPU engine activity, including dragging, covering and
  minimizing the window. A graphics API choice alone is no performance proof.

Upstream references: [Dear ImGui backends](https://github.com/ocornut/imgui/blob/master/docs/BACKENDS.md),
[Win32 backend](https://github.com/ocornut/imgui/blob/master/backends/imgui_impl_win32.cpp),
[ImPlot](https://github.com/epezent/implot). These are implementation inputs,
not evidence that a particular shipped toolchain works on legacy Windows.

## Shared services and Python interposer

```text
Focused / Classic views
          |
Presentation models: drafts, immutable snapshots, history
          |
Application services: commands, sessions, Undo, profiles, recovery
          |
Typed C++ CoreClient ---- drutacore C ABI ---- Python compatibility facade
                                |                         |
                           C++ backend             existing experiments
```

The C ABI remains the stable boundary. Preserve `druta.native.Gpu`, `druta.gpu_backend`
and paired companions: signatures, units, results/exceptions, target checks and lifecycle.
Use parity/contract tests; do not expose C++ layouts or introduce a catch-all forwarding proxy.

| Existing Python use | Required behavior |
| --- | --- |
| Factory/native scripts | Continue through `gpu_backend.open_gpu(kind="native")` and the facade. |
| Direct `nvbackend.GPU` scripts | Keep the reference module usable; migration is explicit. |
| Raw NVAPI/NVML/escape probes | Remain standalone with process-local handles, ctypes packets and hooks. |
| Python/DPG UI tests | Retain reference tests; add native behavioral equivalents without emulating DPG. |
| Optional attached scripts | Use explicit supported commands/snapshots; unsupported operations fail visibly. |

Standalone scripts and the UI share backend code but own independent sessions;
the handle mutex does not serialize separate processes. No Python installation is required
for the native UI. Retain the current Python application during native feature migration.

If attached Python is selected later, use a versioned local IPC service with
caller access control, supported-command negotiation, exact units, adapter
identity and session generation. Commands enter the UI's existing queue and
readback/history/ownership paths. Never send driver pointers or execute arbitrary
Python in the UI, silently open another session, or make `with gpu._lock` a no-op.
Preflight/write/readback is one server command; operation IDs resolve uncertain
results without repeating SET. Client-owned temporary requests and write/batch
leases require explicit ownership contracts. Attachment does not block the shell.

## Backend contracts the UI must preserve

Use one Session per selected adapter, an ordered command worker and immutable
snapshots. Coalesce polls instead of accumulating reads. Tag results with adapter
identity and session generation so late work cannot affect a newly selected card.
Drain users and lock waiters before `dc_gpu_close`; callbacks cannot unwind across
the C ABI or depend on the render thread. A worker does not isolate driver faults.

Port application services before enabling their controls:

- Fresh request/range readers use existing core implementations, including
  `Telemetry::offset_range`, rather than copied UI-side driver logic.
- Scoped Undo/manual profiles follow `write_tuning_action`, `tuning_profiles.py`
  and `profiles.py`: fresh capture, exact units, identity/schema validation,
  existing setter, independent readback and honest partial results.
- Hold/recovery follows `release_on_exit`, `vfheadroom.py` and verification
  contexts: restore owned headroom/manual voltage before releasing its point;
  retain recovery material if restoration is uncertain.
- Restart/relaunch follows `devicerecovery.py`: drain handles, validate the
  device instance, restart, record the result and reopen without tuning replay.
- Port remaining runtimes, controller recipes and tool workflows as services,
  not logic embedded in widget callbacks.

Save is read-only and Load explicit. The narrow persistent registry/VBIOS
relationship-weight exception remains a separate workflow. Keep stored requests,
effective limits, estimates, captured initial state and physical measurements
distinct. Gate by architectural support and current capabilities/API geometry,
never a driver's version or a developer's device/VBIOS identity.

## Implementation increments and acceptance

1. **Native visual shell — implemented.** Build a launchable Win32/D3D9 frontend
   with Focused/Classic fixture views, navigation, draft retention, resize and
   refresh/Pause behavior. Label fixture data clearly; no GPU tuning calls.
   Pin dependencies, add model tests and an import audit. Record which OS/toolchain
   checks actually ran; untested Vista/7 launch/device-loss checks remain open.
2. **Read-only session and interop — implemented on Windows 10.** Typed
   CoreClient, handle lifetime,
   adapter selection, telemetry/history and last-good states. Verify Python
   contracts, stale-result rejection and responsiveness under stalled reads.
3. **Daily actions — implemented, with scoped Windows 10 hardware evidence.**
   Memory offset: whole-MHz dragging, fractional
   typing, Apply/Stock, scoped Undo and exact readback. Test accepted, quantized,
   refused and unreadable outcomes without assuming the 3070 Ti's step elsewhere.
   Core, board power, fan and boost now use fresh capture/readback and scoped
   Undo too. General user-facing hold modes remain disabled pending their recovery lifecycle.
4. **V/F and owned controls — partial.** The point editor, local edit history,
   delta Apply/Reset and hardware Undo are implemented with physical-MHz/raw-unit
   divisor validation. De-flatten, ramp and rephase stage pure C ABI plans;
   owned holds and headroom remain pending.
   Port marker/release/recovery before enabling holds;
   run the same command tests through both presentations.
5. **Profiles and ordinary-control parity — partial.** Native Daily files,
   manual Load/aggregate Undo and Python memory/boost/domain/NVVDD tuning-action imports are
   implemented. The full V/F table is not saved by native Daily profiles.
   Version 2 includes qualified advanced offsets; individual policies, rail
   limits and full-curve profiles remain pending. Exercise partial failures and
   close/switch during work; profile application remains an explicit action.
6. **Advanced parity — partial.** Qualified domain and rail offsets, XOC,
   policy/current rows and selected rail limits are implemented. Native I2C
   discovery and MP2888A manual-command Verify/Apply/Release are implemented;
   other I2C controllers, registry/BIOS, power-saving and timing workflows remain
   incremental work with scoped hardware evidence.

The first shell is a reviewable development increment, not a feature-complete
replacement. The first useful tuning milestone combines qualified Vista/7
behavior, a complete memory/Undo action and the preserved Python experiment path.
Full replacement waits for the service/workflow parity matrix and OS qualification.

## Continuation checkpoint: live Daily actions and manual curve edits

`native/ui/druta-ui` now implements increments 2/3 and part of 4/5 after the
original fixture shell. [ui/README.md](ui/README.md) documents integrated and
standalone builds, staged/explicit DLL loading, `--preview`, `--page curve` and
the read-only `--live-smoke-test` harness. The original fixture evidence remains
in [ui/validation/2026-11-11-smoke.json](ui/validation/2026-11-11-smoke.json).

The typed CoreClient and ordered Session own one selected adapter, coalesce
polls, publish immutable snapshots, preserve last-good state with stale markers
and reject late generations. Commands and reads do not run in widget painting.
The fresh `dc_gpu_clock_offset_range_get` C ABI reader is shared with the Python
facade; Python experiments and the existing Python frontend remain usable.

Daily core/memory offsets, board request, fan and boost have explicit
Apply/Stock/Auto, capture/readback and scoped Undo. Daily also exposes the
independently identified NVVDD core-current request and qualified rail limits.
XOC can expose the core's 1,999 W software request envelope; each Apply still
requires fresh adapter-route and bound validation, which may permit less. Core
Undo includes the original curve; power Undo includes understood peer requests and session pins;
fan Undo retains policy as well as level. Their independent capabilities keep
partial/transient reads separate from unsupported operations. Focused uses
compact rows; Classic uses the 1.9.1-style composition with the same drafts and
commands. Both retain the V/F editor popup.

The curve workspace uses the verified divisor for physical-MHz editing and raw
point requests. The expanded popup has large outlined dots, vertical drag, WASD,
Shift for three steps and local Ctrl Undo/Redo. De-flatten, ramp and rephase
stage through the pure C ABI from the current draft. Their prediction is the
core software model with a grid-derived bound, not a physical measurement. Full
expected geometry and divisor are checked on Apply/Reset; stale drafts require
an explicit fresh rebase. Hardware Undo remains distinct from edit Undo.

Qualified clock-domain, NVVDD and MSVDD offsets, explicit XOC, individual power
policy/current requests and rail-limit fields are available in Advanced. Rail
controls present the base-plus-delta absolute estimate of the signed request;
Initial is a captured request, not a verified factory default. Rail limits have
a separate session gate and selected-field Undo. Risk scoring follows the
existing palette: XOC and writable armed rail limits score 1 each, and an armed
identified writable I2C controller scores 2; stock is neutral, 1 amber, 2 red,
and 3 or more crimson. Unknown states stay distinct; color selection does not
probe the bus. The native I2C path is explicit and on demand: Ex transport on
64-bit processes, MP2888A candidate fingerprinting, and the manual `0x21`/`0xE1`
command/mode path. The fingerprint is not a unique model ID or physical rail
assignment. The 32-bit Ex path remains unqualified and refuses these I2C
operations without disabling the rest of the core.

Explicit native profiles support qualified Daily settings and version 2 advanced
offsets; Python tuning-action import includes memory, boost, clock domain and
NVVDD offset. Individual policies, rail limits and full curve tables are not
saved. Profiles neither enable gates nor apply themselves at startup or after a
driver reset. I2C settings are not saved or automatically applied by these
profiles. Full profile parity remains open.

I2C Apply requires the session gate, a successful bounded A–B–A response Verify
and an owned driver-enumerated low-clock clamp. Release restores the captured
controller request before releasing the clamp. Uncertain restoration retains
recovery and prevents GPU switching or session closure; Scan and Read are
explicit actions rather than ordinary telemetry polls. The GUI Max it and Reset
all workflows remain pending; the existing Python Max it with the C++ backend
was used for the multimeter baseline.

Windows 10 x64 build 19045 / RTX 3070 Ti evidence on 2026-11-12 includes exact
request/readback/Undo round trips: memory 32→33→32 in its reported MHz scale,
core 0→15→0 MHz, board 310→309→310 W, fan 30→31→30% and boost 0→1→0%.
Original scalar requests and the full curve were verified after each helper;
power peer requests/pins and captured fan policies were checked by the backend.
Curve point 0 raw delta 0→-15000→0 restored all 127 deltas/geometry. A native
profile exercise captured raw memory 512/scale 16, changed to 528, then used
manual Load/Undo to restore requests. These observations qualify only this
adapter/configuration and do not establish physical rail effects or constants
for another board.

All 11 live renderer phases passed, including four D3D9 resets; ordinary
minimization and explicit Pause produced zero frames/completed reads, while
Always refresh produced 115 frames and two completed reads during two seconds
minimized. These renderer resets do not test GPU-driver restart persistence.
Release checks passed: 203 view, 171 client, 92 curve-model, 61 Session, 36
profile I/O, 48 policy and 16 PE-policy checks, plus 57 targeted Python tests.
The independent desktop helper timed out twice with
`window capture timed out: timed out waiting on channel`; no physical inputs
were issued through it. The [live evidence record](ui/validation/2026-11-12-live.json)
contains the raw results and final build hash. D3D9 BMP review succeeded, but physical input, shortcut
delivery and broader accessibility qualification remain open.

The later control-arc evidence is in
[ui/validation/2026-11-12-controls.json](ui/validation/2026-11-12-controls.json).
On the tested RTX 3070 Ti, Crossbar's configured request went 0 → -1 → 0 MHz;
all surfaced advanced/rail requests and the complete V/F curve were checked as
restored. This is limited to that adapter and operating point and does not
establish physical response or another board's behavior.

On the tested RTX 3070 Ti/configuration, a native I2C probe found an MP2888A
candidate at port 1, address 0x20, initially reporting 676 mV. Nominal
900 → 925 → 900 mV controller-response Verify passed and restored its captured
entry before releasing the clock clamp. The existing Python Max it path with the
C++ backend established the meter baseline at fan 100%, power request 500 W,
boost 100% and a 1,093.75 mV V/F request; the meter read 1.11 V.

At the owned 405 MHz clamp, native Apply of 1,100 mV read back the exact
request and the controller reported 1,066 mV; the initial 1.06 V meter reading
was truncated and is excluded from the quantitative comparison. A 1,125 mV
request reported 1,091 mV at the controller and 1.097 V at the meter; returning
to 1,100 mV reported 1,067 mV and 1.071 V; repeating 1,125 mV again reported
1,091 mV and 1.097 V. The meter therefore repeated a 26 mV change for a 25 mV
nominal command change. Release verified captured registers before releasing the
clamp; afterward the controller reported 676 mV at 210 MHz and the meter read
675 mV. The helper exited with no ownership remaining. This response is scoped
to this candidate route, operating point and session. Nominal command is not
measured voltage; no factory default, gain, offset or behavior on another board
or controller is established.

The read-only core power-bounds probe on this adapter reported normal
100,000–500,000 mW and XOC 1–1,999,000 mW. These are observed request bounds
for this route, not universal hardware limits.

Remaining work includes general owned holds/headroom/recovery, GUI Max it and
Reset all, I2C qualification on other devices and controller methods,
registry/VBIOS relationship overrides, power saving, timings, configurable
Monitor workflows, full profiles and Vista/7 launch/render/DPI/device-loss/
shutdown qualification.
The Python frontend and experiment facade remain available. This checkpoint is
not full frontend parity.
