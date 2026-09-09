# Policy 14 accepts an effective current-limit change

Measured on 2026-09-08, ASUS RTX 5080 Astral, NVIDIA driver **580.97**, VBIOS
**98.03.3b.c0.6f**. The internal policy-14 limit accepted **120 A → 115 A →
120 A** with the GPU idle.

| Phase | Requested limit | NTSTATUS | RM status | Effective dynamic limit |
|---|---:|---:|---:|---:|
| Temporary decrease | 115 A | 0 | 0 | 115 A, 5/5 samples |
| Restoration | 120 A | 0 | 0 | 120 A |

The full control GET changed only **word 440**, parameter offset **0x6e0**,
from **120000** to **115000**. The separately queried dynamic-policy limit
also changed to **115000**, confirming effective acceptance beyond a success
return or stored-value echo.

The original selected record was restored unconditionally in `finally`,
before diagnostic GETs or report writing. After restoration, the complete
**4432-byte control reply was identical to the saved original**, and the
effective policy-14 limit was **120000 mA**. The interval from the test
attempt through completion of its restoration was **0.386 seconds**.

## ABI and scope

- Policy 14 uses type **0x12**, channel **12**, unit **1 (mA)**.
- Its control record starts at **0x6dc**; the limit is at **0x6e0**.
- Selection mask **0x4000** selects policy 14 alone.
- Control GET/SET are **0x2080a61a / 0x2080e61b**, with a 4432-byte block.
- Independent dynamic readback uses **0x2080a619**.
- Metadata declares minimum **1 mA**, default **120000 mA**, maximum
  **5001000 mA**. This test did **not** test policy 14's upper boundary.

The GPU was gated on P8, reported board power below 50 W, and both measured
rail currents below 30 A. All other settings were preserved. This experiment
establishes write acceptance; it does not measure loaded performance or
identify the physical rail more precisely than the preceding investigation.

## Evidence

- `policy14-write/execution.json`: saved original, requests and statuses,
  full control readbacks, dynamic samples, and restoration verification.
- `policy14-write/suppressed-slider-identity.json`: captured and suppressed
  ordinary NVAPI slider SET used to verify the installed driver's wire format.
  Its synthetic failure is not a real hardware rejection.
- `../probe_5080_policy14_write.py`: fixed 115/120 A probe; default invocation
  is read-only. `--execute` performs the bounded decrease and restoration.

The GPU was left at **120 A for policy 14**, **300 A for policy 13**, and
**450 W for the ordinary board limit**. No production backend or UI code was
modified.

## Public evidence packaging

The execution JSON distributed here is a derived public summary. Bulk control
and parameter arrays are represented by their word counts and SHA-256 hashes;
status, requested values, dynamic observations and restoration checks remain.
Device UUIDs and process handles are omitted. The preparatory
`suppressed-slider-identity.json` capture mentioned above is not distributed;
it is distinct from the measured SET outcomes. The reproduction scripts are
included, but no hardware experiments were repeated while preparing this PR.
