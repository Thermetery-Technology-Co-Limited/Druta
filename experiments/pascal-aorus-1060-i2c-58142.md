# AORUS GTX 1060 Xtreme 9Gbps read-only I2C survey on 581.42

Surveyed 2026-10-10 on the installed Gigabyte AORUS GeForce GTX 1060 Xtreme Edition 6G 9Gbps before the card was removed. Both passes use `NvAPI_I2CReadEx` only. No I2C write, BAR0 map, or kernel driver was loaded.

The board is the one named in published teardowns as using a uP9511P for the core rail. The public uP9511 document behind that marking, uP9511-DS-C3100, describes PWMVID and hardware straps. Its pin list has no SDA or SCL. uP9512 parts are the ones that add those pins. Druta's uP9512R identity is register `0x27 = 0x00` and `0x28 = 0x2B`. Nothing on this bus returned that pair.

## Card

- GPU: NVIDIA GeForce GTX 1060 6GB, GP106
- PCI device: `0x1C03` subsystem: `0x374A1458` (subsystem vendor 0x1458, device 0x374A)
- driver: `581.42` VBIOS: `86.06.4b.00.32`
- vcore at the address sweep: `1081.25 mV`, idle

## Address sweep

`python -m druta.tools.i2c_discover --full`

Ports 0–7, all 112 unicast addresses `0x08`–`0x77`. Presence is any answer from command `0x00`, `0x99`, or `0x8B`.

### Responders: 8

#### port 1, address `0x48`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `99` |
| `0x9A` | MFR_MODEL | *not implemented* |
| `0x9B` | MFR_REVISION | *not implemented* |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | *not implemented* |
| `0x20` | VOUT_MODE | *not implemented* |
| `0x8B` | READ_VOUT | *not implemented* |
| `0x8C` | READ_IOUT | *not implemented* |
| `0x8D` | READ_TEMPERATURE_1 | *not implemented* |
| `0x98` | PMBUS_REVISION | *not implemented* |
| `0x24` | VOUT_MAX | *not implemented* |

#### port 2, address `0x40`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `FF` |
| `0x9A` | MFR_MODEL | *not implemented* |
| `0x9B` | MFR_REVISION | `F8` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | `54` |
| `0x20` | VOUT_MODE | `74` |
| `0x8B` | READ_VOUT | `7F F8` |
| `0x8C` | READ_IOUT | `7F F8` |
| `0x8D` | READ_TEMPERATURE_1 | `00 00` |
| `0x98` | PMBUS_REVISION | `FF` |
| `0x24` | VOUT_MAX | `2F 78` |

#### port 6, address `0x30`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `00` |
| `0x9A` | MFR_MODEL | `FF FF` |
| `0x9B` | MFR_REVISION | `00` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | `AF` |
| `0x20` | VOUT_MODE | `27` |
| `0x8B` | READ_VOUT | `00 00` |
| `0x8C` | READ_IOUT | `12 23` |
| `0x8D` | READ_TEMPERATURE_1 | `00 37` |
| `0x98` | PMBUS_REVISION | `21` |
| `0x24` | VOUT_MAX | `3B 73` |

#### port 6, address `0x37`

Every probed register reads `00`. An address acknowledgement without content.

#### port 6, address `0x3A`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `00` |
| `0x9A` | MFR_MODEL | `00 00` |
| `0x9B` | MFR_REVISION | `00` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | `00` |
| `0x20` | VOUT_MODE | `01` |
| `0x8B` | READ_VOUT | `00 00` |
| `0x8C` | READ_IOUT | `00 00` |
| `0x8D` | READ_TEMPERATURE_1 | `00 00` |
| `0x98` | PMBUS_REVISION | `00` |
| `0x24` | VOUT_MAX | `89 AB` |

#### port 6, address `0x50`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `00` |
| `0x9A` | MFR_MODEL | `0C 00` |
| `0x9B` | MFR_REVISION | `00` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | `62` |
| `0x20` | VOUT_MODE | `0E` |
| `0x8B` | READ_VOUT | `4C 3F` |
| `0x8C` | READ_IOUT | `3F 60` |
| `0x8D` | READ_TEMPERATURE_1 | `60 61` |
| `0x98` | PMBUS_REVISION | `6B` |
| `0x24` | VOUT_MAX | `CF 00` |

`READ_VOUT`, `READ_IOUT`, and `READ_TEMPERATURE_1` are the same buffer shifted by one byte. The device is ignoring the command byte. Address `0x50` is the usual EEPROM slot. These bytes are not a voltage reading.

#### port 6, address `0x54`

Every probed register reads `00`. An address acknowledgement without content.

#### port 6, address `0x58`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `00` |
| `0x9A` | MFR_MODEL | `00 00` |
| `0x9B` | MFR_REVISION | `00` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | `01` |
| `0x20` | VOUT_MODE | `EF` |
| `0x8B` | READ_VOUT | `00 04` |
| `0x8C` | READ_IOUT | `04 00` |
| `0x8D` | READ_TEMPERATURE_1 | `00 00` |
| `0x98` | PMBUS_REVISION | `00` |
| `0x24` | VOUT_MAX | `00 09` |

## uPI identity sweep

A second read-only pass read `0x27`, and `0x28` when `0x27` answered, at every unicast address on ports 0–7. Seven addresses returned `0x27`. Port 1 address `0x48` did not.

| port | address | `0x27` | `0x28` |
| --- | --- | --- | --- |
| 2 | `0x40` | `32` | `7F` |
| 6 | `0x30` | `00` | `4F` |
| 6 | `0x37` | `00` | `00` |
| 6 | `0x3A` | `EF` | `FE` |
| 6 | `0x50` | `00` | `71` |
| 6 | `0x54` | `00` | `00` |
| 6 | `0x58` | `E2` | `28` |

No row is `00` / `2B`.

## Reading

The GPU I2C master answers. None of the responders is the uP9512R Druta already speaks, and none of the PMBus identification bytes is an NCP4206, MP2888A, or MP29816 fingerprint. The core controller named for this board has no SMBus pins in its public datasheet, which agrees with the absence of a regulator identity on the bus.
