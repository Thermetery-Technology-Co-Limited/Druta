# Native frontend parity backlog

Updated 2026-11-13. Reference: Python 1.9.1 `fa6ee83`; latest verified native checkpoint: [twenty-sixth increment](native/ui/validation/2026-11-13-parity-twenty-sixth-increment.md). Scope comes from the [Classic review](native/ui/validation/2026-11-12-verbose-parity-review.md), [Focused inventory](native/ui/validation/2026-11-12-focused-port-inventory.md) and [completion ledger](native/ui/validation/PARITY-COMPLETION.md). Those historical inventories are not current open-item lists.

Update this file after each shipping increment. Strike an item only after its implementation is integrated and appropriate behavior/failure verification is recorded; link the evidence. Tests, UI implementation, semantic qualification and physical/OS validation are separate claims. Naming a scope unsupported or writing a plan does not complete it. The bounded released-parity session is complete; deferred qualification remains open.

## Session scope

Work only on concrete missing or mismatched functionality from released Python
1.9.1, plus the user-approved layout differences. Do not pursue purely theoretical
issues, improvements beyond parity, or closed GitHub issues. Check an issue's
current state before starting work linked to it. Necessary tests/readback for
an actual port remain part of that port.

The deferred section below preserves limitations and previous validation requests
without treating them as active parity tasks or marking them complete.

## At a glance

The released-parity implementation and final review reconciliation are complete. Completed work is checked and struck through. Broader research and validation campaigns remain deferred.

| Workstream | Status | Remaining checklist items |
| --- | --- | ---: |
| Installed voltage offsets | Selected-write/Undo/profile path implemented and tested | 0 |
| Persistent power relationship weights | Explicit activation/recovery implemented and tested; physical qualification scoped separately | 0 |
| Saved-profile/history | Prewrite producers, required timing backups and explicit history Undo verified | 0 |
| Full tune capture and nvtune setup | Implemented and verified in increment 25 | 0 |
| Final released-parity reconciliation | Source audit and final acceptance complete | 0 |
| Completed implementation and release-note checkpoints | 39 checked entries | 0 |

**0 active parity checklist items; 39 completed; 8 deferred outside this session.** The twenty-sixth increment completes disk-backed prewrite history, explicit newest-history Undo and required captured timing backups. All 157 tests and 23 live renderer phases pass. Read-only Full tune capture on this 3070 Ti produces 16 semantic targets with no incomplete fields. This closes the concrete released-parity backlog; it does not claim the deferred physical/OS campaigns are complete.

## Completed: optional CUDA timing capture

Source and acceptance: [CUDA capture map](native/ui/validation/2026-11-13-cuda-capture-port-map.md). [Nineteenth-checkpoint evidence](native/ui/validation/2026-11-13-parity-nineteenth-increment.md): 118 suites and 22 renderer phases pass. This 3070 Ti produced a stable 9427 MHz P2 raw diagnostic below the qualified top band; it did not produce a qualified timing-write snapshot. Lower-P2 qualification beyond the released operating band is deferred.

- [x] ~~Integrate the optional exact-BDF CUDA child, shared driver loader and owned process/job cleanup. Missing CUDA DLL/exports leave direct/manual capture available with a specific reason.~~
- [x] ~~Integrate direct-P0-first → permitted owned hold → workload fallback; capture while load remains active. Require verified cleanup before fallback after an uncertain hold; preserve existing foreign/owned requests.~~
- [x] ~~Share one capture deadline and immediate cancellation across workload and selected-helper subprocesses; expose unresolved cleanup and allow explicit retry, including disconnected telemetry. Pause/close signal outside the command queue; resume does not restart load.~~
- [x] ~~Finish both-skin capture/cancel/status/diagnostic integration, including locked-controls workload use without authorizing tuning holds. Preserve current stable top-band qualification and distinguish qualified P0/P2 from raw below-band diagnostics.~~
- [x] ~~Run the integrated build, blocked-child/cancel/early-exit/failure tests, existing I2C warmup regression tests and renderer checks; record the checkpoint.~~ The bound covers process/wait ownership, not preemption of a hung parent driver/kernel call.

## Voltage evidence: shipped foundation and remaining work

Implementation contract: [native evidence provider map](native/ui/validation/2026-11-13-native-evidence-provider-map.md). Implemented scope and limitations: [twentieth checkpoint](native/ui/validation/2026-11-13-parity-twentieth-increment.md).

