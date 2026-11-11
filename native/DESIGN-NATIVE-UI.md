# Native UI/UX and Python compatibility plan

Agreed direction and implementation checkpoint, 2026-11-11.
Base: `8ceb01896e5a4c20d8b3ba327db1ae16f367ac66`, [PR #83](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/83),
stacked on the first RTX 3070 Ti qualification fixes in PR #82.
This plan covers the new C++ desktop interface. [DESIGN-FRONTEND.md](DESIGN-FRONTEND.md)
records the existing Python-interface/C++-backend integration.

## Compact handoff

- Start the native frontend with a functional, fixture-driven visual shell;
  do not enable GPU tuning in that first increment.
- Keep Windows Vista and Windows 7 in the first-version target, alongside
  modern Windows. Legacy compatibility is an acceptance requirement, not yet
  a demonstrated result for the new executable.
- Default to **Focused**: daily tuning, Advanced and Monitor. Offer the existing
  dense organization as **Classic / Verbose**, using the same application state.
- Preserve standalone Python experiments through the existing C ABI and Python
  compatibility facade. Attaching Python to the running UI session is undecided.
- Preserve the performance policy shipped in PR #83: adaptive visible rendering;
  **Always refresh** off by default; ordinary minimization pauses rendering and
  telemetry; enabling the toggle keeps both running while minimized; explicit
  **Pause** overrides it. Required active-control maintenance continues.
- Ordinary tuning remains session-only. Driver restart is the user's reset:
  never replay tuning at startup or after restart. Saved profiles load explicitly.
- Direct I2C physical-effect qualification remains parked for the owner's
  multimeter session. It does not block frontend work.

## Presentations and interaction

**Focused** puts core/memory offsets, board power, fans and a compact live summary
on Daily tuning. Give the V/F editor its own usable workspace. Advanced contains
domain clocks, rails/current/policies, power saving, I2C, registry/BIOS and timing
tools. Monitor provides configurable plots and sensor tables. Profiles and
history remain reachable from every page.

**Classic / Verbose** retains familiar grouping, bounds, expanded readings,
diagnostics and the operation log. Separate theme from information density
internally, even if the initial settings offer two simple presentation presets.
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

Proposed stack: C++17, Win32, Dear ImGui and ImPlot, with the upstream Direct3D 9
renderer as the common Vista/7/10/11 candidate. Pin dependency revisions during
the compatibility spike. D3D11 can be evaluated later; the first version must
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

1. **Native visual shell — commence now.** Build a launchable Win32/D3D9 frontend
   with Focused/Classic fixture views, navigation, draft retention, resize and
   refresh/Pause behavior. Label fixture data clearly; no GPU tuning calls.
   Pin dependencies, add model tests and an import audit. Record which OS/toolchain
   checks actually ran; untested Vista/7 launch/device-loss checks remain open.
2. **Read-only session and interop.** Add typed CoreClient, handle lifetime,
   adapter selection, telemetry/history and last-good states. Verify Python
   contracts, stale-result rejection and responsiveness under stalled reads.
3. **First complete action.** Memory offset: whole-MHz dragging, fractional
   typing, Apply/Stock, scoped Undo and exact readback. Test accepted, quantized,
   refused and unreadable outcomes without assuming the 3070 Ti's step elsewhere.
   Then core, board power and fans; recovery must be ready before live writes.
4. **V/F and owned controls.** Curve workspace/planners, edit history, delta
   writes, holds and headroom. Port marker/release/recovery before enabling holds;
   run the same command tests through both presentations.
5. **Profiles and ordinary-control parity.** Existing-format import, manual
   Load/Undo, named Reset results and remaining rails/current/policies. Exercise
   partial failures, close/switch during work and restart without replay.
6. **Advanced parity.** Port remaining I2C, registry/BIOS, power-saving and timing
   workflows incrementally with Python parity and scoped hardware evidence.

The first shell is a reviewable development increment, not a feature-complete
replacement. The first useful tuning milestone combines qualified Vista/7
behavior, a complete memory/Undo action and the preserved Python experiment path.
Full replacement waits for the service/workflow parity matrix and OS qualification.

## Continuation checkpoint: first shell implemented

`codex/native-ui-shell` implements increment 1 as `native/ui/druta-ui`:
Win32/D3D9, Focused/Classic views, local drafts, fixture plots, adaptive pacing,
Always refresh and Pause/restore. The source and build instructions are in
[ui/README.md](ui/README.md), with the measured Windows 10 validation scope.
The shell opens no core GPU handle; Apply remains disabled. Vista/7 launch and
physical input qualification remain open. Native border dragging temporarily
stops frame submission. Existing Python tuning and its C ABI facade are intact.

Next: implement increment 2's typed, read-only CoreClient/Session and snapshots,
then a complete memory-offset action with fresh capture, readback and scoped
Undo. Do not enable fixture controls merely by attaching a setter. I2C physical
verification stays with the owner; no new tuning is replayed after driver reset.
