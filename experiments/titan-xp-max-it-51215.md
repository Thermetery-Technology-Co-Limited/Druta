# TITAN Xp Max it on driver 512.15

Druta 1.10.0 stopped before its first Max it write on the TITAN Xp at PCI
`0000:03:00.0` (VBIOS `86.02.3d.00.01`). Its board-power slider showed the
250 W request but was disabled. Both operations require a snapshot of the
coupled policy requests so Undo can restore them.

## Distinguishing two layouts on the same transport

The driver's initial power query used command `0x20802612`, 3548 packet bytes
and 3480 parameter bytes. That signature is also present on a
[previously measured GP106](pascal-gp106-driver47212-20261102.md).
This TITAN Xp accepted different parameter sizes on the same command family.

| Read-only query | Prior GP106 measurement | TITAN Xp measurement |
| --- | ---: | ---: |
| INFO `0x20802618` | 5520 bytes | 5392 bytes |
| STATUS `0x20802619` | 69148 bytes | 70684 bytes |
| CONTROL `0x2080261A` | 2124 bytes | 2124 bytes |

The TITAN Xp rejected the longer INFO and shorter STATUS with RM `0x3A`.
The accepted INFO, STATUS and CONTROL returned mask `0xEDBF`. Record types
agreed at all 13 occupied positions. Board policy 2 reported minimum, default
and maximum of 125, 250 and 300 W, matching NVML. Core-current policy 13
reported type 11, channel 11 and a 204.7 A request.

On the RTX 3070 Ti at `0000:01:00.0` with the same driver, the release
returned zero policies and the same error. The corrected native reader
returned 19 rows with matching types. The board request remained 310 W and
its reported range was 100–380 W. The RTX check was read-only.

The [regression test](../tests/test_policy_get_replay.py) replays the relevant
GET response bytes from both GPUs at their original offsets. It checks both
the Python and native readers against the board range independently reported
by NVML, the occupied policy IDs and types, and the core-current descriptor.
The fixture contains no writable driver handles.

## TITAN Xp Max it and Undo at P0

Both runs started at 250 W, automatic fan control and 0% voltage boost, with
80 V/F rows and no V/F or frequency lock. Max it used a requested 1093.75 mV
cap and 1000 mV ramp floor. The driver accepted 300 W, manual 100% fan, and
100% voltage boost. The board-power slider remained writable. At 300 W,
board policy 2 read 300 W and the derived core-current request read 218 A.

* In the first run, the curve changed at eight points and the V/F hold request
  read back at 1093.75 mV. The headroom check reported a 1087.50 mV ceiling.
  Undo returned all measured requests and the +89 MHz memory offset to their
  starting values and released the hold.
* In the second run, after adding a post-boost ceiling check, the driver
  reported a reachable 1093.75 mV ceiling. The curve changed at seven points
  and an independent lock getter confirmed the 1093.75 mV hold. The rail
  writer reported that it could not recognize a control packet and issued no
  SET. No headroom was active. Undo restored all 13 requests, all 80 curve
  deltas, automatic fan control, boost, rail deltas, both lock states and
  the +129 MHz memory offset present at the start of that run.

These are configured requests and driver readbacks. The tests did not
measure physical rail voltage or board power at the connectors. Selection
of a lower curve point and reporting of a confirmed hold with skipped
headroom are covered by simulated driver responses.

### Why the optional headroom writer stopped

The [512.15 read-only capture](titan-xp-rail-get-51215.json) records two
ordinary NVVDD control reads on the TITAN Xp. Both completed successfully
and returned the same geometry. The [472.12 hardware recording](runtime-rails-47212.json)
contains the matching getter on the same card and VBIOS.

| Driver | Boost at capture | NVAPI getter version | GET command | Total bytes | Parameter bytes |
| --- | ---: | --- | --- | ---: | ---: |
| 472.12 | 0% | V1 | `0x20803213` | 716 | 648 |
| 512.15 | 100% | V1 | `0x20803213` | 972 | 904 |

Druta recognizes the first shape for rail writes. The second falls outside
the layouts its writer understands. The 512.15 getter returned success
for requested rail mask 1. The wire mask and writable fields for that
layout remain undetermined. The captures were made at different boost
settings, so they do not isolate the driver version as the cause of the
difference. Neither the capture nor the offline replay changed a GPU setting.

The [regression test](../tests/test_native_rail_splice.py) supplies the
recorded metadata to fake drivers in both backends. It checks that the
known shape still takes its verified write path and that the unqualified
shape forwards only the getter. The replay uses synthetic payloads and
handles. It establishes dispatch behavior only.

To collect the same diagnostic from a checkout using x64 Windows Python:

```powershell
python experiments/capture_rail_get.py --gpu 0000:03:00.0 --out rail-get.json
```

