# iCX catalog composition port map

Read-only trace of the increment16 working tree against released Python `fa6ee83`. No hardware, build, or implementation work was performed for this map. The mixed iCX catalog gap remains open.

## Existing contracts and release evidence

- `fa6ee83:src/druta/icx_presets.py:165–191` builds changed-only operations in deliberate order: NVVDD Auto, OCP enables, FBVDD, switching frequencies, loadline, manual NVVDD, OCP disables. `:194–245` requires complete current/final readback, the caller's identity/unlock/clock-floor checks, and stops after a failed operation without automatic retry or rollback. A preset is the complete eight-field semantic request state; it is not raw register state or a physical voltage measurement.
- `fa6ee83:src/druta/druta.py:5313–5343` validates and explicitly applies a staged preset only after obtaining the clock floor. `:5520–5549` retains the floor lock after the write. The source explicitly says Release/quit clear that lock without clearing the iCX setpoint. It provides no automatic post-write clock-release step.
- `fa6ee83:src/druta/profiles.py:803` deliberately excludes iCX voltage from the ordinary saved/startup profile, and `:1352` refuses its replay there. Mixed native catalog composition is therefore an explicit extension using qualified native transactions, not an already-established Python full-tune replay contract.
- Native `i2c_session.cpp:395–419` already performs read-only complete preflight, paired target/route/selection binding, clock API/table checks, and captures a semantic prior preset. `:421–447` requires an unchanged prior, acquires the floor, applies changed operations, and checks complete desired settings. `icx_preset.cpp::icx_preset_requests` retains the release operation order.
- `i2c_icx.cpp::send` preserves unselected bytes of the selected state packet, retains original/previous/attempted semantic values for restoration, and does not retry a setting SET. B2 enable has no known disable command. `needs_clock_clamp()` returns true. `i2c_session.cpp:257–308` validates its exact owned floor request and observed settled graphics clock; `write_bytes` requires that clock request for this driver.
- Native Release (`i2c_session.cpp:593–627`) restores the driver's owned selected requests before releasing its own clamp. This is the existing native lifecycle; a preset document must not bypass it or replay unrelated state.

## Two existing lifecycle defects to fix first

1. **Partial Undo cannot resume after a fully successful Apply.** `restore_icx_profile`, `i2c_session.cpp:456–463`, requires the complete current settings to equal the desired document whenever `catalog_complete_` is true. The driver's `restore`, `i2c_icx.cpp:159–165`, erases each successfully restored field before moving on. With two changed fields, let the first restore succeed and the second fail. The retained receipt now sees a mixed original/desired state and the next Undo refuses before calling the driver again. Replace the whole-desired-only guard with an issued-field candidate receipt, complete candidate preflight, and per-step rechecks; retain completed-field comparison evidence until the entire original is verified.

2. **An old catalog plan can adopt later scalar ownership.** `IcxProfilePlan` contains identity/route/recipe/selection revision and documents, but no session owner token or mutation epoch. Explicit `release()` leaves `catalog_plan_`/`catalog_complete_` intact. Example: catalog A changes 950 to 1000 mV; explicit Release restores 950 and drops ownership; a later scalar request acquires 1000 again; old A's Undo matches the same desired document and calls Release on the later scalar lifecycle. Introduce an opaque per-driver/session receipt with ownership epoch. Every scalar mutation/reacquisition must retire earlier catalog authorization, even if values match. Read-only observations must not retire it.

## Smallest justified composition boundary

Keep the clamp after changed iCX Apply. Do not add an implicit clamp-only release to make other writers fit. The release permits an explicit user clock release, but does not justify automatically dropping the lock after request readback, and the current native API combines request restoration with clock release.

For the first mixed increment, execute iCX **last**, after complete read-only preflight of every component. This supports independently qualified ordinary request writers without adding an exception allowing ordinary writes through an owned iCX clamp:

- Existing Daily/Advanced and scoped request writers (fan, board/policy power, voltage boost, rail requests, ordinary/additional memory offsets and core offsets) can run before the clamp. Keep the current per-writer pairing/layout/expected-value checks and freeze selected memory originals through recapture, including initial no-ops.
- Preserve existing rail/voltage-before-clock ordering among those ordinary components; then acquire the iCX clamp and apply the preset. Retaining the clamp means the resulting operating point is intentionally the floor even when an offset request is selected. Final verification concerns configured requests, not an assertion that the GPU is running at an offset-derived clock.
- Require final fresh verification of every earlier selected request after iCX/clamp acquisition, plus complete iCX desired state and exact owned clamp readback. Fail and retain issued Undo if any earlier request changes or its existing qualified getter no longer works under the floor. Do not manufacture new operating-band evidence.
- Keep timing-helper/top-band capture, explicit frequency locks, active V/F holds, additional controllers, and other ownership-acquiring operations outside this first boundary. Timings are especially unsuitable for an unconditional last-iCX adapter because the clamp can remove the qualified top-memory operating band. Separate timing/clamp handover research remains named work.
- An iCX preset that already matches all fields should neither acquire a clamp nor obtain write ownership. It still participates in final desired-state verification, so interference with an initially matching field is detected.

