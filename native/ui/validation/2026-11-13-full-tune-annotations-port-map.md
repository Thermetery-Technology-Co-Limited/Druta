# Full-tune power annotations: next implementation increment

Date: 2026-11-13. Native baseline inspected: `6ae658b` plus the current increment's working tree in `C:\Users\Administrator\Druta-cpp-core-live`. Python reference: release commit `fa6ee83` (1.9.1). Native line numbers below describe the inspected working tree and can move during integration.

This is a design and evidence map. Annotation execution is still unimplemented in the catalog. No production code, build, application launch, or GPU operation was performed for this report.

## Grounded behavior

* Python `src/druta/profiles.py:275–281` captures `power_policy_names` as policy-index mappings containing `name`, `channel`, and `type`. `POLICY_NAME_MAX` is 80 at line 57. Validation at `1081–1092` checks the complete names mapping, canonical indices 0–31, string names of at most 80 Python characters, and integer channel/type metadata.
* Python `src/druta/policynames.py:52–66` cleans nonempty names by stripping whitespace and taking the first 80 characters. `matching()` at `118–123` requires the same live policy index, channel, and record type. It does not require a writable policy, a named-current setter, a particular unit, or a user write gate.
* Python `src/druta/druta.py:2894–2907`, `apply_profile_policy_names()`, merges matching names into the current card's existing notes. Unmatched or omitted names do not delete other notes. It marks the UI-owned store dirty and refreshes display labels.
* `finish_profile_load()` at `druta.py:11317–11336` refreshes the full profile even after reporting per-control failures. Thus annotation completion is independent display metadata: a partial tuning failure does not inherently prevent still-matching labels from being imported. This is different from claiming that the GPU transaction succeeded.
* Python `policynames.py:81–115` saves per UUID and preserves stored names whose policy identity is not currently visible. Labels are persistent user metadata, not persisted GPU tuning requests.

## Current native ownership and gaps

* `native/ui/python_full_profile.cpp:41–43` recognizes the names mapping and the atomic semantic target `annotation:power`. The current validator checks structure/string/type metadata but does not enforce the Python 80-character limit. `core_client.cpp:1260` still refuses execution of this target.
* `ViewState::policy_annotations` is owned and mutated on the main/UI thread. `main.cpp:206–212` loads the native annotation store, `587–600` handles the existing separate Python label-store import, and `602–610` saves it. No worker should receive a mutable reference or pointer to this object.
* `policy_workspace.cpp:17–35` preserves unknown annotation JSON and binds entries to UUID, slot, policy, binding, and reported channel/type. `import_python_policy_names()` at `47–57` copies the existing store before updating matched entries. Reuse this atomic-copy pattern, but the existing function accepts the separate per-UUID Python store, not a full-tune source document.
* Do not use `CoreClient::scoped_capture()` as the authority for annotation availability. Its policy loop at `core_client.cpp:1001–1003` excludes board, inconsistent, nonwritable/non-current and unknown-unit rows. Names need fresh identity metadata, not a qualified tuning writer. `power_capture()` also requires setters, pin interfaces, and a configured board request through `power_api()` (`core_client.cpp:385,410–429`), which are unnecessary annotation dependencies.
* Existing `dc_cl_power_policies_read` is a read interface; its confirmation callback establishes derived-clamp evidence rather than authorizing a SET (`native/include/drutacore/drutacore.h:1010–1018,1126–1136`). A dedicated metadata capture should use the existing read ABI and its required read-only inputs, validate unique row identities, and avoid adding setter/pin/rail gates. Do not invent a new hardware protocol.
* `SessionSnapshot` currently carries only the latest action operation/OK/log ID (`session.h:18–23`). The worker updates that result at `session.cpp:400–416`; device selection clears it at `466`. `main.cpp::consume_profile_catalog()` (`381–435`) tracks one submitted immutable document and consumes a matching latest result. This is not a durable completion queue: another action can replace the latest result before a delayed UI consumer processes its effect.

