# Profile composition, I2C discovery and timing diagnostics

Continuation after `7e75f33`, against Python release `fa6ee83` and the
[full completion ledger](PARITY-COMPLETION.md). Sol workers implemented the
catalog/import transactions and I2C discovery/typed iCX plans; Terra implemented
timing diagnostics and reviewed worker integration. Root integrated both skins,
the host and session callbacks, reviewed transaction failures, and ran validation.
The full review goal remains open.

## Implemented behavior

- Both skins expose a profile catalog with explicit file selection, scope/target
  summaries, included-control selection, composition, immutable staging and
  separate Preflight/Apply. Capture uses the existing selected-scope writers.
  Loading, staging and saving perform no tuning writes or startup registration.
- Native daily/advanced, curve, rail/power, timing and iCX formats are recognized.
  Python tuning-action, rail-limit, full power-action and supported combined
  scopes import through typed adapters. Unknown scopes are refused by name.
  Whole composition checks identity, units, duplicate targets, layout, selected
  gates and current capabilities before any selected write. Apply repeats these
  checks and verifies all selected results after every component completes.
- Coupled board/policy requests execute in canonical order and share one original
  power Undo capture. Python power imports preserve requested rows separately
  from explicit pins. No opaque packet replay is introduced.
- Grouped Undo retains typed comparison receipts for completed requests until
  the complete restoration verifies. A later sibling disturbing an earlier
  restored field requeues only affected/unreadable entries. Partial nested
  recovery state survives retries; a refresh exception after a verified child
  does not discard its parent or older independent Undo records.
- iCX catalog Apply is standalone. Its plan binds the current adapter, route,
  recipe, selection revision and complete prior settings. Apply reacquires and
  compares those settings; Undo uses the existing controller's selected-field
  ownership/restoration and releases its clock clamp. Unknown global enable
  semantics remain outside the restoration claim.
- I2C discovery stores only UUID-bound route/recipe hints in
  `i2c-routes-native.json`, alongside the existing local display configuration.
  Fresh probing/identity/read qualification is required on reconnect; gates,
  physical-response verification and tuning requests are never cached. Full
  scan bypasses hints. Invalid cache documents are preserved. New state
  directories are created only for a qualified hint save.
- Discovery progress crosses the existing worker through immutable, generation-
  checked snapshots. Cancel is an atomic read-only signal outside the command
  queue. Cancelled scans retain established selection and staged state; partial
  hits are explicitly unverified. Selection has a clear commit boundary, and
  cancellation never interrupts a write or restoration.
- Explicit timing reads additionally request the pinned helper's read-only JSON
  save. Target, helper identity, broadcast words and operating state must match
  before partition values are associated. Optional missing/malformed data leaves
  the broadcast capture usable; matching last partition evidence is labelled
  stale. A failed post-save adapter check invalidates the capture.
- Qualified known-layout cycle fields use the current adapter's memory-divisor
  capability and a stable clock for nanoseconds. Split fields, RFC/WL and unknown
  evidence retain raw/cycle-only presentation. Per-partition missing values and
  differences are visible independently.

## Deliberate limits

iCX cannot compose with ordinary tuning because it owns a separate controller
and clock lifecycle. Timing cannot compose with memory-offset changes that alter
its qualified operating band. These cases fail whole preflight before any writes.
Python board-only power snapshots without a qualified peer-preserving route are
refused. Unported Python full-tune/voltage/low-power scopes are named explicitly;
the catalog does not claim their semantics. Hardware effects and other controller
families still require their own physical qualification.

## Validation

- Windows 10 x64 Release build passed with the project's warning-as-error flags.
  All **64 CTest suites passed**, 10.65 seconds: 884 client checks, 279 catalog
  worker checks, 242 session checks, 1,071 existing UI checks, new catalog/import,
  route-cache/discovery/iCX-plan tests, and 23 session cancellation checks.
  Headless tests activate the real catalog entry in both skins. Timing service
  tests invoke a real fixture child process for selected-target JSON save,
  failed exit, invalid slot and bounded output handling.
- Independent review found and fixed cancellation after selection commit,
  whole-composition readback gaps, intermediate power Undo captures, a parent
  Undo loss after post-success refresh exceptions, and lost nested recovery
  receipts. A changed completed field is detected without rewriting it during
  that retry; another explicit Undo is required for the retained typed recovery.
  Automatic fan receipts compare request mode/policy, not changing automatic RPM
  or duty. Regressions cover these observed failure paths.
- The 14-phase live renderer harness passed with the exact selected RTX 3070 Ti
  and zero tuning commands. Explicit Pause and default minimization produced
  zero frames and zero telemetry reads during their phases. Always-refresh kept
  both running while minimized. Focused and Classic catalog BMPs were inspected;
  clipped helper text was shortened and wrapped before the final capture.
  Renderer device resets in the report are Direct3D resets, not driver resets.
- Separate read-only native I2C selection/read of port 1 / address `0x20` passed:
  MP2888A candidate, offset recipe, offset 0, gate/verification/ownership/clamp all
  off, and `changed=0` for both operations. This establishes current-board
  discovery/read behavior only; it adds no physical write qualification.
- Desktop automation capture failed twice (`FrameArrived timed out`, then
  `window capture timed out: timed out waiting on channel`). No desktop clicks
  were issued; visual evidence comes from the application's own renderer, and
  interaction evidence from the headless ImGui tests.

Build/test logs are in `native/build/ui-integrated/parity-sixth-build-final.log`,
`parity-sixth-tests-final.log` and `Testing/Temporary/LastTest.log`. Renderer BMPs
and report are in `parity-sixth-live/`; I2C output is
`parity-sixth-i2c-read.log`. The report is also retained
[with this checkpoint](2026-11-13-parity-sixth-smoke.json).

SHA-256:

- `druta-ui.exe`: `97940C74B0EE68EF3E32BA535198C0A586A4FAD75400B15B9B4AAD863FFB0360`
- `drutacore.dll`: `0CB6005074D3266198B5A41BEE3D28900F0EF4953ACFF984BB9E32F8C7B0043D`

The DLL is unchanged from the preceding checkpoint. No GPU-driver restart or new
tuning write was issued during this checkpoint. Windows Vista/7 execution and
physical effects on other adapters/controllers remain unqualified.
