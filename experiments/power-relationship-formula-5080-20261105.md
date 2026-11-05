# Power-policy relationship arithmetic on the captured RTX 5080

The saved host driver contains endpoint interpolation with Q12 rounding. Using
policy 2's returned PMU output as its input reproduces policy 9's current vote
in all 64 saved paired observations. This is an exact fit to those captures,
not proof that the PMU uses the traced host routine or a physical conversion
from power to current. The traced host getter reads host current arbitration,
which differs from the PMU input assumed in the fit. No product control or hardware setting was changed by
this investigation.

## Captured relationship and coefficient

On the captured RTX 5080 / VBIOS 98.03.3b.c0.6f, source-bound configured
references connect policies 2 and 7 through relationship 9 to policy 9. The
earlier policy-8 links to policies 2 and 7 are candidate joins only: its
two serialized fields contain 7 and 8, but their general selector/range
semantics have not been bound to a current consumer or schema. Numeric PMU
client IDs must not be treated as universal policy indices solely because
they match configured references or candidate joins.

The saved 621.01 host parser carries relationship 9's ROM parameter 4096 into
the class-0 relationship object's coefficient. Its INFO getter exposes that
coefficient, and the host arithmetic supports a unitless Q12 weight: 4096 is
unity in the examined arithmetic domain. Root verified the value in 30 saved
INFO snapshots across three receipts. The INFO geometry is independently
traced in the 621.01 host; identical 617.42 packet bytes are observations, not
a separate trace of that driver's serializer.

## Endpoint interpolation hypothesis

The host's relative-adjustment callback has separate branches above and below
the source's rated/default value. The upward branch uses the default-to-maximum
span; the downward branch uses the minimum-to-default span and requires the
corresponding adjustment flags. It normalizes the source displacement to Q12,
applies the relationship weight, then interpolates the target span. A zero
span, disabled adjustment mode or overflow must not be modeled by blindly
dividing or extrapolating the simplified formula.

In these captures, source policy 2 has minimum 1 and default/maximum 450000 mW;
target policy 9 has minimum 1 and default/maximum 45000 mA. With weight 4096,
the downward branch's integer arithmetic becomes:

```text
# Hypothesis: x is policy 2's PMU arbitration output, in mW.
q = ((450000 - x) * 4096 + 224999) // 449999
w = (q * 4096 + 2048) // 4096
y = 45000 - (44999 * w + 2048) // 4096
```

The sampled domain is 249000 <= x <= 450000. At x=450000 the displacement is
zero. These constants belong to this captured configuration, not to all
Blackwell GPUs or all current policies.

| Assumed input x, mW | Q12 displacement q | Predicted and reported client-2 vote, mA |
|---:|---:|---:|
| 249000 | 1830 | 24895 |
| 355000 | 865 | 35497 |
| 360000 | 819 | 36002 |
| 361000 | 810 | 36101 |
| 365000 | 774 | 36497 |
| 450000 | 0 | 45000 |

Root independently evaluated the arithmetic using integers against the original
capture inventory: 11 observations from the 617.42 trial, 37 from the 621.01
extended trial, and 16 from the 361 W propagation trial. All 64 residuals are
zero. They include repeated observations and transition samples on one GPU,
not 64 independent devices or operating points. The [receipt](power-relationship-formula-5080-20261105.json)
retains every compared value and source hash.

Board REQUEST is not interchangeable with x. Requests of 451 W and 999 W had
a policy-2 PMU output of 450 W in the saved extended trial. Nor is policy 2's
effective STATUS interchangeable with x: changing its upper feedback bound
from 4219 to 4178 changed the effective limit from 370811 to 367207 mW while
the PMU output stayed 360000 mW and policy 9's vote stayed 36002 mA. This rejects
a fixed nonzero multiplier of that effective STATUS across the intervention.
After restoration, the first raw effective read was 360000 mW and the later
decoded read was 370811 mW; sequential reads are not atomic snapshots.

## Live weight-setting boundary

The traced local native `0x2080E61B` relationship SET dispatch does not update
the class-0 coefficient. Its inherited setter checks pointers/type and marks
the object for refresh; it does not copy a weight from CONTROL to the object.
The derived constructor overrides INFO/serialization callbacks and separate
arithmetic callbacks, but not that interface's SET callback. A success status
on this path would therefore not prove a weight change. The public relationship
CONTROL union contains BALANCE controls, not a WEIGHT member.

No speculative SET was issued. This trace does not exclude another interface
or establish which local/forwarded route a running driver takes. A live weight
control remains unqualified and is not exposed in Druta.

## Source provenance and remaining boundary

Saved 621.01 `nvlddmkm.sys` SHA-256:
`3F8D33495CABEB2FA11EBDCF0B12705E9CCFB4E53005324BCD86E9DEA8BA2AB4`.
Relevant source paths are the relationship parser at `5D62DC..5D6501`, derived
constructor `C57490`, INFO group `4ECC75..4ECC90`, INFO field copy
`C57A2B..C57A32`, normalization `C39140`, relative interpolation `C57640`, and
local SET handler/group `4F0AA0` / `4EDB00` with inherited setter `CB5A80`.
Addresses identify this saved binary only; they are not product compatibility
gates or runtime offsets.

The traced class-0 constructor chain retains source getter `+0x338 = 4EF750`
and limit getter `+0x2E0 = 4EB640`. The first forwards selector 3 to the second,
which reads object `+0x1F8`, the host current-arbitration output. The caller's
fifth stack argument of zero is not consumed by these getters and is not
established as a client identifier. The saved host value was 450000 mW while
the PMU output was 360000 mW; using that host value gives a target of 45000 mA,
not the observed PMU client-2 vote of 36002 mA. Thus this host path does not
establish the input binding used by the exact fit. Runtime invocation and any
other producer remain unobserved.

The host path submits a computed target request as client `0xFE`. This is
distinct from proving the origin of the returned PMU client-2 vote. Actual
PMU execution and physical current enforcement remain unestablished. The
exact numerical fit is useful evidence, but it must not become a universal
formula, a physical voltage estimate, or an unverified write capability.

Full private source captures and reproducible extraction/audit scripts are in
`registry-overrides/power-policy9-formula-20261105`. This pass used saved files
only: no GPU probes, registry changes, workload, driver restart or firmware
analysis.
