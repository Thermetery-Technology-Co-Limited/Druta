# Full review completion ledger

Objective: finish all incongruities in the [Classic review](2026-11-12-verbose-parity-review.md)
and [Focused inventory](2026-11-12-focused-port-inventory.md), using Python release
`fa6ee83` as the reference. The original inventories remain revision-pinned.
This ledger records subsequent completion evidence without narrowing their scope.

Session scope clarified by the user on 2026-11-13: implement concrete released
Python 1.9.1 UI/behavior gaps and the explicit layout requests. Do not work on
purely theoretical issues, improvements beyond parity, or closed GitHub issues.
The current [backlog](../../../backlog.md) separates active parity work from
deferred research and broader validation. Historical limitations below remain
recorded; deferred work is neither complete nor an active-session blocker.

## Current checkpoint and remaining parity

[Increment 25](2026-11-13-parity-twenty-fifth-increment.md) adds the saved-profile
browser and storage, aggregate Full tune capture and remembered nvtune/Forget/
test-signing setup. All 155 tests and 23 live renderer phases pass. The exact-target
read-only 3070 Ti capture parses with 16 semantic targets and no incomplete fields.
No tuning or boot configuration was changed during this checkpoint.

Of the three workflows found by the [source reconciliation](2026-11-13-released-parity-reconciliation.md),
automatic disk snapshots and newest-history Undo still need integration. Browser
and storage coverage does not complete those producers. The backlog has two
active entries including final acceptance, 37 completed and eight deferred items.
Overall parity remains open; broader hardware/layout qualification is deferred.

## Required work

