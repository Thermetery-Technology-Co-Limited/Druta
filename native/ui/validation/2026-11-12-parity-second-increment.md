# Classic interactions, qualified Monitor and V/F profiles

Continuation after `4c6f1f5`, against the released Python reference `fa6ee83`.
This is a bounded implementation checkpoint, not complete frontend parity.
The [full completion ledger](PARITY-COMPLETION.md) preserves every remaining
requirement from both source reviews.

## Implemented

- Classic page floating/docking shares one session and existing drafts.
  Close docks the page; each surface renders once. Classic Reset arms first,
  displays scope, expires after ten seconds and cancels across adapter/lock
  changes. Focused retains its existing Reset behavior.
- Classic I2C uses the released six-column geometry, family order/units/help
  and one-time opening when a connection discovers candidates. Route revision,
  stale-field caching and independent controller qualification remain intact.
- Classic curve editing is window-wide when not typing or using another widget;
  Focused remains editor-local. Nudge buttons use one/five adapter clock bins.
  Cap/ramp/acknowledged-hard/confirmed-held guides do not expand plot fitting;
  a corner annotation shows the selected point and scaled delta.
- Hard de-flatten uses the existing pure planner export, an explicit hardware-mod
  acknowledgement and staged review. Requested peak/change count and below-floor
  model consequences remain separate from physical measurements. Planning never
  writes implicitly; the existing Apply and Undo paths remain authoritative.
- Manual V/F profiles save a fresh configured curve, not unsaved edits. Load
  checks adapter identity, sparse layout, divisor, point voltage and current
  envelope before staging one undoable gesture. Invalid/dirty/stale input is
  rejected atomically; a no-op preserves Undo/Redo. No startup/reset replay.
- Monitor binds the pure clock classifier to existing telemetry polls. It
  preserves doubled/raw units and unknown rows, qualifies B evidence and only
  grades core gaps with an independently measured device clock bin. The bin
  survives unrelated V/F failure and transient probe failure; adapter close
  clears it. Unlike the release's short-gap carryover, any missed read breaks
  qualification continuity immediately. Last secondary values remain marked
  stale for at most three seconds; raw table rows retain explicit stale state.
- Partial fan duty/RPM reads preserve each established field independently.
  Fresh zero is valid; unknown fields remain absent. Fresh consistent counts can
  remove departed indices, while missing counts cannot erase established rows.
  Monitor and Classic Daily label retained values stale.
- NCT3933 Zero all outputs is one guarded intent using existing per-output
  writes. It stops on the first failure, reports completed/uncertain outputs and
  retains owned restoration state. GPU switching is blocked in flight and while
  ownership is unresolved. It was not exercised on physical NCT hardware.

## Validation

Release build and bounded PE/import audit pass. All **37 CTest suites** pass,
including 1,071 UI checks, dedicated Classic geometry/behavior tests, 609 client
checks, 187 session checks, 145 clock-service checks, real pure ABI classifier
tests, 34 curve-profile checks, 56 planner checks and 216 I2C-session checks.
Tests cover alternate identities/layouts, missing capabilities, partial reads,
failure preservation, restore/uncertain ownership and selected command binding.

Read-only renderer captures were inspected on this Windows 10 x64 / RTX 3070 Ti:
Classic Daily at 2400x1500, Classic curve popup at 1800x1200, Focused Monitor at
1800x1300, and Focused curve popup at 1280x800 / 150% scale. The compact popup
retains its visible plot and reachable controls; Classic shows the integrated
large-point curve, guides and corner annotation. Local artifacts are under
`native/build/ui-integrated/parity-second/` (not committed).

The 12-phase live read-only renderer smoke test passed with zero tuning actions.
Pause and ordinary minimization each recorded zero frames and telemetry reads;
Always refresh while minimized recorded 114 frames and two telemetry reads in
its approximately two-second phase. The report's three `device_resets` are
Direct3D rendering-device resets, not GPU-driver/PnP resets. See the adjacent
[machine-readable smoke report](2026-11-12-parity-second-smoke.json).

No GPU tuning write was issued for this checkpoint. Headless input tests and
renderer captures do not claim physical mouse/keyboard qualification, physical
NCT response or Vista/7 execution. Earlier hardware evidence keeps its original
board/controller/operating-point scope.

SHA-256:

- `druta-ui.exe`: `37C267ED187CABEB332A4FF5B321BB370F36B8A902928E89E18BFCF591E79311`
- `drutacore.dll`: `687DFF65557C785388D01887FF36FCC20E6C43FB2FF3D0E1D426D03F25012F64`
