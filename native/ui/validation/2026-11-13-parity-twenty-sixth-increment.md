# Twenty-sixth parity increment: disk history and final reconciliation

Reference: released Python 1.9.1 `fa6ee83`. This increment completes the
automatic snapshots and explicit newest-history workflow identified in the
[history map](2026-11-13-history-parity-map.md). It does not expand the deferred
firmware, controller or operating-system qualification scope.

## Implemented behavior

The production CoreClient installs a disk-history sink. Qualified changed
requests capture semantic profiles before their first effect; no-op and refused
requests do not create history. Selected-control snapshots are required, while
the released full-snapshot and recovery exceptions report failures and continue.
Grouped operations save once. Ordinary Undo, recovery, release-owned cleanup,
telemetry and close do not create new history or replay saved requests.

History covers Daily and advanced controls, coupled power policies and explicit
pins, rail fields, individual fan requests, V/F edits, Max it/Reset, P0/max fan,
I2C actions, timing writes, standalone power/voltage panels and catalog loads.
Loading a saved history entry captures the reverse state through the same
qualified write paths. Opaque in-memory ownership receipts are never serialized.

Both skins expose **Undo from history...** through the File menu and profile
catalog. It refreshes the library and stages the newest autosave. It does not
silently fall back to an older file when the newest is unreadable or invalid.
Adapter changes invalidate the request. Preflight and Apply remain explicit.
Native-owned autosaves retain 20 entries; named and external Python files are
preserved.

Timing commits require a durable captured backup before the optional full
autosave. The backup stores all currently qualified, restorable typed fields,
not an opaque nvtune register image or a claimed factory default. A successful
backup is cached only for the current helper/session; failed backups prevent
commit and are retried on a later explicit request. Backups live in the separate
`timing-backups` directory and are excluded from autosave pruning. Their path is
reported in the operation log. No saved document is applied at startup,
reconnect or driver reset.

Review corrected three concrete regressions before acceptance: P0/max-fan saves
before releasing an existing hold; Daily fan-profile reverse capture preserves
mixed per-fan policies; and the direct power-saving Release button retains the
release's warning-only snapshot failure behavior. Adaptive Allow and staged
profile application retain their normal selected-capture requirements.

## Validation

The integrated Release build passes. All **157/157 tests pass** in 49.80 seconds.
The actual-command tests cover no-op/refusal suppression, required failures
before setters or ownership handover, warning-only full snapshots, grouped
capture, explicit reverse loads, mixed fan policies and the direct Release
exception. Service tests verify retained prior Undo when capture fails.

The timing test runs a bounded fake helper through the real transport: missing
or failed backup storage produces zero commits, a successful backup precedes
the optional autosave, helper reselection requires a new backup, and Undo/no-op
paths do not create backups. Library tests cover unique atomic storage, malformed
newest entries, native-only retention and backup exclusion from pruning. UI tests
exercise the actual File menu, catalog intents and power-saving Release button.

An intermediate rebuild caught a missing test-header include, corrected before
this final successful build and suite. Physical effects are not inferred from
injected transports or the fake timing helper.

All **23 live renderer phases pass** in the
[report](2026-11-13-parity-twenty-sixth-smoke.json). Pause and default minimization
produce zero frames and telemetry reads; Always refresh while minimized produces
113 frames and two reads. Both catalog skins were visually inspected. The live
history action refreshes/stages without applying tuning. The single completed
worker action is the read-only nvtune-path selection; four resets are Direct3D
renderer resets, not GPU driver restarts.

The [exact-target read-only capture](2026-11-13-parity-twenty-sixth-capture.json)
matches this RTX 3070 Ti's BDF/UUID, saves and parses 16 semantic targets with an
empty incomplete list, and closes without a tuning change. No controller was
selected by this probe. Controller/history write failure behavior is covered by
injected tests; this checkpoint adds no physical write qualification.

The static GUI import audit passes within its documented limits; it is not a
Vista/7 runtime result. No tuning, registry, boot configuration, workload or PnP
restart was requested in these live checks.

| Tested artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `1E9F331450C827D5767D0EC411F4BC30825AC9A5647511B9024A0C185F8F7377` |
| `drutacore.dll` | `9C1033839A2F89A2A39BEA90CE6CFB8BC08F09A6310D6C45D6E7DEDF5CCD8A05` |
| `druta-ui-full-profile-capture-probe.exe` | `7D21E7D2E474E86852CF344C7573C29D9DA3F191B13F8C30FE82B6620BD0E856` |

Local build, test and import logs are under `native/build/ui-integrated/parity26-*`.
The successful build log is `parity26-build-verified.log`; the complete test
result is `parity26-tests-final.log`.

## Final source reconciliation

The original Classic and Focused review rows map to the implementations and
evidence in the [completion ledger](PARITY-COMPLETION.md). The bounded final
source review found the three workflows recorded in the
[released reconciliation](2026-11-13-released-parity-reconciliation.md): aggregate
Full tune capture, helper/setup registration and disk history. Increment 25
implements the first two and library storage; this increment connects history
producers and explicit restoration.

The historical profile audit predates the completed typed imports and catalog
composition. It is not an additional open-work list. Unsupported source layouts,
unknown prior-state recovery, BLCG ENABLE and the other eight deferred items in
the [backlog](../../../backlog.md) remain explicitly unqualified or deferred;
they are not marked as implemented. Approved layout, staging, risk-color and
session-lifetime differences remain intentional.

The bounded released-parity reconciliation is complete: **0 active parity
entries, 39 completed and 8 deferred**. This completes the session's parity goal,
not the deferred research/physical/legacy-Windows qualification campaigns.
