# Eleventh parity increment: voltage catalog transactions

Reference: released Python `fa6ee83` and the eleventh integration working tree.
The [completion ledger](PARITY-COMPLETION.md) remains active; previous
checkpoint notes retain their historical scope and evidence.

## Scope

- Native voltage request/limit profiles and released Python `voltage_requests`
  and `voltage_controls` switch-delay documents can participate in selected
  catalog composition. Strict descriptors validate the entire source before
  subsetting; shared canonical targets detect overlaps with rail profiles.
- The worker preflights the complete selection using session-bound typed plans.
  An explicitly selected admission enable may be projected during preflight;
  actual admission is checked at each dependent write. Admission enable is
  ordered first and disable last. Final selected readback includes no-op fields.
  Issued typed receipts support partial recovery and preserve prior Undo without
  replaying opaque state.
- Fresh failure publication distinguishes malformed, unsupported and transient
  results. No new ROM role, graph or scaffold semantic provider is claimed.
  Catalog capture selectors navigate to the appropriate Advanced voltage panel
  in both skins without an automatic read, write or save. Profile application
  remains explicit in the current session; ordinary tuning is not replayed on
  startup or after driver reset.

## Validation

The final Release build passed, and **92/92 CTest suites passed** in 14.44
seconds. Coverage includes whole-issued restoration preflight refusing a
selected conflict before writes, partial recovery and retained prior Undo.

The [22-phase read-only live renderer smoke](2026-11-13-parity-eleventh-smoke.json)
passed, including voltage-request and device-delay panels. Explicit Pause and
default minimization each measured zero frames, presents, telemetry reads and
CPU time. Always refresh while minimized recorded 114 frames and two telemetry
reads. The four `device_resets` are Direct3D renderer resets, not GPU driver
resets. No hardware writes were issued in this validation.

Final executable: `native/build/ui-integrated/ui/Release/druta-ui.exe`;
SHA-256 `25B73991C31919C840ADEAD8D485B119ED5B4763E1B3C78E4B15880ADD231782`.
Core DLL: `native/build/ui-integrated/ui/Release/drutacore.dll`;
SHA-256 `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`.
These results do not establish new physical tuning or broad adapter qualification.

## Remaining work

Low-power and clock/PCIe catalog composition and unscoped full-tune import remain
open. iCX remains standalone, and timing plus memory-offset changes require
separate preflights/applications. The optional CUDA timing-capture fallback
remains unported ([CUDA map](2026-11-13-cuda-capture-port-map.md)). Current-ROM
role/graph and installed-scaffold receipt/policy/retained-request providers
remain open ([provider map](2026-11-13-native-evidence-provider-map.md)), as
does the explicitly scoped persistent relationship-weight exception
([port map](2026-11-13-relationship-weight-port-map.md)). Physical effects,
controller and adapter coverage, actual device-reset execution and Vista/7
execution retain their existing qualification boundaries. Overall parity is
not complete.