## UTF-8 limitation to resolve in the same increment

Python's length/slicing counts Unicode characters. Native `PolicyAnnotations::set()` currently rejects `label.size()>80` (`policy_workspace.cpp:23`), and the separate importer truncates with `name.resize(80)` (`54`). These count bytes and can split UTF-8 sequences. `PolicyWorkspaceViewState` also has an 81-byte editor buffer (`policy_workspace_view.h:7`). For example, 80 non-ASCII letters can be a valid released Python name but exceed the current native byte limit.

Use one shared, tested UTF-8 validation/normalization helper: validate encoding, reject embedded NUL, apply the agreed Python-compatible whitespace/name handling, and count at most 80 Unicode code points without cutting a sequence. Full-source validation must reject an overlong saved field before any tuning SET; normalization must not conceal a malformed source. An editor capable of retaining 80 four-byte code points needs 321 bytes including NUL (or a dynamic string). Preserve existing unknown JSON and all unrelated labels. Do not turn storage conversion into silent loss of a valid imported name.

## Minimal implementation architecture

### 1. Pure selected annotation plan

Add a small pure model, preferably `policy_annotation_profile.h/.cpp`, with explicit desired rows: policy index, channel, type, normalized label, current binding, and exact UUID/slot target. Keep source validation separate from live matching. The full-tune envelope's `annotation:power` selection remains authoritative; loading or staging a document never changes labels.

Validate the complete original source through `describe_python_full_profile()` before selecting fields. The worker can then compile the selected names against fresh read-only policy metadata. Unmatched identities become explicit skips and leave existing notes unchanged; they are not rewritten by numeric policy index alone. A transient failure to obtain required fresh metadata must not be treated as an authoritative empty table or erase established labels. Import rows directly; do not fan a single saved row out with `set_channel()` unless the selected source explicitly contains those matching rows.

### 2. Worker qualification and immutable effects

In `core_client.cpp`, replace the expansion refusal with a prepared annotation component. Preflight it with every other selected component before the first tuning write. Recapture and match target/row bindings at the point the annotation effect is finalized. Do not use stale telemetry or a previous scoped-profile capture as proof.

Add an immutable effect payload to `ActionResult`, for example `shared_ptr<const PolicyAnnotationEffect>`. It should contain target identity, qualified selected rows/bindings, matched/skipped counts, and a transaction token. It must contain no UI object, file path to execute, or callback into `ViewState`.

Preflight and file Load return no Apply effect. For an Apply whose whole-source/preflight validation passed, labels may still produce an independent qualified effect after another tuning component fails, matching released behavior. Report the hardware failure and label outcome separately. If current identity/bindings cannot be requalified, defer/refuse that metadata effect rather than claiming labels were applied. Annotation-only Apply performs no GPU SET and does not claim a hardware mutation.

### 3. Retained completion delivery

Extend `session.h/.cpp` with a small retained immutable completion-event channel rather than borrowing the current last-action slot. Each event must include generation, unique event/transaction ID, exact action/log ID, operation, Apply/Undo phase, and effect payload. Enqueue while publishing the corresponding worker result, under the session lock. The UI drains or acknowledges events through a Session API; it must never inspect the worker's mutable containers.

Event lifetime requirements:

1. Keep an event until the UI explicitly consumes/acknowledges it; polling or later actions must not overwrite it.
2. Consume each event at most once, including duplicate notifications or repeated render frames.
3. On generation/adapter change, discard or reject old-generation events explicitly; never attach them to the newly selected card. Device reset must not trigger GPU request replay.
4. Bound queue storage with admission/backpressure before a new effect-producing action starts; never silently evict an unacknowledged event after its associated hardware action ran.
5. Acknowledge after the UI decides the effect's disposition. Disk-save failure is a separate state: do not repeatedly import the same effect while retrying storage.

