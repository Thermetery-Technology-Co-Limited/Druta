# Policy 13: enforced maximum is 5,001,000 mA (5,001 A)

Measured on 2026-09-08 on the ASUS RTX 5080 Astral, NVIDIA driver **580.97**,
VBIOS **98.03.3b.c0.6f**. The user stopped FurMark before this experiment.

The policy metadata advertises minimum **1 mA**, default **300,000 mA**,
and maximum **5,001,000 mA**. Direct RM SET tests confirmed that the maximum
is accepted and the immediately adjacent value, one milliamp higher, is
rejected. This establishes the enforced request boundary on this card and
driver, even when bypassing the ordinary NVAPI wrapper's validation.

| Requested limit | NTSTATUS | RM status | Stored limit | Effective dynamic limit | Original state restored |
|---|---:|---:|---:|---:|---|
| 305 A | 0 | 0 | 305 A | 305 A, 3/3 samples | Yes |
| 5,001 A | 0 | 0 | 5,001 A | 5,001 A, 3/3 samples | Yes |
| 5,001.001 A | 0 | 31 (`0x1F`) | 300 A, unchanged | 300 A, 3/3 samples | Yes |

The rejected request was not clamped to the maximum: the previous **300 A**
setting remained intact. A zero escape NTSTATUS alone does not mean the
request was accepted; the separate RM status reports the rejection.

## Restoration and workload

Each request was followed by an unconditional restoration of the saved
policy-13 record. All restoration calls returned NTSTATUS 0 and RM status 0.
After each trial the entire **4432-byte control GET** equaled the original,
and the separately queried effective limit was **300,000 mA**.

The intervals from each test attempt through completion of its restore were
**0.231 s**, **0.261 s**, and **0.241 s**, respectively. These are measured
durations, not guaranteed timeouts on synchronous driver calls.

Across all **22** recorded snapshots:

- The GPU stayed in **P8**.
- Reported board power ranged from **20.332 to 24.247 W**.
- Policy-13 measured current ranged from **1.046 to 3.285 A**.
- The ordinary board-power setting remained **450 W**.
- Policy 14's effective ceiling remained **120 A**.

The probe required P8, less than 50 W reported board power, and less than
30 A policy-13 current before and during each trial. It saved the original
control block before any actual SET, selected only policy 13, and restored
before diagnostic readback or report writing. The 305 A and 5,001 A trials
changed only the requested-limit dword at parameter offset **0x664**.

## Interpretation

**5,001 A is an API/firmware policy-setting ceiling, not the GPU's physical
current rating or a usable operating recommendation.** These idle tests
establish effective limit acceptance; they do not measure loaded performance
or prove that other policies and protections would allow that current.
The GPU was left at its original **300 A** policy-13 limit.

## Reproduction and evidence

- `policy13-maximum/execution.json`: original control block, all SET results,
  full control readbacks, dynamic observations, and restoration checks.
- `policy13-maximum/suppressed-slider-identity.json`: captured ordinary
  NVAPI identity SET used to verify the wire format, suppressed before
  dispatch. Its synthetic error is separate from the real rejection above.
- `../probe_5080_policy13_max.py`: bounded probe with fixed test values,
  GPU/driver/VBIOS/DLL checks, idle gates, and unconditional restoration.
- `POLICY13-WRITE-RESULT.md`: preceding 300/295/300 A write experiment.

No production Druta UI or backend code was modified.

## Public evidence packaging

The execution JSON distributed here is a derived public summary. Bulk control
and parameter arrays are represented by their word counts and SHA-256 hashes;
status, requested values, dynamic observations and restoration checks remain.
Device UUIDs and process handles are omitted. The preparatory
`suppressed-slider-identity.json` capture mentioned above is not distributed;
it is distinct from the measured SET outcomes. The reproduction scripts are
included, but no hardware experiments were repeated while preparing this PR.