| Requirement | Current status / evidence |
|---|---|
| Classic fixed columns, label wrapping, slider formats, font fallback/DPI, units | Implemented/tested in `4c6f1f5`; first-increment checkpoint |
| Classic real tabs and copyable monospaced log | Implemented/tested in `4c6f1f5`; floating pages remain below |
| Same-session floating pages, dock-back, preserved drafts, no duplicate controls | Implemented/tested in second checkpoint: real headless drag/dock/close events, all four pages |
| Classic two-press Reset, scope display, expiry/adapter-change cancellation | Implemented/tested in second checkpoint; Focused retains its existing action |
| Header Device/Panic actions and menu hierarchy | Device report/copy/save, capability refresh, explicit-target second window and helper navigation implemented/tested in third checkpoint. Eighth checkpoint adds exact-target out-of-process PnP recovery, bounded parent-exit handoff, durable result and fresh relaunch without tuning; actual driver restart remains physically unqualified |
| Help, licences and About | Implemented/tested in third checkpoint; actual bundled licence/guide sources and keyboard help |
| Seven Monitor tiles, reason statuses, power/utilization, PCIe/state/fans, optional histories | Implemented/tested in `4c6f1f5`; second checkpoint adds independent partial fan last-good merging |
| Classified A/B clock-domain table with architecture/unit/unknown-domain handling | Implemented/tested in second checkpoint, including real pure C ABI classifier |
| Measured-clock evidence: identical/unproven, stale, settling/load, secondary readings | Implemented/tested in second checkpoint; evidence policy is not universal physical measurement proof |
| Ctrl+H initiation/release, typing/stale/dirty guards, ownership/status | Implemented/tested in `4c6f1f5` |
| Legacy Classic keyboard scope and curve plot guides/nudge buttons | Implemented/tested/rendered in second checkpoint; Focused remains editor-local |
| Continuous curve consequences, parameter organization, acknowledged hard de-flatten | Implemented/tested in second checkpoint; software predictions remain distinct from measurements |
| Manual V/F profiles, identity/geometry validation, stage/preview/explicit Apply | Implemented/tested in second checkpoint; thirteenth adds selected indexed full-tune composition bound to fresh current geometry |
| Manual rail-limit profiles using independent qualified fields | Implemented/tested in third checkpoint: explicit native subset, fresh capture, gate-off preview, exact typed requests and grouped Undo |
| Manual policy profiles and coupled board/current state | Implemented/tested in third checkpoint: board-first writes, selected explicit pins, complete selected readback, partial-failure Undo and unrelated-pin conflict protection; no new hardware-write qualification |
| Profile catalog, included/omitted scopes, composition/conflict validation, partial-failure Undo | Implemented/tested in sixth: both-skin file catalog and selected targets, typed Python rail/full-power imports, whole preflight, final selected readback and grouped recovery receipts. Unsupported Python scopes are named; unqualified board-only Python power snapshots are refused before writes. Seventeenth adds qualified iCX composition as described below. Fourteenth adds timing plus ordinary/additional memory composition with timing first, memory-first Undo and frozen baselines; 100/100 suites and 22 read-only renderer phases passed |
| Remaining manual profiles, Python imports and catalog composition | Sixteenth adds exact coarse MP2888/MP29816 imports alongside NCT3933, uP9512R and NCP4206. Qualified legacy controllers now compose with ordinary requests in released voltage/controller-before-clock order, with frozen memory originals and controller-first grouped Undo. All 109 suites and 22 read-only renderer phases pass; see [evidence](2026-11-13-parity-sixteenth-increment.md). Seventeenth adds iCX-last composition, typed partial recovery, lifecycle retirement and controller-first Undo; all 111 suites and 22 read-only renderer phases pass ([evidence](2026-11-13-parity-seventeenth-increment.md)). Eighteenth adds explicit full-profile power labels, independent metadata delivery and conflict-aware Undo; see [evidence](2026-11-13-parity-eighteenth-increment.md). The historical [scope audit](2026-11-13-profile-scope-gap-map.md) predates these additions |
| General frequency min/max/lock-max and legacy P0/max fan ownership workflow | Implemented in fourth checkpoint: current-adapter tables, explicit range/lock-max, exact accepted request checks, owned handover/release, independent legacy P0 and fan Undo; physical qualification remains adapter-specific |
| Advanced read-only PERF groups and power arbitration diagnostics | PERF implemented in fourth checkpoint; explicit arbitration acquisition, real pure decoder integration, independent availability and stale retention implemented/tested in fifth. Current 3070 Ti layout is outside the traced semantic decoder; scoped evidence recorded |
| Advanced power feedback/integral bounds, selected-field restoration | Fifth: qualified selected ratios, exact preserved-field readback, previous-edit Undo and captured-initial Restore. Ninth adds selected native/Python ratio profiles, ordered whole-selection preview and issued-only recovery; 87-suite and 20-phase renderer validation passed. Additional packet geometry and physical-write qualification remain separate, explicitly unproven |
| Voltage gate, VF monotonicity/smoothing | Fifth: independent admission/VF reads, selected requests, atomic-prior-aware recovery, Undo and captured-initial requests. Ninth adds selected native profiles and released Python gate/flag imports with staged explicit Apply; 87-suite and 20-phase renderer validation passed. Current 3070 Ti read-only evidence recorded; physical effects remain unproven |
| Voltage offset/request tables, REL bounds | Seventh: six request fields, preservation/readback, Undo/recovery and explicit native profiles implemented/tested. Twentieth: production ROM/graph/policy/retained-request provider integrated for understood layouts. Twenty-first: bounded installed-receipt recognition. Twenty-second: signed selected offset Apply/readback/initial Restore/Undo/recovery and native/Python profiles are connected in both skins and actual CoreClient/catalog tests; [134 suites and 22 renderer phases pass](2026-11-13-parity-twenty-second-increment.md). No installed receipt on this card, so physical offset writes remain unqualified. Additional layouts are deferred; no persistent scaffold activation |
| Voltage constants and device switch delays | Seventh: independent typed device delays, exact readback, Undo and native profiles implemented/tested. Twentieth: production factory/provider wiring and REL/ALT/OV/VMIN Apply/Undo/manual profiles tested with injected transports. Current 3070 Ti ROM selector layout remains explicitly unqualified; loaded definitions and independent delay/request paths stay available |
| GR/individual/engine/block/SLCG gating, idle threshold, adaptive power, DIFR, PCIe speed | Thirteenth adds qualified adaptive/idle, exclusive GR/counted-request, and opposite ELCG/SLCG handovers; unknown adaptive prior, simultaneous shared GR/count ownership and BLCG ENABLE remain unqualified. 95/95 suites and 22 read-only renderer phases passed. Physical effects remain adapter-specific |
| Registry/VBIOS relationship-weight exception: preview/receipt/explicit refresh/verification/removal | Twenty-third Read/Preview plus [twenty-fourth activation/recovery](2026-11-13-parity-twenty-fourth-increment.md): immutable durable receipts, per-name journaling, exact registry readback, parent-exit handoff, fresh-process complete image/INFO proof and three-way owned removal. 147/147 tests and 23 renderer phases pass. This 3070 Ti layout is outside the released decoder; no physical activation is claimed |
| Timing setup/helper/driver diagnostics, decoded units and comparison/divergence | Implemented/tested in fourth/sixth: bounded helper save with target/broadcast/operating-state checks, qualified cycles/ns, raw fallback, partial partition differences and labelled stale retention. Fake child-process transport exercised; no new physical timing-write qualification  Increment 25 adds remembered registration/Forget and explicit test-signing setup, with injected OS/action tests; no boot settings changed. |
| Timing multiple staged fields, selected-bit transaction, captured restore | Implemented in fourth checkpoint: whole-plan preflight, cumulative issued-only Undo, selected conflict refusal, unrelated-bit preservation, separate qualified captured-initial restore |
| Timing profile save/load staged only | Fourth: native typed save with staged overlay, validated load into local fields, explicit whole Preview/Apply. Ninth adds released Python timing, `nvtune-profile-1` and field-exchange imports with current-helper aliases and qualification; 87-suite and 20-phase renderer validation passed. Physical timing-write qualification remains open |
| Bounded owned-P0 capture and qualifying-band auto-capture | Seventh: scoped helper pin, observed P0/current-adapter band, bounded temporary hold and owned-only cleanup, switch exclusion/recovery and telemetry-only auto-capture implemented/tested. Nineteenth adds optional CUDA below; no new physical timing-write qualification |
| Optional CUDA workload fallback for bounded P0 timing capture | Nineteenth: exact-BDF owned child, shared deadline/cancellation, clean-exit publication, locked-controls fallback and separate raw diagnostics implemented/tested. All 118 suites and 22 renderer phases pass. Current 3070 Ti captured stable P2/9427 MHz below the top band and correctly retained diagnostics only; lower-P2 timing-write qualification remains unresolved. See [evidence](2026-11-13-parity-nineteenth-increment.md) |
| Classic I2C per-family geometry/order/help and auto-open on discovery | Implemented/tested in second checkpoint across all six controller families; physical qualification remains controller-specific |
| NCT3933 Zero all outputs, selected-field batch with stop-on-failure result | Implemented/tested in second checkpoint: ordered selected-field writes, stop/uncertain outcome and restoration tests; no live NCT write |
| I2C remembered read-only discovery routes, progress and bounded cancellation | Implemented/tested in sixth: UUID/recipe-contract hints require fresh probe/read, Full scan bypasses hints, immutable worker progress and atomic Cancel, transactional selection and unverified partial candidates. No cached gates, verification or tuning replay; current 3070 Ti selection/read passed without owned requests |
| Policy user annotations and reviewed Max-all with meaningful Undo | Seventh: binding-scoped free-text labels and reviewed selected policy maxima reuse whole preflight/readback/grouped Undo. Eighth adds source connector presets, guarded channel-shared labels, explicit Python name-store import and observed-current/per-wire coloring with an explicit even-split assumption |

