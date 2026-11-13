# Nineteenth parity increment: optional CUDA timing capture

The released workload path traced in the [CUDA port map](2026-11-13-cuda-capture-port-map.md)
now has a native timing-capture implementation. The [local backlog](../../../backlog.md)
and [completion ledger](PARITY-COMPLETION.md) retain the unresolved work.

Capture uses already-qualified P0 first, then an allowed temporary owned hold,
then optional CUDA loading after verified hold cleanup. Locked controls permit
the workload without acquiring a tuning hold. Existing or foreign requests are
not adopted or released. CUDA resolves and checks the exact selected PCI BDF;
it never substitutes an ordinal or default device. The shared driver loader
retains the existing I2C warmup API and loads the installed system CUDA DLL.
No toolkit or static CUDA dependency is required.

The sibling `druta-ui-timing-cuda.exe` owns the context and copy buffers on one
thread. A private inherited mapping/event pair, explicit handle list, pinned
executable and parent-owned job bind the child. Capture happens while copies
are active; a qualified snapshot is published only after clean acknowledged
shutdown and a fresh paired-identity/helper check. Forced termination, malformed
messages, early exits and uncertain cleanup do not qualify timing writes.

One 60-second deadline covers workload and selected-helper waits. Parent
termination has a separate bounded cleanup grace; pending handles remain owned
until exit is confirmed. This is a process/wait bound, not preemption of a hung
parent driver or kernel call. Cancel, Pause and close signal outside the worker
queue. Resume starts no new workload. Explicit owned-resource cleanup remains
available with telemetry disconnected; unrelated tuning and new capture remain
blocked. A helper whose exit is unconfirmed retains the temporary hold.

Both skins expose capture, cancellation, pending-resource recovery and the
actual capture method. The separate read-only raw diagnostic inspector shows
captured words, clocks, offsets and P-state without staging fields or replacing
the qualified editor snapshot. P0/P2 qualification still requires the existing
stable current-adapter top band. This increment does not qualify a lower P2 band.

## Verification

- Release build passed. All **118 CTest suites passed in 41.76 seconds**.
  The later console-subsystem import-audit addition passed its focused suite
  (17 Python cases) and rebuilt audit/probe targets. Main UI and core binaries
  were unchanged by that addition.
- Tests exercise the real CoreClient routing with injected CUDA processes:
  direct P0 creates no child; locked/NVML-only capture issues no tuning SET;
  alternate target identities, below-band diagnostics, cleanup-time identity
  changes, early exit and cleanup retry preserve qualification boundaries.
  Session/view tests cover immediate cancellation, Pause, close, no resume
  restart and disconnected explicit cleanup.
- Real fake-child process tests cover clean stop, malformed frames, explicit
  driver-export errors, early exit, blocked copying, blocked cleanup and
  deadline/termination. Injected driver tests cover allocation/copy/free/context
  failures and owned-only cleanup. Existing I2C warmup tests pass.
- Main GUI and console worker PE header/import audits pass. The worker has no
  static CUDA import. The [worker inventory](2026-11-13-parity-nineteenth-cuda-imports.json)
  records the exact image and audit limits; it does not establish Vista/7 launch.
- All **22 live renderer phases pass** ([report](2026-11-13-parity-nineteenth-smoke.json)).
  Explicit Pause and default minimization produced zero frames and telemetry
  reads; Always refresh while minimized produced 114 frames and two reads.
  Classic Control and Timings captures were visually inspected. The four
  device resets were Direct3D renderer resets, not GPU-driver restarts.

## Scoped RTX 3070 Ti read-only run

Windows 10 x64, selected BDF `0000:01:00.0`, UUID
`GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`.
The installed nvtune helper SHA-256 was
`AF4E5FB9D975509F908046AC37F47D1CC752C397362A6AB6FB9AC232DCDA4E21`.

The [standalone load probe](2026-11-13-parity-nineteenth-cuda-load-read.json)
started the exact-target child, observed repeated 9427 MHz memory readings and
cleanly stopped/closed all owned resources in 985 ms. The first sampled P-state
was 8 and subsequent samples were 2; this probe verifies workload lifecycle,
not timing-band qualification.

The [integrated helper probe](2026-11-13-parity-nineteenth-cuda-capture-read.json)
disabled tuning holds. CUDA loading produced a stable P2 capture at 9427 MHz
before and after the helper reads. Raw registers were captured, but the current
top-band check failed. The command correctly returned unsuccessful qualification
and probe exit code 1, retained the raw diagnostic, published no qualified editor
snapshot and left no pending workload/hold or close recovery. It finished in
2125 ms. An earlier run gave the same P2/9427 MHz diagnostic-only result.

No timing SET, controller write, tuning hold, PnP restart or registry mutation
was issued in these probes or the renderer run. CUDA copies create GPU load;
these results are specific to this adapter and operating point. Successful
qualified CUDA captures are covered by injected integration tests, not claimed
as a new physical timing-write qualification on this board.

SHA-256 of tested Release artifacts:

| Artifact | SHA-256 |
|---|---|
| `druta-ui.exe` | `907594180341FCED694E0DFD9170A7F4E0375CBA9B1E87F05860829012E3BF4E` |
| `drutacore.dll` | `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715` |
| `druta-ui-timing-cuda.exe` | `50DA04D5597D82CFF2DE933DEDDE65855182BA669316F06705B4B6361AD97D91` |
| `druta-ui-timing-cuda-read-check.exe` | `3DC66D6F295B7D016A7AB50E96E2E0D6C0B1DAF2401FE7788C5A97F0C1714A99` |

The core DLL is unchanged. The final application was reopened visibly on the
selected adapter. Current-ROM/scaffold providers, persistent relationship-weight
transactions, unresolved firmware semantics and broader hardware/legacy-OS
qualification remain open; overall parity is not complete.
