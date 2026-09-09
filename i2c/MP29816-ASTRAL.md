# ASUS RTX 5080 Astral / MP29816

Druta supports **core-voltage monitoring** at NVAPI internal I2C **port 2**,
**7-bit address 0x30** (NVAPI address field 0x60), using
`asus-rtx5080-astral-mp29816.toml`. The profile also defines an **experimental
voltage-offset write function**. The tested NVIDIA driver rejects these writes;
physical voltage control is not verified. Druta requires successful loaded
verification in the current session before Apply can dispatch an offset.
The only permitted write is the offset word at `0x22`. PAGE, configuration,
protection and nonvolatile writes are excluded, including in XOC mode.

## Identification

Measured 2026-09-08 on PCI device `2C02`, subsystem `89DE1043`, VBIOS
`98.03.3b.c0.6f`, NVIDIA driver `580.97`. The owner physically identifies the
fitted controller as MP29816. Count-prefixed manufacturer block responses:

| Command | Complete bytes (count first) |
|---|---|
| 0x99 MFR_ID | `03 53 50 4D` |
| 0x9A MFR_MODEL | `06 36 31 38 3A 32 4D` |
| 0x9B MFR_REVISION | `02 01 00` |
| 0xAD device ID | `04 16 A8 02 00` |

The manufacturer payload is numeric MPS code `0x4D5053`. The model payload
reverses to `M2:816`. Command `0xAD` returns payload `0x0002A816`, matching
the exact MP29816 detection entry in ElmorLabs EVC2 (annotated `MPS29816A`).
The profile combines this device ID with the measured board fingerprints and
PCI candidate filtering. Other boards and revisions remain untested.

## Measured voltage decoding