## Invariants and completion gate

Second checkpoint: [implementation and validation evidence](2026-11-12-parity-second-increment.md).
Third checkpoint: [menus, selected profiles and validation evidence](2026-11-12-parity-third-increment.md).
Fourth checkpoint: [clock ownership, timing batches and PERF diagnostics](2026-11-12-parity-fourth-increment.md).
Fifth checkpoint: [feedback, voltage policies and arbitration](2026-11-13-parity-fifth-increment.md).
Sixth checkpoint: [catalog composition, discovery and timing diagnostics](2026-11-13-parity-sixth-increment.md).
Seventh checkpoint: [voltage requests, device delays and temporary P0 capture](2026-11-13-parity-seventh-increment.md).
Eighth checkpoint: [power saving, recovery and annotations](2026-11-13-parity-eighth-increment.md).
Ninth checkpoint: [selected profiles and timing exchange](2026-11-13-parity-ninth-increment.md); 87 suites and 20 live renderer phases passed.
Tenth increment: [selected catalog composition and Python adapters](2026-11-13-parity-tenth-increment.md); 91 suites and 20 live renderer phases passed.
Eleventh increment: [voltage catalog transactions](2026-11-13-parity-eleventh-increment.md); 92 suites and 22 read-only renderer phases passed.
Twelfth increment: [saving and clock catalog transactions](2026-11-13-parity-twelfth-increment.md); 93 suites and 22 read-only renderer phases passed.
Thirteenth increment: [selected full-tune import and handover](2026-11-13-parity-thirteenth-increment.md); 95/95 suites passed in 14.43 seconds and 22 read-only renderer phases passed.
Fourteenth increment: [NCT profile import and timing ordering](2026-11-13-parity-fourteenth-increment.md); 100/100 suites and 22 read-only renderer phases passed.
Fifteenth increment: [exact uP/NCP imports and recovery](2026-11-13-parity-fifteenth-increment.md); all 104 suites and 22 read-only renderer phases passed.
Sixteenth increment: [coarse controller imports and composition](2026-11-13-parity-sixteenth-increment.md); all 109 suites and 22 read-only renderer phases pass.
Seventeenth increment: [iCX composition and recovery](2026-11-13-parity-seventeenth-increment.md); all 111 suites passed in 34.86 seconds and 22 read-only renderer phases pass.
Eighteenth increment: [full-profile power labels and Undo](2026-11-13-parity-eighteenth-increment.md); all 114 suites passed in 35.68 seconds. Live validation is recorded in that checkpoint.
Nineteenth increment: [optional CUDA capture and cancellation](2026-11-13-parity-nineteenth-increment.md); all 118 suites and 22 renderer phases pass. Exact-target workload cleanup and below-band diagnostic-only capture were exercised on this 3070 Ti.
Open rows still require implementation; completing this increment does not complete the goal.

