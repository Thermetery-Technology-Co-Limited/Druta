# GTX 745 DDR3 (GM107): ROM and private clock domains

Verified 2026-09-07 on `0000:02:00.0`, driver 472.12, VBIOS
82.07.32.00.6a. The supplied `GM107_edited.rom` and Maxwell II BIOS Tweaker
screenshot provide distinct XBAR, L2C and SYS P0 targets. These are edited
ROM values, not claims about factory clocks. The live held-P0 values match
the supplied targets within clock quantization; no ROM was flashed or modified
during this work. [ROM decode and live samples](maxwell-gtx745-clock-domains.json)
retain the observations separately from the naming interpretation below.

## ROM table

SHA-256: `a3e50225b6ee8f10ddda8c811e1fb70199fd3ec77e87c2c506606eab470dfa0e`.
File size: 168,448 bytes. The NVGI header at `0x14` declares the PCI image at
`0x600`, unlike the GTX 690 dump's `0x400`. Image length is `0xF600`, checksum
zero, PCI `10DE:1382`. BIT is at `0x7C0`, its P payload at `0x8C1`, and the
version-0x40 performance table at `0x73D0`. It has a 33-byte table header,
32-byte state headers and nine four-byte clock records. Two of its four
state slots are disabled (`0xFF`); they are retained explicitly in the decode.

| State | GPC | XBAR | L2C | DDR | SYS | HUB | MSD | PWR | DISP |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P0 | 1080 | 1165 | 1130 | 900 | 1120 | 1080 | 540 | 324 | 648 |
| P8 | 810 | 810 | 810 | 405 | 810 | 648 | 405 | 324 | 405 |

Stored MHz use the low 14 bits, matching Nouveau's
[performance table parser](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/bios/perf.c).
The first five P0 fields also set `0x4000`, which is retained as a separate
flag rather than added to frequency. BIOS indices 0-8 are not NVAPI indices.
The decoder retains its existing command name and now accepts both validated
NVGI header offsets, with pointer bounds, signature and checksum checks:

```powershell
python tools/decode_kepler_clocks.py C:\Users\Administrator\Documents\GM107_edited.rom
```

## Controlled live readings

Four samples were taken in each condition: initial P8, normal CUDA load,
verified P0 hold while idle, the same hold under CUDA load, and after release.
The hold uses the previously verified GTX 745 ForcePstate path, not a new
clock setter. It was released in `finally`. Clock offsets stayed zero and no
fan, voltage or ROM writes were made. This is a brief correlation test, not
an overclock stability test.

| Private domain | Initial idle MHz | Held P0 MHz | Loaded boost MHz (last sample) | Interpretation |
|---|---:|---:|---:|---|
| 4 | 405 | 900.001 | 900.001 | MEM, matches public memory clock |
| 15 | 270 | 1079.578 | 2064.550 | GPC2CLK, twice public graphics clock within integer reporting |
| 16 | 810 | 1164.375 | 1899.310 | XBAR2CLK?, matches ROM XBAR 1165 |
| 17 | 810 | 1118.571 | 1858.235 | SYS2CLK?, matches ROM SYS 1120 |
| 6 | 405 | 648 | 648 | DISP?, matches both ROM states |
| 18 | 648 | 1080 | 1080 | HUB?, matches both ROM states |
| 20 | 324 | 324 | 324 | PWR?, fixed ROM PWR value |

At held P0, XBAR and SYS differ by about 45.8 MHz, separating the pair that
was indistinguishable in the GTX 690's ROM and captures. The distinct 1130 MHz
L2C target does not appear in a populated row: domain 25 is absent. Domain 21
is empty and no populated row matches MSD's 540 MHz P0 target. These two
BIOS entries therefore receive no private-telemetry label. Domains 5
(277.778/571.428 MHz), 8 and 9 (27 MHz), and 22 (108 MHz) remain unidentified.

Public graphics reported 135 MHz at initial idle, 539 MHz at held P0, and
1032 MHz in the last normal-load sample. The initial GPC2CLK270 reading is
below the BIOS P8 target810: a performance-state table is not a guaranteed
instantaneous clock. Normal boost also exceeds the BIOS P0 GPC target.

All captured A and B values were bit-identical. Thus neither these readings
nor their zero delta establishes an independent physical counter on GM107.
The monitor now scopes its target/counter explanation accordingly.

## Application scope

Maxwell receives the above inferred labels with `?`, preserving the distinction
between ROM correlation and independently confirmed primary clock identities.
Only identified doubled domains receive scale2 delta thresholds. The GTX690
Kepler XBAR/SYS ordering remains explicitly unresolved; this Maxwell finding
is not silently applied to a different architecture. L2C/MSD are not invented
when their rows are missing. No private offset controls are enabled by these
telemetry labels; such controls remain **suppressed** (unavailable in Druta and
therefore not shown) on the tested GM107 path.

The decoder, monitor changes, tests and public evidence are also propagated
into the Windows7/Vista drafts. Live hardware measurements above are from
Windows10; legacy-runtime build checks do not establish physical GPU behavior
on those operating systems. The original ROM is not redistributed.
