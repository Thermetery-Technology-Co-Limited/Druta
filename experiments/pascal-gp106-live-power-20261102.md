# Pascal live client-power qualification: Gigabyte GTX 1060 6GB

On 2026-11-02 the local Gigabyte GTX 1060 6GB (GP106, PCI device `10DE:1C03`,
subsystem `1458:374A`, address `0000:01:00.0`) ran driver 576.80 and VBIOS
`86.06.4b.00.32`. Its reported ordinary power range was 90/180/200 W
minimum/rated/maximum, and the captured initial request was **180 W**. Every
trial restored that request and the complete captured client and native
definitions and controls. No driver update, reset or low-power transition was
requested, and no V/F hold was established: this board needs none, and the
Turing qualification board's P0 precondition is specific to that board.

## What was blocking live power here

The unmodified power getter supplied a paired A612 transport with **11740-byte
packet / 11672-byte parameter** geometry. That pair is in none of the four
`_CURRENT_LIMIT_LAYOUTS` entries, which describe current-policy *record*
layouts observed on 431.60, 581.42 and 621.01.

Druta previously refused to hand back the captured transport at all when its
parameter size was unrecognized, so live power (W) was withheld because the
current (A) record layout was unknown. These are separate questions: the
client-power group selects its own packet sizes by read-only INFO acceptance.
The transport is now validated as an envelope — a 17-word header of a known
command family whose packet length is its parameter length plus the 68-byte
prologue — and the record layout is selected separately. On this driver the
current sliders are still unavailable, which is reported as an unsupported
interface rather than a failed read.

The second gate was the fallback from the modern client group to the smaller
one, which was limited to the generations validated at the time (Turing and
Ampere). Pascal is added by this qualification. Which group a card speaks is
still decided by INFO acceptance, never by a device, board or driver version.

## Read-only mapping evidence

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
Native STATUS is not read for this layout, so detailed arbitration, the native
effective limit and original-BIOS slider coloring remain unavailable.

Role 1 uniquely bound client row 0 to native policy 2. The client row reported
type `0x57`, role 1 and milliwatts; the role and native definition reported
table version `0x30`, native type 0 and channel 0, with identical 90/180/200 W
bounds — the same values `nvidia-smi` reports for this adapter. Nine other
native policies were occupied and preserved, including the type `0x0B`
channel 11 core-current policy at index 13. These row indices and identities
are observations, not support allowlists. The live SET changes the existing FE
(`0xFE`) request; unknown fields, opaque tails and unrelated control bytes
remain preserved.

## Reversible idle trials

The [sanitized report](pascal-gp106-live-power-20261102.json) contains buffer
hashes, all occupied policy requests, sample telemetry and restoration results.
Raw buffers and durable undo records remain outside the repository and package.

| Board request | XOC required | Exact request readback | Native board status | Full restoration |
| ---: | --- | --- | --- | --- |
| 190 W | No | Verified | Unavailable | Verified |
| 201 W | Yes | Verified | Unavailable | Verified |
| 250 W | Yes | Verified | Unavailable | Verified |

Test points are derived from this adapter's reported bounds rather than fixed:
one inside the ordinary range, one 1 W above the reported maximum, and one
50 W above it. NVML's configured getter retained each requested board value
across four samples per trial, and the complete 180 W baseline was restored
and verified after each. An independent hidden 30-second watchdog with durable
undo state was armed for every trial; parent restoration succeeded, so the
timed watchdog did not intervene.

The native effective limit was explicitly unavailable throughout, reported as
`unvalidated current-policy transport geometry`. It did not disable the
ordinary slider, which is the behavior 1.9.05 specified for missing or failed
native reads. With XOC unticked the slider bound correctly carried over the
live extended request (90..201 W and 90..250 W) so the value could stay or
move back toward the ordinary envelope.

The desktop remained at P8 with low GPU utilization, about 8 W reported power
and 31 C edge temperature. No workload, voltage change, clock change, registry
write, driver restart or firmware flash was performed.

These trials establish request acceptance, exact readback and restoration on
this one adapter and driver. They do **not** establish physical enforcement
under load, behavior on other Pascal boards or other drivers, or that this
board draws more power at an extended request. Nothing here was measured with
a power meter. The inherited 1999 W software XOC envelope is a request
envelope, not a newly measured Pascal physical ceiling.

## Reproduction

Install the pinned requirements, then use read-only mode first:

```powershell
python experiments/validate_pascal_live_power.py --slot 0000:01:00.0 --output local-read.json
```

The explicit `--execute --private-dir C:\path\outside-repository` option runs
the idle trials with a restoration watchdog. Its idle bounds are local
qualification conditions, not product compatibility gates.
