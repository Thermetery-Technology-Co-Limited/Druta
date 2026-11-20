# Thirteenth parity increment: selected full-tune import and handover

Reference: released Python `fa6ee83` and the thirteenth integration working
tree. The [completion ledger](PARITY-COMPLETION.md) remains active; previous
checkpoint notes retain their historical scope and evidence.

The catalog recognizes unscoped Python schema-2 full-tune snapshots and keeps
the original document in an immutable selection envelope. Whole-source
structural validation precedes subsetting, and selected targets use current
qualified native preflight and explicit session-only Apply. The import maps
indexed V/F requests against current geometry, memory driver units against the
current scale and grid, and qualified fan, rail and clock/voltage offsets. The
historical full-tune curve has no saved point voltages or layout; current-layout
validation cannot establish those historical values. No missing geometry or
physical defaults are invented.

Selected coupled power restoration checks current policy identity and pins,
with unpinned driver-derived values handled dynamically after board changes.
I2C controller settings and power-name annotations remain visible selections
but their full-tune execution adapters are pending. Unknown or malformed
requested fields cannot be silently dropped by selecting an unrelated target.

Clock handover supports qualified opposite-mode transitions for ELCG and SLCG;
BLCG does not provide an ENABLE operation. Power-saving work adds qualified
adaptive/idle and exclusive GR/counted-request handover. Simultaneous overlapping
ownership remains unqualified. Typed issued receipts retain previous ownership
and Undo through failure; the source review and injected failure tests exercise
preflight, transition, partial recovery and conflicting selected values. These
are request/lifecycle results, not evidence of physical effects on other GPUs.

## Validation

The Release build passed, followed by **95/95 CTest suites** in 14.43 seconds.
Coverage includes full-source validation before selection, exact raw rail and
offset units, curve geometry changes between preflight and Apply, fan/core
precedence, memory scale conversion, unrelated read-only policies, driver-derived
policy changes after both board and named-current writes, and partial-failure
Undo. Opposing-mode and exclusive ownership handovers include failed allocation,
release and readback cases. The curve's frozen layout uses an immutable heap
snapshot, avoiding growth of recursive Windows Undo stack frames.

The [22-phase live renderer smoke](2026-11-13-parity-thirteenth-smoke.json)
passed on Windows 10 x64 and the owner's RTX 3070 Ti, PCI slot `0000:01:00.0`,
UUID `GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. Focused and Classic catalog
captures were visually inspected. The full-tune example remains unincluded;
loading it issues no tuning command. Pause and default minimization produced
zero frames and telemetry reads; Always refresh continued both while minimized.
The report's four device resets are Direct3D renderer resets.

No tuning SET, I2C write, PnP restart or registry mutation was issued for this
increment. Mock request/lifecycle coverage and the live read-only rendering run
do not establish new physical-write qualification or support on other adapters.

SHA-256 of the tested Release artifacts:

- `druta-ui.exe`: `BEB221644574E2163712D81A029B629A57ABCC724692094BD78D65A9C227E701`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The core DLL is unchanged from the preceding checkpoint.

## Remaining work

Full-tune I2C and annotation execution remain pending, as do iCX catalog and
combined timing/memory-offset composition. The optional CUDA timing fallback
remains unported ([CUDA map](2026-11-13-cuda-capture-port-map.md)). Current-ROM
role/graph and scaffold evidence providers remain open
([provider map](2026-11-13-native-evidence-provider-map.md)), as does the
narrow persistent relationship-weight exception
([port map](2026-11-13-relationship-weight-port-map.md)). Physical effects,
controller and adapter coverage, actual device-reset execution and Vista/7
execution retain their existing qualification boundaries. Overall parity is
not complete.
