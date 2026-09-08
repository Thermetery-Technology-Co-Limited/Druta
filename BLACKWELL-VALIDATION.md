# Blackwell validation after package organization

Validated on 2026-09-07 against PR19's organized source, including merged PR20
and the earlier Maxwell/Pascal fixes. This review started at `d3ffb89`.
The defects below include inherited tuning bugs exposed by the expanded
coverage; they were not all introduced by moving files.

## Hardware and entry state

NVIDIA GeForce RTX 5080, GB203 / Blackwell, PCI `0000:01:00.0`, device `0x2C02`,
subsystem `0x89DE1043`, VBIOS `98.03.3b.c0.6f`, driver `580.97`.
Core/memory/private offsets and voltage boost were zero, power was configured
to 360 W, both fans used automatic policy, and neither clock-lock mechanism
was held. The curve contained 127 GPU points with a 7.5 MHz graphics grid.

Independent rail getters agreed on the initial limits:

| Rail | Reliability | Alternate | Overvoltage | Minimum |
| --- | ---: | ---: | ---: | ---: |
| NVVDD | 1040 mV | 1060 mV | 1200 mV | 800 mV |
| MSVDD | 990 mV | 1060 mV | 1200 mV | 800 mV |

These measurements validate this board/firmware/driver combination. They do
not establish the same limits or behavior for every RTX 50-series board.

## Defects fixed

- Fixed-size graphics-clock buffers discarded the 389-entry performance rows
  returned by this driver. Only the 95-entry idle row remained, so a requested
  1500 MHz frequency lock snapped to 885 MHz. Bounded dynamic enumeration now
  retains every row, including its 3090 MHz maximum, and rejects malformed or
  truncated lists. Inputs and the hold banner also retain the exact dispatched
  range after snapping: 1501 MHz displays and reads back as 1500 MHz.
- Blackwell Core Apply used the private controls' 1 MHz granularity instead
  of the graphics clock's 7.5 MHz grid. It now displays the encoded graphics
  bin and remains stable on reapplication.
- This exact RTX 5080 combination truncates odd NVML memory-offset units toward
  zero. Apply now stages a representable request; the backend and profile
  preflight reject off-grid values before writing. Other adapters retain their
  existing precision rather than inheriting this measurement.
- Power profiles and widgets used enforced-limit telemetry as the saved
  setting. The configured getter immediately retained exact requests while
  enforced telemetry lagged. Profiles, Apply verification and the input widget
  now preserve the configured whole-milliwatt request, including fractional W.
  Profile summaries preserve the same fractional watt value.
- A successful V/F SET followed by failed GET could leave an unowned hold.
  Failed verification now rolls back the target from a fresh buffer; unresolved
  recovery remains owned and visible. Subsequent UI read failure retains
  ownership. Target-specific observation and release preserve another writer's
  hold on a different domain or a replacement request on the same domain.
- A failed timing save could leave a parseable partial file and appear valid;
  a failed preview could retain partial operations. Nonzero exits now remain
  failures. Unknown chip layouts keep raw captures but cannot authorize decoded
  timing Apply or Restore.
- Newer nvtune helpers commit by default, unlike the installed legacy helper.
  Preview discovers the convention through read-only help and uses explicit
  `--dry-run` when advertised. Legacy bare previews require an explicit promise
  that `--commit` is necessary. Unknown conventions refuse; a content hash
  invalidates cached help even after a same-size replacement retaining Windows
  timestamps.
- Profiles now mark each missing known rail or configured-power read incomplete
  and reject fractional milliwatts or unsupported memory increments before
  any profile writes.

## Controlled live checks

Each run verified the selected GPU's identity and saved the entry state.
Cleanup compared the full saved control profile and fan policy, every raw
boost-table row, and the original raw lock buffer where applicable.
A bounded CUDA checker repeatedly copied a seeded 64 MiB pattern through eight
device-to-device copies and compared the entire returned buffer on the CPU.
The completed runs had no comparison mismatches or CUDA errors.

