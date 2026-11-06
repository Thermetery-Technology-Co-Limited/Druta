# GRPG entry disable on the RTX 3090 Ti Kingpin (GA102), 617.42

First VBIOS-override transaction on Ampere, and a GRPG dependency outcome on a
controller set the RTX 5080 did not have (#51). Raw results:
[JSON](ampere-ga102-kingpin-grpg-61742-20261105.json).

Scope: one board on one driver at idle. Configured controller membership, not
gating residency or power.

## ROM

The ROM has no BIOS object directory (legacy tables). Its LPWR index table has
five 10-byte rows, the GRPG table five 5-byte rows, and PERF is v0x60. Druta now
reads index rows by their declared size, accepts legacy-table ownership, and
fingerprints a non-v70 PERF header instead of parsing its rows.

## Trial

`lpwr.gr_pg.0.enabled` → off: one byte at ROM offset `0x8501E`, `e7` → `e6`.
Firmware policy preserved; no managed registry values beforehand. Applied and
restored through Druta's registry transaction (stage, GPU restart, exact image
and readback verification).

Two P-states are loaded: P8 routes to GRPG entry 0 (enabled), P0 to entry 4
(disabled).

| LPWR controller | Before | Projected | Observed | Restored |
| --- | ---: | ---: | ---: | ---: |
| 0 (GR) | `0x100` | `0` | `0` | `0x100` |
| 1 | `0x100` | `0x100` | `0x100` | `0x100` |
| 4 (MS) | `0x100` | `0x100` | `0x100` | `0x100` |
| 5 | `0x101` | `0x101` | `0x101` | `0x101` |
| 2, 6 | unsupported | unsupported | unsupported | unsupported |

GR stayed supported (entries 1 and 3 remain enabled) but lost its only routed
P-state. No peer controller disappeared or changed. After restore the running
image hash matched the original and no managed registry values remained.
