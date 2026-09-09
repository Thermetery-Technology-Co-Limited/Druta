# RTX 5080 Astral MP29816 investigation

These measurements are from the ASUS RTX 5080 Astral on NVIDIA 580.97,
VBIOS 98.03.3b.c0.6f, on 2026-09-08. They establish telemetry and rejected
write attempts, not working voltage adjustment.

The detected device is on logical port 2, 7-bit address 0x30. Identity,
PAGE 0 and scale word 0x0420 were checked around transactions. The profile
provides measured NVVDD and an experimental sourced offset mapping at
command 0x22 in 5 mV steps. The production UI requires successful loaded
verification in the current session before permitting offset Apply. See
[profile contract](../../i2c/MP29816-ASTRAL.md).

The [correlation capture](mp29816-correlation.json) records read-only
regulator observations during a bounded PCI-selected CUDA load. The
[production writer check](mp29816-production-writer.json) attempted +5 mV;
NVAPI rejected the write, offset word 0x0000 remained unchanged and
verification remained inconclusive. These results must not be presented
as a functioning voltage-control path.

A [minimal post-reboot check](mp29816-post-reboot-minimal.json) initialized
NVAPI without a GPU/Rail object or the Druta UI. A same-value 0x22 write
still failed; identity, PAGE, scale and offset readbacks remained intact.
The [read matrix](mp29816-nv40-i2c-read-matrix.json) established that explicit
100 kHz settings allowed NV40_I2C buffer/word reads. After reboot, both
[SMBus-word](mp29816-nv40-i2c-post-reboot-100khz-smbus-same-value.json) and
[buffer](mp29816-nv40-i2c-post-reboot-100khz-buffer-same-value.json)
same-value writes returned RM 0x10 (NV_ERR_ILLEGAL_ACTION).

That status does not identify the internal source of rejection. These
observations provide no evidence that Rail object use had locked the bus,
that reboot enabled writes, or that a controller password is required.
An accepted transaction or external bus trace is still needed to identify
the missing prerequisite. No further writes were performed for this PR.

The public JSON files remove device UUIDs and process transport handles.
Bulk unused arrays are represented by word counts and SHA-256 hashes.
Original capture hashes preserve provenance. Driver dumps, vendor XML,
DLLs, datasheets, upstream source and private session records are excluded.
The [correlation script](../correlate_mp29816.py) retains its load bound,
board selection and refusal to overwrite earlier evidence.
