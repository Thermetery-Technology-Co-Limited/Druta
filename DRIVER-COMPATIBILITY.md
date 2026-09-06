# Driver compatibility: 472.12 and 580.97

This matrix tracks the local TITAN RTX (TU102, VBIOS 90.02.1E.00.02) and
TITAN Xp (GP102, VBIOS 86.02.3D.00.01) on Windows. Results are scoped to
these boards and drivers. Blackwell's existing 580.97 controls are separate;
this comparison does not establish a 472.12 Blackwell path.

| Feature | 580.97 baseline | TITAN RTX on 472.12 | TITAN Xp on 472.12 |
|---|---|---|---|
| NVML loading and GPU identity | Confirmed | Confirmed; Standard-driver NVSMI directory supported | Confirmed; selected by PCI slot |
| Core offset and range | Confirmed through NVML | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20 |
| Ordinary memory offset and range | Confirmed through NVML | Confirmed; same true-MHz slider units | Confirmed; same true-MHz slider units |
| Applied-offset and P0 maximum-clock telemetry | Confirmed through NVML | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20 |
| V/F point lock | Confirmed | Confirmed during loaded offset checks; exact lock restored | Confirmed during loaded offset checks; exact lock restored |
| Fan duty, RPM, manual control and Auto | Confirmed | Both fan controls' requested levels and Auto policies verified through NVAPI; zero RPM is expected on this water-cooled card | Manual duty/RPM response and Auto verified through NVAPI cooler controls |
| Clock event/performance-limit reasons | Confirmed | Legacy NVML ThrottleReasons fallback implemented | Legacy NVML ThrottleReasons fallback implemented |
| V/F curve editing and de-flatten planners | Confirmed | Negative-point write/reset and raised-cap de-flatten confirmed | Negative-point write/reset and raised-cap de-flatten confirmed; default ramp can have no room below the clock-list bound |
| NVVDD rail offsets and all four limits | Confirmed; see voltage measurements below | Confirmed, including each live ceiling clamp and idle floor | Confirmed, including each live ceiling clamp; floor uses verified legacy re-send |
| Per-domain clock offsets | Confirmed for mapped controls | XBAR, Additional Memory Clock Offset, SYS, VIDEO and LTC each moved by about +30 MHz under load | Additional Memory Clock Offset +25 MHz moved reported memory by +20.25 MHz twice; other paired controls remain hidden |
| Power limit and voltage boost | Confirmed | Confirmed with independent readback | Confirmed with independent readback |
| NVML frequency lock | Works on Turing; unsupported on Pascal | Confirmed at 1500 MHz under load; legacy RM readback also sees another process's range | Unsupported baseline; V/F point lock remains available |
| Profiles, Undo, Reset all and Max it | Existing composite actions | All 13 UI callback checks passed; exact controls/table/lock restoration | All 13 UI callback checks passed; exact controls/table/lock restoration |
| I2C regulator control | Board/tool dependent | MP2888A verified under load: +75 mV request moved rail-minus-VID by +45 mV; original raw value restored | No matching regulator found on this board |
| Memory timing capture and writes | Board/tool dependent | Capture works; FAW 16→17 is dropped by hardware and reported as dropped | Capture, FAW 24→25 write, and exact restore confirmed |
| MSVDD | Unavailable on these TITAN boards | Unavailable; no confirmed rail | Unavailable; no confirmed rail |

Export presence or a successful write return alone is not a functional result.
The owner confirms that the RTX is on a water loop, explaining its zero RPM
readings on both drivers. Fan controls remain exposed: availability follows
the confirmed control APIs, independently of the measured RPM. Manual targets
and Auto policies were verified and restored on both fan channels.
The [public validation summary](experiments/compatibility-validation-47212.json)
records the integrated checks and measured outcomes without private paths,
device UUIDs, or raw recovery buffers.

## Ordinary clock offsets

Driver 472.12 lacks both the modern NVML clock-offset API and its older
GpcClkVfOffset/MemClkVfOffset family. Druta reads the public NVAPI Pstates20
table and submits a sparse P0 request containing one clock delta and no voltage
or other-domain entries. The modern NVML path retains precedence when present.

NVAPI reports memory offsets in the reported memory clock's MHz. NVML doubles
those numbers. The backend converts between them so profiles, bounds, and
the true-memory-MHz slider retain their meaning across drivers.

| Card | Core range | Memory range in NVAPI MHz | Normalized NVML memory units | Memory slider range in true MHz |
|---|---:|---:|---:|---:|
| TITAN RTX | -1000..+1000 MHz | -1000..+3000 | -2000..+6000 | -250..+750 |
| TITAN Xp | -200..+1200 MHz | -1000..+1000 | -2000..+2000 | -250..+250 |

The 472.12 checks held a 900 mV V/F point under a CUDA bandwidth workload
targeted by PCI slot. These are short functional measurements, with memory
requests subject to the card's clock quantization.