Approved differences stay: weighted risk colors; Focused page routing; integrated
Classic and popup Focused V/F editors; staged curve Apply; explicit manual profile
application; session-only ordinary tuning. No startup/post-reset tuning replay.
The narrow persistent relationship-weight exception does not permit general
persistent tuning or unrelated registry edits.

Unknown protocol fields are preserved. Capability decisions use the current
adapter and understood API/layout, not developer board or driver-version lists.
Unresolved firmware research remains parked and must be named explicitly rather
than hidden behind a placeholder or counted as completed functionality.

For each row, completion requires source integration plus behavior/failure tests
and rendered or runtime evidence appropriate to its scope. Record remaining
device/OS qualification separately from implementation. A green test suite or a
partial presentation port cannot mark the full goal complete. The goal remains
active until every applicable review requirement has authoritative evidence.


## Historical remaining scope after the twenty-first increment

Superseded for active-session status by the current checkpoint and backlog above.
This section retains earlier limitations; it does not reopen completed work or
turn deferred qualification into an active task.

- Remaining ROM selector/runtime equation mappings and independent policy/
  retained-request verification for selected signed offset writes, Undo and
  profiles. The [twenty-first checkpoint](2026-11-13-parity-twenty-first-increment.md)
  records bounded installed-receipt recognition and explicit read-only inspection,
  with 127 passing suites. This 3070 Ti has no installed receipt; its previously
  recorded ROM selector layout remains unqualified. The
  [provider map](2026-11-13-native-evidence-provider-map.md) is the historical contract.
- Persistent relationship-weight exception: selected preview, journal, exact registry
  projection, explicit refresh, image/INFO verification and three-way removal. The
  [port map](2026-11-13-relationship-weight-port-map.md) retains all stages and failures.
- Qualify lower-P2 timing-band equivalence without changing the existing floor
  from one board's observed clocks; optional CUDA fallback is implemented in the
  [nineteenth checkpoint](2026-11-13-parity-nineteenth-increment.md). The
  [CUDA map](2026-11-13-cuda-capture-port-map.md) retains its historical source
  trace. Complete the provider-dependent profile scopes. The [profile scope audit](2026-11-13-profile-scope-gap-map.md)
  predates subsequent increments. Eleventh-increment voltage request/limit
  catalog composition passed the documented integration checks. Low-power and clock/PCIe catalog
  composition is validated within its documented boundaries. Thirteenth-increment
  handovers qualify adaptive/idle, exclusive GR/count and opposite ELCG/SLCG;
  unknown adaptive prior, simultaneous shared GR/count and BLCG ENABLE remain
  unqualified. Selected full-tune NCT, uP, NCP and coarse MP2888/MP29816 imports
  compose with ordinary catalog requests using qualified controller ownership,
  exact readback and frozen memory baselines. Full-profile power labels now use
  independent UI-owned metadata commits, durable worker effects, Unicode validation
  and conflict-aware chained Undo; the [historical map](2026-11-13-full-tune-annotations-port-map.md)
  records the source contract. iCX now
  composes last with qualified ordinary requests and restores first, retaining
  typed partial recovery and retired-lifecycle guards. Its historical
  [composition map](2026-11-13-icx-composition-port-map.md) describes the repaired
  defects. Timing and a second controller remain incompatible with its retained
  floor clamp. Timing plus ordinary/additional memory catalog
  ordering retains its documented qualification boundaries.
- Physical effects, controller coverage and Vista/7 execution retain their original
  qualification boundaries. Mock transport tests and current read-only evidence do
  not establish those results.