- [x] ~~Add the typed, paired-target current-ROM getter with bounded buffer/descriptor validation, matching read pairs and distinct absent/transient/malformed/identity outcomes.~~
- [x] ~~Port the pure ROM ownership/table/graph parser: BIT/EFI relocation and bounds, voltage roles, clock roots, sharing, aliases, cycles and dangling references. Derive roles from the current image rather than equation ordinals or board constants.~~
- [x] ~~Expose bounded read-only loaded-equation, rail-policy and retained-request captures; preserve unknown fields as comparison evidence. Distinguish optional getter absence from a selected getter failing.~~
- [x] ~~Wire `VoltageLimitsApi::equation_evidence` to independently matched ROM graph, loaded equations, policies and retained requests; connect understood REL/constants to the existing selected Apply/readback/Undo/manual-profile paths.~~ This closes wiring for the released understood layouts; broader layout research is deferred.
- [x] ~~Implement a separately bounded installed-scaffold receipt reader and fresh variable inventory. Reconstruct and verify original/current graph, retained equation packets, original roots and installed heads; reject stale/forged evidence.~~ [Twenty-first checkpoint](native/ui/validation/2026-11-13-parity-twenty-first-increment.md): explicit inspection is wired through the production factory and both skins. The next completed item adds independent policy/clock/rail preservation for selected writes.
- [x] ~~Connect the recognized installed heads to the exact signed selected writer/readback/Undo/profile path, with fresh independent policy effects and retained clock/rail request verification. Preserve the original chain and unknown fields; never substitute an absolute constant or replay receipt packets.~~ [Twenty-second checkpoint](native/ui/validation/2026-11-13-parity-twenty-second-increment.md): both-skin actions, actual CoreClient routing and native/Python catalog round trips pass with injected transports. Scaffold installation/activation and physical writes on this card are not supplied by this work.
- [x] ~~Validate understood alternate layouts/identities, partial independent families, transient retention, malformed failures, unknown-byte drift and graph/inventory changes; record production wiring, integration and renderer evidence.~~ [Twentieth checkpoint](native/ui/validation/2026-11-13-parity-twentieth-increment.md); physical voltage effects and broader semantic layouts remain open.

## Completed: persistent power relationship-weight exception

Bounded authorized scope and source: [relationship-weight map](native/ui/validation/2026-11-13-relationship-weight-port-map.md). This exception permits selected weights to persist and requires explicit refresh; ordinary tuning stays session-only.

- [x] ~~Reuse the current-ROM getter; add pure power-table and relationship INFO decoders, including configured source references. Match selected relationship index/type/target, policy channel/unit/bounds and exact Q4.12 values from current evidence.~~ [Twenty-third checkpoint](native/ui/validation/2026-11-13-parity-twenty-third-increment.md).
- [x] ~~Implement side-effect-free selected Preview with immutable target/image/INFO/registry evidence, exact patch spans and stale-preview/conflict rejection. Preserve independent read-only inspection and last-good stale values.~~ [Twenty-third checkpoint](native/ui/validation/2026-11-13-parity-twenty-third-increment.md).
- [x] ~~Resolve the exact current PCI instance and driver key; capture registry names/types/bytes. Project minimal dense `romOverride` records without overwriting old records, changing firmware selectors or touching unrelated values.~~ [Twenty-third checkpoint](native/ui/validation/2026-11-13-parity-twenty-third-increment.md).
- [x] ~~Persist a bounded durable receipt and discoverable active pointer before the first possible registry effect; journal each phase and retain accepted-but-failed/partial outcomes for explicit recovery.~~ [Twenty-fourth checkpoint](native/ui/validation/2026-11-13-parity-twenty-fourth-increment.md).
- [x] ~~Implement explicit Apply and selected-device refresh through an owned transaction host. Revalidate the complete preview before mutation and perform exact registry readback; no automatic startup/reconnect/reset application or session-request replay.~~ [Twenty-fourth checkpoint](native/ui/validation/2026-11-13-parity-twenty-fourth-increment.md).
- [x] ~~Verify activation from a fresh process: exact expected image and complete semantic INFO with only selected weight changes. Report refresh/reboot-required/timeout/identity/verification failures distinctly; registry-write success alone is insufficient.~~ [Twenty-fourth checkpoint](native/ui/validation/2026-11-13-parity-twenty-fourth-increment.md).
- [x] ~~Implement explicit three-way removal/recovery of transaction-owned names only, restoring exact prior type/value or absence, preserving unrelated additions and refusing third-value conflicts. Refresh and verify original image/INFO afterward.~~ [Twenty-fourth checkpoint](native/ui/validation/2026-11-13-parity-twenty-fourth-increment.md).
- [x] ~~Add read/preview/apply/verify/removal/recovery UI and adversarial transaction tests; integrate the production adapters and record scoped runtime validation. Keep general persistent BIOS/registry editing outside this narrow exception.~~ [Twenty-fourth checkpoint](native/ui/validation/2026-11-13-parity-twenty-fourth-increment.md).

