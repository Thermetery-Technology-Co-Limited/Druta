# Tenth parity increment: selected profile composition

Reference: released Python `fa6ee83` and the tenth integration working tree.
The [completion ledger](PARITY-COMPLETION.md) remains active. The earlier
[profile scope audit](2026-11-13-profile-scope-gap-map.md) records a historical
baseline, not the current implementation.

## Scope

- Released Python voltage-request and device switch-delay profiles can be
  structurally checked and staged through their existing standalone typed
  controls. Low-power and PCIe-speed profiles likewise stage understood selected
  requests against current adapter evidence. These imports do not themselves
  write hardware; Apply remains an explicit current-session operation.
- Shared profile descriptors expose selected targets, and strict subsetting
  retains identity and selected semantics while rejecting malformed, duplicate,
  unknown or conflicting selections. Structural acceptance is not a claim that
  a current adapter supports the selected operation; the worker's fresh checks
  remain necessary.
- Catalog composition now includes selected voltage admission/V/F policy flags
  and power feedback integral fields. The worker coalesces compatible selected
  fields and performs fresh whole-selection preflight before any writes, uses
  the existing qualified writers and journals issued typed receipts for grouped
  Undo/recovery, and verifies selected final readback. Feedback lower/upper
  ordering and admission versus V/F transaction boundaries remain meaningful.
  Unselected fields and opaque state are not replayed.
- Standalone voltage-request/limit, low-power and clock/PCIe staging does not
  yet imply catalog execution for those families. External typed preflight and
  receipt contracts are still needed before adding them to catalog composition.

Ordinary tuning is session-only. Loading a profile stages it; saving or restart
never authorizes automatic application. The separately authorized persistent
relationship-weight exception does not change that rule.

## Validation

- Release build passed; **91/91 CTest suites passed** in 14.26 seconds.
  Actual CoreClient tests cover native/Python composition, coalesced bounds,
  gate ordering, complete preflight, selected no-op interference, uncertain
  partial writes, grouped restoration and preservation of earlier Undo.
  Service tests cover alternate identities, stale metadata, partial capabilities,
  transient reads, unknown-field preservation and retained recovery receipts.
- Review found and fixed a preflight gap: pending grouped or independently owned
  feedback recovery blocks a new combined transaction before any component SET.
  The client regression suite also caught added stack pressure from embedding
  a full integral receipt in every generic Undo record. Integral receipts now
  use immutable heap snapshots, and journal temporaries are heap allocated.
  The original regression tests and executable stack reserve are unchanged.
- The final [20-phase live renderer report](2026-11-13-parity-tenth-smoke.json)
  passed on the owner's RTX 3070 Ti / Windows 10 x64. Both layouts, catalog
  windows, policy panels, resize, shunt editor and Pause/minimize paths passed.
  Focused catalog and Classic feedback captures were visually inspected.
  Pause and default minimization each produced zero frames and telemetry reads;
  Always refresh while minimized produced 114 frames and two reads.
- The renderer harness issued no tuning commands. Its four device resets are
  Direct3D renderer resets, not GPU driver restarts. No new tuning SET, I2C write,
  PnP restart or registry mutation was performed in this increment. An initial
  smoke invocation lacked its output directory and could not save artifacts;
  the final run used the created directory and exited successfully.

Final artifacts under `native/build/ui-integrated/ui/Release`:

| Artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `2E371CB65FE3BB454E688364AEB54A06EFB471BE9ACC8EDB30D62611091B3346` |
| `drutacore.dll` | `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715` |

The core DLL is unchanged from the preceding checkpoint. These fixtures and
read-only rendering results do not establish successful physical tuning writes
or broad adapter qualification.

## Remaining work

The [provider map](2026-11-13-native-evidence-provider-map.md) retains the
current-ROM graph/role, installed-scaffold receipt/policy and retained-request
provider gaps for semantic REL/constants and scaffold offsets. The narrow
persistent relationship-weight exception still needs the stages recorded in its
[port map](2026-11-13-relationship-weight-port-map.md).

The optional CUDA timing-capture fallback remains unported; see the
[CUDA map](2026-11-13-cuda-capture-port-map.md). Unscoped full-tune import and
catalog contracts for the standalone families above remain open. iCX remains
standalone; timing and memory-offset changes still require separate
preflights/applications. Physical effects, actual device-reset execution,
controller and adapter coverage, and Vista/7 execution retain their existing
qualification boundaries. This increment does not complete overall parity.
