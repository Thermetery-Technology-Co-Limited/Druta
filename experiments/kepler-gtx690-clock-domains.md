# GTX 690: BIOS clock table and private telemetry

Verified 2026-09-07 on both GK104 cores (`0000:04:00.0`, `0000:05:00.0`),
Windows driver 472.12, VBIOS 80.04.1e.00.18. The supplied GTX690.rom was read
without modification. [Decoded ROM and live samples](kepler-gtx690-clock-domains.json)
record four samples per condition and the P0 hold/release results.

## ROM decoding

SHA-256: `25b0ad6624a4dc73c1fc53c465f089a2b1269db191a169c162e844a6b4af21ce`.
The 98,304-byte dump has an NVGI wrapper and a PCI image at file offset `0x400`.
The image is `0xF000` bytes with a valid checksum. BIT is at `0x5C0`; its P
payload at `0x6AF` points to the performance table at `0x6D55`.

The version-0x40 table has a 30-byte header, four states, 21-byte state headers,
and nine four-byte clock records per state. The low 14 bits encode stored MHz.
The following values reproduce the supplied Kepler BIOS Tweaker screenshot:

| State | GPC | XBAR | L2C | DDR | SYS | HUB | MSD | PWR | DISP |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P0/P2 | 1411 | 1481 | 1411 | 3004 | 1481 | 1080 | 540 | 324 | 540 |
| P5 | 1080 | 1134 | 1080 | 810 | 1134 | 1080 | 405 | 324 | 540 |
| P8 | 648 | 648 | 648 | 324 | 648 | 648 | 405 | 324 | 540 |

These columns have **BIOS indices 0-8**, not private NVAPI domain IDs.
The decoder follows Nouveau's [performance table parser](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/bios/perf.c)
and [GK104 clock sources](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/clk/gk104.c).
DISP and the familiar column names are cross-checked against the user's BIOS
Tweaker screenshot. The tool validates bounds and checksums, rejects unsupported
table shapes, and never accesses a GPU:

```powershell
python tools/decode_kepler_clocks.py C:\Users\Administrator\Downloads\GTX690.rom
```

## Live mapping

The controlled sequence was initial state, CUDA memory-copy load with normal
boost, the previously verified P0 hold while idle and loaded, then release.
Both cores produced the same held-P0 values. The first core reached P8 before
its initial capture; the second was still transitioning through P5. P0 holds
were released in `finally`; offset readback remained zero throughout. No fan,
voltage, offset, or ROM writes were made. Peak sampled temperature was 36 C. These brief captures establish clock correlation, not load stability.

| Private domain | P8, MHz | Held P0, MHz | Normal loaded boost, MHz | Druta label / evidence |
|---|---:|---:|---:|---|
| 4 | 324 | 3004.679 | 3004.679 | MEM: independently matches public memory clock |
| 15 | 648 | 1410.967 | 2403.870 | GPC2CLK: approximately twice public graphics clock 324 / 705 / 1201 MHz |
| 16, 17 | 648 | 1480.344 | 2403.870 | XBAR/SYS2CLK?: both follow the ROM's XBAR/SYS pair; individual order unresolved |
| 25 | 648 | 1410.967 | 2403.870 | L2C2CLK?: remaining doubled GPC-linked clock matches BIOS L2C |
| 18 | 648 | 1080 | 1080 | HUB?: matches the ROM's state-dependent HUB values |
| 21 | 405 | 540 | 540 | MSD?: matches BIOS MSD and public video telemetry |
| 20 | 324 | 324 | 324 | PWR?: matches the fixed BIOS PWR value |
| 6 | 540 | 540 | 540 | DISP?: matches fixed BIOS DISP, distinguishable from MSD at P8 |

`?` means an inferred name. Extra names use this GK104 reference when Kepler
is selected; they are not promoted to confirmed identities on other Kepler
chips. XBAR and SYS have identical stored values in all four ROM states and
identical live readings, so these observations cannot establish which is 16
and which is 17. Separate clock effects or an authoritative private-domain map
would resolve that ordering. Domains 5 (277.778/571 MHz), 8 (13.5/27 MHz),
9 (27 MHz), and 22 (108 MHz) remain unidentified; they are not assigned names
solely because a plausible frequency exists.

OpenHardwareMonitor's [NVIDIA clock reader](https://github.com/openhardwaremonitor/openhardwaremonitor/blob/master/Hardware/Nvidia/NvidiaGPU.cs)
independently uses raw index 8 for memory and raw index 30 divided by two for
graphics. With the two-word private array stride, those are domains 4 and 15.

## Application changes and scope

Druta now uses established primary domain identities per architecture before
considering frequency correlation. Idle core=MEM=324 MHz no longer names memory
as GPC. Unknown architectures require an unambiguous correlation. Missing or
zero slots do not inherit another clock's name. The monitor uses each domain's
explicit clock scale, so idle memory and PWR no longer receive doubled delta
thresholds just because they happen to be near GPC2CLK.

Every captured GK104 A value equalled B exactly, including loaded samples.
Thus B has not been established as an independent physical counter on GK104.
The monitor calls them reported arrays and qualifies the target/counter
interpretation as TU102 evidence; it no longer promises every GPU uses a
15 MHz grid. The 2CLK labels preserve the driver's doubled units.

This decodes clock telemetry. Private per-domain offset controls remain
**suppressed** (unavailable in Druta and therefore not shown): the control GET
still supplies no domain records, as documented in the
[separate control-layout probes](legacy-private-layout-47212.json).
The original ROM is not included in the source distribution.