## Completed released workflows

Source trace and acceptance boundary: [released-parity reconciliation](native/ui/validation/2026-11-13-released-parity-reconciliation.md). The previously missing capture, storage and setup interactions are now integrated with the typed imports/writers.

- [x] ~~Finish disk-backed prewrite snapshots and newest-history Undo, preserving required capture failures, recovery exceptions and one snapshot per grouped write. Use qualified semantic documents, never opaque ownership receipts or startup/reset replay.~~ [Increment 26](native/ui/validation/2026-11-13-parity-twenty-sixth-increment.md): actual-command failure/reverse-load tests, unique storage/retention, required timing backups, both-skin staging and 23 live renderer phases pass. [The source map](native/ui/validation/2026-11-13-history-parity-map.md) retains the released per-action behavior.
- [x] ~~Add the released aggregate Full tune capture/save choice, reusing current typed captures and naming unavailable/omitted contents.~~ [Increment 25](native/ui/validation/2026-11-13-parity-twenty-fifth-increment.md): current-request capture, independent partial-family tests and exact-target read-only 3070 Ti capture pass; controller physical response remains separately scoped.
- [x] ~~Restore remembered nvtune registration, Forget with fallback discovery, and the released test-signing setup/status/actions. Keep boot changes and reboot explicit.~~ [Increment 25](native/ui/validation/2026-11-13-parity-twenty-fifth-increment.md): config/discovery, worker attribution, partial failures and explicit action UI pass through injected OS boundaries. This machine's boot configuration was not changed.

## Final parity reconciliation

Use the released source and the original reviews to find concrete omissions. Do not convert unsupported release behavior or speculative combinations into new implementation tasks.

- [x] ~~After disk-history integration, finish acceptance of every applicable original-review row and advertised profile scope against current behavior and evidence.~~ [Increment 26](native/ui/validation/2026-11-13-parity-twenty-sixth-increment.md) and the [completion ledger](native/ui/validation/PARITY-COMPLETION.md) record final acceptance. The [historical profile audit](native/ui/validation/2026-11-13-profile-scope-gap-map.md) predates completed imports; deferred provider-dependent scopes remain unqualified, rather than counted as ported.

## Completed implementation checkpoints

