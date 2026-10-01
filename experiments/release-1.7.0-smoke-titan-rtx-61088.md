# Druta 1.7.0 release smoke test: TITAN RTX on 610.88

The source tree of the 1.7.0 release commit (PRs #30, #31 and #32 combined,
with the release-review fixes), run from source on one NVIDIA TITAN RTX
(TU102, VBIOS 90.02.1e.00.02, driver 610.88) on 2026-10-01. Every write was
restored, and the starting state was compared afterwards. One card, one
driver, one session: none of these figures is a constant for another card.

## Hold with headroom under load

The real backend fed the real Monitor tab while GPUPI 10B (OpenCL) ran. The V/F
point lock (domain 6) held the highest point at or below the 1068.75 mV
ceiling; the card was cool (33 °C idle).

| Phase | Core tile (programmed) | Measured line | Subtitle |
|---|---|---|---|
| Hold on the ceiling | 1950 | measured 1950  Δ -0.2 | P2 · steady at load |
| +25 mV headroom (reliability 1093.75 mV) | 1950 | measured 1947  Δ -3.0 | P2 · steady at load |
| Released, after the load | 390 | measured 390  Δ +0.0 | P8 · load 30 % |

Headroom applied, restored and released; the rail limits read back as they
started, and no V/F lock remained. This cool run lost less than a clock step
on the ceiling. The loss on this card depends on the run and the held point;
see `experiments/hold-headroom-titan-rtx-61088-20260929.md`.

## Hold, a value set by hand, a board-limit change

At idle: a V/F hold with the 25 mV raise, then power policy 4 set by hand
(138 W), then the board limit 260 → 270 W.

- The board-limit write made the driver recompute policy 4; Druta re-applied
  138 W ("the driver recalculated policies 4; re-applied your values").
- The headroom raise was untouched by the power writes, and its record stayed
  active.
- Afterwards: board limit back to 260 W, policy 4 back to the driver's 143 W
  (Stock), the raise restored, the lock released. Rail limits, power limit,
  every policy's request and the lock all matched the starting state.

## The application loop

The full application (real UI and backend; no startup profile, no input) ran
20 s on each tab: version 1.7.0, about 60 frames a second, 15-17 % of one CPU
core, and no panel error logged.
