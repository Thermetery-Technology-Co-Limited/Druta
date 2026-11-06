# MS shutdown on the RTX 3090 Ti Kingpin (GA102), 617.42

MS contract on a legacy-table ROM whose MS table has 15-byte rows (#52). Raw
results: [JSON](ampere-ga102-kingpin-ms-61742-20261105.json).

Scope: one board on one driver at idle. Configured controller membership, not
gating residency or power.

## ROM and contracts

The MS table (BIT P+0x84) has five 15-byte rows; rows 0 and 1 are enabled. Druta
now steps MS and DIFR-witness rows by their declared size. Read-only contracts:

| Change | Result |
| --- | --- |
| Row 0 alone | Refused: drops aggregate flag `0x40`, which needs a DIFR witness this card lacks |
| Row 1 alone | Refused: no observable configured-state effect |
| Rows 0 and 1 (shutdown) | Contract, schema 1 |

## Trial

Rows 0 and 1 → off: `0x8503E` `6d` → `6c` and `0x8504D` `2d` → `2c`, 15 bytes
apart. Firmware policy preserved. Applied and restored through Druta's registry
transaction.

| LPWR controller | Before | Projected | Observed | Restored |
| --- | ---: | ---: | ---: | ---: |
| 0 (GR) | `0x100` | `0x100` | `0x100` | `0x100` |
| 1 | `0x100` | `0` | `0` | `0x100` |
| 4 (MS) | `0x100` | unsupported | unsupported | `0x100` |
| 5 | `0x101` | `0x101` | `0x101` | `0x101` |
| 6, 16 | unsupported | unsupported | unsupported | unsupported |

After restore the running image hash matched the original and no managed
registry values remained.
