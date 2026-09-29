# Clock sources under load: TITAN RTX on 610.88

Evidence behind the measured line under the Monitor's clock tiles
(`src/druta/realclock.py`). One NVIDIA TITAN RTX (TU102), VBIOS
`90.02.1e.00.02`, driver 610.88, voltage boost 0 %, NVVDD limits at their
first-read values (the effective ceiling is reliability, 1068.75 mV). GPUPI 3.3.3
10B, OpenCL, ran throughout: every sample below is at 100 % GPU load. One sample
per 0.25 s, 36 per phase, each phase starting 2 s after the last clock change.

"Tile" is the CORE CLOCK tile's source (NVAPI public `GetAllClockFrequencies`,
CURRENT). A and B are the private `GetAllClocks` arrays (medians).

| phase | tile | NVML graphics | GPC A | GPC B | B − A |
|---|---|---|---|---|---|
| V/F point lock at 1068.75 mV, on the ceiling | 1965 | 1965 | 1965.0 | 1950.18 | −14.81 |
| the same lock, +25 mV headroom | 1950 | 1950 | 1950.0 | 1947.73 | −2.27 |
| no lock, first-read limits | 1950 | 1950 | 1950.0 | 1937.36 | −12.64 |

- **The applied figure:** the tile, NVML and A agreed exactly in every sample; only B differed. B was never
  equal to A for GPC, XBAR, MEM or VIDEO (0 of 36 in each phase), and took 7 to 11 distinct values per
  phase while A held still.
- **The core rail's other domains:** XBAR (A 1875 / B 1861.47), SYSCLK and VIDEO followed GPC's gap on the
  ceiling (about −13.5) and closed it with headroom (−0.2).
- **MEM** read B − A = −6.8 MHz of 6801 in every phase, loaded or not: a fixed difference between the arrays
  on this card, not a loss. That is why the memory line is shown but never colour-graded.
- **Fixed clocks** (domains 3, 5, 6, 20, 22) read B exactly equal to A in 10 to 25 of 36 samples, on a card
  whose B is otherwise a counter. So B equal to A at one fixed target proves nothing either way. The
  "copies A" test therefore needs B to have equalled A through a change of A.
- **At idle, unlocked:** GPC read A 390 / B 390.00 (with B once at 402.28) and XBAR B equalled A in 16 of 32
  samples.

What this does not establish: B's behaviour on any other card, driver or load. The
readout decides per card, at runtime, whether B may be called measured.
