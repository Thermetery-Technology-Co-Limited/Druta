# Eighteenth parity increment: full-profile power labels

Reference: released Python `fa6ee83` full-tune `power_policy_names` and the
native catalog, annotation model, session and host integration in the current
working tree. The [completion ledger](PARITY-COMPLETION.md) remains active;
earlier notes retain their historical scope and evidence.

Selected full-profile power labels are display metadata. Whole-source
validation precedes selection, including strict UTF-8 and the released
80-Unicode-code-point limit. A fresh read-only policy identity capture binds
qualified labels to the current adapter and policy/channel/type; unavailable,
changed or ambiguous rows are not relabelled. Metadata inspection does not
require policy setters, pins, power control or XOC. The immutable UI annotation
snapshot is an input to preflight and is not modified by the worker.

Apply and Undo publish qualified, generation-bound effects for the UI thread to
commit to the annotation store. The host preserves unknown document fields,
tracks commits for conflict-aware Undo, and reports an unsaved state if writing
the label file fails. Metadata Undo is independent of hardware Undo: a fresh
metadata-read failure retains the label Undo for retry, while a hardware Undo
failure can still deliver the qualified label effect once. A failed final
metadata capture must not manufacture an empty effect; issued hardware recovery
remains available. These labels do not modify tuning requests or establish
physical power measurements, and no tuning is applied at startup or after a
driver reset.

Typed Undo also preserves earlier label history: restoring a newer profile
rebases only the matching earlier transaction's exact revision witness. A
standalone edit, even to the same text, breaks that witness and remains protected.
Card UUID/slot and policy/channel/type must all match. Repeated completion
notifications cannot import or undo a label twice.

Metadata-only Preflight, Apply and Undo remain available with tuning controls
locked. Ordinary telemetry failure does not block them when the session still
has a valid paired adapter identity; each operation independently rereads the
policy identities. Hardware and mixed profiles retain their normal admission
checks. A bounded completion queue preserves effects across polling, Pause and
later operation results until the UI acknowledges them.

## Validation

The final Release build and all **114 CTest suites pass** in 35.68 seconds.
New model, actual CoreClient and Session suites cover Unicode limits and invalid
UTF-8, validation before subsetting, alternate identities, partial policy tables,
missing unrelated setters/capabilities, delayed and duplicate completion events,
queue capacity before dispatch, generation changes, telemetry failure, selected
edit conflicts, nested Undo and metadata/hardware failures in either order.
Existing rendered workspace tests cover the unsaved-label retry state.

Root integration review and an independent Sol review found and repaired the
nested Undo revision issue described above. The complete build and test suite
were rerun after the fix. Same-value manual edits and different card/channel
receipts remain protected by dedicated regressions.

The [read-only annotation probe](2026-11-13-parity-eighteenth-annotation-read.json)
passed on Windows 10 x64 and the owner's RTX 3070 Ti at `0000:01:00.0`, UUID
`GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. Fresh policy selection, Preflight,
Apply, local metadata commit, Undo and close all passed. A Unicode label was
committed only to a private in-memory annotation store, then restored; an
unrelated opaque field survived. No user annotation file or GPU tuning request
was written. The probe needs an absolute core DLL path.

All [22 live renderer phases](2026-11-13-parity-eighteenth-smoke.json) passed.
Focused and Classic catalog captures and the initially unrefreshed policy
workspace were visually inspected. Pause and default minimization produced
zero frames and telemetry reads; Always refresh while minimized produced
114 frames and two reads. The four resets are Direct3D renderer resets.
The full-tune example stayed unselected. No tuning SET, controller write,
PnP restart or registry mutation was issued during this checkpoint.

SHA-256 of the tested Release artifacts:

- `druta-ui.exe`: `F1F8173E557ACB270D65270A0FE5DD9A0C6CDA16A624587841A8D5A4396188C9`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The core DLL is unchanged. The final normal application was reopened visibly
on the selected adapter for review. Injected fixtures and this read-only probe
do not establish physical effects or qualification on other adapters.

## Remaining work

The optional CUDA timing-capture fallback remains unported
([CUDA map](2026-11-13-cuda-capture-port-map.md)). Current-ROM role/graph and
scaffold evidence providers remain open
([provider map](2026-11-13-native-evidence-provider-map.md)), as does the narrow
persistent relationship-weight exception
([port map](2026-11-13-relationship-weight-port-map.md)). Physical effects,
controller and adapter coverage, actual device-reset execution and Vista/7
execution retain their existing qualification boundaries. Overall parity is
not complete.
