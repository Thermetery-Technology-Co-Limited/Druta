# RTX 5080 fixed-build driver regression

This follows the [595.97 compatibility fix](blackwell-59597-native-compatibility-20261127.md)
on PR #100 head `2298568a04cfc2500ec88548688fba7231de5e62`. The same frozen
native binaries were exercised on all five drivers. No regression was observed
in the covered 600-series operations; both repaired 595.97 controls passed.

Host: RTX 5080 / GB203, Windows 11 Enterprise 10.0.22000 (customized image).
These observations qualify the stated adapter/configuration, not every board
or every control on the same GPU generation.

| Driver | Ordinary/profile | V/F edit + hold | 451 W idle | 1999 W idle | BIOS weight | NVVDD REL | GUI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 595.97 | 7/7 | Pass | Pass | Pass | Pass | Pass | 23/23 |
| 617.14 | 7/7 | Pass | Pass | Pass | Pass | Pass | 23/23 |
| 617.42 | 7/7 | Pass | Pass | Pass | Pass | Pass | 23/23 |
| 621.01 | 7/7 | Pass | Pass | Pass | Pass | Refused; restored | 23/23 |
| 621.40 | 7/7 | Pass | Pass | Pass | Pass | Refused; restored | 23/23 |

## What was verified

- 35 basic cases: detection, small core/memory offsets, ordinary board power,
  fan, boost, and manual memory-only profile application. Writes included
  request readback and Undo to captured state.
- Five V/F point edits and five hold/release cases. A brief CUDA device-copy
  workload ran only at the original 360 W board request and was stopped before
  the separate elevated-power cases. This did not qualify extra clock domains
  through P0 operating-counter response.
- Ten XOC round trips: 451 W and 1999 W on each driver. The helper required
  P8 and at most 2% reported GPU utilization before and immediately before SET.
  Every recorded before/ready/after/restored/final sample met those conditions.
  Both requests echoed exactly; Undo restored 360 W and XOC was disabled.
- Five production BIOS weight Apply/Remove transactions: raw relationship 0
  weight 4096 -> 4097, native helper GPU-driver refresh, independent image and
  decoded INFO readback, then complete original-image restoration. Other rows,
  policy metadata, and exposed source/link/range references matched throughout.
- 115 native GUI smoke phases, 23 per driver, with captured bitmaps.
- A -6.25 mV NVVDD REL request worked through 617.42. On 621.01/621.40 the
  driver again reported `[0, 0, 0, 0]` instead of `[-6250, 0, 0, 0]`. The
  application reported failure and original state was verified. Those are
  unchanged baseline refusals, not successful writes.

These checks establish software request/readback and image restoration, not
physical power enforcement, voltage response or performance. No workload ran
with an elevated board-power request. Original 621.01, 360 W and firmware bytes
were restored; Windows boot time was unchanged. No NVIDIA App was installed.

## Build and source provenance

The driver matrix used the local Blackwell patch over the head above, before
the subsequent UI-only timing disclaimers. The timing UI was then rebuilt,
visually checked in Classic and Focused, and passed a further 23-phase
read-only smoke run with zero completed worker actions. The disclaimer changes
do not alter tuning capability or dispatch.

The targeted native protocol suite passed 315 tests, including 11 new
legacy32/V40 cases. The relationship suite passed 459 checks; its normal CTest
target also passed. Actual captured-data replay qualified 14 editable
relationships with exact projected INFO/ROM verification.

This was an exploratory GCC 16.1.0/CMake 4.3.2 build, not MSVC shipping-build or
Windows XP qualification. External build configuration supplied `<cstdint>`
and wide-entry linking and disabled WERROR and the core XP import audit. The
earlier broad offline run retained four exceptions: three GCC `wmain` test
entry-point failures (external test-only `return 0` copies passed 327 checks)
and a logon test that could not create its private registry fixture hive.
Those original failures were retained and are not erased by this driver run.

Core DLL SHA-256:
`0d886b90d6ed4e71ce7e40d20c25e71734a3f8522f3b01a6388afd0ae093e461`

Original firmware image SHA-256:
`80d7d77fd21d19af6a3008e4c7386a06c26b6d79c17df34c7d5c17d691bad202`

One-weight active image SHA-256:
`5fac3a2ef1281d79587471871525f1d7d785c529edd3fe6505d0fa2fbbb71e83`

The [machine-readable summary](blackwell-driver-regression-20261128.json) and
[CSV](blackwell-driver-regression-20261128.csv) preserve the audited results.
Raw per-case logs, bitmap captures, signed-package hashes, bind records,
before/active/restored weight receipts, build/source/tool manifests, and the
read-only audit are retained locally in
`Druta-pr100-evidence-20261127/rerun595-fix/`. Driver installers, firmware images,
and build output are not included in this source change.

## Remaining qualification

The combined branch still needs the affected-control rerun on the Pascal and
Ampere host noted in PR #100. Max It, individual extra clock domains,
independent current/named-power policy writes, other voltage fields, low-power
writes, timing/I2C writes and legacy Windows are outside this Blackwell run.
