# Live power-policy upper adjustment ratio

On the tested RTX 5080 / driver 617.42 / VBIOS 98.03.3b.c0.6f, changing
policy 2's active upper ratio from 4219 to 4178 changed its reported effective
limit from 370.811 W to 367.207 W. All three changed-state samples agreed.
Restoring 4219 returned the effective limit to 370.811 W. No workload,
registry change, driver restart, or persistent setting was involved.

| Observation | Active upper word | Board request | Policy 2 request | PMU output | Effective limit |
|---|---:|---:|---:|---:|---:|
| Before | 4219 | 360 W | 450 W | 360 W | 370.811 W |
| Changed, three samples | 4178 | 360 W | 450 W | 360 W | 367.207 W |
| Restored | 4219 | 360 W | 450 W | 360 W | 370.811 W |

These observations match the candidate relation, with all power values in mW:

```text
effective = floor((PMU_output * active_upper_word + 2048) / 4096)
```

This is an intervention on the coefficient, extending the earlier passive
fit. It establishes an effect on the returned software limit at this operating
point. It does not establish physical consumption, the complete feedback
algorithm, its rounding at ties, or that the upper ratio is a permanent gain
under load. NVIDIA's public integral-controller definitions describe bounds
relative to the current policy limit in UFXP4.12; the observations are
consistent with operation at the upper bound.

## Field and transaction evidence

The saved original ROM's row +9/+11 contains the two u16 values 3072/4219.
The traced row schema and unpacker map these to descriptor +0x4c/+0x4e.
The constructor copies the default block to object +0x48 and initializes a
separately packed active block at +0x52. INFO exposes default ratios at row
+0x24/+0x26; CONTROL exposes active ratios at row +0x0c/+0x0e. The mode and
sample-count byte packing differs between these blocks and must be preserved.
Both blocks are separately serialized for the PMU.

The corresponding CONTROL getter/setter and serializer were traced in the
saved 617.42 host binary as well as 621.01. The setter also processes the
selected policy's ordinary request before copying its active block; this
trial preserved that request exactly. Binary addresses and local values are
research provenance, not product compatibility gates.

The trial used the existing negotiated native policy transport with one
occupied policy selected. Relative to each fresh getter buffer, both SETs
changed only the selection mask and policy 2's upper halfword. Exact full
native/client request-state comparison showed only the intended halfword
change during the trial, unchanged INFO/default definitions, and exact
restoration afterward. Root and a separate offline audit checked the packets.

Dynamic STATUS did not remain byte-identical: measured values varied, and
policy 0's effective limit also varied. This trial does not isolate the cause
of those differences. All 18 policies' effective observations are retained in
the [compact receipt](power-policy-integral-upper-5080-61742-20261105.json).

## Product boundary

This initial trial identified and locally qualified the live upper-adjustment
control path. The subsequent [production qualification](power-integral-production-20261105.json)
also exercised the new backend at 4178 and 4260, below and above the reported
default, with exact restoration; the manual control is now in the Power
policies UI. The later [lower-bound trial](power-integral-lower-5080-61742-20261105.md)
qualified a retained lower setting and documented a failed past-count change;
future-count and mode changes remain untested. Arbitrary ratio ranges and other devices remain
unqualified. Any integration must use current reported capabilities and
preserve opaque fields, with explicit manual application and no startup or
post-reset replay.

The compact receipt records source hashes. Full private evidence is under
`registry-overrides/power-integral-control-terra-20261105`, parser evidence
under `power-ratio-parser-20261105`, and the constructor capture and independent
receipt verifier under `power-ratio-constructor-20261105` in the same parent.
