# RTX 3090 Ti Kingpin (GA102) on 617.42: core current, EnableCoreVoltage, IDDQ

Same board and driver as the [survey](ampere-ga102-kingpin-61742-20261105.md);
no GSP firmware. Idle, water-cooled, each change restored and verified. Raw
results: [JSON](ampere-ga102-kingpin-trials-61742-20261105.json).

Scope: one board on one driver. Request, readback and restoration only; no
physical measurement.

## Core current (#59)

Policy 13 reports minimum 1 mA, normal maximum 500 A, default and maximum
845 A. With XOC enabled, 844.2 A was accepted with exact readback, 845.001 A was
refused by Druta's bound, and 845 A was restored exactly.

Without XOC, the setter bounds a request by the larger of the normal maximum and
the current limit. After lowering to 844.2 A, typing 845 A back was refused.
Stock writes the default by a different path and is not affected.

## EnableCoreVoltage (#46)

Absent originally. With DWORD 0, then binary `00000000`, then absent again, the
voltage-request gate read enabled after each GPU restart. `RmFan2XOverride` in
the same key was consumed (survey), so the key is read at startup. The original
absent state was restored.

## IDDQ model inputs (#69)

Druta's model-input reader decodes only runtime type 13, so it lists no IDDQ
input on this board. The sensed-fuse rows here are runtime type 7. Their active,
raw, default, override, presence, readiness and scalar-mirror fields sit at the
same INFO offsets; every mirror agreed.

Identification: each IDDQ name was written with its candidate row's current
active value. After a restart the driver set `override_present` on exactly that
row:

| Registry name | VFE row | Selector | Raw | Scale (Q12) | Active | Default |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| `RmPmgrIddqNvvddOverride` | 3 | `(0x10, 0x0A)` | 728 | 204800 (×50) | 36400 | 1500 |
| `RmPmgrIddqMsvddOverride` | 7 | `(0x11, 0x0A)` | 218 | 204800 (×50) | 10900 | 500 |

Active equals raw × scale at baseline. An override replaced active exactly and
left raw unchanged.

NVVDD IDDQ sweep, MSVDD held at 10900:

| NVVDD active | REL | ALT | OV | VMIN |
| ---: | ---: | ---: | ---: | ---: |
| 36400 (baseline) | 1080000 | 1100000 | 1100000 | 745000 |
| 54600 | 1080000 | 1100000 | 1100000 | 745000 |
| 72800 | 1080000 | 1100000 | 1100000 | 745000 |
| 27300 | 1080000 | 1100000 | 1100000 | 745000 |

The policy did not move because nothing in the loaded VFE graph reads IDDQ:
variable 8 (runtime type 3) references variables 3 and 7, and none of the 205
loaded equations references variable 3, 7 or 8, nor does any other derived
variable. The RTX 5080's 120 mV REL response therefore depends on that BIOS's
equations. It is not a property of the input.

After restore no IDDQ value remained, and the fuse rows and rail policy matched
the baseline exactly.
