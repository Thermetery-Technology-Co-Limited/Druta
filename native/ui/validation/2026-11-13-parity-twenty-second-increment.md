# Twenty-second parity increment: selected installed offsets

The released installed-offset workflow is now connected in both skins: signed
request edits, explicit Apply, independent reported policy, captured-initial
Restore, Undo/recovery, selected manual save/load and native/Python catalog
imports. Reference: Python 1.9.1 `fa6ee83`, `voltage_offsets_live.py`,
`voltage_offset_profiles.py`, `voltage_offset_scaffold.py` and
`vfe_equation_runtime.py`.

Installed heads use a separate singleton equation writer. They never enter the
public six-field rail request setter. Each operation re-recognizes the receipt,
current image, original chain and loaded heads; the capture retains the exact
equation packets used for recognition. Independent rail policies, boost and
opaque clock/rail request bytes come from the same shared provider used by the
ordinary voltage service. The provider's lower read helpers own bounded recovery;
the new composition adds no duplicate retry loop.

Only the selected constant's lower, upper and coefficient words change. Requests
must be exact signed int32/float32 microvolts. Readback compares complete loaded
definitions and retained requests. Reported policy deltas follow the release's
nonzero-boost REL/ALT exceptions: REL/effective/reported boost are derived, while
the selected ALT delta remains exact. Missing independent boost leaves REL/ALT
inspectable without allowing a write. Fresh values remain readable/saveable when
the writer is absent or recovery is pending; failed reads retain stale values.

Process-local receipts record issued selections before the first possible
effect. A partial or uncertain result exposes explicit recovery. Restoration
rebuilds a selected projection from fresh evidence and rejects conflicting
selected or unselected state; it never replays receipt packets. Previous Undo
survives no-op and guaranteed-nondispatch failures. Ordinary rail requests and
installed offsets have separate Undo histories and family routing.

Profiles contain selected typed values and target identity only. Loading stages
requests; catalog execution preflights and verifies the whole selected set.
Complete source validation happens before subsetting, including omitted fields.
Python scope `voltage_offsets` and native `druta-native-voltage-offsets` are
supported. Historical receipt values are not written, no scaffold is installed
or activated, and no ordinary setting is automatically reapplied after restart.

## Validation

- Integrated Release build passed. **134/134 CTest suites passed in 46.14 s**.
- New injected tests exercise shared retained evidence, the production capture
  factory, signed singleton writes, exact projection/readback, missing independent
  rails/boost/writer, alternate identities, transient retention, partial failure,
  journaling failure, foreign edits, initial Restore and chained Undo.
- Table-wrapper and actual CoreClient tests exercise explicit family routing,
  real manual profile file save, native/Python catalog preflight/Apply/grouped
  Undo and rejection of foreign identities without SET. Headless ImGui tests
  exercise expanded offset controls, locked/stale/recovery states, selection,
  staged Apply and generation retirement in the shared both-skin component.
- The [read-only live probe](2026-11-13-parity-twenty-second-voltage-read.json)
  reports no installed scaffold receipt on `0000:01:00.0`, UUID
  `GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. All six independent NVVDD request
  fields, 203 loaded definitions and two 128 us device delays remain available;
  close succeeds. This does **not** physically qualify signed offset writes on
  this card. Its previously recorded unsupported ROM selector layout remains
  outside both this port and the released Python parser.
- All **22 live renderer phases pass** in the
  [renderer report](2026-11-13-parity-twenty-second-smoke.json). Pause and default
  minimization produce zero frames and telemetry reads; Always refresh while
  minimized produces 115 frames and two reads. The voltage request page was
  visually inspected; expanded offset interactions use the injected ImGui test.
  Four renderer device resets are Direct3D resets, not GPU driver restarts.
- GUI and CUDA-worker import audits pass within their stated scope; they do not
  establish Vista/7 runtime support. The freshly built Classic app was left
  running for review.

No tuning/registry write, workload launch or PnP restart was requested during
this increment's live validation. The tests above use injected write transports.

| Tested artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `96D420115F4D9EAFC9E79CE092FA90FF096FB6E5C93FDDB95FE3B87CD24D30F7` |
| `drutacore.dll` | `9C1033839A2F89A2A39BEA90CE6CFB8BC08F09A6310D6C45D6E7DEDF5CCD8A05` |
| `druta-ui-voltage-read-check.exe` | `611BFD6C8AC81FFBC521CA931904A3701CACDE8A896C3DBC6138E505343766DB` |

The [backlog](../../../backlog.md) now has **9 active parity entries, 27
completed and 8 deferred**. The released persistent power relationship-weight
workflow and final reconciliation remain active. Broader research, theoretical
improvements and closed GitHub issues remain outside this session.