| Area | Exercise and readback |
| --- | --- |
| Ordinary clocks and profiles | Two JSON capture/restore/replay cycles for core +15 MHz and memory +12/-12 reported MHz; manual fan 50% then Auto; power 360→350→360 W. |
| Memory granularity | Positive and negative half/whole-MHz probes established truncation toward zero; each restored. Final UI 12.5→12 MHz and -12.5→-12 MHz requests read back exactly. |
| Configured power | 350000, 349200, 349000 and 355000 mW read back exactly while enforced telemetry lagged. UI 349.2 W survived reapplication and JSON replay as 349200 mW. |
| Private controls | Two cycles each of XBAR, SYSCLK, VIDEO and additional MEM -20/+20 MHz, with unchanged other control rows and restoration after each. NVVDD +12.5→0 mV and voltage boost 10→0% read back. |
| Rail limits | Two cycles for each of four fields on each rail: ceilings decreased 5 mV, minimum increased 10 mV, then restored. Every untouched field and rail stayed exact. UI fractional reliability requests 1039.5/989.5 mV also survived Apply/reapply and restoration. |
| V/F and frequency locks | Two 900 mV hold/release cycles; one point lowered by one 7500 kHz delta, with only its expected raw word changed. Corrected 1500 MHz frequency lock read back as 1500/1500, then released. |
| Real UI callbacks | Physical core-bin display, memory snapping, fractional power/NVVDD/rail fields, bounded Max it at 900 mV with 875 mV ramp floor, Undo, owned Release and two-press Reset. Full baseline restored. |
| Timing reads | Idle P8/405 MHz and three loaded P1/14801 MHz raw captures. Final source suppressed cycles/ns for `UNKNOWN_1B3`. No timing plan or write was sent to this board. |

The completed ordinary, private-control, final UI and final raw-timing runs
recorded **1,169 full-buffer comparisons** (342 + 611 + 159 + 57), with zero
mismatches or CUDA errors. This count excludes exploratory and superseded
runs; it spans baseline, modified and restored controls rather than counting
only observations with a particular tune applied.

The bounded Max it ramp was a no-op on this curve; the separate single-point
test exercised V/F mutation. Undo retained the separately owned hold until
Release. Rail checks establish stored requests, not a physical voltage change
for every field: live idle readings varied while changing vmin and did not
establish exact tracking of the requested floor. Offset acceptance likewise
does not prove a corresponding throughput improvement.

## Scope and reproducibility

NVAPI reported memory type 16, without a confirmed command-clock divisor.
The app retains its raw label and does not invent nanosecond timings. The
installed nvtune reported `UNKNOWN_1B3`; timing writes remain unavailable, and
no generic P1 performance-band permission was added. No matching I2C regulator
was discovered, so no I2C writes were attempted. Independent MSVDD offset
support remains unconfirmed and its writer stays disabled by default.

These are short functional checks, not exhaustive VRAM capacity coverage,
performance benchmarks or proof of long-term overclock stability. Automatic
fan duty, live rail voltage and instantaneous boost/P-state can change while
the original requests and automatic fan policy remain restored.

Regression tests use fake drivers for failed reads/writes, recovery and
concurrent locks, oversized clock arrays, helper-version changes, unknown
layouts, incomplete profiles and precise input/replay. Packaging tests cover
source/frozen launch construction, relocated resources and wheel rebuilding
from an sdist. The distributable includes this report and matching source,
with per-file SHA256 hashes and its build commit in `SOURCE-MANIFEST.json`.
Frozen startup/exit checks are separate from the live source-UI write tests.

The final source pytest and unittest runs each passed **627 tests**. The final
live UI run's hashes match the completed runtime source. Independent readback
confirmed the original control profile, 360 W configured power, automatic fan
policies, full raw boost table and raw lock buffer, with no held clock or
pending recovery. Independent hardware-free reviews found no remaining
blocker in the organization, resource paths, entrypoints or changes above.

Local raw evidence, source hashes, scripts and screenshots are retained under
`build/blackwell-validation/` and intentionally excluded from distribution.
The earlier [Maxwell/Pascal validation](MAXWELL-PASCAL-VALIDATION.md) remains a
separate record for driver 472.12; those cards were removed for this pass.
