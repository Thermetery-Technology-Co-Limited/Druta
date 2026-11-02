# Pascal live power and current qualification: Gigabyte GTX 1060 6GB

On 2026-11-02 the local Gigabyte GTX 1060 6GB (GP106, PCI device `10DE:1C03`,
subsystem `1458:374A`, address `0000:01:00.0`) ran driver 576.80 and VBIOS
`86.06.4b.00.32`. Its reported ordinary power range was 90/180/200 W
minimum/rated/maximum, and the captured initial request was **180 W**. Core
current policy 13 reported 1/86.8/95 A. Every trial restored its captured
value and the complete captured client and native definitions and controls.
No driver update, reset or low-power transition was requested, and no V/F hold
was established: this board needs none, and the Turing qualification board's
P0 precondition is specific to that board.

## What was blocking live power here

The unmodified power getter supplied a paired A612 transport with **11740-byte
packet / 11672-byte parameter** geometry. That pair is in none of the four
pre-existing `_CURRENT_LIMIT_LAYOUTS` entries, which describe current-policy
*record* layouts observed on 431.60, 581.42 and 621.01.

Druta previously refused to hand back the captured transport at all when its
parameter size was unrecognized, so live power (W) was withheld because the
current (A) record layout was unknown. These are separate questions: the
client-power group selects its own packet sizes by read-only INFO acceptance.
The transport is now validated as an envelope — a 17-word header of a known
command family whose packet length is its parameter length plus the 68-byte
prologue — and the record layout is selected separately.

The second gate was the fallback from the modern client group to the smaller
one, which was limited to the generations validated at the time (Turing and
Ampere). Pascal is added by this qualification. Which group a card speaks is
still decided by INFO acceptance, never by a device, board or driver version.

## Read-only mapping evidence: client power

Two identical reads of each accepted response established the local geometry.
Larger and smaller candidates returned RM `0x1F` (INVALID_ARGUMENT): client
INFO 2340, 1152, 1036, 1028, 900 and 644 bytes, and native INFO 8632 bytes.
Accepted packets were:

| RM command | Meaning | Parameter bytes | Row mapping |
| --- | --- | ---: | --- |
| `2080A630` | Client INFO | 1160 | `8 + 36*i`, 32-bit mask at +4 |
| `2080A632` / `2080E633` | Client CONTROL GET / SET | 392 | `8 + 12*i`, request at row+4, client at row+8 |
| `2080A61C` | Role INFO | 232 | Role selector 1; 228-byte definition at +4 |
| `2080A618` | Native INFO | 8620 | `88 + 228*i`, 32-bit mask at +4 |
| `2080A61A` | Native CONTROL GET | 4432 | `20 + 124*i`, request at row+4, mask at +16 |

This is the same small client group and 124-byte native control layout already
qualified on Turing 581.42, reached here through the same acceptance probe.

Role 1 uniquely bound client row 0 to native policy 2. The client row reported
type `0x57`, role 1 and milliwatts; the role and native definition reported
table version `0x30`, native type 0 and channel 0, with identical 90/180/200 W
bounds — the same values `nvidia-smi` reports for this adapter. Nine other
native policies were occupied and preserved. These row indices and identities
are observations, not support allowlists. The live SET changes the existing FE
(`0xFE`) request; unknown fields, opaque tails and unrelated control bytes
remain preserved.

## The current-policy STATUS block is NOT the Turing one

Sharing the info and control blocks with the 11748/11680 transport makes it
tempting to carry its status block over too. That is wrong here, and the
driver says so:

| A619 STATUS parameter bytes | Result |
| ---: | --- |
| **171048** | **accepted** |
| 172080 (the 11748/11680 entry) | refused, RM `0x1F` |
| 190272 | refused, RM `0x1F` |
| 396024 | refused, RM `0x1F` |
| 397048 | refused, RM `0x1F` |

171048 was the only size accepted across a 140000..210000 sweep at 4-byte
steps (17500 requests); every other size in that band returned RM `0x1F`.

The record geometry was then **solved from live data**, not carried over. With
the mask placed at word 1 the driver populates the block; searching every
`(base, stride)` with base 0..2048 and stride up to 12000 at 4-byte steps,
exactly one pair makes all ten occupied policies' status type bytes equal
their info type bytes **and** puts every effective value inside that policy's
own info bounds:

| | 11748/11680 (Turing 581.42) | 11740/11672 (this adapter) |
| --- | ---: | ---: |
| STATUS parameter bytes | 172080 | **171048** |
| STATUS header | `0x70` (112) | **`0x68` (104)** |
| STATUS record stride | `0x1454` (5204) | **`0x1434` (5172)** |
| INFO bytes / header / stride | 8620 / `0x58` / `0xE4` | same |
| CONTROL bytes / header / stride | 4432 / `0x14` / `0x7C` | same |

