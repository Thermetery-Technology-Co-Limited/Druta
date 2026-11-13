# Native frontend parity backlog

Updated 2026-11-13. Reference: Python 1.9.1 `fa6ee83`; latest verified native checkpoint: [twentieth increment](native/ui/validation/2026-11-13-parity-twentieth-increment.md). Scope comes from the [Classic review](native/ui/validation/2026-11-12-verbose-parity-review.md), [Focused inventory](native/ui/validation/2026-11-12-focused-port-inventory.md) and [completion ledger](native/ui/validation/PARITY-COMPLETION.md). Those historical inventories are not current open-item lists.

Update this file after each shipping increment. Strike an item only after its implementation is integrated and appropriate behavior/failure verification is recorded; link the evidence. Tests, UI implementation, semantic qualification and physical/OS validation are separate claims. Naming a scope unsupported or writing a plan does not complete it. Overall parity remains open.

## At a glance

Current order: finish installed-offset evidence, then the authorized persistent power-weight workflow, then reconcile the remaining review and validation gaps. Unresolved firmware research stays separate from implemented independent controls. Completed work is checked and struck through; work in progress stays unchecked.

| Workstream | Status | Remaining checklist items |
| --- | --- | ---: |
| Current-ROM voltage evidence and installed offsets | Core provider shipped; offsets/layout gaps open | 3 |
| Persistent power relationship weights | Queued | 8 |
| Unresolved semantics and final parity reconciliation | Open | 5 |
| Scoped hardware and legacy Windows validation | Open | 3 |
| Completed implementation and release-note checkpoints | 25 checked entries | 0 |

**19 open checklist items; 25 completed.** The twentieth increment integrates the ROM getter, graph parser, retained-request capture and production provider for understood layouts. All 122 suites and 22 renderer phases pass. The current 3070 Ti ROM selector layout is still unqualified in both this port and the Python reference; that limitation remains open below.

## Completed: optional CUDA timing capture

Source and acceptance: [CUDA capture map](native/ui/validation/2026-11-13-cuda-capture-port-map.md). [Nineteenth-checkpoint evidence](native/ui/validation/2026-11-13-parity-nineteenth-increment.md): 118 suites and 22 renderer phases pass. This 3070 Ti produced a stable 9427 MHz P2 raw diagnostic below the qualified top band; it did not produce a qualified timing-write snapshot. Lower-P2 qualification remains open below.

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
- [x] ~~Wire `VoltageLimitsApi::equation_evidence` to independently matched ROM graph, loaded equations, policies and retained requests; connect understood REL/constants to the existing selected Apply/readback/Undo/manual-profile paths.~~ This closes wiring for the qualified layouts, not the remaining layout research below.
- [ ] Qualify additional ROM selector/VFE layouts and complete runtime equation mappings from evidence. The current 3070 Ti's selector header is `20 05 11 01`; the traced parser understands `30 05 41`. Runtime mappings for active ROM equation kinds 2/3/4 are also unresolved. Preserve independent rail requests/device delays and raw definitions while these semantic layouts remain unqualified.
- [ ] Implement a separately bounded installed-scaffold receipt reader and fresh variable inventory. Reconstruct and verify original/current graph, retained packets, original roots and installed heads; reject stale/forged evidence.
- [ ] Wire the distinct installed-head offset provider and exact signed selected writer/readback/Undo/profile path. Preserve the original chain and retained requests; never substitute an absolute constant or replay receipt packets. Scaffold installation/activation is not authorized by this session-only provider work.
- [x] ~~Validate understood alternate layouts/identities, partial independent families, transient retention, malformed failures, unknown-byte drift and graph/inventory changes; record production wiring, integration and renderer evidence.~~ [Twentieth checkpoint](native/ui/validation/2026-11-13-parity-twentieth-increment.md); physical voltage effects and broader semantic layouts remain open.

## Next: persistent power relationship-weight exception

Bounded authorized scope and source: [relationship-weight map](native/ui/validation/2026-11-13-relationship-weight-port-map.md). This exception permits selected weights to persist and requires explicit refresh; ordinary tuning stays session-only.

