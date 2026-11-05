# Adapter and interface matrix

This matrix records what each tested adapter has shown for each Druta
interface, as of 1.9.08. It keeps source qualification, live reads and
changed-state tests apart. Each cell covers one board on the drivers in the key,
not its generation. A W means the request was written, read back exactly and
restored. It does not mean the hardware responded physically. Druta decides
support at runtime from capability checks; this matrix is evidence, not an
allowlist.

## Legend

| Mark | Meaning |
| --- | --- |
| S | Source or static qualification only: driver disassembly, packet geometry, code analysis. Nothing ran on this adapter. |
| R | Live read-only observation on this adapter. Nothing was changed. |
| W | Changed-state test on this adapter: a real write, exact readback and verified restoration. |
| N | Tested and unavailable or refused on this adapter. |
| blank | No evidence. |

S cells for BLCG, SLCG and the individual inhibits mark that generation's
layout, bound from driver source. The same S appears under every board of that
generation. An N from RM `0x1F` is a size refusal: Druta has no layout that
driver accepts, which is not proof the interface is absent.

## Adapters

| Key | Adapter | Drivers with evidence |
| --- | --- | --- |
| 770 | GTX 770 (GK104) | 472.12 |
| 690 | GTX 690, both cores (GK104) | 472.12 |
| 745 | GTX 745 OEM, DDR3 (GM107) | 472.12 |
| TXp | TITAN Xp, stock board (GP102) | 472.12, 566.36, 580.97, 582.66 |
| 1060 | Gigabyte AORUS GTX 1060 Xtreme 6G 9Gbps (GP106) | 472.12, 516.94, 572.83, 576.80, 581.42 |
| TRTX | TITAN RTX on an ASUS RTX 2080 Ti Strix PCB (TU102) | 472.12, 566.36, 580.97, 610.88 |
| 2080S | Gigabyte RTX 2080 SUPER AORUS Xtreme, shunt and 16 GB mods (TU104) | 431.60, 472.12, 581.42, 621.01 |
| 3070Ti | ASUS Strix RTX 3070 Ti, shunt mod (GA104) | 595.97 |
| KP | EVGA RTX 3090 Ti Kingpin, water-cooled (GA102) | 617.42 |
| 4080S | Manli RTX 4080 SUPER (AD103) | 581.42, 617.42 |
| 5080 | ASUS RTX 5080 Astral (GB203) | 580.97, 610.88, 617.14, 617.42, 621.01 |

## Matrix

| Interface | 770 | 690 | 745 | TXp | 1060 | TRTX | 2080S | 3070Ti | KP | 4080S | 5080 |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| **Clocks and V/F** | | | | | | | | | | | |
| Core and memory offsets | W | W | W | W | R | W | W | | R | W | W |
| Per-domain clocks (XBAR, SYS, VIDEO, LTC, extra MEM) | R | N | R | W¹ | | W | | W | R | W | W |
| V/F curve edit, point lock and hold | N | N | N | W | R | W | R | | R | W | W |
| NVML frequency lock | N | N | N | N | | W | | |  | W | W |
| Legacy P0 hold and max fan | W | W | W | | | | | |  | | |
| V/F hold headroom | | | | | | W | | W |  | W | |
| Clock V/F policy (monotonicity, smoothing) | | | | | N | | | | W | | W² |
| Memory timings (nvtune) | W | W | W | W | | N | | |  | N | R |
| **Voltage** | | | | | | | | | | | |
| Voltage boost | N | N | N | W | R | W | W | | R | W | W |
| NVVDD offset | | N | | W | R | W | | W | R | W | W |
| NVVDD rail limits (REL, ALT, OV, VMIN) | | N | | W | R | W | R | W | R | W | W |
| MSVDD rail | | N | | N | N | N | | N | N | N | W³ |
| Six-word rail requests | | | | | N | | | W | R | | W⁴ |
| Voltage-request gate | | | | | R | | | R | R | | W |
| `EnableCoreVoltage` registry startup gate | | | | | N | | | |  | | N |
| VFE equations: constants, REL clamps and bounds, startup REL | | | | | N | | | | N | | W |
| Independent VFE policy offsets | | | | | N | | | | N | | W |
| PERF-limit voltage request, PWM switch delay | | | | | | | | |  | | W⁵ |
| **Power and current** | | | | | | | | | | | |
| Board power limit | N | N | N | W | W⁸ | W | W | | W | W | W |
| Live client power above reported max (XOC) | | | | | W⁸ | | W | W¹⁰ | W¹⁰ | S | W |
| Current limits and named power policies | N | | N | W | W⁸ | W | W | W | R | W | W |
| Power-policy feedback bounds | | | | | N | | | | R¹¹ | | W⁶ |
| Power relationships: weight, linked base current | | | | | N | | | | R¹¹ | | W |
| **Low power** | | | | | | | | | | | |
| GR power gating (GRPG) entry disables (registry) | | | | | N | | | | R | | W |
| MS, NVDEC, NVENC, OFA, NVJPG entry disables (registry) | | | | | N | | | | R | | W |
| DIFR (entry disable, live inhibit) | | | | | N | | | | N | | W |
| Grouped engine-level power gating (ELPG) hold | | | | | R | | | | R | | W |
| Engine clock gating (ELCG) hold | | | | | R | | | | R | | W |
| Block clock gating (BLCG) hold | | | | | N | S | S | S | R | S | W |
| Second-level clock gating (SLCG) hold | | | | | N | S | S | S | R | S | W |
| Individual power-gating inhibits | | | | | N | | | S | R¹² | S | W |
| Idle thresholds | | | | | N | | | | N | | W |
| Adaptive power policy | | | | | N | | | | N | | W |
| **Other** | | | | | | | | | | | |
| PCIe link speed | | | | | N | | | | N | | W⁷ |
| Fan manual and Auto | W | W | W | W | R | W | W | | R | W | W |
| `RmFan2XOverride` fan-init selector (registry) | | | | | W⁹ | | | | W⁹ | | W⁹ |
| Registry and BIOS overrides, Undo, recovery | | | | | | | | R |  | | W |
| Profiles and Undo | W | W | W | W | | W | | | W¹³ | W | W |

