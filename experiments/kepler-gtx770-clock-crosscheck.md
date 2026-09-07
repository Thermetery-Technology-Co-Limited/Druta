# GTX 770 cross-check of GTX 690 clock-domain decoding

Measured 2026-09-07 on PNY GTX 770 (`0000:02:00.0`, PCI1184,
subsystem1033196e, driver472.12, VBIOS80.04.c3.00.01). These IDs identify the
measurement; **clock labels are selected by Kepler architecture, not by PCI
or subsystem ID**. [ROM decode and live samples](kepler-gtx770-clock-crosscheck.json)
retain original measurements and the pre-crosscheck labels.

## BIOS comparison

The supplied `GK104.rom` is 188,928bytes, SHA-256
`7c4f19ed1bf85fa8e69f597460448eab59e9f5b5161e5b5ec37459f1b30d63a1`.
The NVGI header points to its PCI image at0x600; the 0xF600-byte image has a
valid checksum. Its version0x40 performance table is at0x724e. The decoder
correctly follows a 30-byte table header, 25-byte state headers and nine
four-byte clock records, rather than assuming the GTX690's 21-byte state
header. All 36 stored frequencies match the supplied BIOS Tweaker screenshot.

| BIOS clock | GTX690 P0/P2 MHz | GTX770 P0/P1 MHz |
|---|---:|---:|
| GPC | 1411 | 1080 |
| XBAR | 1481 | 1165 |
| L2C | 1411 | 1115 |
| DDR | 3004 | 3505 |
| SYS | 1481 | 1134 |
| HUB | 1080 | 1080 |
| MSD | 540 | 540 |
| PWR | 324 | 324 |
| DISP | 540 | 540 |

Both boards have identical P5 table values. At P8, GTX770's GPC/XBAR/L2C/SYS
are810MHz versus GTX690's648MHz; the remaining P8 values match. Raw clock
words carry0x4000/0x8000 flags, which are kept separate from the low14-bit MHz
field. BIOS indices remain distinct from NVAPI indices. No ROM changes or
flashing were performed.

## Live discrimination

Initial P8 readings were collected, followed by normal CUDA load. Two
independent temporary `ForcePstate(0,2)` holds were then sampled while idle
and under CUDA load, each released with `(16,2)` in `finally`. Four samples
were taken per condition. Both cycles returned identical held-P0 frequencies
for the domains below; both release calls succeeded. Core/memory offsets
remained zero, and no fan or voltage writes were made. Peak sampled
temperature was42C. These short captures establish correlation, not stability.

| Domain | GTX690 held P0 MHz | GTX770 held P0 MHz | Kepler interpretation |
|---|---:|---:|---|
| 4 | 3004.679 | 3505.500 | MEM |
| 15 | 1410.967 | 1071.290 | GPC2CLK |
| 16 | 1480.344 | 1164.375 | XBAR2CLK? |
| 17 | 1480.344 | 1134.000 | SYS2CLK? |
| 25 | 1410.967 | 1113.750 | L2C2CLK? |
| 18 | 1080 | 1080 | HUB? |
| 21 | 540 | 540 | MSD? |
| 20 | 324 | 324 | PWR? |
| 6 | 540 | 540 | DISP? |

GTX770 separates the previously equal pairs:1164.375 matches XBAR1165,
1134 matches SYS1134, and1113.75 matches L2C1115. Domain15 agrees with twice
the public535MHz graphics reading within integer reporting; the programmed
clock need not equal the ROM's requested1080MHz exactly. Under normal boost,
public graphics reached1175MHz and domain15 was2351.612MHz; domain16/17 both
read2470.5MHz, showing why a boosted or idle equality alone cannot identify
the pair.

All sampled A/B words matched exactly, as on GTX690 and GM107. This does not
establish independent physical clock counters. Domains5,8,9,22 remain
unidentified. The separate GM107 absence of populated MSD/L2C rows remains
unchanged; Kepler's extra names are not transferred to empty Maxwell rows.

## Result in Druta

The **Kepler generation map** now labels16 as XBAR2CLK and17 as SYS2CLK,
including when a GTX690 reports the same value for both. Domain25 retains
L2C2CLK with stronger evidence from the GTX770 separation. The secondary
names retain `?` because they are ROM-correlated identities; MEM/GPC use
independent public-clock references. No device/subsystem allowlist selects
these labels. No private offset controls or new board-specific P0 button
eligibility are enabled by this decoding change.

The source, public evidence and regression tests are included in the modern
bundle and propagated to Windows7/Vista drafts. Measurements above were made
on Windows10. Legacy-runtime builds do not establish physical NVIDIA behavior
on those operating systems. The original ROM is not redistributed.
