# Twenty-fifth parity increment: profile capture and helper setup

This increment restores released Python 1.9.1 profile-library, Full tune capture
and nvtune setup workflows in both skins. The source reference is `fa6ee83`:
`profiles.capture`, `profiles.capture_rails`, `profiles.list_profiles`, and the
frontend's profile, helper-registration and test-signing actions.

The catalog lists native and existing Python/portable profiles newest first,
with saved time, contents and explicit parse errors. Browsing creates no
directories and applies nothing. Named saves capture current configured requests
through the existing worker paths; reading and staging a saved entry still
require explicit preflight and Apply. Strict native documents remain unchanged,
using file time for display/sort when embedded metadata is absent.

Full tune capture writes the released unscoped schema-2 format. It reads fresh
identity, indexed V/F deltas, memory/core requests, requested power and pins,
current limits, per-fan policies, boost, rail requests, domain/voltage offsets,
display labels and the selected supported controller. Captured initial/requested
values are distinct from observations. Owned temporary rail headroom is removed
only where current values still match that owner's written value. Missing known
reads produce an incomplete document, never a fabricated default or a claim of
complete restoration. Required-XOC metadata includes already-live wider requests;
capture never enables a mode.

I2C capture reuses the canonical released identity/hash and typed NCT3933,
uP9512R, NCP4206, MP2888 and MP29816 formats. It preserves manual-PMBus ownership
as session-only and omits iCX voltage from aggregate capture; iCX keeps its
separate settings preset. Named iCX Save freshly prepares settings and binds the
subsequent file write to that selection and preparation revision.

Helper Locate persists the path only after a matching successful worker result.
Forget respects live timing Undo/capture ownership, clears registration and
performs read-only fallback discovery. Rejected/stale actions and failed config
writes preserve the registration and report the error. The test-signing dialog
provides the released copyable recipes, status, explicit enable/disable actions,
per-command results and a separate reboot action. No boot changes are automatic.

The library includes atomic named writes and collision-free, 20-entry retention
for its own autosaves. **Automatic prewrite producers and newest-history Undo
remain open**; storage and browsing alone do not complete that backlog item.
The [remaining history map](2026-11-13-history-parity-map.md) records released
per-action failure and no-op behavior for the next implementation.

## Validation

The integrated Release build passes. All **155/155 tests pass** in 47.19 seconds.
Coverage includes current Full tune and per-family I2C capture, library discovery/
ordering/retention, named capture selection, registration/config preservation,
worker-result attribution and injected signing/partial-failure/reboot boundaries.
The first full run found two test assumptions: catalog opening now requests a
filesystem refresh, and ImGui activation must be scheduled before `NewFrame`.
Their corrected checks retain the no-apply and explicit-reboot assertions.

All **23 live renderer phases pass** in the [report](2026-11-13-parity-twenty-fifth-smoke.json).
Pause and default minimization produce zero frames and telemetry reads; Always
refresh while minimized produces 115 frames and two reads. Four Direct3D resets
are renderer resets. Both catalog skins were visually inspected. The smoke harness
now permits exactly one successful, read-only helper-path selection after entering
Timings; it still rejects capture/write actions. The previous zero-worker-actions
assertion rejected that newly restored discovery behavior. No helper was executed
for a timing capture in this renderer run.

The [read-only 3070 Ti capture](2026-11-13-parity-twenty-fifth-capture.json)
matches the exact BDF/UUID before and after saving, parses as Full tune with
16 semantic targets and an empty incomplete list, and closes without a tuning
write. No I2C controller was selected by this probe. Controller-specific capture
coverage is injected; this run does not qualify physical controller behavior.

The static GUI import audit passes within its documented limits. This is not
Vista/7 runtime qualification. Boot/signing/reboot outcomes are tested through
injected boundaries; no machine boot configuration was changed.

| Tested artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `9BF9BFCF421DDD9023A7A6783A21053668A1A32CDA182728A6F66674CAE99409` |
| `drutacore.dll` | `9C1033839A2F89A2A39BEA90CE6CFB8BC08F09A6310D6C45D6E7DEDF5CCD8A05` |
| `druta-ui-full-profile-capture-probe.exe` | `B8DB21D5E0E4B109C97006EDA873C6536784BE013C60C93C6945A2D49ED99FF3` |

Local build, test and import logs are under `native/build/ui-integrated/parity25-*`.
The [backlog](../../../backlog.md) now has **2 active entries, 37 completed and
8 deferred**. History integration and final acceptance remain active; this
checkpoint does not declare overall parity complete.