- [x] ~~Classic geometry, wrapping, formats/units, font fallback, real tabs and copyable log; restored Monitor tiles/panels and guarded Ctrl+H.~~ [First checkpoint](native/ui/validation/2026-11-12-parity-first-increment.md).
- [x] ~~Floating/dock-back pages, armed Classic Reset, clock-domain/evidence tracking, independent partial-fan retention, curve consequences/hard de-flatten and keyboard scope.~~ [Second checkpoint](native/ui/validation/2026-11-12-parity-second-increment.md).
- [x] ~~Device/report/second-window menus, Help/licences/About, selected rail/policy profiles and coupled board/current Undo.~~ [Third checkpoint](native/ui/validation/2026-11-12-parity-third-increment.md).
- [x] ~~General clock locks/legacy P0 ownership, multi-field timing plans/profiles/initial Restore and PERF diagnostics.~~ [Fourth checkpoint](native/ui/validation/2026-11-12-parity-fourth-increment.md).
- [x] ~~Integral bounds, voltage admission/VF flags, arbitration inspection; six-word voltage requests and device switch delays with typed restoration.~~ [Fifth](native/ui/validation/2026-11-13-parity-fifth-increment.md) and [seventh](native/ui/validation/2026-11-13-parity-seventh-increment.md) checkpoints. Semantic REL/constants/scaffold offsets remain above.
- [x] ~~Catalog selection/composition, whole preflight/final selected readback, partial-failure grouped Undo, remembered read-only discovery with cancellation and timing diagnostics.~~ [Sixth checkpoint](native/ui/validation/2026-11-13-parity-sixth-increment.md).
- [x] ~~Owned temporary-P0 capture and telemetry-only stable-entry auto-capture.~~ [Seventh checkpoint](native/ui/validation/2026-11-13-parity-seventh-increment.md). CUDA fallback is completed above with separate lower-P2 qualification limits.
- [x] ~~Independent saving/gating/PCIe controls, explicit exact-target recovery helper, policy labels/connector presets and reviewed policy maxima.~~ [Eighth checkpoint](native/ui/validation/2026-11-13-parity-eighth-increment.md). Unresolved modes and physical restart validation remain separate.
- [x] ~~Selected native/Python integral and VF/gate profiles, timing exchange/aliases, per-fan profiles and qualified catalog adapters.~~ [Ninth](native/ui/validation/2026-11-13-parity-ninth-increment.md) and [tenth](native/ui/validation/2026-11-13-parity-tenth-increment.md) checkpoints.
- [x] ~~Voltage request/device-delay and saving/clock catalog transactions with issued receipts and owned lifecycle recovery.~~ [Eleventh](native/ui/validation/2026-11-13-parity-eleventh-increment.md) and [twelfth](native/ui/validation/2026-11-13-parity-twelfth-increment.md) checkpoints.
- [x] ~~Selected full-tune expansion and qualified adaptive/idle/clock-mode handovers.~~ [Thirteenth checkpoint](native/ui/validation/2026-11-13-parity-thirteenth-increment.md).
- [x] ~~Exact NCT3933, uP9512R, NCP4206 and coarse MP2888/MP29816 profile imports; controller ordering and timing plus ordinary/additional memory composition with frozen originals.~~ [Fourteenth](native/ui/validation/2026-11-13-parity-fourteenth-increment.md), [fifteenth](native/ui/validation/2026-11-13-parity-fifteenth-increment.md) and [sixteenth](native/ui/validation/2026-11-13-parity-sixteenth-increment.md) checkpoints.
- [x] ~~Qualified iCX-last composition, controller-first Undo, typed partial recovery and retired-lifecycle protection.~~ [Seventeenth checkpoint](native/ui/validation/2026-11-13-parity-seventeenth-increment.md).
- [x] ~~Full-profile Unicode power labels, immutable retained completion delivery, current identity qualification and conflict-aware chained metadata Undo independent of hardware failure.~~ [Eighteenth checkpoint](native/ui/validation/2026-11-13-parity-eighteenth-increment.md): 114 suites, 22 renderer phases and the documented read-only metadata probe passed.

## Deferred outside this parity session

These are unresolved or previously requested follow-ups, not completed work and
not blockers to matching the released UI/behavior. Existing limitations remain
visible. Do not work on these during this session without new direction:

- Additional ROM/VFE layouts and runtime mappings beyond the released parser.
  Both versions reject the documented 3070 Ti selector layout.
- Adaptive-policy restoration where the prior value is unknown: further ownership
  qualification, not an established missing released path.
- Simultaneous shared GR/count ownership and BLCG ENABLE. The released Python
  implementation documents the ownership limitation and explicitly refuses BLCG ENABLE.
- Lower-P2 timing-band equivalence beyond the release's existing qualification band.
- Expanding iCX composition to timing or another controller. This is broader than
  the released profile workflow.
- Broad physical qualification across other controllers, adapters and operating
  points. Retain existing scoped evidence; validate a concrete port when needed.
- A separate physical PnP restart/relaunch validation campaign. The implemented
  workflow remains available; ordinary tuning must remain session-only.
- Vista/7 branch build/runtime/recovery validation. This prior compatibility
  request is deferred, not cancelled or claimed complete.

## Separate release-note cleanup

- [x] ~~Move the 19 root release notes into an indexed archive, repair links/packaging and open a separate PR.~~ [PR #81](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/81), commit `04b3ab4`: packaging and archive-link validation recorded there. The PR is open and awaits human review; it has not been merged.

Approved differences remain: Focused routing/fluid layout and popup V/F editor, Classic integrated editor, weighted risk colors, staged explicit curve/profile Apply, captured initial state distinct from factory defaults, and session-only ordinary tuning. These are not backlog defects.