- [ ] Reuse the current-ROM getter; add pure power-table and relationship INFO decoders, including configured source references. Match selected relationship index/type/target, policy channel/unit/bounds and exact Q4.12 values from current evidence.
- [ ] Implement side-effect-free selected Preview with immutable target/image/INFO/registry evidence, exact patch spans and stale-preview/conflict rejection. Preserve independent read-only inspection and last-good stale values.
- [ ] Resolve the exact current PCI instance and driver key; capture registry names/types/bytes. Project minimal dense `romOverride` records without overwriting old records, changing firmware selectors or touching unrelated values.
- [ ] Persist a bounded durable receipt and discoverable active pointer before the first possible registry effect; journal each phase and retain accepted-but-failed/partial outcomes for explicit recovery.
- [ ] Implement explicit Apply and selected-device refresh through an owned transaction host. Revalidate the complete preview before mutation and perform exact registry readback; no automatic startup/reconnect/reset application or session-request replay.
- [ ] Verify activation from a fresh process: exact expected image and complete semantic INFO with only selected weight changes. Report refresh/reboot-required/timeout/identity/verification failures distinctly; registry-write success alone is insufficient.
- [ ] Implement explicit three-way removal/recovery of transaction-owned names only, restoring exact prior type/value or absence, preserving unrelated additions and refusing third-value conflicts. Refresh and verify original image/INFO afterward.
- [ ] Add read/preview/apply/verify/removal/recovery UI and adversarial transaction tests; integrate the production adapters and record scoped runtime validation. Keep general persistent BIOS/registry editing outside this narrow exception.

## Semantic qualifications still unresolved

These are specific remaining capabilities or evidence dependencies, not a request to disable implemented independent controls or to infer support from local hardware.

- [ ] Establish a typed known prior for adaptive-policy cases that currently report it unknown before permitting reversible writes in those cases.
- [ ] Resolve simultaneous shared GR/count ownership semantics and BLCG ENABLE before offering those presently unqualified combinations/modes. Existing exclusive GR/count and qualified clock-mode handovers remain implemented ([thirteenth evidence](native/ui/validation/2026-11-13-parity-thirteenth-increment.md)).
- [ ] Resolve lower-P2 timing-band equivalence only from authoritative layout evidence or independently bound current-adapter observations. The CUDA port must retain the current top-band floor until then; no developer-board clock exceptions.
- [ ] Retain explicit composition limits where iCX's floor clamp conflicts with timing or another controller. Extend only after dependency/preflight/readback/Undo semantics are qualified; ordinary qualified iCX composition is complete ([seventeenth evidence](native/ui/validation/2026-11-13-parity-seventeenth-increment.md)).
- [ ] After the provider increments, reconcile every applicable original-review row and advertised profile scope against current behavior and evidence. The [historical profile audit](native/ui/validation/2026-11-13-profile-scope-gap-map.md) predates completed imports; do not reopen those implementations or count the remaining provider-dependent scopes as ported.

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

## Scoped validation still to record

- [ ] Qualify physical controller/timing/voltage/clock effects only for the adapters, controller recipes and operating points actually exercised. Preserve the existing MP2888 evidence scope; mock coverage of other families is not live qualification. Record any unsupported layout independently rather than inventing board/driver gates.
- [ ] Record actual selected-device PnP restart and relaunch validation; the helper implementation/read-only tests do not establish physical restart success. Ordinary tuning remains session-only; restart persistence is not a validation objective.
- [ ] Run the applicable native build/execution and recovery checks on Vista/7 branches. Compare with current main refusal/validation logic and report differences; do not retain obsolete refusals as presumed legacy necessities.

## Separate release-note cleanup

- [x] ~~Move the 19 root release notes into an indexed archive, repair links/packaging and open a separate PR.~~ [PR #81](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/81), commit `04b3ab4`: packaging and archive-link validation recorded there. The PR is open and awaits human review; it has not been merged.

Approved differences remain: Focused routing/fluid layout and popup V/F editor, Classic integrated editor, weighted risk colors, staged explicit curve/profile Apply, captured initial state distinct from factory defaults, and session-only ordinary tuning. These are not backlog defects.
