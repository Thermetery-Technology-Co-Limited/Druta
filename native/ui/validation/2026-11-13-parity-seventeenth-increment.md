# Seventeenth parity increment: iCX catalog ownership

Reference: released Python `fa6ee83`, the native iCX controller/session
implementation, and the current integration working tree. The
[completion ledger](PARITY-COMPLETION.md) remains active; previous checkpoint
notes retain their historical evidence.

Selected iCX settings use typed opaque owner/epoch receipts. The journal is
published before an owned clamp, B2 enable or selected setting can be issued.
Partial or uncertain operations retain their receipt for explicit Undo and
retry; restoration checks the current candidate before writing and refuses a
foreign replacement. Ownership epochs prevent an old receipt from adopting a
later scalar request merely because its value matches.

Qualified ordinary catalog operations apply first and iCX applies last, retaining
its owned clock floor. Undo restores the controller and releases its own clamp
before restoring earlier ordinary requests. Whole-composition final readback
includes selected no-op fields, and a completed controller receipt remains
available for verification if a later Undo step fails. Application is explicit
and session-only; no startup or post-reset tuning replay is introduced.

The FBVDD response byte 7 is source-documented as an unknown response field; it
is not inferred to be mode 1 or replayed as a requested mode. B2 may remain
enabled after the selected request is restored. These are controller request and
lifecycle semantics, not a claim of measured physical voltage.

NVVDD Auto comparisons use the reported mode; inactive manual-request bytes
may remain nonzero. Manual requests, unrelated bytes, firmware identity and
selected-field candidates still require exact comparisons. Timing profiles
and a second controller are refused before effects because the retained iCX
floor cannot share those ownership/operating-band requirements.

## Validation

The Release build and all **111 CTest suites pass**. The final full run took
34.86 seconds. New driver and actual CoreClient suites cover mixed Daily,
memory and NVVDD settings in either catalog order; the existing session suite
now covers opaque receipts and partial cleanup. Coverage includes all B2/setting
failure positions, accepted-but-failed requests, rejected/unsettled clock
clamps, journal failure, unknown-byte conflicts, retained Auto bytes, final
selected no-op interference and old receipts after scalar reacquisition.

Root review and the independent Sol review found that final verification could
fail after controller and clock cleanup while ownership appeared empty. The
fix retains and publishes recovery until verification succeeds, blocks
conflicting mutations and retries comparison without another SET or clock
reset. The complete suite was rebuilt and rerun after that fix. Completed
controller restoration also remains verifiable while later ordinary Undo is
pending.

The [22-phase live renderer report](2026-11-13-parity-seventeenth-smoke.json)
passes on Windows 10 x64 and the owner's RTX 3070 Ti at `0000:01:00.0`, UUID
`GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. Classic and both catalog captures
were visually inspected. Pause and default minimization produced zero frames
and telemetry reads; Always refresh while minimized produced 113 frames and
two reads. The four device resets are Direct3D renderer resets. The full-tune
example remained unselected. No tuning SET, controller write, PnP restart or
registry mutation was issued during this check. Injected fixtures do not
establish physical iCX response or qualification on other adapters.

SHA-256 of the tested Release artifacts:

- `druta-ui.exe`: `6A8538EBB8F53927A0E3FC1A294AFDAD00A803C54DAE0DDF9248FF43B53FA964`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The core DLL is unchanged. The fresh normal application was reopened visibly
on the selected adapter for human review.

## Remaining work

Full-tune power annotation execution remains open. The optional CUDA timing
fallback remains unported ([CUDA map](2026-11-13-cuda-capture-port-map.md)).
Current-ROM role/graph and scaffold evidence providers remain open
([provider map](2026-11-13-native-evidence-provider-map.md)), as does the narrow
persistent relationship-weight exception
([port map](2026-11-13-relationship-weight-port-map.md)). Physical effects,
controller and adapter coverage, actual device-reset execution and Vista/7
execution retain their existing qualification boundaries. Overall parity is
not complete.
