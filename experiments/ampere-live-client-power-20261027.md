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
