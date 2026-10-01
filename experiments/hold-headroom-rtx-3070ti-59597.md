# V/F hold headroom: RTX 3070 Ti on 595.97

Evidence behind the 25 mV hold-headroom margin's wording (issue #29). One NVIDIA
GeForce RTX 3070 Ti (GA104), VBIOS `94.04.5a.00.56`, driver 595.97, one
operating point. Run with the issue #29 `clock_drift_test.py` against
the `test/headroom-on-ampere` build (d6c69b9: Ampere rail control plus hold
headroom); output received 2026-10-01. None of these figures is a constant for
any other card, driver or generation. The 25 mV margin is a fixed safe margin
for every card, not tuned per card or generation; this run shows the loss and
that 25 mV clears it here, and no contributor is expected to repeat it with
smaller margins or on other generations.

## Setup

First-read NVVDD limits: reliability 1081.25 / alt-reliability 1100.00 /
overvoltage 1100.00 mV, boost contribution 0, the card's effective limit
1081.25 mV. The V/F point lock (domain 6) held the 1081.25 mV point (1965 MHz
on the curve), on the ceiling. Voltage grain from the card's curve: 6.25 mV.
A steady GPU load ran through all phases (load median 99 %). Each phase sampled
for 20 s (69 samples). "prog"/"meas" are the confirmed GPC row's programmed and
measured clocks; "rail live" is the rail block's live NVVDD field.

## Result

| Phase | Ceiling | prog median | meas median (min-max) | prog - meas | Rail live |
|---|---|---|---|---|---|
| A, limits as they were | 1081.25 | 1965 | 1950.0 (1950-1960) | 15.0 | 1081.25 |
| B, +25 mV headroom | 1106.25 | 1965 | 1964.8 (1950-1965) | 0.2 | 1106.25 |
| A again, limits restored | 1081.25 | 1965 | 1950.0 (1950-1965) | 15.0 | 1081.25 |

The raise wrote reliability, alt-reliability and overvoltage 1106.25 mV; the
live offset measured before it was 0.0 mV and the live check's allowance 3.125
mV. The run checked every phase-B live reading with `hold_headroom_live_ok`:
none was above the raised ceiling plus the allowance, so the app would have
kept the raise. Afterwards the limits read back as they started.

## What it shows, on this card

- Held on its voltage ceiling, the GPC ran one 15 MHz step below the clock it
  showed, steadily, at 99 % load; +25 mV of headroom removed it, and it came
  back when the limits were restored (both A phases match, so heat drift does
  not explain it).
- The rail sat exactly on the ceiling in every phase: 1081.25 mV without the
  raise, 1106.25 mV with it. The clock recovered with the rail running the full
  25 mV higher; the held point's voltage stayed 1081.25. One TITAN RTX ran its
  rail up to 18.75 mV higher under the same raise
  (`hold-headroom-titan-rtx-61088-20260929.md`).
- With the rail on the raised ceiling, the live check's slack on this card is
  its allowance alone (3.125 mV); the live reading did not move from 1106.25.
