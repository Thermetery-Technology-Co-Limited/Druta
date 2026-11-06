# Explicit source request and board-limit crossover

An explicit policy request acted as a competing cap, not a disconnection from
the main limit, on the tested RTX 5080 / 617.42 configuration. This experiment
used Druta's existing session-only controls, without a workload or restart.

Root set policy 2 to 350 W, moved the ordinary board request to 365 W and then
345 W, and restored the original complete request state. Each phase has three
sequential observations; values below were identical across those observations.

| Phase | Board request W | Policy 2 stored/host W | Policy 2 PMU output W | Policy 2 effective W | Policy 9 client-2 vote A |
|---|---:|---:|---:|---:|---:|
| Baseline | 360 | 450 | 360 | 370.811 | 36.002 |
| Policy 2 pinned | 360 | 350 | 350 | 360.510 | 35.003 |
| Board raised | 365 | 350 | 350 | 360.510 | 35.003 |
| Board below pin | 345 | 350 | 345 | 355.360 | 34.497 |

The board change to 365 W did not raise the capped output. The change to 345 W
lowered it despite the unchanged 350 W request and pin. Thus, in this trial,
the result followed the lower of the competing inputs. The stored request,
PMU arbitration output, feedback-adjusted effective limit, and measured power
are different quantities; the table contains policy values, not physical draw.

Policy 9's host request stayed 45 A. Its returned PMU inputs remained numeric
clients 254, 2 and 7, with clients 254 and 7 at 45 A. The client-2 value changed
as shown. The previously documented Q12 interpolation fits all twelve samples
when supplied policy 2's PMU output. In the last phase, substituting its 350 W
host output instead predicts 35.003 A, which differs from the returned 34.497 A.
This further discriminates the input candidates; it still does not identify
the actual firmware producer or prove physical current enforcement.

## Request and restoration evidence

Five production method calls succeeded: policy 2 to 350 W, board to 365 W,
board to 345 W, board back to 360 W, and policy 2 back to 450 W. There were two
native policy SETs selecting policy 2; board changes used the existing NVML
path. The ordinary board changes did not overwrite policy 2's stored request
in this trial, so no pin re-application was needed.

Restoration verified equality of the complete captured power-action requests,
this test instance's pin bookkeeping, the stable native/client INFO and CONTROL
state, and NVML's 360 W board request. Another app's private pin bookkeeping
is not observable from this test instance. The running user app was preserved.

The [receipt](power-explicit-source-20261105.json) records the raw checks and
hashes. Full private evidence, including native SET envelopes and bounded raw
STATUS prefixes, is under `registry-overrides/power-explicit-source-20261105`.
Only the useful STATUS prefixes were retained, avoiding repeated large opaque
payloads. No registry write, new tuning interface, physical measurement,
firmware analysis or Windows/GPU restart occurred.

## Correction to the configured-graph interpretation

The current host trace binds type-33 policy INFO row+0x34/+0x36 to two u16
fields copied from object+0x378/+0x37A. On policy 8 their values are 7 and 8.
The 621 host parser at 5D5A8F..5D5AA6, constructor 9104E0 and INFO callback
910350 establish those copies. They do not establish whether the fields are
independent selectors or the endpoints of an inclusive relationship range.
The older public PROP_LIMIT schema is a different type and is insufficient
to establish that meaning. Earlier policy-8 edges to policies 7 and 2 are
therefore candidate joins, not source-proven configured edges. Druta continues
to mark this source type uninterpreted. The type-0 and type-32 references
already displayed have separate serializer and consumer evidence.