Two of the three blocks matching is not evidence that the third does. The
decoded table is self-consistent: effective equals requested on every policy
that is not derived, and live values are small and plausible.

| Policy | Type / channel / unit | min / default / max | Requested | Effective |
| ---: | --- | ---: | ---: | ---: |
| 0 | `0x0B` / 1 / mW | 1 / 5001000 / 5001000 | 5001000 | 174424 |
| 1 | `0x03` / 7 / mW | 1 / 1001000 / 1001000 | 1001000 | 1001000 |
| 2 | `0x00` / 0 / mW | 90000 / 180000 / 200000 | 180000 | 185625 |
| 3 | `0x03` / 3 / mW | 1000 / 66000 / 78000 | 66000 | 66000 |
| 4 | `0x03` / 4 / mW | 1000 / 162000 / 175000 | 162000 | 162000 |
| 7 | `0x04` / 4 / mA | 1000 / 27000 / 34000 | 27000 | 27000 |
| 10 | `0x04` / 3 / mA | 1000 / 10000 / 13000 | 10000 | 10000 |
| 13 | `0x0B` / 11 / mA | 1000 / 86800 / 95000 | 86800 | 86800 |
| 14 | `0x0B` / 1 / mW | 1 / 5001000 / 5001000 | 5001000 | 5001000 |
| 15 | `0x0B` / 1 / mW | 1 / 5001000 / 5001000 | 5001000 | 5001000 |

Policy 13 matches the type `0x0B` / channel 11 descriptor the build already
expected for Pascal core current, which was derived on other Pascal hardware.

While the board request was temporarily raised to 699 W during this session,
policies 2, 3, 4, 7 and 13 all read back *derived* requests above their own
advertised maxima, and returned to the values above once the board request was
restored. That is the v30 coupled-peer recalculation the Turing work recorded,
observed here independently.

## Reversible idle trials: power

The [sanitized report](pascal-gp106-live-power-20261102.json) contains buffer
hashes, all occupied policy requests, sample telemetry and restoration results.
Raw buffers and durable undo records remain outside the repository and package.

| Board request | XOC required | Exact request readback | Native effective | Full restoration |
| ---: | --- | --- | ---: | --- |
| 190 W | No | Verified | 185.625 W baseline | Verified |
| 201 W | Yes | Verified | read per sample | Verified |
| 250 W | Yes | Verified | read per sample | Verified |

Test points are derived from this adapter's reported bounds rather than fixed:
one inside the ordinary range, one 1 W above the reported maximum, and one
50 W above it. NVML's configured getter retained each requested board value
across four samples per trial, and the complete 180 W baseline was restored
and verified after each. An independent hidden 30-second watchdog with durable
undo state was armed for every trial; parent restoration succeeded, so the
timed watchdog did not intervene.

With the record layout now identified, the native effective power limit reads
**185.625 W** against a configured 180 W, rather than being unavailable. That
is a software status field, not a measurement of board input power.

## Reversible idle trial: core current

| Step | Result |
| --- | --- |
| Captured baseline | 86.8 A requested, 86.8 A effective |
| Request 86 A | accepted, "effective limit verified", exact readback |
| Request 95.001 A (above the reported maximum) | refused, no state change |
| Restore 86.8 A | accepted, effective limit verified, matches baseline |

This exercises the `2080E61B` SET at 4432 bytes and the stored-and-effective
verification `set_current_limit_ma` promises, which is why the STATUS block
had to be right rather than merely present.

## Scope

The desktop remained idle with low GPU utilization, about 8 W reported power
and 25-31 C edge temperature. No workload, voltage change, clock change,
registry write, driver restart or firmware flash was performed.

These trials establish request acceptance, exact readback and restoration on
this one adapter and driver. They do **not** establish physical enforcement
under load, behavior on other Pascal boards or other drivers, or that this
board draws more power at an extended request. Nothing here was measured with
a power meter. The inherited 1999 W software XOC envelope is a request
envelope, not a newly measured Pascal physical ceiling, and the core-current
range remains bounded by the adapter's own reported 95 A maximum.

## Reproduction

Install the pinned requirements, then use read-only mode first:

```powershell
python experiments/validate_pascal_live_power.py --slot 0000:01:00.0 --output local-read.json
```

The explicit `--execute --private-dir C:\path\outside-repository` option runs
the idle trials with a restoration watchdog. Its idle bounds are local
qualification conditions, not product compatibility gates.
