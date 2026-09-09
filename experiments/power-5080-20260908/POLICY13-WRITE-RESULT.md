# Policy 13 accepts an effective current-limit change

**Subsequent test:** The enforced upper request boundary was confirmed at
**5,001 A** while idle, with 300 A restored after every trial. See
[maximum-test results](POLICY13-MAXIMUM-RESULT.md).

On the RTX 5080 Astral, driver **580.97**, VBIOS **98.03.3b.c0.6f**, the
internal policy-13 control accepted **300 A → 295 A → 300 A** while FurMark
remained running.

| Phase | Requested limit | Escape status | RM status | Effective dynamic limit |
|---|---:|---:|---:|---:|
| Baseline | — | — | — | 300 A, 8/8 samples |
| Unchanged identity write | 300 A | 0 | 0 | 300 A, 8/8 samples |
| Temporary decrease | 295 A | 0 | 0 | 295 A, 8/8 samples |
| Restore | 300 A | 0 | 0 | 300 A, 8/8 samples |

The decrease changed **exactly one dword** in the full control GET reply:
offset **0x664**, from **300000** to **295000**. The separately queried
dynamic-policy limit also changed to **295000**, establishing effective
controller acceptance rather than merely a successful return or stored echo.

After restoration, the entire **4432-byte control GET reply was byte-identical
to the saved original**. The ordinary board limit stayed **450 W**, and
policy 14's limit stayed **120 A**, in every observed phase.

## Method

Before applying anything, the probe captured the ordinary NVAPI power-slider
identity SET and suppressed it before driver dispatch. Its parameter block
differed from the preceding full control GET only in the selection mask:
`0x3ffff → 0x100`. This dynamically verified the installed driver's control
GET/SET ABI without changing the board-power slider.

The policy-13 test then used:

- Control GET: `0x2080a61a`.
- Control SET: `0x2080e61b`.
- Parameter block: **4432 bytes**, preserved from GET.
- Selection mask at `+0x10`: **0x2000**, selecting policy 13 alone.
- Record 13: start `+0x660`, type `0x12`, limit at **+0x664**, in mA.
- Effective readback: separate dynamic GET **`0x2080a619`**.

The original control block was saved and flushed to disk before the test.
The test used a `finally` restoration path, replaying the saved selected
record if anything changed or a readback failed. Restoration does not depend
on a clamped value matching the intended test value.

An independent agent checked the static ABI before execution and reviewed the
saved execution evidence afterward. All actual hardware writes came from the
single parent-controlled probe process.

## What this establishes

**Policy 13 is writable through the internal RM control, and its effective
ceiling can be changed.** The hidden current policy is not merely read-only
telemetry on this driver/card combination.

This test used a small decrease. **This experiment did not test acceptance
above the original 300 A ceiling.** The short sample windows and small 5 A change also do
not establish a statistically meaningful FPS or clock-performance effect.
The evidence is the exact stored and effective limit transition, plus complete
restoration.

## Files

- `policy13-write/execution.json`: saved original and final full control
  blocks, all three SET requests and responses, and 8 dynamic observations
  per phase.
- `policy13-write/suppressed-slider-identity.json`: the preparatory captured
  slider SET, explicitly labeled as suppressed with a synthetic failure.
  Its synthetic failure is not a firmware rejection result.
- `../probe_5080_policy13_write.py`: bounded reproduction script. Without
  `--execute`, it only captures and suppresses the normal slider SET. With
  `--execute`, it allows only the measured 300/295/300 A sequence, verifies
  GPU/driver/VBIOS/DLL identity, and refuses to overwrite an execution record.

No production Druta UI or backend code was modified.

## Public evidence packaging

The execution JSON distributed here is a derived public summary. Bulk control
and parameter arrays are represented by their word counts and SHA-256 hashes;
status, requested values, dynamic observations and restoration checks remain.
Device UUIDs and process handles are omitted. The preparatory
`suppressed-slider-identity.json` capture mentioned above is not distributed;
it is distinct from the measured SET outcomes. The reproduction scripts are
included, but no hardware experiments were repeated while preparing this PR.
