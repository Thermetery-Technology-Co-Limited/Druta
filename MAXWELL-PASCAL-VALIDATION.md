# Maxwell and Pascal validation after package organization

Validated on 2026-09-07 against PR19's organized source, including merged PR20
and the fixes carried from PR21. The fresh review started at `4dc05a3` and fixed
the issues below. Several were inherited tuning defects exposed by the expanded
coverage, rather than defects introduced by moving files.

## Hardware

| GPU | Chip / memory | PCI slot | VBIOS | Driver |
| --- | --- | --- | --- | --- |
| NVIDIA TITAN Xp | GP102 / GDDR5X | 0000:01:00.0 | 86.02.3d.00.01 | 472.12 |
| NVIDIA GeForce GTX 745 | GM107 / DDR3 | 0000:02:00.0 | 82.07.32.00.6a | 472.12 |

Both started with zero core/memory offsets and automatic fans. TITAN Xp's power
limit was 250 W, voltage boost and NVVDD offset were zero, and its absolute rail
limits were 1068.75 / 1093.75 / 1200 / 650 mV (reliability / alternate /
overvoltage / minimum). Neither card had an active clock hold. No matching I2C
regulator was discovered on either installed board.

## Defects fixed

- Timing Apply could bypass Unlock and use an old performance capture after the
  card returned to idle. It now checks permission and live offset-normalized
  P0/P2 state again immediately before committing.
- The generic second-highest-memory-clock rule classified GTX 745's idle
  405 MHz state as a performance band. It now requires the 900 MHz band on this
  board, while preserving measured GP102/TU102 nearby P2/P0 behavior. Unknown
  clocks, offsets and states remain unverified, including in the capture producer.
- Nonzero helper exits and missing timing readbacks could be called hardware
  rejection. They now remain failed/unconfirmed, retaining actual partial
  readback and error details.
- Pascal V/F frequency units could change when a curve was flattened far below
  stock, and a sufficiently high Turing curve could be wrongly halved. The
  measured Pascal and Turing formats now retain their units independently of
  the current tune and clock-list availability.
- Core Apply could display a rounded 13 MHz-grid request while the driver used
  Pascal's 12.657 MHz or Maxwell's 13.052 MHz grid. It now displays the encoded
  physical bin, stable when reapplied.
- Callbacks already queued for an outgoing GPU could run against a newly built
  card panel. The dispatcher now discards the rest of a batch after a card
  generation change or shutdown.
- Reset could claim success after supported private-control read failures, and
  V/F release could treat failed readback as unlocked. Failures now retain their
  named results; the UI refreshes actual readable controls and retains an
  unconfirmed hold indicator.
- Voltage widgets truncated fractional NVVDD/I2C offsets and rounded fractional
  rail limits. Readback, input mirroring, range carryover and Apply now preserve
  the relevant driver or controller precision. A retained -100.25 mV NVVDD
  request also remains exact after disabling XOC; the backend still refuses
  movement farther outside the normal envelope.

## Controlled live checks

Each test captured its own original state, targeted one verified GPU identity,
and restored the original state in cleanup. A CUDA load repeatedly compared a
seeded 64 MiB pattern copied back to the CPU after eight device-to-device copies.
No comparison mismatch or CUDA error occurred in the completed live checks.

| Area | Exercise and readback |
| --- | --- |
| Clocks and profiles | Two JSON capture/restore/replay cycles per card; core offsets, +12.5 and -12.5 true-MHz memory offsets; manual fan 50% then Auto; Pascal power 250→240→250 W. |
| Maxwell P0 | Three direct hold/release cycles at P0/memory 900 MHz; manual fan 100%; profile restore retained the separately owned P0 hold and restored Auto. |
| Pascal private controls | Two cycles of Additional Memory Clock Offset +25→0 MHz, NVVDD +12.5→0 mV, voltage boost 10→0%, and four rail fields moved by 12.5 mV then restored. |
| Pascal curve and locks | One 900 mV GPU point lowered by one physical bin; exactly its delta word changed. Two point-lock/release cycles. All 84 raw boost-table rows, including four memory rows, and the original raw lock buffer were restored. |
| Pascal idle minimum | Two stored-vmin 650→700→650 mV cycles. Driver-reported physical idle voltage moved 687.5→700→687.5 mV. The initial physical reading was not 650 mV. |
| Timing writes | FAW 24→25→24 on Pascal and 38→39→38 on Maxwell, twice each. Both writes landed and held across three loaded observations per cycle. One inverse field write and one full snapshot restore per card restored every captured word. |
| Timing scope | Pascal: broadcast plus six FBPA partitions, 49 captured words. Maxwell: broadcast plus two FBPA partitions, 21 words. Only the expected CONFIG3 FAW bits changed; all unrelated words stayed exact. Idle captures on both cards were correctly ineligible. |
| Real UI callbacks | Fractional memory and NVVDD Apply/reapply, 1068.75 mV limit reapply, physical core-bin display, bounded Pascal Max it at 900 mV, Undo/release, Maxwell P0-plus-fan/Undo/release/reset, and repeated card switches. |

The Max it test used a 900 mV cap and 875 mV ramp floor. Its ramp was a valid
no-op on this curve; the separate single-point test exercised actual V/F
mutation. Undo intentionally retained the independently owned hold until
Release. Reset's first press arms it and its second press performs the reset.
On GTX 745, Reset restored the available controls and released the owned P0
hold; unsupported power, NVML clock-reset and voltage-control operations were
reported as failed steps rather than as a completely successful reset.

The completed ordinary-control, P0, private-control, timing and final UI runs
recorded 1,067 full-buffer comparisons with zero mismatches. The timing portion
contributed 270 (211 Pascal, 59 Maxwell). These are short functional
checks, not exhaustive VRAM capacity coverage, performance benchmarks, or proof
of long-term overclock stability. Timing register acceptance alone does not
establish a physical latency improvement. Structural and inferred fields,
CL/WL/preamble sweeps, and unrecognized I2C writes were not exercised.
The timing checker ran across baseline, modified and restored states; its
comparison count is not a count exclusively taken with modified timings. It
used the production writer with a fresh load/temperature/band guard; the actual
timing button's locked/stale-state refusal paths were verified with fake-driver
regressions. Automatic fan duty and instantaneous boost clocks may vary after
restoring the saved requests and fan policy.

## Regression and artifact verification

Tracked fake-driver regressions cover the failure paths above, including stale
and forced timing writes, partial helper failures, missing state, curve unit
changes, queued GPU-switch callbacks, failed reset reads, exact fractional
voltage requests and reapplication. The complete pytest suite also covers
package entrypoints, source/frozen launch construction, resources, profile paths,
wheel contents and rebuilding a wheel from the source distribution.
The final source suite passed 544 tests and 502 subtests under pytest; unittest
discovery independently passed the same 544 tests. The final real-UI run's
recorded hashes match all runtime Python files in the completed source tree.

The final Windows bundle is built with `build.ps1`. Its
`source/SOURCE-MANIFEST.json` records the source commit, cleanliness, every
included source hash and the EXE hash. Local validation evidence is under
`build/maxwell-pascal-validation/`, including identity/baseline snapshots, live
readbacks, source hashes, regression output, UI frame captures and the final
bundle/process audit. Source callback tests and frozen EXE startup tests are
reported separately; a responsive window alone is not a tuning test.
