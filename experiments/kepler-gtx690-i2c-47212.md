# GTX 690 read-only I2C discovery on 472.12

NCP4206 was identified at port 2 / address 0x20 on each core by controller registers. This survey issues only reads; the separate validation record contains the bounded Verify/write/restore checks. The earlier PAGE-only presence test missed NCP4206; this corrected survey also tries MFR_ID and READ_VOUT.

## PCI slot 0000:04:00.0

## I2C survey - NVIDIA GeForce GTX 690

- PCI device: `0x1188`   subsystem: `0x84061043`
- driver: `472.12`   VBIOS: `80.04.1e.00.18`
- vcore at scan time: `None mV` (run under load for a meaningful number)
- sweep: ports [0, 1, 2, 3, 4, 5, 6, 7], 68 addresses

Read-only survey. This tool issues no I2C writes.

### Responders: 3

#### port 2, address `0x20`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `41` (`A`) |
| `0x9A` | MFR_MODEL | `98 32` (`.2`) |
| `0x9B` | MFR_REVISION | `01` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | *not implemented* |
| `0x20` | VOUT_MODE | `20` (` `) |
| `0x8B` | READ_VOUT | `5E 00` (`^.`) |
| `0x8C` | READ_IOUT | `2C BA` (`,.`) |
| `0x8D` | READ_TEMPERATURE_1 | *not implemented* |
| `0x98` | PMBUS_REVISION | *not implemented* |
| `0x24` | VOUT_MAX | *not implemented* |

#### port 2, address `0x38`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `00` |
| `0x9A` | MFR_MODEL | `00 00` |
| `0x9B` | MFR_REVISION | `00` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | `00` |
| `0x20` | VOUT_MODE | `00` |
| `0x8B` | READ_VOUT | `00 00` |
| `0x8C` | READ_IOUT | `00 00` |
| `0x8D` | READ_TEMPERATURE_1 | `00 00` |
| `0x98` | PMBUS_REVISION | `00` |
| `0x24` | VOUT_MAX | `00 00` |

> Every register reads `00`. Same conclusion as all-`FF`: nothing here is answering with content.

#### port 2, address `0x4F`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `00` |
| `0x9A` | MFR_MODEL | `00 9E` |
| `0x9B` | MFR_REVISION | `00` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | `00` |
| `0x20` | VOUT_MODE | `66` (`f`) |
| `0x8B` | READ_VOUT | `00 57` (`.W`) |
| `0x8C` | READ_IOUT | `00 41` (`.A`) |
| `0x8D` | READ_TEMPERATURE_1 | `00 2A` (`.*`) |
| `0x98` | PMBUS_REVISION | `00` |
| `0x24` | VOUT_MAX | `00 58` (`.X`) |

### What to do with this

Identifying the part is YOUR step and it needs a datasheet. Match the bytes above against candidate parts, then copy `i2c/TEMPLATE.toml` and fill it in with page citations. If nothing above names a part, say so in the issue rather than guessing - a profile built on a guessed identity is worse than no profile, because it will be trusted.


## PCI slot 0000:05:00.0

## I2C survey - NVIDIA GeForce GTX 690

- PCI device: `0x1188`   subsystem: `0x84061043`
- driver: `472.12`   VBIOS: `80.04.1e.00.18`
- vcore at scan time: `None mV` (run under load for a meaningful number)
- sweep: ports [0, 1, 2, 3, 4, 5, 6, 7], 68 addresses

Read-only survey. This tool issues no I2C writes.

### Responders: 1

#### port 2, address `0x20`

| reg | name | bytes read |
| --- | --- | --- |
| `0x99` | MFR_ID | `41` (`A`) |
| `0x9A` | MFR_MODEL | `98 32` (`.2`) |
| `0x9B` | MFR_REVISION | `01` |
| `0xBE` | MFR_PMBUS_ADDR (MPS) | *not implemented* |
| `0x20` | VOUT_MODE | `20` (` `) |
| `0x8B` | READ_VOUT | `5E 00` (`^.`) |
| `0x8C` | READ_IOUT | `2C BA` (`,.`) |
| `0x8D` | READ_TEMPERATURE_1 | *not implemented* |
| `0x98` | PMBUS_REVISION | *not implemented* |
| `0x24` | VOUT_MAX | *not implemented* |

### What to do with this

Identifying the part is YOUR step and it needs a datasheet. Match the bytes above against candidate parts, then copy `i2c/TEMPLATE.toml` and fill it in with page citations. If nothing above names a part, say so in the issue rather than guessing - a profile built on a guessed identity is worse than no profile, because it will be trusted.

