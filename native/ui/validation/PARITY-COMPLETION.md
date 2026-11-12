# Full review completion ledger

Objective: finish all incongruities in the [Classic review](2026-11-12-verbose-parity-review.md)
and [Focused inventory](2026-11-12-focused-port-inventory.md), using Python release
`fa6ee83` as the reference. The original inventories remain revision-pinned.
This ledger records subsequent completion evidence without narrowing their scope.

## Required work

| Requirement | Current status / evidence |
|---|---|
| Classic fixed columns, label wrapping, slider formats, font fallback/DPI, units | Implemented/tested in `4c6f1f5`; first-increment checkpoint |
| Classic real tabs and copyable monospaced log | Implemented/tested in `4c6f1f5`; floating pages remain below |
| Same-session floating pages, dock-back, preserved drafts, no duplicate controls | Implemented/tested in second checkpoint: real headless drag/dock/close events, all four pages |
| Classic two-press Reset, scope display, expiry/adapter-change cancellation | Implemented/tested in second checkpoint; Focused retains its existing action |
| Header Device/Panic actions and menu hierarchy | Device report/copy/save, capability refresh, explicit-target second window and helper navigation implemented/tested in third checkpoint. Exact-target out-of-process PnP service and no-replay recovery remain open |
| Help, licences and About | Implemented/tested in third checkpoint; actual bundled licence/guide sources and keyboard help |
| Seven Monitor tiles, reason statuses, power/utilization, PCIe/state/fans, optional histories | Implemented/tested in `4c6f1f5`; second checkpoint adds independent partial fan last-good merging |
| Classified A/B clock-domain table with architecture/unit/unknown-domain handling | Implemented/tested in second checkpoint, including real pure C ABI classifier |
| Measured-clock evidence: identical/unproven, stale, settling/load, secondary readings | Implemented/tested in second checkpoint; evidence policy is not universal physical measurement proof |
| Ctrl+H initiation/release, typing/stale/dirty guards, ownership/status | Implemented/tested in `4c6f1f5` |
| Legacy Classic keyboard scope and curve plot guides/nudge buttons | Implemented/tested/rendered in second checkpoint; Focused remains editor-local |
| Continuous curve consequences, parameter organization, acknowledged hard de-flatten | Implemented/tested in second checkpoint; software predictions remain distinct from measurements |
| Manual V/F profiles, identity/geometry validation, stage/preview/explicit Apply | Implemented/tested in second checkpoint; standalone scope, not full-tune composition |
| Manual rail-limit profiles using independent qualified fields | Implemented/tested in third checkpoint: explicit native subset, fresh capture, gate-off preview, exact typed requests and grouped Undo |
| Manual policy profiles and coupled board/current state | Implemented/tested in third checkpoint: board-first writes, selected explicit pins, complete selected readback, partial-failure Undo and unrelated-pin conflict protection; no new hardware-write qualification |
| Profile catalog, included/omitted scopes, composition/conflict validation, partial-failure Undo | Rail/power selection workspace and its partial-failure Undo implemented/tested. Broader catalog, Python rail/power format import, cross-scope composition and conflict review remain open |
| General frequency min/max/lock-max and legacy P0/max fan ownership workflow | Open |
| Advanced read-only PERF groups and power arbitration diagnostics | Open |
| Advanced power feedback/integral bounds, selected-field restoration | Open |
| Voltage gate, VF monotonicity/smoothing | Open |
| Voltage offset/request tables, REL bounds | Open |
| Voltage constants and device switch delays | Open |
| GR/individual/engine/block/SLCG gating, idle threshold, adaptive power, DIFR, PCIe speed | Open |
| Registry/VBIOS relationship-weight exception: preview/receipt/explicit refresh/verification/removal | Open |
| Timing setup/helper/driver diagnostics, decoded units and comparison/divergence | Open |
| Timing multiple staged fields, selected-bit transaction, captured restore | Open |
| Timing profile save/load staged only | Pure typed profile capture/validation module tested in third checkpoint; UI/staging and batch integration remain open |
| Bounded owned-P0 capture and qualifying-band auto-capture | Open |
| Classic I2C per-family geometry/order/help and auto-open on discovery | Implemented/tested in second checkpoint across all six controller families; physical qualification remains controller-specific |
| NCT3933 Zero all outputs, selected-field batch with stop-on-failure result | Implemented/tested in second checkpoint: ordered selected-field writes, stop/uncertain outcome and restoration tests; no live NCT write |
| I2C remembered read-only discovery routes, progress and bounded cancellation | Open |
| Policy user annotations and reviewed Max-all with meaningful Undo | Open |

## Invariants and completion gate

Second checkpoint: [implementation and validation evidence](2026-11-12-parity-second-increment.md).
Third checkpoint: [menus, selected profiles and validation evidence](2026-11-12-parity-third-increment.md).
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
