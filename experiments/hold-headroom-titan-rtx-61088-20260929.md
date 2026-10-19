# V/F hold headroom: TITAN RTX on 610.88

Evidence behind the hold-headroom notes in `GPU.HOLD_HEADROOM_FIELDS` and
`apply_hold_headroom` (issue #29). One NVIDIA TITAN RTX (TU102), VBIOS
`90.02.1e.00.02`, driver 610.88, one operating point each. None of these figures
is a constant for any other card, driver or generation; the margin is a user
setting and the live check takes its grain and offset from the current adapter.

## Ceiling sweep under load (2026-09-27)

Setup: first-read NVVDD limits reliability 1068.75 / alt-reliability 1093.75 /
overvoltage 1125 mV, voltage boost 100% (a 25 mV reliability contribution on
this card), so the effective ceiling is 1093.75 mV. The V/F point lock (domain
6) held the 1093.75 mV point. The ceiling was raised to the value shown (the
run's own read-back is the `[True]` in its output), then GPUPI 1B ran. "Point" is the held V/F point's voltage (what
GPU-Z shows); "rail live" is the rail block's live NVVDD field; prog/meas are
the programmed and measured GPC clocks in MHz. Samples as printed by the run:

| Ceiling | Idle point | Idle rail live | Idle prog/meas | Load point | Load rail live | Load prog/meas | GPUPI 1B |
|---|---|---|---|---|---|---|---|
| 1093.75 (+0) | 1093.75 | 1093.75 | 2130 / 1992 | 1093.75 | 1093.75 | 2115 / 2087 | 3.111 s |
| 1100.00 (+6.25) | 1093.75 | 1100.0 | 2115 / 2110 | 1093.75 | 1100.0 | 2115 / 2102 | 3.101 s |
| 1106.25 (+12.5) | 1093.75 | 1093.75, 1100.0 | 2115 / 2112 | 1093.75 | 1106.25 | 2115 / 2110 | 3.094 s |
| 1118.75 (+25) | 1093.75 | 1093.75, 1100.0, 1106.25 | 2115 / 2113 | 1093.75 | 1112.5 | 2115 / 2115 | 3.084 s |
| 1143.75 (+50) | 1093.75 | 1093.75, 1100.0, 1106.25 | 2115 / 2115 | 1093.75 | 1112.5 | 2115 / 2112 | 3.097 s |

The lock was released and the limits returned to their first-read values
afterwards (read back equal).

What this shows, for this card and driver only:

- The held point's voltage stayed at 1093.75 mV throughout.
- The live rail rose above the point as far as the ceiling allowed, up to
  1112.5 mV (the point + 18.75 mV) and no further at +50.
- A ceiling at the point cost 28 MHz under load; +25 mV removed it.

What it does not establish: whether the stop at 1112.5 mV is a fixed rise
above the point or a limit near 1112.5 mV on this card. A single hold voltage
cannot tell them apart. It also cannot tell whether the live field is sensed or
commanded (see the note at `GPU.LIVE_RAIL_VER`).

## Counter-observation on 580.97

`voltage-rails-20260906.json` (same card, driver 580.97, `raised_ceiling`
trials) held a 1112.5 mV point under a 1125 mV ceiling at idle (P2, ~52 W):
the live field read 1112.5 mV, equal to the point, in every sample. So a rise
above the point is not seen on every driver or at every load.

## Reworked guard on hardware (2026-09-29)

Branch `feat/vf-ceiling-headroom` after the whole-branch review, idle, voltage
boost 0% (so reliability alone set the 1068.75 mV ceiling):

- The V/F point lock held 1068.75 mV. The live rail read 1068.75 mV before the
  raise, so the card's own reading offset above its ceiling was 0.
- The grain taken from this card's V/F curve was 6.25 mV (allowance 3.125 mV).
- The raise wrote reliability 1093.75 mV. The card's own effective limit then
  read 1093.75 mV, equal to Druta's estimate.
- The live rail then read 1068.75, then 1081.25 mV for five samples at idle:
  above the held point, under the raised ceiling. The live check kept the
  raise.
- The restore and release returned all four NVVDD limit deltas to exactly
  their starting values (`[0, 0, 0, 0]`); no V/F lock remained.