Use the selected card's PCI slot and a new output path. The collector
records the current boost when available, the getter status, and the
incoming and returned packet shapes. The GUI's **Rail limits → Copy info**
reports its cached getter and most recent headroom-apply evidence for the
current session. A fresh session has no prior apply result.

### Where the 1093.75 mV ceiling comes from

Two fresh reads on PCI `0000:03:00.0` confirmed a 1093.75 mV V/F lock, 100%
voltage boost, and a 1093.75 mV effective NVVDD limit. The public P-state
table marked P0 editable and returned zero base-voltage and overvoltage
entries. The installed driver's 98 named profile settings exposed no
independent REL or ALT adjustment. Druta's loaded-equation reader could
not bind its supported transport in this session.
[Hardware capture and offline comparison](titan-xp-headroom-bios-51215.json)

The running BIOS has one logic rail. Its V1 rail table selects two distinct
temperature-input polynomials in the 24-byte VFE equation format. The Python
BIOS inspector exposes their stored definitions in its read-only table
inventory. [NVIDIA's BIOS definitions](https://github.com/OE4T/linux-nvgpu/blob/302165ad4b9becb6b80e0880afb39e8433cde1a0/drivers/gpu/nvgpu/include/nvgpu/bios.h)

| Role | Equation | Stored range | Live driver limit |
| --- | ---: | ---: | ---: |
| Reliability (REL) | 0 | 1025–1068.75 mV | 1093.75 mV, including 25 mV boost |
| Alternate reliability (ALT) | 1 | 1075–1093.75 mV | 1093.75 mV |
| Overvoltage | 3 | 1200 mV constant | 1200 mV |

The documented clock-program and FLL references on this image select other
equations. Neither REL nor ALT appeared as an input to another decoded VFE
equation. The boot equation uses a separate fixed-point type whose internals
were outside this check.

In a memory copy, increasing only the two upper bounds by 6.25 mV changed
six bytes. Reparsing confirmed both changes and the original definitions for
the other captured roles. Reversing the edits recovered the original image
byte for byte. A higher upper bound can affect the result only while the
temperature-dependent polynomial exceeds its previous upper bound.

The same inspector found upper bounds of 1056.25 mV (REL) and 1118.75 mV
(ALT) in the PCI image from the GTX 1050 Ti's saved GP107 ROM. Those
board-specific values illustrate why Druta reads the selectors and equations
from the current image. This comparison used a saved file only.

### One-step ceiling raise and restoration on hardware

On November 25, 2026, the 512.15 driver loaded the two temporary upper-bound
changes on this TITAN Xp. Its absolute rail-status getter reported a 6.25 mV
rise in both reliability limits and the effective ceiling. Each phase had
nine samples at P0, 100% voltage boost, and the same 1093.75 mV V/F
hold. [Hardware readings and restoration checks](titan-xp-headroom-aba-51215.json)

| Phase | REL | ALT | Effective ceiling | Reported live voltage | GPU temperature |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original | 1093.75 mV | 1093.75 mV | 1093.75 mV | 1093.75 mV | 42 °C |
| Raised upper bounds | 1100 mV | 1100 mV | 1100 mV | 1093.75 mV | 40–41 °C |
| Restored | 1093.75 mV | 1093.75 mV | 1093.75 mV | 1093.75 mV | 40–41 °C |

A fresh process verified the candidate image before the test removed the
two staged configuration records. A second device refresh restored the
byte-identical original image. After each refresh the session restored and
checked the original tuning, including all 80 curve deltas and 13 power
policy requests. VMIN read 650 mV, OV read 1200 mV, and the four rail
delta requests read zero throughout the measured phases. This verifies the
driver-reported ceiling through the BIOS configuration route. The separate
live headroom setter on 512.15 remains unverified.

## 1,999 W XOC slider and Apply on driver 512.15

Both cards returned RM `0x3A` for the 136-byte role query on the extended
`26xx` transport. Both accepted the 132-byte form of the same read-only
query. Its 128-byte definition uniquely matched the board row in the
5,392-byte native INFO table. Two successive reads agreed on the client
request, native request, milliwatt units and ordinary board limits.

At idle in P8, each card ran one 1,999 W Apply and Undo through the native
GUI action path, with a separate restoration watchdog. The XOC slider on
each card reached 1,999 W. The independent client and native request
getters and the NVML configured-power getter all returned 1,999,000 mW.

| Adapter | Slider with XOC off | Starting request | Native effective status, one sample | Request after Undo |
| --- | ---: | ---: | ---: | ---: |
| RTX 3070 Ti | 100–380 W | 310 W | 391.875 W | 310 W |
| TITAN Xp | 125–300 W | 300 W | 300 W | 300 W |

GUI Undo restored the captured client and native INFO and CONTROL buffers
byte for byte, including all 19 policy requests on the 3070 Ti and all 13
on the TITAN Xp. Neither watchdog intervened. The single native status
sample on each adapter describes that instant. Physical enforcement under
load remains unmeasured.
