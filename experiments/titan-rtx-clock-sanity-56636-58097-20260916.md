# TITAN RTX clock sanity: 566.36 and 580.97

This is a bounded validation of released Druta **1.5.0a**, commit
`23d40976ba06f322d6583e71558755dd301f9e75`, on one NVIDIA TITAN RTX with
VBIOS `90.02.1e.00.02`. The released EXE SHA-256 was
`88696332730594b48da45766bd15f6b5a42054798930eb5c5041caeb832fd22f`;
its verified handback launch was observed as PID 13996. No release code was
changed during this validation.

## Runtime discovery and UI

Both driver sessions returned the same validated Turing control layout:
version `0x000261A4`, size `0x61A4`, header `0x124`, stride `0x304`, and
frequency field `+0x10C`. One-hot discovery accepted controls 0 through 9;
Druta exposed its understood set `[0, 1, 2, 3, 5, 9]`. The getter returned
status 0 with the expected version and mask echoes and no capability error.

Four live-GPU GUI captures used the real Dear PyGui framebuffer. XBAR widgets
`sl_xbar`/`in_xbar` and SYS widgets `sl_sys`/`in_sys` existed and were visible
on both 566.36 and 580.97. The normal runs used NVML architecture detection.
The other two runs deliberately made NVML loading unavailable; the real public
NVAPI architecture getter selected Turing as `NVAPI V2`, and the same controls
remained visible. I2C was unchecked and its scan-call count stayed **zero** in
all four runs.

## Loaded control checks

The GPU clock was held at 1500 MHz under load. Each selected control stored a
`+30000 kHz` request, its mapped physical counter moved, the other mapped
domain's programmed target did not change, and the decoded mode, frequency, and
MSVDD request fields for all accepted controls were restored exactly. Each range below contains all five physical-counter samples,
including initial settling samples; the full arrays are retained in the
companion JSON.

| Driver | Trial | Baseline measured, MHz | Changed measured, MHz | Restored measured, MHz |
|---|---|---:|---:|---:|
| 566.36 | XBAR control 1 | 1439.998–1439.999 | 1468.156–1469.999 | 1439.999 |
| 566.36 | SYS control 3 | 1499.998–1500.000 | 1529.998–1530.000 | 1499.997–1500.002 |
| 580.97 | XBAR control 1 | 1439.999 | 1468.146–1469.999 | 1439.999 |
| 580.97 | SYS control 3 | 1499.999–1500.000 | 1529.996–1530.002 | 1499.998–1500.000 |

This establishes the expected programmed movement of **XBAR
1440 → 1470 → 1440 MHz** and **SYS 1500 → 1530 → 1500 MHz** on both tested
drivers. During the XBAR trials, SYS remained programmed at 1500 MHz; during
the SYS trials, XBAR remained programmed at 1440 MHz and measured
1439.998–1439.999 MHz. GPC remained programmed at 1500 MHz throughout. The
capture includes all measured GPC/XBAR/SYS samples so normal load-settling
jitter is visible rather than reduced to the rounded expectation.

Both clock-lock requests and releases succeeded, the load stopped without an
error, and every XBAR/SYS trial reported exact decoded control-field
restoration. Opaque packet bytes were not independently compared after restoration. The machine was switched between drivers without rebooting and
returned to 580.97. Profile restoration reported zero differences among
authoritative settings and both V/F and clock locks were null. A fresh-process
580.97 check independently reported the same zero differences and null locks.
Profile capture timestamps were ignored. Estimated absolute rail values were
excluded from the equality check because their first-read reference is
quantized per session.

The existing relevant unit-test selection also passed **74 tests**.

## Limits

This evidence is specific to this board, VBIOS, release commit, and the two
named driver sessions. It is not a general guarantee for the 566 driver family
or another board.

NVAPI architecture fallback only covers architecture identification when NVML
cannot load or answer. It does not negotiate or alter the private clock-control
wire layout. Clock-domain discovery also retains an empty or partial first
scan in a negative cache; 1.5.0a does not automatically retry that getter. A
transient miss can keep a clock row hidden until **Refresh capabilities** is
used manually.

The condensed machine-readable evidence is in
[`titan-rtx-clock-sanity-56636-58097-20260916.json`](titan-rtx-clock-sanity-56636-58097-20260916.json).
UUIDs, local paths, and full user tuning snapshots are intentionally omitted.
