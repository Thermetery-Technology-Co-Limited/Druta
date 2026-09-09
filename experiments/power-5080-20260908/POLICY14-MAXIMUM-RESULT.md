# Policy 14: maximum request is 5,001,000 mA (5,001 A)

Measured on 2026-09-08, ASUS RTX 5080 Astral, NVIDIA driver **580.97**,
VBIOS **98.03.3b.c0.6f**, with FurMark stopped.

| Request | NTSTATUS | RM status | Stored and effective limit |
|---|---:|---:|---:|
| 5,001 A | 0 | 0 | 5,001 A, 3/3 dynamic observations |
| 5,001.001 A | 0 | 31 (`0x1F`) | Unchanged at 120 A, 3/3 observations |

The accepted request changed only word **440**, parameter offset **0x6e0**.
The rejected request left the complete control block unchanged. Both restore
calls returned NTSTATUS 0 / RM 0, restored the complete **4432-byte** control
reply exactly, and restored the effective **120 A** limit. The intervals
through restoration were **0.240 s** and **0.228 s**.

All **17** saved samples were in P8, at **16.433–27.484 W** reported board
power. Policy 13 remained **300 A** and the board limit **450 W**. An
independent read-only audit confirmed the results and restoration.

This is a software request ceiling, not a physical current rating. Loaded
performance was not tested. Policy 14 was left at its original 120 A limit.

Evidence: `policy14-maximum/execution.json`,
`policy14-maximum/suppressed-slider-identity.json`, and
`../probe_5080_policy14_max.py`. The preparatory suppressed SET has a synthetic
error, separate from the actual firmware rejection above.

## Public evidence packaging

The execution JSON distributed here is a derived public summary. Bulk control
and parameter arrays are represented by their word counts and SHA-256 hashes;
status, requested values, dynamic observations and restoration checks remain.
Device UUIDs and process handles are omitted. The preparatory
`suppressed-slider-identity.json` capture mentioned above is not distributed;
it is distinct from the measured SET outcomes. The reproduction scripts are
included, but no hardware experiments were repeated while preparing this PR.