The [upstream Linux MP2869-family driver](https://github.com/torvalds/linux/blob/master/drivers/hwmon/pmbus/mp2869.c)
(GPL-2.0-or-later) explicitly supports MP29816. `0x29[12:10] = 1` selects
5 mV per count; READ_VOUT `0x8B` uses its lower 12 bits. This board returned
`0x29 = 0x0420` and PAGE `0x00 = 0`. The profile requires those PAGE and scale
conditions and checks them before and after telemetry. It never selects PAGE.

A PCI-selected CUDA memory-copy load ran for 5.17 seconds without tuning or
I2C writes. All 40 loaded samples returned **1040 mV**; NVAPI returned
**1035–1040 mV**, with **0.625 mV mean absolute difference**. Across 70 samples
before, during and after the load, all manufacturer/model/revision, PAGE and
scale checks remained stable. This correlation supports identifying PAGE 0
as the core rail; it is not independent electrical calibration.

Idle transitions produced larger differences (800–1040 mV at the controller
while NVAPI reported 815–820 mV). Sensor timing or averaging may explain this;
the measurements do not establish the cause. The UI shows controller voltage
directly rather than interpreting idle disagreement as an applied offset.

Raw current and temperature were also read during discovery. Their conversion
has not been validated on this board, so neither is exposed. The other loop
has not been selected or identified.

## Offset mapping and write results

The public [ElmorLabs EVC2 release](https://elmorlabs.com/forum/topic/evc2-beta-software/)
provides device profiles. Its
[1.0.1.18_t6 archive](https://1drv.ms/u/s!Atmpv-6qHr_67tU2rVOcJMjcXyyH5A?e=g2VV9R)
contains `I2C_DEVICES/MP29816.xml`, which specifies:

| Field | Loop 1 VID offset |
|---|---|
| PAGE | 0 |
| Command / transaction | `0x22`, two-byte word |
| Offset field | Bits 7:0, signed two's complement |
| Step | 5 mV when `0x29[12:10] = 1` |
| Other bits | Preserved from the original word |

The normal envelope is -50 to +50 mV. The signed encoding represents -640 to
+635 mV; those endpoints are encoding limits, not a validated operating range.
The profile's verification steps are +5, +10, +15 and +20 mV relative to the
entry offset. Each request checks identity, PAGE, scaling, the full word and
voltage bounds. Verification restores and checks the exact entry word. Failed
restoration cannot unlock Apply. A rejected write whose readback still equals
the entry word needs no additional restore write.

Bench results on driver 580.97:

- A +5 mV request was rejected by `NvAPI_I2CWriteEx` (status -1); readback stayed
  `0x0000`. Further offset steps were not attempted.
- Same-value word writes also failed, including ordinary and packed NVAPI
  transactions. EVC2's normal SMBus path uses an ordinary word write with no
  PEC or unlock sequence. An explicit PEC trial also failed.
- NVIDIA's public RM `I2C_READ_BUFFER` (`0x20800601`) returned the exact device
  ID. `I2C_WRITE_BUFFER` (`0x20800602`) returned `0x40`, named
  `NV_ERR_INVALID_STATE`, for a same-value offset write, both with ordinary
  flags and the documented privileged-access flag. See NVIDIA's
  [I2C interface](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/common/sdk/nvidia/inc/ctrl/ctrl2080/ctrl2080i2c.h)
  and [status definitions](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/common/sdk/nvidia/inc/nvstatuscodes.h).
- The separate `NV40_I2C` object (`0x402c`), captured from normal NVAPI
  initialization and matched to the live GPU client/subdevice, also reads the
  exact device ID. Its indexed write (`0x402c0102`) returns `NV_ERR_GENERIC`.
- After a user-requested Windows reboot, a minimal NVAPI test still rejects the
  unchanged-value write before constructing Druta's GPU/Rail objects or opening
  Druta. A Windows restart therefore did not clear the observed failure.
- `NV40_I2C` transaction command `0x402c0105`, using explicit 100 kHz flags,
  successfully reads the device ID/configuration and offset. Both its SMBus
  word and I2C buffer write variants return `0x16`, `NV_ERR_ILLEGAL_ACTION`, for
  the unchanged offset. Readback remains zero and all identity checks pass.
  Its default-speed flags instead return `NV_ERR_INVALID_ARGUMENT`; this was
  resolved by selecting 100 kHz for the read/write comparison. These contracts
  are documented in NVIDIA's
  [NV40_I2C interface](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/src/common/sdk/nvidia/inc/ctrl/ctrl402c.h).
- The alternate register and byte interfaces did not pass the device-ID read
  check, so no offset data was written through those paths.

These results establish a rejected driver write path. They do not establish
whether rejection occurs before bus transmission, whether a controller lock is
involved, or whether another driver or external adapter would succeed. No
physical voltage change has been demonstrated.

The remaining diagnostic requirement is evidence that distinguishes driver
rejection from controller or bus behavior: a successful external-controller
transaction, a bus trace of the failed word write, or a documented prerequisite
for this board/controller. A Windows restart does not establish that all
controller power was removed. The supplied error codes alone do not justify
writing guessed unlock, PAGE or protection registers.

The EVC archive SHA-256 is
`3b194c8e6d3234d359936dd6d1aa82ce00e89809251f4e1525e9d04a1b03f481`;
the XML SHA-256 is
`a13af80d2a49fd6a1ed2bc9fab89cd1d046973cef4e7babe45b8a23335b9c0a8`.
Only register facts are transcribed into Druta; the third-party archive and XML
are not redistributed.

## Local evidence

The development workspace preserves the 896-address read-only survey in
`experiments/power-5080-20260908/i2c-full.json`, its interpretation in
`I2C-SCAN-RESULT.md`, and all 70 correlation samples in
`mp29816-correlation.json` in the same directory. Reproduction scripts are
`experiments/probe_5080_i2c.py` and `experiments/correlate_mp29816.py`.
Write results are in `mp29816-offset-probe.json`, `mp29816-rm-i2c.json`,
`mp29816-rm-i2c-privileged.json` and `mp29816-production-writer.json` in the
same evidence directory. The production verification at 1040 mV attempted
one +5 mV write, returned failure, and confirmed the original zero word and
all identity checks afterward. Its bounded load ran for 1.19 seconds.
The integration script is `experiments/validate_mp29816_writer.py`.
These raw research captures are not included in the distributable.
