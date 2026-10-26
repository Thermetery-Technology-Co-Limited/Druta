# Live client-power validation: ASUS Strix RTX 3070 Ti

On 2026-10-27, the PCI-paired RTX 3070 Ti at `0000:01:00.0`, driver 595.97,
VBIOS 94.04.5a.00.56, reported a 100/310/380 W minimum/default/maximum.
The user reports that the board is shunt-modified. Software power values are
not calibrated measurements of its physical input power.

The 2340-byte INFO request used by the existing modern path returned RM
INVALID_ARGUMENT. An [independent published trace](https://github.com/reindertpelsma/kayfabe/blob/04773c348d3228940392bc206a64a4a1f364ffd6/docs/design/V3_CONSTANT_INVENTORY_20261011.md#L124)
reported a 1160-byte result for this command on an RTX 4070; it supplied a
read-only candidate, not a complete Ampere ABI. On this card, two 1160-byte
INFO reads and two 392-byte CONTROL reads matched exactly. The role-1 INFO
read, using 232 bytes, uniquely matched an occupied native policy. Druta checks
the adapter's own geometry, identities, units, bounds and restorable client
request; observed row indices are not compatibility rules.

At idle, an in-range client request changed 310 to 309 W and restored 310 W.
Three separate 381 W requests were accepted and read back; the final trial
reported NVML requested/enforced 381 W across nine samples over four seconds.
One earlier single enforced reading was 310 W; the reason was not established.
The native board status reported 392.906 W during the final trial. Those
fields have different meanings and do not prove actual physical power.

The driver recomputed eight peer request words when the board request changed.
The final production-backend test pinned one peer at 65.9 W, applied 381 W,
and verified that Druta re-applied the pin after the driver recalculated it.
The test then removed the test pin, restored 310 W, and verified the complete
client and native INFO/CONTROL buffers against an independently captured
baseline. A hidden watchdog with a durable baseline was armed during each
write; the parent restored successfully and the watchdog did not need to act.
After the standalone trials, independent NVML reads returned 310 W requested
and enforced with no nondefault policy requests.

This demonstrates driver acceptance and readback on one Ampere card. It does
not establish physical enforcement, all-Ampere or Ada write support, or the
behavior of other firmware and current limits. No registry override, BIOS
flash, workload, or voltage change was made. No Hydra or mVolt material was
used to derive this implementation.

## Peer policies and core current

On the same card, at 310 W versus 381 W, the following native policy requests
changed. These are driver values, not measurements of physical limits. Channel
numbers do not establish connector identity.

| Policy | Channel | Unit | 310 W request | Advertised maximum | 381 W request |
| --- | --- | --- | ---: | ---: | ---: |
| 5 | 4 | W | 66 | 78 | 78.173 |
| 6, 7 | 5, 6 | W | 150 each | 175 | 175.360 each |
| 9, 10 | 5, 6 | A | 22.750 each | 24.820 | 24.850 each |
| 13, core current | 19 | A | 243 | 270 | 270.389 |
| 16 | 18 | W | 85 | 92.800 | 92.912 |
| 17 | 18 | W | 66 | 72 | 72.086 |

The driver-derived values above the advertised maxima made the existing current
getter reject policy 13. At 381 W, a bounded 242 A native current request was
accepted and read back as effective; the existing named setter then accepted
241 A, and Druta re-applied that pin when 381 W was requested again. After the
fix, a second guarded test read the derived 270.389 A value and used the named
setter directly for 242 A and 241 A, with the same pin reapplication result.
Both runs restored the complete independent client and native INFO/CONTROL
baseline exactly. Tests were at P8 with reported power below 11 W, not under a
stress workload; the shunt modification means reported power is not calibrated.
Neither run establishes actual current enforcement or current-sensor accuracy.
New current requests remain limited to the policy's advertised maximum; these
tests do not establish a way to request a higher core-current ceiling.

## 999 W request and independent core-current probe

At P8 and approximately 11 W of uncalibrated reported power, a brief 999 W
board request was accepted and read back on the same card. The driver's core
current policy then reported a 508.755 A request and effective limit, compared
with the policy's advertised 270 A maximum. Peer policies 5, 6, 7, 9, 10, 16,
and 17 were also recalculated. This is driver readback at idle; it does not
establish actual board power or physical current-limit enforcement under load.

A native policy SET requesting 508.855 A, only 0.1 A above that derived value,
was rejected with RM 0x1F. A compensating native SET of the original 508.755 A
was also rejected, but the fresh policy read still reported 508.755 A. The
board request was then restored to 310 W and the complete client and native
INFO/CONTROL buffers matched the independent baseline exactly. An independent
eight-second watchdog was armed throughout; the parent restored successfully.
This single rejected request does not rule out every possible independent
current-control path. No voltage or workload change was made.

## Four-digit board request

Two brief P8 trials requested 1,999 W using the production setter with its
software envelope expanded **only inside the experimental process**. The
released application retains its 999 W XOC software ceiling. Both requests were
accepted and read back; the core-current policy reported 894.474 A as its
derived request and effective limit. In the second trial, NVML's enforced
reading was initially 310 W and became 1,999 W after approximately 0.6 seconds.
The native board-policy status instead changed from an initial 1,999 W to
1,012.893 W by approximately 0.32 seconds and stayed there for the remaining
samples through 1.18 seconds. These are distinct software readbacks: no physical
power or current enforcement was measured, and the source of the lower native
value is not established. The shunt-modified card reported approximately 11 W
at idle. All captured client and native INFO/CONTROL buffers were restored
exactly after each trial, with an independent eight-second watchdog armed.

A hardware-free Dear PyGui callback check with an experimentally widened slider
range preserved typed 999, 1,000 and 1,999 W, passed 1,999,000 mW to its mocked
Apply setter, and clamped 2,000 to 1,999. It did not visually inspect the
rendered field or change the released GUI's 999 W ceiling.
