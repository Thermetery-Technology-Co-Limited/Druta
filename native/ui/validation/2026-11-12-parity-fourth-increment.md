# Clock ownership, timing batches and PERF diagnostics

Continuation after `3e1c0e5`, using Python release `fa6ee83` and the full
[completion ledger](PARITY-COMPLETION.md). The two Sol workers implemented
frequency ownership and the timing editor/profile workflow. Terra handled PERF,
Session ownership, backend regressions and bounded review fixes. Root integrated
and reviewed the paths. This checkpoint does not complete the full parity goal.

## Implemented

- Both skins expose a Clocks menu and shared clock-request window. NVML range
  locking uses architecture, actual export presence, current-adapter lists,
  top-memory bounds, snapped requests and independent accepted/current readback.
  Legacy P0 is probed independently and uses the existing verified helper plus
  typed fan capture/Undo. No request is persisted or reapplied automatically.
- Each clock owner retains its cleanup responsibility after uncertain writes.
  Request validation precedes release of another owned control. A changed foreign
  range is not blindly reset. I2C invokes handover only at a qualified clock
  acquisition; read-only NCT/iCX verification, stale selection and gate-off
  attempts do not release a frequency request. Session publishes ownership even
  if ordinary readback throws, blocks switching while ownership is unresolved,
  and does not add maintenance polling for a static frequency lock.
- Legacy P0's backend no longer drops ownership solely because ForcePstate
  returned failure: an accepted request can still report an error. Explicit
  release remains retryable after a failed release. This intentionally fixes
  the native failure behavior instead of copying the old Python bug.
- Timing fields can be staged together and previewed as a complete selection.
  Preflight checks every helper preview, target, operating band and register
  binding before writes. Each selected-field write verifies preserved bits and
  final selected values. Partial failure and transport exceptions retain only
  dispatched or possibly dispatched fields for Undo. Restore refuses a selected
  value changed outside the recorded attempt and preserves unrelated current
  bits. First-qualified captured-initial restore is separate from Undo and is
  never described as factory state.
- Timing profiles save understood captured fields with explicit staged overrides.
  Load validates target, band and field geometry and only stages local edits.
  File selection checks adapter/helper continuity. Preview and Apply remain
  separate actions. Helper process failure and malformed output are distinct.
- Advanced PERF diagnostics explicitly read `limits`, `cf_controller` and
  `cf_policy`. Each group has independent availability, bounded read-only retry,
  target validation and retained stale data. Unknown values remain raw. Page
  rendering and ordinary polling never trigger these reads. Power arbitration
  orchestration remains open.

## Validation and limits

Release build and bounded PE/import audit passed. All **46 CTest suites** passed,
including 852 CoreClient checks, 242 Session checks, 1,071 UI checks, 261 timing
batch checks and the new frequency ownership/view and PERF model/view suites.
All **51 native legacy-P0 Python tests** passed against this DLL. The combined
client regression test initially exceeded its default stack reserve: its main
frame held 881,776 bytes of accumulated fixtures and snapshots. Splitting it
into bounded non-inlined test groups fixed this without raising the stack limit.

Mock-backed tests exercise alternate identities, partial capabilities, transient
reads, accepted-but-failed writes, foreign replacements, failed release,
same-register timing composition, partial launch/transport exceptions, helper
changes, selected-value conflicts and preservation of unknown bits.

Built-in read-only renderer captures were inspected on Windows 10 x64 / RTX
3070 Ti: Classic Advanced at 2100x1400 and Focused Advanced at 1800x1200 show
the shared Clocks menu and independent PERF panel. Captures are local under
`native/build/ui-integrated/parity-fourth/`. Production ImGui interaction tests
exercise intents; native profile dialogs and physical timing writes were not
tested in this checkpoint. The fresh EXE is left running for review.

A direct read-only call through the existing Python interposer to the same C ABI
on PCI slot `0000:01:00.0` returned architecture 7: `limits` ready with 224 entries,
`cf_controller` ready with 8 entries, and `cf_policy` unsupported
(`PERF GET 0x2080A0CC rejected (RM 0x1F)`). Each completed in one attempt. This
records current-adapter evidence, not a compatibility gate for other boards.

SHA-256:

- `druta-ui.exe`: `73B8F774BE2EC2C4DECD600681EA4AFB330BA6227B6816F7B3973EC13C354494`
- `drutacore.dll`: `0CB6005074D3266198B5A41BEE3D28900F0EF4953ACFF984BB9E32F8C7B0043D`

This increment issues no live tuning write or GPU-driver restart. It does not
qualify physical legacy P0 on this Ampere adapter, timing-register writes,
other I2C controllers or Vista/7 execution. Existing physical evidence keeps its
original adapter and operating-point scope. Cross-process writers still prevent
these separate API operations from being an atomic hardware transaction.
