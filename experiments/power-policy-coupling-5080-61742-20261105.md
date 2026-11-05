# RTX 5080: board request versus secondary effective limits

On the tested RTX 5080 / driver 617.42 / VBIOS 98.03.3b.c0.6f, ordinary board
requests of 355 W and 365 W changed secondary **effective limits** without
changing any of the 17 secondary **requests**. The nominal/default definition
remained 360 W; the advertised board range remained 250–450 W.

| Policy | Stored request | Effective at baseline 360 W | Effective at 355 W | Effective at 365 W |
|---|---:|---:|---:|---:|
| 2 (power) | 450 W | 370.811 W | 365.660 W | 375.961 W |
| 7 (power) | 450 W | 360 W | 355 W | 365 W |
| 8 (board request) | 360 / 355 / 365 W | 360 W | 355 W | 365 W |
| 9 (current, channel 9) | 45 A | 36.002 A | 35.497 A | 36.497 A |
| 13 (core current) | 300 A | 300 A | 300 A | 300 A |
| 14 (other-rail current) | 120 A | 120 A | 120 A | 120 A |

These effective values were stable across the three samples at each changed
request. Policy 0's effective value also moved but varied across samples; the
evidence retains its range without inferring a conversion formula. Every
other policy's effective limit matched the captured baseline in these samples.

The separate arbitration readbacks explain why an unchanged request can
produce a changed limit. Policies 2, 7 and 9 report **minimum** arbitration.
Their host requests remain fixed while another PMU vote changes. For example,
at 355 W, policy 9 selects 35.497 A from votes of 45 A, 35.497 A and 45 A.
A higher host request alone cannot exceed that lower vote under the captured
minimum-arbitration rule. The source of its numerical conversion remains
unbound; the numeric client IDs in the evidence are not assigned speculative
hardware meanings. Policy 2's PMU output is distinct from its effective limit:
355 W versus 365.660 W in the same trial.

This differs from the captured Ampere path, where eight secondary request words
were recomputed from the board/default/maximum endpoints. Druta's session pins
preserve explicit user requests after its board writes when the driver moves
those requests; pins do not remove independent firmware votes.

## Qualification

The test used the production `GPU.set_power_limit_mw` path. A transparent call
recorder observed NVML setters for 355, 360, 365 and 360 W, in that order. The
initial operating point was P8 with 6% GPU utilization, and no workload was
started. No registry change, driver restart or application interaction occurred.

The complete captured power requests and native/client definitions and CONTROL
bytes matched the original baseline after each step and again at final cleanup.
Dynamic STATUS telemetry was not required to equal its earlier value. Both
existing application instances remained running. No runtime implementation
change was needed.

[Sanitized evidence](power-policy-coupling-5080-61742-20261105.json) contains all
18 rows, sample ranges, selected arbitration inputs, source hashes and
restoration results. These are software request and limit readbacks, not
physical power/current measurements. The test does not establish behavior at
other endpoints, across other boards/drivers, or after changing the nominal
TDP definition itself.