### 4. Main-thread commit and storage

`main.cpp::consume_profile_catalog()` should consume exact completion events, make a copy of `view.policy_annotations`, apply only qualified selected rows, and swap the copy after the complete metadata update succeeds. Reuse the existing save path and unreadable-store protection. Preserve unrelated/unknown JSON and hidden policy notes.

Capture the selected before-labels on the UI thread when the action is submitted. Before committing the effect, compare those selected labels with current UI labels. If the user edited the same labels while the worker ran, do not overwrite them silently: retain an explicit conflict/review outcome. A global annotation-store revision alone is too broad because an unrelated label edit should not invalidate selected matches.

On save failure, retain the in-memory result and dirty/retry state, preserve the existing file, and report that labels are session-visible but not saved. Do not misreport this as an unverified hardware write.

### 5. Grouped Undo contract

Do not pretend hardware Undo has reverted UI notes. For full grouped profile Undo, keep a UI-owned before/after annotation receipt keyed by the worker's transaction token; retain only immutable identity/binding/effect information in the worker's Undo group. A successful grouped Undo emits a corresponding restore-metadata event. The UI restores only labels still matching the applied receipt and the freshly qualified identity, preserving later user edits and unrelated notes.

An annotation-only profile can use a metadata-only grouped receipt/event, with `may_have_changed`/status clearly distinguishing display metadata from GPU SETs. No raw state replay or startup tuning application is involved. If unified metadata Undo is deferred, that must be stated as an incomplete profile behavior; merely removing the unsupported-scope message is not completion.

## Suggested ownership for the next increment

* One model owner: new `policy_annotation_profile.h/.cpp`, dedicated tests, shared UTF-8 helper, and coordinated narrow `policy_workspace.*`/`python_full_profile.cpp` validation changes.
* Root/shared owner: `backend.h`, `core_client.cpp`, `session.h/.cpp`, `main.cpp`, and CMake. This owner wires immutable plans/effects, retained delivery, catalog admission, completion consumption and grouped Undo tokens.
* Independent test/review owner: actual CoreClient fake tests, Session delayed-completion tests, and host metadata commit/conflict tests. No GPU access is necessary.
* Coordinate `policy_workspace_view.*` editor-buffer/UTF-8 edits with the model owner. Keep one owner per shared-file region.

## Acceptance criteria

* Malformed names anywhere in the original full source reject before any selected SET, even when that malformed field is deselected; file Load and Preview change neither notes nor hardware.
* Selecting/deselecting `annotation:power` actually controls execution. Annotation-only Apply needs fresh reported metadata but no rail, pin, policy-setter, XOC or tuning-unlock capability.
* Alternate UUID/slot, changed index/channel/type, duplicate current identities and stale captures cannot mislabel another row. Read-only policies remain eligible; absent unrelated rows do not remove matched labels or existing hidden notes.
* ASCII, 80-code-point multibyte labels and four-byte code points round-trip intact. Overlong, invalid UTF-8 and embedded-NUL inputs are handled explicitly; no byte-splitting truncation occurs.
* Worker effects never touch mutable UI annotations. Delayed, duplicate, overwritten-latest-result and generation-switch cases cannot lose, duplicate or misapply a completion.
* A partial hardware failure and independent label import produce truthful separate outcomes consistent with the released completion behavior. No annotation effect is emitted for a failed whole preflight.
* Selected labels are atomically committed; concurrent same-label edits are preserved as conflicts, and unrelated edits do not block the import. Unknown annotation JSON and unreadable-store protection remain intact.
* Grouped Undo restores qualified before-labels only when the applied labels still match; later user changes survive. Metadata-only Undo never issues a GPU SET. File-save failure retains visible labels and an actionable storage outcome.
* Unit/model, actual CoreClient fake, Session completion lifetime, and host commit/Undo tests pass under the root's coordinated build. No hardware qualification claim is inferred from these tests.