| Card | Request | Reported clock before → after | P0 maximum before → after |
|---|---|---|---|
| TITAN RTX | Core +15 MHz | 1755 → 1770 MHz | 2160 → 2175 MHz |
| TITAN RTX | Memory +10 true MHz | 6801 → 6840 MHz, approximately +9.75 true MHz | 7001 → 7041 MHz |
| TITAN Xp | Core +13 MHz | 1721 → 1733 MHz | 1911 → 1923 MHz |
| TITAN Xp | Memory +10 true MHz | 5508 → 5544 MHz, approximately +9 true MHz | 5705 → 5745 MHz |

Each test first checked identity writes. Complete raw Pstates, V/F-table,
and lock buffers matched the originals after restoration. Independent
physical-clock counters also responded. The RTX captures additionally verify
that other-domain offset fields and all P-state voltage fields stayed unchanged.

The local EXE/source package includes the raw measurements:

- [TITAN RTX offset measurements](experiments/legacy-offsets-47212-0000-01-00.0.json)
- [TITAN Xp offset measurements](experiments/legacy-offsets-47212-0000-02-00.0.json)
- [580.97 voltage measurements](experiments/voltage-rails-20260906.json), explained in [VOLTAGE-RAILS-TITAN.md](VOLTAGE-RAILS-TITAN.md)

Public API references: [NVIDIA's Pstates20 declarations](https://github.com/NVIDIA/nvapi/blob/main/nvapi.h)
and [NVML API version history](https://docs.nvidia.com/deploy/nvml-api/change-log.html).

## Frequency locks and voltage rails

The R472 NVML frequency setter does not populate the NVAPI BoostLock table.
Druta reads RM command `0x20802077` instead: two 328-byte records identify
the minimum (`0x4C`) and maximum (`0x4B`) requests in kHz. The getter is
scoped to the measured TITAN RTX and driver 472.12. It uses a PCI-matched
Windows adapter handle and does not submit RM's paired setter command.

The production check held 1500 MHz with up to 100% CUDA utilization,
read an asymmetric 1200–1500 MHz range from a fresh process, and retained
that range while adding and removing an independent 900 mV V/F point lock.
Reset restored the original RM records, NVAPI lock, V/F table, and Pstates
byte for byte. The package includes the
[production frequency-lock measurements](experiments/legacy-frequency-production-47212.json).

Both cards on 472.12 reached 1112.5 mV with raised 1125 mV ceilings in two
raise/restore cycles. NVVDD +12.5 mV offsets moved the live voltage by
12.5 mV, and each ceiling independently clamped live voltage to 875 mV.
The idle floor also reached 875 mV and returned to its original value;
Pascal needs a verified identical re-send after a floor change on this driver.
See [the 472.12 rail report](VOLTAGE-RAILS-47212.md) for measured bases and
the separate per-field results.

The regular 1.2 V / XOC 1.5 V limit bounds and +200 / +500 mV offset bounds
are request ranges. These measurements do not establish a 1.15 V physical
maximum or promise that either board will deliver the top slider value.

Profiles and Undo restore their captured tuning controls; the separately
managed V/F hold remains until released. A successful stored V/F delta can
still be clipped in the evaluated curve by the driver's hardware ceiling.
The staging and apply results report those cases, and a default Pascal ramp
with no room for a whole clock bin reports no applicable change.

The local UI callback checks exercised Max it, its single Undo action,
named-profile restoration, and Reset all on each card. All 13 checks per
card passed, and the original profile controls, raw V/F table, and lock
buffer were restored exactly. On RTX, the ramp has 19 points from 975 to
1093.75 mV and reaches 2160 MHz at 1093.75 mV.

The RTX MP2888A I2C ladder was measured under CUDA load with a 900 mV
V/F hold. Its +75 mV request increased rail-minus-VID by 45 mV. Requests
through +50 mV did not clear the probe's detection threshold; this is why
the control reports measured response separately from its requested offset.
The regulator's original raw offset of zero was restored and read back.

## Additional clock controls and card switching

The RTX per-domain checks used CUDA load and a 1.3-second settling interval
before sampling physical counters. Each +30 MHz request matched its stored
value, moved the corresponding counter, and restored its entire original
control buffer:

| RTX control | Physical clock before → after → restored, MHz |
|---|---:|
| XBAR | 1679.897 → 1709.896 → 1679.895 |
| Additional Memory Clock Offset | 6794.210 → 6824.185 → 6794.203 |
| SYS | 1754.899 → 1784.896 → 1754.899 |
| VIDEO | 1619.892 → 1649.897 → 1619.890 |
| LTC | 1409.999 → 1439.999 → 1409.999 |

On Xp, two +25 MHz Additional Memory Clock Offset cycles stored +25000 kHz
and moved the reported memory clock from 5508.00 to 5528.25 MHz. The ordinary
memory offset stayed zero. Two negative V/F point edits at a 900 mV hold
changed the evaluated curve from 1721.0 to 1708.5 MHz and live core from
1721 to 1708 MHz. Full domain, curve, and point-lock buffers restored exactly.

The real GUI switch checks alternated RTX's 128-point and Xp's 80-point
curves without disappearing curves or leaked GUI items. A clean curve
switches immediately; actual staged edits require the existing confirmation.
If the new GPU cannot initialize, the previous curve and edits remain intact.
