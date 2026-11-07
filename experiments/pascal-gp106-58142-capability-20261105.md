# GTX 1060 (GP106) on 581.42: capability survey and registry trials

Gigabyte AORUS GTX 1060 Xtreme 6G 9Gbps, PCI `0x1C03` / `0x374A1458`, VBIOS
`86.06.4b.00.32`, driver 581.42. No GSP firmware runs on Pascal, so the whole
resource manager is the kernel driver. Raw results:
[JSON](pascal-gp106-58142-capability-20261105.json).

Scope: one board on one driver. RM `0x1F` is a size refusal: Druta has no layout
this driver accepts, which is not proof the interface is absent on Pascal.

## Read-only survey

Druta 1.9.08 `--ui-capture`, the registry snapshot reader and the LPWR readers.

| Interface | Result |
| --- | --- |
| Client power and current | Read: 180 W configured and enforced, 185.625 W native effective, 86.8 A |
| Voltage-request gate | Read: enabled |
| NVVDD rail policy | Read; MSVDD absent |
| ELPG | Read: LPWR controllers 0 (GR) and 4 (MS) only |
| ELCG | Read: 12 engines |
| BLCG, SLCG request readback | Unavailable |
| Individual power gating, DIFR | Unavailable |
| Configured LPWR (GRPG, MS, media, NVDEC) | Refused, RM `0x1F` |
| Driver flags (`0x2080A022`), clock INFO, clock V/F, VFE equations, rail requests | Refused, RM `0x1F` |
| Fan2X, fan policy, fan stop | Refused, RM `0x1F`; NVML fan control works |
| Power relationships, feedback bounds | Need the modern policy layout |
| Idle thresholds, adaptive policy, PCIe speed | Druta implements these for GB20x only |
| I2C regulator | None; the uP9511P core rail is PWM-VID ([survey](pascal-aorus-1060-i2c-58142.md)) |

## ELPG parallel capability (#48)

A801 type 12 / index 0 / attribute 3 returned the invalid flag on three stable
reads, as on the RTX 5080. A type-3 GR support read in the same session was
valid. The parallel path's state stays unknown, so no ENABLE was sent.

## EnableCoreVoltage (#46)

The PowerMizer keys (`PowerMizerEnable`, `PerfLevelSrc`, `PowerMizerDefault`,
`PowerMizerDefaultAC`) served as a positive control in the same key. Each restart
moved idle from P8 to P0, so a PnP restart re-reads the key.

| Input at driver load | Gate after restart |
| --- | --- |
| Absent | Enabled |
| DWORD 0 | Enabled |
| Binary `00000000` | Enabled |
| Absent, after a verified live disable | Enabled |

The name is in the kernel driver's registry table. On this configuration the
gate is set to enabled at every load, and neither zero form changes it. All
values were restored to absent; idle returned to P8.

## RmFan2XOverride (#71)

DWORD 1 is consumed here too. After restart, NVML fan speed read N/A, manual and
automatic control were unavailable, the reported minimum became 30%, and Druta
saw no fans. Idle temperature held at 33–36 °C. The physical fan state was not
observed, since no software readback remained. Restoring the absent value
returned NVML control and the fan.
