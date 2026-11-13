# Twenty-fourth parity increment: explicit weight activation and recovery

Both native skins now complete the released relationship-weight workflow with
durable preparation, explicit Apply and selected-device refresh, independent
verification, and explicit removal/recovery. This is the narrow persistent
weight exception; ordinary tuning stays session-only. No generic registry or
firmware editor, new relationship layout, or automatic tuning replay is added.

The recovery record binds UUID, logical PCI identity, exact Windows instance and
driver key, original image/INFO, selected semantic weights and typed registry
values. A bounded atomic record and discoverable active pointer are written
before any registry effect. Journal updates preserve immutable evidence and
record each potentially issued name before dispatch. Partial or accepted-but-
failed writes retain their recovery state instead of being reported as success.

Apply revalidates the complete Preview, then closes the initiating session.
The helper waits for that process to exit, reacquires the exact target, writes
only derived added ROM-override records with exact readback, and invokes the
existing selected-device refresh helper. A separate fresh process must verify
the expected complete image, complete relationship INFO, Windows binding and
registry before activation is marked successful. Reboot-required, timeout,
identity and proof failures remain explicit. Ordinary requests are never replayed.

Removal compares current values with the recorded original and issued values,
restores only owned names and preserves unrelated additions. A third value is
a conflict. When live capture is temporarily unavailable, removal can use the
independently checked PCI-instance/driver-key registry path; success still
requires refresh and fresh original image/INFO proof. The active pointer is
cleared only after verified removal; the historical receipt remains readable.
Verify is read-only and retries the recorded activation/removal direction.

Both skins provide Inspect, Apply, Verify and Remove. Activation requires a
changed Preview and unlocked controls. Saved recovery remains accessible after
relaunch even when ordinary GPU telemetry cannot open. Displaying receipt data
does not discard newly read relationship rows or invoke hardware operations.

## Validation

- Integrated Release build passes. The final full run passes **147/147 tests**
  in **46.75 seconds**. Twelve targeted weight suites also pass.
- The first full run exposed stack exhaustion in the existing monolithic client
  test. Its observed stack frame reserved 845,824 bytes before nested calls.
  Extracting its final scenario group into an existing-style non-inlined helper
  preserves all **884 checks**, which pass. No production stack limit or runtime
  behavior was changed for this test fix.
- Tests exercise actual CoreClient preparation, immutable Preview revalidation,
  bounded durable storage, alternate identities, per-name journaling, accepted
  failures, partial writes, removal conflicts, disconnected recovery, fresh
  proof/reboot/timeout outcomes, and both-skin ImGui activation guards. Host
  process timeout/restart outcomes use injected boundaries; they are not a
  physical activation claim. Three invalid-helper argument cases also exit
  through the actual EXE before opening a UI or performing an operation.
- All **23 live renderer phases pass** in the [report](2026-11-13-parity-twenty-fourth-smoke.json).
  Pause and default minimization produce zero frames and telemetry reads;
  Always refresh while minimized produces 113 frames and two reads. Four
  Direct3D resets are renderer resets. The relationship panel was inspected.
  The first smoke invocation omitted its required existing output directory;
  the correctly prepared invocation passes without a product change.
- The [read-only probe](2026-11-13-parity-twenty-fourth-weight-read.json) captures
  matching ROM reads and closes cleanly. This 3070 Ti still reports INFO
  **8632 / 0x58 / 0xE4**, outside the released decoder's **20512 / 0xCC / 0x104**.
  The nonzero probe exit records that unavailable capability. No live Preview,
  activation, removal, registry write, workload or PnP restart was attempted.
- Static GUI import audit passes within its documented limits; this does not
  establish Vista/7 runtime execution or physical controller behavior.

| Tested artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `0053E199105FF1AEABB7A1F3CB41BC7CB4C4B2688D08AAC79F5C0630B701D540` |
| `drutacore.dll` | `9C1033839A2F89A2A39BEA90CE6CFB8BC08F09A6310D6C45D6E7DEDF5CCD8A05` |
| `druta-ui-power-weights-read-check.exe` | `966981810162C6D4D79405CF5BDCD0D5FDCD1028D3F7F7F561FA6ECF33682B00` |

Local logs are under `native/build/parity-twenty-fourth-*`. The fresh source
[reconciliation](2026-11-13-released-parity-reconciliation.md) found three
remaining released workflows: disk-backed prewrite snapshots/profile browsing,
aggregate Full tune capture, and remembered nvtune/setup actions. These are
active parity work, not speculative improvements. The [backlog](../../../backlog.md)
has **4 active entries, 35 completed and 8 deferred**, including the final
reconciliation after those workflows. Overall parity remains open.
