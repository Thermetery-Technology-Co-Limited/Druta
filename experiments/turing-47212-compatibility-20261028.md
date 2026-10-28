# Turing 472.12 compatibility and 1.9.05 fixes

472.12 was installed and tested live on the owner's Gigabyte RTX 2080 SUPER
AORUS Xtreme / TU104 on 2026-10-28. Ordinary core/memory offsets, fan control,
voltage boost, graphics-curve preservation, P0 hold, configured power requests
and bounded current pins passed the checks below. **Accepted power requests
above the ordinary maximum still encounter a lower native effective limit.**
Druta 1.9.05 reports that limit separately rather than presenting successful
request readback as proof of enforcement.

These observations apply to this shunt-modified board, VBIOS
`90.04.7a.40.3d`, PCI `0000:0a:00.0`, with its imperfect 16 GB VRAM
modification. They are not universal Turing constants or compatibility lists.
No new workload or calibrated electrical-power measurement was used.

## Installation and runtime capabilities

The supplied Standard display INF was activated using the Windows PnP driver
update API with a non-null reboot-result pointer. Installation succeeded,
reported no reboot required and did not request one. A fresh NVAPI process
reported 47212 / `r470_00`; Windows reported display driver `30.0.14.7212`
with no device error. The Windows boot time stayed unchanged during our work.
The owner's preceding OPP interruption occurred before this installation.

The existing domain-6 mode-3 hold at 1062500 uV was preserved or reacquired
as the driver became available. Every qualified sample verified P0. Reset
intervals were not continuously observable. There was no deliberate P5/P8
transition, reboot, registry override or VBIOS write.

The normal installed-library loader selected retained system NVML
`8.17.16.2101`. Its clock-offset exports exist but report unsupported on this
runtime. A separate test process explicitly loaded the supplied NVML
`8.17.14.7212`, whose offset exports are absent, without replacing installed
files. Both runtime combinations passed the clock/fan/boost trials. See
`alternate_nvml_runtime` in the [clock report](turing-47212-clock-compat-20261028.json).
Selection uses actual exports, return codes, transport geometry, generation,
role binding and readback. No driver-version allowlist or denylist was added.

## Clock offsets and other controls

The clock experiment used [this script](validate_turing_clock_compat.py):

| Check | Result on 472.12 |
| --- | --- |
| Core offset -15 MHz | Accepted through NVAPI Pstates20; exact signed offset readback |
| Memory offset -1 MHz true | Accepted; -8 NVML-equivalent units / -4000 kHz NVAPI delta; live memory 7750 to 7746 MHz |
| Core write with nonzero memory offset | Memory offset remained -8 units while core became -15 MHz |
| Graphics V/F curve | All 128 captured delta records restored after each offset trial |
| P0 V/F hold | Original 1062.5 mV hold matched after every trial |
| Fan manual 100% | Both fans' requested manual levels verified; original policy/levels restored |
| Voltage boost 1% | Accepted and read back; original 0% restored |
| Memory clock after restoration | Returned to the captured 7750 MHz baseline |
| Rail metadata | NVVDD readable; no claim of a separate writable MSVDD rail |

Tests ran at idle with P0 required, utilization at most 5%, reported power at
most 80 W and temperature at most 55 C. Private undo records included the
captured offsets, graphics deltas, hold, fan state and boost setting. Full raw
Pstates20 captures were kept private around the 472.12 clock trials.
Additional private per-domain offset controls were not exposed by this runtime;
timing-register, persistent registry and external I2C writes were not qualified.

The fallback fixes an export-presence mistake: `NVML_ERROR_NOT_SUPPORTED`
(3) now permits the independently validated Pstates20 route. Permission,
transient and ambiguous errors do not dispatch another setter. Range,
editability, units, sparse P0 targeting and exact offset readback remain
required, with the existing graphics-shape preservation around the operation.

