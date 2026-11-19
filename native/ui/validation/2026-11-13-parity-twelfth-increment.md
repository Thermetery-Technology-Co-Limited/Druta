# Twelfth parity increment: saving and clock catalog transactions

Reference: released Python `fa6ee83` and the twelfth integration working tree.
The [completion ledger](PARITY-COMPLETION.md) remains active; previous notes
retain their historical evidence.

Selected native and released Python low-power and clock-gating/PCIe profiles
compose in the catalog. The worker uses fresh typed private ownership plans,
issued-prefix receipts and prior Undo. Final verification includes selected
no-op fields, and restoration checks all candidates for conflicts before any
write. Both skins navigate capture selectors to the corresponding panels
without implicit reads, writes or saves. Apply is explicit and session-only;
no profile or ordinary tuning request is replayed on startup or driver restart.

Adaptive requests with unknown prior state are refused. Adaptive plus idle and
GR plus individual acquisition overlap are refused up front pending qualified
typed handover. Changing an already held gating request to its opposite mode
requires an explicit Release followed by Apply. The coupled transactions and
opposing-mode transitions remain open work; the unknown-prior check is a
restoration requirement.

Ownership lifecycle epochs reject stale Undo after a release and reacquisition.
Typed reverse handover preserves earlier catalog history, and panel Undo/recover
routes the matching catalog receipt. Unknown allocation or FREE outcomes block
further candidate control and repeated FREE; cleanup remains unresolved without
an implicit reset. Only guaranteed non-dispatch permits retry. An independently
resolved PCIe recovery is not revived by a later Undo. Accepted adaptive failures
retain pending recovery through failed restoration reads; a GR allocation whose
request is guaranteed not dispatched is cleaned up once.

## Validation

The final Release build passed; **93/93 CTest suites passed** in 14.40 seconds.
Coverage includes actual CoreClient mixed native/Python catalog execution,
whole-preflight refusal, final no-op interference, launched-prefix recovery,
previous Undo, lifecycle replacement, malformed allocation/FREE responses,
guaranteed non-dispatch cleanup, and retained adaptive uncertainty. An initial
orphan-cleanup regression exposed and verified the corrected GR request flag.

The [22-phase read-only live renderer smoke](2026-11-13-parity-twelfth-smoke.json)
passed on Windows 10 x64 and the owner's RTX 3070 Ti. The Focused power-saving
and Classic clock-gating captures were visually inspected. Opening these panels
did not issue diagnostic reads or tuning commands. Explicit Pause and default
minimization each recorded zero frames, presents, telemetry reads and CPU time
within the measured phases; Always refresh while minimized continued with 111
frames and two telemetry reads. Four `device_resets` are Direct3D renderer
resets, not GPU driver resets. No tuning SET, controller write, PnP restart or
registry mutation was issued in this validation.

Final artifacts under `native/build/ui-integrated/ui/Release`:

- EXE SHA-256: `9949A8483BC74DCFC9EAC77564928E1B7B65D9B2F61F0BA8035938FC97FBCEC9`.
- Core DLL SHA-256: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715` (unchanged).

Mock transport and renderer results do not establish new physical effects,
other-adapter qualification or legacy-OS execution.

## Remaining work

Unscoped full-tune import, iCX catalog composition and combined timing/memory
offset application remain open. The optional CUDA timing fallback remains
unported ([CUDA map](2026-11-13-cuda-capture-port-map.md)). Current-ROM role,
graph and scaffold evidence providers remain open
([provider map](2026-11-13-native-evidence-provider-map.md)), as does the
narrow persistent relationship-weight exception
([port map](2026-11-13-relationship-weight-port-map.md)). Physical effects,
controller and adapter coverage, actual device-reset execution and Vista/7
execution retain their existing qualification boundaries. Overall parity is
not complete.
