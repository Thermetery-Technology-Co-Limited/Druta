# Seventh parity increment: voltage requests, device delays and timing capture

Reference: Python release `fa6ee83`; native parent `aa0cf20`.
The [full completion ledger](PARITY-COMPLETION.md) remains active.

## Implemented scope

- Voltage request tables: paired native/public getters, all six signed fields,
  complete-inventory preservation, admission read without automatic enable,
  selected Apply, full exact readback, binding-scoped initial values, Undo and
  recovery. An uncertain SET never becomes an automatic redispatch. Gate-off
  loading is a local draft operation; Apply repeats current qualification.
- Narrow C ABI setter for V2 `0xAC8` / NVAPI `0x87C55C8A`. It validates the
  selected paired adapter, forwards the complete request, guards the actual
  aligned driver buffer and reports raw status. Input mutation or sentinel
  corruption after dispatch is an uncertain failure.
- Independent device switch delays: current INFO/CONTROL type/domain/operation
  checks, exact selected singleton request/readback, preservation, initial
  Restore, Undo/recovery and explicit typed native profile save/load.
- VFE ordinal inspection and an injectable semantic equation contract. Production
  REL/constants and scaffold offsets remain unavailable: native current-ROM
  role/dependency inspection, policy/retained-request evidence and installed
  scaffold receipts are still missing. No persistent scaffold activation or
  arbitrary constant-as-offset substitution was introduced.
- Temporary P0 timing capture: a scoped helper pin, readiness before acquisition,
  observed current-adapter top memory band, bounded wait, and release of only a
  newly acquired legacy/V/F hold. Foreign ownership remains untouched; failed
  cleanup is visible and blocks adapter switching. No CUDA workload fallback is
  claimed. Auto-capture consumes two fresh matching memory/top/offset samples
  from ordinary telemetry; it creates no background poller and respects Pause.
- Binding-scoped policy display labels and reviewed selected maximum requests,
  using existing whole preflight, selected readback and grouped Undo. Connector
  presets, channel-shared labels and per-wire current coloring remain open.

All tuning remains session-only. Saving is explicit; loading stages drafts.
The new voltage profiles are standalone scopes, not additional catalog-composition
adapters. Missing UUIDs do not disable otherwise paired runtime controls; saved
voltage files still require a UUID. Unknown fields and unrelated families survive
partial capabilities and transient reads.

## Review and validation

Two Sol implementations and bounded Terra model/protocol/test tasks were
integrated by the root agent. Cross-review fixed gate-off staging, stale initial
bindings, role disappearance on partial reads, identity errors being retried as
transients, actual driver-buffer guards, and placement behind existing hold/I2C
ownership checks. Root integration review additionally corrected helper pin
lifetime, temporary-owner cleanup and the device-switch race.

- Release build passed with no warnings in the final build log. All **76 CTest
  suites passed** (12.26 seconds), including bounded PE/import checks, actual
  guarded driver-buffer mutation tests, independent/transient voltage families,
  read-only profile staging, immutable CoreClient results, real fixture-child
  helper processes, temporary-hold ownership, failed cleanup, adapter-switch
  exclusion and absent/changing UUIDs.
- The **16-phase live renderer check passed** on the selected RTX 3070 Ti.
  Pause and default minimization each produced zero frames and zero telemetry
  reads. Always refresh while minimized produced 113 frames and two reads in
  its two-second window. New policy-workspace and timing-control captures were
  visually inspected. The workspace capture is deliberately uncaptured until
  Refresh; populated-row interactions are covered by the ImGui fixture tests.
  The report's four device resets are renderer resets, not GPU-driver resets.
- Explicit read-only qualification on this adapter found a complete six-field
  request table for rail 0 and two qualified device-delay rows. Both actions
  reported `may_have_changed=false`; no SET, temporary P0 request, fan/power
  change, timing-register write or GPU-driver restart was issued in this arc.
  This establishes API/layout/read capability only, not physical voltage or
  switching-delay response. Other adapters and Vista/7 execution remain untested.

Current adapter: RTX 3070 Ti, PCI `0000:01:00.0`, UUID
`GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`, Windows 10 x64.

[Renderer report](2026-11-13-parity-seventh-smoke.json) and
[read-only voltage report](2026-11-13-parity-seventh-voltage-read.json) are retained
with this checkpoint. Local logs: `native/build/ui-integrated/` files
`parity-seventh-build-final.log`, `parity-seventh-tests-final.log` and
`parity-seventh-voltage-read.json`; renderer BMPs are in `parity-seventh-live/`.

SHA-256:

- `druta-ui.exe`: `C500B6C4A23DE8D24C2543CC3FC2D17C9FD28A2EB3A9523779A3BA254629A179`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The DLL changed to add the narrow guarded rail setter. Existing runtime tuning
is not persisted or automatically reapplied by any new control.