1. Additional Memory Clock only. XBAR, SYS and VIDEO are hidden on Pascal
   because the measured clocks did not follow.
2. The registry flags are W. A live disable was refused (RM 0x56) in the default
   startup mode and accepted after the registry change.
3. Rail limits are W on both rails. The MSVDD requested offset stores, but no
   voltage response has been shown.
4. W on 617.14 and 617.42. On 621.01 the SET reported success and the request
   did not change.
5. The PERF-limit VMIN request stored in CONTROL and STATUS. The reported voltage
   policy did not change.
6. Upper and lower bounds are W. A sample-count write returned RM 0x65 during a
   TDR, so counts stay read-only.
7. A link-width request was refused (RM 0x56). An LTR request returned success,
   and the register did not change.
8. W on 472.12, 516.94, 572.83 and 576.80. On 581.42 the result is R only.
9. DWORD 1 was consumed: after a GPU restart the public fan readbacks and
   controls were unavailable. Restoring the absent value returned them. Physical
   fan behavior is unmeasured.
10. Native board status equals floor((request × policy-2 upper word + 2048) / 4096)
    mod 2^20 at every request tested from 850 to 1,999 W, with this board's word
    4424. The 3070 Ti's 33/32 fit corresponds to a word of 4224.
11. Raw bounds or weights read. Ampere policy rows are not decoded, so Druta
    reports the controls unavailable.
12. GR and MS only; the media inhibits are unavailable.
13. Clock V/F policy profiles and selected Undo only.

Evidence from 2026-11-05: [GTX 1060, 581.42](experiments/pascal-gp106-58142-capability-20261105.md); [RTX 3090 Ti Kingpin, 617.42](experiments/ampere-ga102-kingpin-61742-20261105.md).

## I2C regulators

| Board, driver | Controller | Result | External meter |
| --- | --- | :-: | --- |
| GTX 770, 472.12 | NCP4206 | W | |
| GTX 690, both cores, 472.12 | NCP4206 | W | |
| GTX 745, 472.12 | None found | N | |
| TITAN Xp, stock board | None found | N | |
| TITAN Xp on a GTX 1080 Ti Poseidon PCB, 582.66 | NCT3933U | W | 1.128 → 1.119 V |
| AORUS GTX 1060 Xtreme, 581.42 | None; uP9511P core rail is PWM-VID | N | |
| TITAN RTX, 472.12 | MP2888A, VID offset | W | |
| RTX 3070 Ti, 595.97 | MP2888A, PMBus command | W | 1.049 → 1.074 → 1.049 V |
| EVGA RTX 3090 Ti Kingpin | iCX | W | A 1295 mV request read 1.285 V (user) |
| RTX 4080 SUPER, 581.42 and 617.42 | uP9512R | W (Verify only) | |
| RTX 5080, 580.97 | MP29816 | R reads, N writes | |

A controller's own ADC or VMON reading is not an external measurement. The only
physical measurements are the three meter readings above and an owner-reported,
uncalibrated FurMark A/B test of the 2080S extended power limit on 581.42.

## Not covered

- Other boards, including the HOF TITAN RTX, and the Windows 7 and Vista branches.
- Physical enforcement of any power or current request, gating residency or
  savings, and regulator output for the driver-side voltage controls.
- 4080 SUPER: uP9512R Apply, Reset and profile replay from the UI; Max all;
  live client-power writes.
- 2080 SUPER four-digit power requests. Activation of the 3070 Ti persistent
  power override.
- Paths with no working qualification anywhere: ELPG and BLCG ENABLE, the second
  idle threshold, ASPM, `EnableCoreVoltage`, VCRIT effective thresholds,
  arbitrary six-word replay, live relationship-weight SET, enabling
  factory-disabled DIFR.