This is narrower than allowing arbitrary writes while the iCX clamp remains active. Such broader support would require an explicit typed authorization context, exact clamp/observed-clock checks around every dependent mutation, and evidence for each affected operating-point-dependent writer. It is not needed for the last-component boundary.

## Required typed transaction hooks

Use heap-backed opaque `IcxProfilePlan`/`IcxProfileReceipt`, matching the newer controller transaction contracts. The existing value-owned plan may remain as an internal semantic document representation, but not as the sole ownership receipt.

- `preview(document, plan)`: complete current identity/selection/recipe/packet/type/bounds qualification, complete prior/desired semantic state, changed-operation ordering, restore representability, no ownership or SET. Bind a session token and mutation epoch. Unknown unrelated packet bytes are comparison/preservation evidence only.
- `apply(plan, before_effect)`: freshly recheck prior, gates and clock eligibility before the first effect. Journal once before the first possible clamp/B2/setting mutation. The shared receipt accumulates issued fields and possible effects before each dispatch. A clamp failure or B2-only effect must remain recoverable; a guaranteed nondispatch must not fabricate a setting receipt. Preserve successful prefix and uncertain operation without automatic rollback.
- `verify(plan)`: fresh complete desired semantic state, including initially matching fields. If a clamp was acquired, require its exact current owned tuple; do not treat another client's identical numeric lock as proof after lifecycle replacement.
- `restore(receipt)`: fresh whole-issued candidate/identity/epoch preflight before any SET, then per-step checks. Use qualified semantic setters in the release's safe dependency order for the original values; do not simply iterate a map alphabetically or replay the prior document. Restore only actually issued fields, while verifying initially unmodified/completed fields. Keep the clamp until all owned requests restore, then release only this lifecycle's request. Retain candidates/completed receipts on partial failure, including controller-restored/clock-release-failed retries.
- `verify(receipt, original=true)`: comparison-only original verification after cleanup, suitable for aggregate completed receipts. Invalid/foreign receipts must not replace an existing valid recovery receipt.

## CoreClient integration

`core_client.cpp:1454` currently refuses all mixed iCX catalogs. `:1631–1633` journals the old plan before Apply; final composition verification currently treats iCX as already verified because it is standalone. `:2359–2362` recognizes a direct receipt or a sole-child iCX group only. Those assumptions must change together:

1. Retain the refusal until typed hooks above are complete; classify allowed companion targets before any component writes.
2. Whole-preflight all components and order iCX last independent of file order. Use a cumulative before-effect receipt rather than assuming a plan proves dispatch.
3. Replace the final `verified.ok=true` iCX shortcut with the new fresh complete verification.
4. Restore an issued iCX receipt **first** during aggregate Undo, while retaining its clamp through controller restoration. Stop before ordinary Undo if controller restoration or owned clamp release is unconfirmed. Then restore ordinary requests with existing grouping/ranks.
5. Keep restored iCX comparison receipts until the whole group verifies; do not drop a group on partial Undo. No temporary authorization may survive failure, return, close, or another command.

## Required hardware-free regressions

- Mixed fan/power/rail/ordinary memory/domain2/core request cases in both file orders: whole preflight is read-only; ordinary setters precede clamp/iCX; final configured requests and all iCX fields are checked; Undo restores iCX and releases its own clamp before ordinary Undo.
- A matching iCX document: no B2/setting/clamp mutation or ownership; later interference with a selected no-op is detected.
- Later-component preflight failure, bad prior grid/range, missing current state/export, changed identity/route/firmware/selection: zero effects in every scope.
- Clamp accepted-but-failed and unsettled/foreign clamp; B2 accepted-but-failed; each setting-prefix failure; no blind retry; before-effect journal already exists. Preserve the diagnostic that B2 may remain enabled.
- Two-field successful Apply, first Undo field restored, second Undo failure, then successful retry. Unknown/unissued fields remain untouched. Restore order includes OCP-enable-before-voltage and OCP-disable-last where selected originals require it.
- Catalog A / explicit Release / scalar same-value reacquisition: old A receipt causes zero writes/frees/releases. Read-only refresh does not invalidate A. New selected adapter/session also rejects A.
- Foreign replacement of an issued field or unrelated packet byte, both before Undo and injected between restoration steps: no overwrite; full/per-step candidate checks stop progress and preserve the receipt.
- Controller restoration succeeds but clock release fails or becomes unreadable: retry does not resend restored settings or reset a foreign clock request.
- Final iCX verification fails after a successful prefix or earlier ordinary request changes during clamp acquisition: retain meaningful original group Undo; no automatic cross-scope rollback.
- Reject timing/active-hold/explicit-lock/second-controller compositions before all writes for this bounded increment. Do not count those combinations complete.

Ownership for an implementation arc: iCX driver + session typed lifecycle; CoreClient catalog ordering/Undo; dedicated driver/session/client tests; root coordinates shared headers and CMake. No new controller packet semantics, hardware allowlists, startup persistence, or automatic post-reset application are required.
