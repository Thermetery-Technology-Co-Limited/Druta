# Device menus and selected rail/power profiles

Continuation after `4107f34`, against released Python `fa6ee83`. The two Sol
workers handled Device/Help workflows and the selected-profile model/view.
Terra handled bounded timing-profile, session-identity and regression work.
Root integrated and reviewed the changes. The [full ledger](PARITY-COMPLETION.md)
still records the remaining parity work; this checkpoint does not complete it.

## Implemented

- Both skins expose File, Device and Help menus. Device supports a captured
  selected-adapter report, copy/save, capability refresh without dropping drafts,
  timing-helper navigation and a second window. The launcher passes only the
  exact EXE/core DLL, PCI slot, expected UUID and presentation. Constrained
  enumeration never falls back to another adapter. Fresh identity validation,
  reconnect and explicit retry retain the UUID constraint; deliberate adapter
  selection starts a new session. Report dialogs recheck generation/target.
- Help shows keyboard controls, About and actual bundled GPL/third-party licence
  and native-guide files. Missing files, missing readings, zero values and stale
  values remain explicit. Report and launch intents are cancelled during close.
- File → Rail / power profiles captures understood fields on request, saves an
  explicit subset of fresh configured values and loads a preview. Apply rebuilds
  the whole plan from fresh target, binding, units, gates and bounds. Rail deltas
  and estimated absolute voltage are separate. Independent missing rails/policies
  do not prevent a qualified subset; absent selected fields cannot silently vanish.
- Board power runs before selected current/power policies and rail fields.
  Selected policies become explicit session pins. Existing writers perform
  exact readback, with pre-dispatch request/pin drift checks and final complete
  selected readback. One original power baseline and independent rail requests
  form grouped Undo. Partial failures retain it. Changed unselected pins refuse
  Undo before any pin mutation; pin restoration is read back. No opaque state is
  replayed. Current/captured-initial rail bounds match the backend's expansion.
- The workspace displays the actual action result using its exact log entry ID;
  later maintenance or unrelated rejection logs cannot replace it. Apply respects
  owned/recovery state, while read-only capture remains available.
- A pure timing-profile capture/validation module binds typed fields, selected
  register geometry, adapter and qualified operating band. It has no file I/O,
  automatic Apply or raw-register replay. Full timing UI/batch integration remains
  open. Python rail/power profile import and cross-scope composition also remain
  open; the new explicit profile format is native.

## Validation and limits

Release build and bounded PE/import audit passed. All **41 CTest suites** passed:
830 CoreClient checks, 224 Session checks, 1,071 general UI checks, 45 scoped-model
checks, 52 scoped-view checks, 32 timing-profile checks, dedicated app-menu,
Classic geometry/behavior and the existing controller/monitor suites. Failure
coverage includes alternate identities, transient enumeration/identity reads,
malformed/duplicate inputs, partial capabilities, gate-off capture, changed
bindings/requests/pins, coupled no-op policy reassertion, partial application,
selected final mismatch, meaningful Undo and actual result-message association.

Built-in renderer captures on this Windows 10 x64 / RTX 3070 Ti were inspected:
Classic Daily at 2100×1400 and Focused Daily at 1800×1200. The new menu bar is
visible in both. Classic keeps fixed knob widths with horizontal scrolling;
Focused keeps its current Daily routing. Local captures are under
`native/build/ui-integrated/parity-third/` and are not committed.

The Windows computer-use capture helper timed out on the first capture and its
documented recovery retry. No coordinate clicks were attempted. Menu/workspace
interactions were exercised through the production ImGui headless tests; live
clicks, native file dialogs and child-process launch were not physically verified
in this checkpoint. The freshly built EXE was left visibly running for review.

No GPU tuning write or driver restart was issued. This does not qualify another
controller, timing-register writes or Vista/7 execution. The import audit remains
bounded and is not proof of the entire legacy-OS API floor. Multiple external
writers can still make a post-write observation ambiguous with driver coupling;
the exact readbacks and drift checks do not provide cross-process transaction
isolation. Earlier hardware evidence retains its original operating-point scope.

SHA-256:

- `druta-ui.exe`: `F5507A6B3362A08EE5F4C22C52B00DA5CA84419C2E85E6FB5119E95A98CEF2ED`
- `drutacore.dll`: `687DFF65557C785388D01887FF36FCC20E6C43FB2FF3D0E1D426D03F25012F64`