An earlier live [431.60 clock trial](turing-43160-clock-compat-20261028.json)
also accepted both offsets and restored the captured offset words, graphics
curve and hold. However, the observed memory clock changed from 8000 to
7750 MHz while the reported memory offset returned to zero. That independent
initial clock target was not inventoried, so complete physical-state
restoration was not established. Offline inspection found that the old
Pstates20 converter forwards counts/domain/delta into a fresh internal buffer;
it does not forward public clock kind/flags/base-range fields. Replaying those
public fields would not be an evidence-backed repair. The 472.12 tests do not
explain that 431.60 change. Users should inspect live clocks after legacy
offset operations; encoded readback alone does not inventory every clock target.

## Power interface and live results

Static inspection supplied candidates, then guarded live GETs validated
the geometry and full captured state before any SET. Client row 0's role 1
uniquely matched native policy 2's complete 132-byte definition. The existing
raw FE caller returned 250000 mW; the reported ordinary range was
105000/250000/350000 mW. No BAR0 scan or caller creation was needed.

| Operation | Wire command | Parameter bytes | Geometry |
| --- | --- | ---: | --- |
| Transport capture | `20802612` | 3480 | Complete packet 3548 bytes |
| Client INFO | `20802630` | 900 | Mask +0; rows `4 + 28*i` |
| Client CONTROL GET / SET | `20802632` / `20802633` | 388 | Mask +0; rows `4 + 12*i`; request +4, caller +8 |
| Role INFO | `2080261C` | 136 | Selector 1; native definition at +4 |
| Native INFO | `20802618` | 5520 | Mask +4; rows `60 + 132*i` |
| Native CONTROL GET / SET | `2080261A` / `2080261B` | 2124 | Mask +16; rows `20 + 52*i` |
| Native STATUS | `20802619` | 69148 | Mask +0; rows `100 + 2088*i` |

This extended RM26 geometry remains distinct from the short 431.60 geometry.
Bindings retain their selected native layout; a transport change requires a
fresh controller. Unknown bytes and client INFO tails remain exact.

The [power experiment](validate_turing_live_power.py) performed four trials
with durable private undo records and independent restoration watchdogs.
The [public report](turing-47212-live-power-20261028.json) records all samples:

| Configured request | NVML-reported enforced limit | Settled native effective board limit | Core-current request / effective |
| ---: | ---: | ---: | ---: |
| 349 W | 349 W | 359.906 W | 359.640 / 359.640 A |
| 351 W | 351 W | 360.938 W | 360.360 / 360 A |
| 400 W | 400 W | 360.938 W | 378 / 360 A |

The new native-effective accessor's first samples were 349/350/350 W;
the next four samples per request agreed at the settled values above.
Independent raw STATUS was sampled afterward and was already settled in
all five samples. Sequential GETs are not an atomic firmware snapshot.
The plateau is consistent with a 350 W clamp plus the approximately 3.125%
status uplift, an inference rather than a decoded physical-power calibration.

The production backend accepted 351 W, set a separate 350 A current pin and
replayed it after repeating the board request. A new independent 360.001 A
request was refused. Driver-derived above-range current requests remain
visible only with matching v30 extended-board/native snapshots and either
equal effective current or saturation at the reported maximum. Every new
independent write still uses the current policy's own bounds.

Every power trial restored the entire captured 250 W client/native baseline
and original P0 hold. This is request/readback/recovery qualification, not
proof that 400 W is physically enforced. The earlier
[431.60/621.01 comparison](turing-driver-live-comparison-20261028.md) and
[owner-reported 581.42 FurMark result](turing-owner-furmark-ab-20261028.md)
have separate scopes.

## What the power slider now communicates

A rejected or mismatched 400 W write that reads back 350 W returns both
widgets to 350 W. Unavailable readback is labeled unverified; a last verified
value is explicitly identified as last known. An accepted configured 400 W
request remains displayed as 400 W, alongside NVML's value and the lower
native limit, with a visible warning. It would be misleading to call that
accepted request rejected solely because a separate constraint is lower.

Native status requires a fresh role binding, full matching native INFO and
CONTROL captures, milliwatt units, matching policy types and a valid STATUS
value. Missing native status remains explicitly unavailable. Neither NVML
agreement nor native status establishes actual electrical power on this board.

The complete hardware-free suite passed **6672 tests** in **149.75 seconds**.
Focused protocol, offset, readback/recovery and new UI tests passed Ruff.
