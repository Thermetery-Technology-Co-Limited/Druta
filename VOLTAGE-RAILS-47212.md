# TITAN voltage rails on driver 472.12

Measured on the same TITAN RTX (TU102, 90.02.1e.00.02) and TITAN Xp
(GP102, 86.02.3d.00.01) used for the 580.97 tests. Both again reached
**1112.5 mV** with reliability/alternate ceilings raised to **1125 mV**
and a de-flattened V/F point requesting 1112.5 mV. Two raise/restore cycles
per card produced the same result. These are driver-reported live readings.

## Compatibility changes

472.12 accepts the rail-control NVAPI block at **V1, 0x00010AC8**;
the V2 block used on 580.97 returns `NVAPI_INCOMPATIBLE_STRUCT_VERSION`.
The four signed control fields retain the same NVAPI offsets. Its absolute
rail getter also remains V1, 0x00010AC8.

The legacy control getter returns success with empty records for absent rail
masks. Druta checks those records against the independent absolute getter,
which confirms only NVVDD on both boards. MSVDD remains unavailable.

An unchanged write through the existing NVAPI voltage-boost setter identified
the legacy RM transport without guessing a command:

| Property | 472.12 | 580.97 |
|---|---:|---:|
| Get command | `0x20803213` | `0x2080B213` |
| Set command | `0x20803214` | `0x2080F214` |
| Escape size | 716 bytes | 1104 bytes |
| Parameter size | 648 bytes | 1036 bytes |
| Parameter header | mask, boost | selector, mask, boost |
| Rail record | type 1, four deltas | type 5, four deltas, reserved/valid fields |

The production writer selects the measured transport through an exact
board/VBIOS/driver profile and preserves all unrelated packet words. A shared
lock serializes hook installation across GPU objects; only the installing
native thread may substitute a write. A background thread's intercepted read
is forwarded without modification.

## Measured bases and live behavior

All values below are mV at zero stored delta. Reliability is shown at zero
voltage boost.

| Field | TITAN RTX | TITAN Xp |
|---|---:|---:|
| Reliability | 1068.75 | 1068.75 |
| Alternate reliability | 1093.75 | 1093.75 |
| Overvoltage | 1125 | 1200 |
| Minimum voltage | 650 | 650 |
| Boost allowance at 100% | 25 | 25 |

**Pascal differs from 580.97:** that driver measured a 1062.5 mV reliability
base and 31.25 mV boost allowance. Reusing that profile on 472.12 would shift
the requested limit by 6.25 mV.

Every limit was changed independently by 12.5 mV and its absolute field
matched. Voltage boost alternated 0/100%, confirming the bases above.
An NVVDD offset of +12.5 mV also moved the live voltage by 12.5 mV:
781.25 to 793.75 mV at a 1500 MHz frequency lock on RTX, and 1043.75 to
1056.25 mV at a steady 1911 MHz CUDA workload on Xp. The Pascal baseline
shifted by one 6.25 mV step as temperature rose; the paired restore also
moved voltage by exactly 12.5 mV.

On both cards, each ceiling independently clamped the live voltage to
**875 mV** under CUDA load. RTX used an existing 1000 mV V/F point to
establish demand; Xp's unrestricted workload started around 1050 mV.
RTX's idle minimum immediately raised voltage from **681.25 to 875 mV**
and restoring the original floor returned it to 681.25 mV.

Xp's idle minimum also
raises voltage from **650 to 875 mV**, but R470 recomputes the idle voltage
using the previously stored minimum on the first write. An identical second
write makes the live voltage catch up. Druta performs that identity re-send
only for a changed minimum on the measured GP102/472.12 profile, after
verifying all stored records and the preserved boost setting. The same step
makes reset return the idle voltage to 650 mV. Two complete cycles through
the production set/reset methods verified this behavior.

## Evidence and restoration

Full local samples and original recovery bytes are under `docs/rail-probes/`:

- `47212-titan-{rtx,xp}-fields.json`: field isolation, bases and boost.
- `47212-titan-{rtx,xp}-load.json`: NVVDD offsets and raised-cap cycles.
- `47212-titan-xp-live-fields-verified.json` and
  `47212-titan-rtx-live-clamps-verified.json`: independent live ceiling clamps.
- `47212-titan-rtx-idle-floor-repeat.json`: immediate RTX floor response.
- `47212-titan-xp-idle-floor-repeat.json`: the legacy floor delay.
- `47212-titan-xp-idle-floor-production.json`: corrected production set/reset.
- `47212-titan-{rtx,xp}-production-verified.json`: all four production field writes.

The tests preserved and verified the original V/F table, lock bytes, clock
domain control bytes, voltage boost and rail fields. The scripts save recovery
data before writing and restore in `finally`. Tests that failed to establish
a low idle baseline also restored all saved state; their captures remain
available separately.

`test_volt_rails.py` and `test_volt_rails_legacy.py` cover the transports,
identity gates, conversion bases, absent rails, packet preservation, thread
ownership, installer serialization, and verified floor re-send without a GPU.
