# Driver compatibility: 472.12 and 580.97

This matrix tracks the local TITAN RTX (TU102, VBIOS 90.02.1E.00.02),
TITAN Xp (GP102, VBIOS 86.02.3D.00.01), and GTX 770 (GK104, VBIOS
80.04.c3.00.01, PCI 1184 / subsystem 1033196e), and GTX 745 (GM107 DDR3,
VBIOS 82.07.32.00.6a, PCI 1382), and both GTX 690 cores (GK104,
VBIOS 80.04.1e.00.18, PCI 1188 / subsystem 84061043) on Windows. Results are scoped to
these boards and drivers. Blackwell's existing 580.97 controls are separate;
this comparison does not establish a 472.12 Blackwell path. The 580.97 baseline
column describes the TITAN boards; GTX 770, GTX 745 and GTX 690 were tested only on 472.12.

**Current-policy controls:** eligibility follows Pascal, Turing or Blackwell
architecture plus exact runtime descriptor/mask checks, never device ID,
driver string or VBIOS. On driver **580.97**, the TITAN Xp and TITAN RTX
core-current policies passed 5 A down/up/readback/restore checks and expose
maxima of **218 A / 390 A**. The diagnostic build's Turing 472.12 path passed
a 1 A reduction, stored/effective readback and exact restoration on the local
TITAN RTX; Pascal remains untested on 472.12. See the
[472.12 live record](experiments/current-limits-titan-47212-20260908.json).
Separate read-only checks on the GTX 745 (Maxwell)
and GTX 770 (Kepler) with 472.12 found no supported current-control interface;
see the [Kepler/Maxwell measurements](CURRENT-LIMITS-KEPLER-MAXWELL.md).
Blackwell policy 13 (core) and policy 14 (other rail) retain normal caps of
**500 A / 200 A**, with the advertised API maximum available in XOC. See
[current-limit validation](CURRENT-LIMITS-RTX5080.md).

**Suppressed** means **unavailable in Druta and therefore not shown**. It is a
UI capability status, not proof that hardware lacks the feature. Each entry
states whether the reason is an unsupported getter, missing records, an
undecoded field mapping, or an unverified write effect. A visible action whose
write is refused (for example, Apply before Verify) is described as *refused*,
not suppressed.

| Feature | 580.97 baseline | TITAN RTX on 472.12 | TITAN Xp on 472.12 | GTX 770 on 472.12 | GTX 745 DDR3 on 472.12 | GTX 690, both cores on 472.12 |
|---|---|---|---|---| --- | --- |
| NVML loading and GPU identity | Confirmed | Confirmed; Standard-driver NVSMI directory supported | Confirmed; selected by PCI slot | Confirmed; NVAPI/NVML agree on PCI identity | Confirmed; NVAPI/NVML identity agrees | Confirmed; distinct PCI slots and UUIDs alongside TITAN RTX |
| Core offset and range | Confirmed through NVML | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20; repeated load/restore cycles | NVAPI Pstates20; +40 → +27 MHz moved 1072 → 1059 MHz, restored twice | Independent Pstates20 writes; +13 request moved 1201 to 1215 MHz, restored twice per core |
| Ordinary memory offset and range | Confirmed through NVML | Confirmed; same true-MHz slider units | Confirmed; same true-MHz slider units | Confirmed; +25 true MHz moved reported memory 3505 → 3557 MHz | +10 true MHz moved reported DDR3 900 → 910 MHz, restored twice; divisor 1 | Independent +12.5 true MHz request moved reported GDDR5 3004 to 3029 MHz; divisor 2 |
| Applied-offset and P0 maximum-clock telemetry | Confirmed through NVML | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20 | Confirmed through NVAPI Pstates20 |
| V/F point lock | Confirmed | Confirmed during loaded offset checks; exact lock restored | Confirmed during loaded offset checks; exact lock restored | Suppressed: no applicable V/F point-lock path | No supported V/F table at idle or P0; suppressed | Inapplicable; V/F controls suppressed |
| Fan duty, RPM, manual control and Auto | Confirmed | Both fan controls' requested levels and Auto policies verified through NVAPI; zero RPM is expected on this water-cooled card | Manual duty/RPM response and Auto verified through NVAPI cooler controls | Manual 50%, RPM response and exact Auto-policy restoration confirmed | Manual 90% request reads back; original manual 100% restored; RPM unavailable | 55/60/100% and Auto verified from either core; shared blower; readback takes about one second |
| Clock event/performance-limit reasons | Confirmed | Legacy NVML ThrottleReasons fallback implemented | Legacy NVML ThrottleReasons fallback implemented | NVAPI performance-decrease reasons readable | NVML/NVAPI limit reasons readable | NVAPI performance-decrease and NVML event telemetry readable |
| V/F curve editing and de-flatten planners | Confirmed | Negative-point write/reset and raised-cap de-flatten confirmed | Negative-point write/reset and raised-cap de-flatten confirmed; the regular ramp no longer treats the stock clock-list maximum as an overclock ceiling | Not applicable: editor, planners, point locks and shortcuts suppressed; switching back restores RTX’s 128 points | Suppressed; yellow Lock P0 and max fan replaces Max it on the verified board; switching back to a GPU that has V/F curve would restore the curve | Suppressed; yellow Lock P0 and max fan replaces Max it on the verified board; switching back to a GPU that has V/F curve would restore the curve |
| NVVDD rail offset | Confirmed | Confirmed | Confirmed | Suppressed: offset-field mapping/write effect unverified | Suppressed: offset-field mapping/write effect unverified | Suppressed: private getter supplies no domain records; no offset-field mapping |
| Four NVVDD limits | Confirmed; see measurements below | Confirmed, including live ceiling clamps and idle floor | Confirmed, including live ceiling clamps; floor uses verified legacy re-send | Suppressed: no verified limit-write path | Suppressed: no verified limit-write path | Suppressed: control getter has no rail records; live getter reports Not Supported |
| Per-domain clock offsets | Confirmed for mapped controls | XBAR, Additional Memory Clock Offset, SYS, VIDEO and LTC each moved by about +30 MHz under load | Additional Memory Clock Offset +25 MHz moved reported memory by +20.25 MHz twice; other paired controls remain suppressed | Suppressed: offset-control fields/write effects unverified, including Additional Memory Clock Offset | Suppressed: control-to-clock pairing/write effects unverified | Suppressed: private getter supplies no domain records |
| Power limit and voltage boost | Confirmed | Confirmed with independent readback | Confirmed with independent readback | NVML power-limit range and voltage-boost getter unavailable; sliders suppressed | Power-limit range and voltage-boost getter unavailable; sliders suppressed | NVML watt limits and voltage-boost getter unsupported; sliders suppressed |
| Core current limit | TITAN Xp / RTX: 218 A / 390 A maxima; ±5 A apply/readback/restore verified | Diagnostic build: 350.780 -> 349.780 -> 350.780 A verified; API max 390 A | Not tested on 472.12 | Modern info GET unsupported; no validated legacy current policy | Modern info GET unsupported; zero active legacy policies | Not tested |
| NVML frequency lock | Works on Turing; unsupported on Pascal | Confirmed at 1500 MHz under load; legacy RM readback also sees another process's range | Unsupported baseline; V/F point lock remains available | GPU 1176..1176 MHz and memory 3505..3505 MHz both return Not Supported (3), while elevated | GPU 1072 MHz / memory 900 MHz locks return Not Supported through NVML and nvidia-smi; application-clock writes/readback succeed but do not hold P0 | GPU 1202 MHz / memory 3004 MHz locks and application-clock queries return Not Supported (3) |
| Profiles, Undo, Reset all and Max it | Existing composite actions | All 13 UI callback checks passed; exact controls/table/lock restoration | All 13 UI callback checks passed; exact controls/table/lock restoration | Profiles omit the inapplicable V/F table; Reset all skips curve writes; Max it suppressed. Core/memory/fan and I2C restoration verified separately; default profile replay covered by hardware-free tests | Full profile replay restored +40 core, zero memory offset and manual 100% fan; no V/F requirement | Independent profile identity/replay; P0/fan Undo, Release, Reset and exit verified on both cores |
| I2C regulator control | Board/tool dependent | MP2888A verified under load: +75 mV request moved rail-minus-VID by +45 mV; original raw value restored | No matching regulator found on this board | NCP4206 absolute target verified at 1250/1262.5 mV; Auto and profile restoration exact | No matching registered profile found; no voltage writes | NCP4206 discovered independently on both cores; bounded response and exact restoration verified |
| Memory timing capture and writes | Board/tool dependent | Capture works; FAW 16→17 is dropped by hardware and reported as dropped | Capture, FAW 24→25 write, and exact restore confirmed | Capture and 15 delay fields verified; exact restoration; CL 18→19 triggered driver recovery (details below) | All 33 timing fields queried; 16 delay fields accepted +1 and restored across both partitions; CL queried only; zero-value restore caveat below | 33 fields queried on each core; 14 delay fields accepted +1 and restored across four partitions; peer unchanged |
| MSVDD | Unavailable on these TITAN boards | Unavailable; no confirmed rail | Unavailable; no confirmed rail | No confirmed rail | No confirmed rail | No confirmed rail |

Export presence or a successful write return alone is not a functional result. Manual targets
and Auto policies were verified and restored on both fan channels.
The [public validation summary](experiments/compatibility-validation-47212.json)
records the integrated checks and measured outcomes without private paths,
device UUIDs, or raw recovery buffers.

GTX 745 DDR3 evidence is in the [Maxwell validation record](experiments/maxwell-gtx745-validation-47212.json).
Its DDR3 reports 900 MHz at P0, matching NVIDIA's [1.8 Gbps reference specification](https://www.nvidia.com/en-us/geforce/graphics-cards/geforce-gtx-745-oem/specifications/).
The RAM-type 7 label and divisor 1 are now explicit; a +20-unit Pstates20 delta
produced +10 MHz physical memory clock. Ordinary controls, FAW and profile
replay completed 207 checked 64 MiB transfers with no mismatches. The highest
observed temperature in the ordinary-control checks was 30 °C. Original
+40 MHz core offset, zero memory offset, manual 100% fan, and timing registers
were restored. These short checks do not establish maximum clocks or stability.
No voltage or CAS-latency writes were made. Windows sign-in replay is untested.

The subsequent [full timing query and bounded sweep](experiments/maxwell-gtx745-timing-sweep-47212.md) verified 16 writable delay fields and exact final restoration with 270 checked transfers. CCDL/CCDS start at zero; nvtune warns on restoring zero despite allowing a +1 test. CCDL restored on a P-state transition; CCDS used an exact-original restoration override. Latency/preamble/protocol, structural/split, inferred and warning-producing fields remain query-only.

## Legacy private-control decoding status

The earlier phrase "private layout unvalidated" conflated separate interfaces.
Private **clock telemetry** arrays are already decoded on Kepler/Maxwell.
The [GTX 690 ROM and controlled state comparison](experiments/kepler-gtx690-clock-domains.md)
now also identifies its primary clock domains and infers the BIOS clock roles.
The [GTX 770 cross-check](experiments/kepler-gtx770-clock-crosscheck.md)
resolves Kepler domain16 as XBAR2CLK and17 as SYS2CLK, and separates25 as
L2C2CLK. Extra Kepler names retain `?` and apply by generation.
The idle core/memory frequency collision no longer mislabels memory as GPC.
The [GM107 ROM/state comparison](experiments/maxwell-gtx745-clock-domains.md)
resolves Maxwell domain 16 as XBAR2CLK and 17 as SYS2CLK by their distinct
held-P0 values. Those ROM-correlated names retain `?`; L2C/MSD have no
populated private rows on the tested GTX 745. Private clock-offset controls
remain suppressed; telemetry labels do not authorize writes.
The private
**offset-control fields** and their physical write effects have not been
established on GTX 770/745/690. The four-limit getter uses a separate, already
decoded API; its write path additionally needs measured bases/defaults and a
validated transport. These are not one shared undecoded structure.

[Raw read-only probes](experiments/legacy-private-layout-47212.json) on 2026-09-07
compared both installed GTX 690 cores with the TITAN RTX, at idle and under
CUDA load (GTX 690 P0, RTX P2). The GTX 745 and GTX 770 were not installed for
this follow-up; their cells retain the earlier, narrower evidence.

| Query on each GTX 690 core | Observed result on 472.12 |
|---|---|
| Clock-domain control GET `0xF58938F5`, version `0x261A4` | Masks 0 and one-hot bits 0–8 succeed; only header dword at `0x0C` changes to `0x01010000`. No domain records are populated, including at P0. Other one-hot bits and the all-bits mask return `-1`. |
| Same GET, version `0x161A4` | `-9`, incompatible structure version |
| Rail control GET `0xA3070DB0`, version `0x10AC8` | Masks 0/1/2/3 succeed with unchanged buffers; no rail records. Version `0x20AC8` returns `-9`. |
| Live rail GET `0x5D0634EE`, version `0x10AC8` | `-104`, Not Supported, for masks 0/1/2/3. Version `0x21620` returns `-9`. |

The RTX positive control populated clock-mode records and live rail fields via
the same getters. Thus the GTX 690 result is not just Druta declining to parse
available legacy records: these requests returned no offset records to decode.
It does not prove that every possible driver interface lacks such controls.
No private setter or voltage write was used during these probes. NVIDIA's
[status definitions](https://docs.nvidia.com/nvapi/group__nvapistatus.html)
distinguish generic error `-1`, incompatible version `-9`, and unsupported `-104`.

`GPU.clkdom_debug_report()` now retains raw changed dwords, request masks and
statuses when the control path is suppressed, alongside decoded clock telemetry.
It does not apply Turing field names to unknown legacy bytes or enable writes.
For a specific slot, a read-only report can be collected from source with:

```powershell
python -c "from nvbackend import GPU; import json; print(json.dumps(GPU('0000:04:00.0').clkdom_debug_report(), indent=2))"
```

New mapping evidence must include actual record data and a measured, restorable
write effect before the corresponding adjustment can be exposed.

## GTX 690 with three GPUs

The [GTX 690 validation record](experiments/kepler-gtx690-validation-47212.json)
covers TITAN RTX at `0000:01:00.0` and GTX 690 cores at `0000:04:00.0` and
`0000:05:00.0`. All three have distinct UUIDs and matching NVAPI/NVML PCI
identities. CUDA selected each requested PCI slot, including during three
simultaneous data-checked workloads. Nine repeated UI switches preserved all
three selector entries and restored the RTX's 128-point V/F curve. GTX 690
profiles distinguish the two identical cores by UUID; replay restored each
core's offsets and Auto fan policy without changing its peer's offsets.

Clock writes were checked twice per core under load: +13 MHz core requested
produced a +14 MHz reported delta (1201 to 1215 MHz), and +12.5 true MHz memory
moved reported GDDR5 from 3004 to 3029 MHz. The other core's offsets stayed
unchanged. A workload on one core can raise the other core's performance state;
that is separate from an offset being written to it. These clocks describe the
installed firmware at zero offsets, not every GTX 690 BIOS or a stability limit.

The physical blower is shared. A manual request made through either core
changes RPM observed through both, while NVAPI exposes separate requested
policies. Do not interpret the two fan rows as two independent physical fans.
55%, 60%, 100% and Auto were verified. The requested policy/level updates after
roughly 0.7–1.0 seconds on this driver; Druta now waits up to two seconds so it
does not report a successful delayed request as a failed write.

NVML GPU/memory locks and application-clock queries return Not Supported (3).
The legacy force request `(0,2)` instead holds P0 at **705 MHz core / 3004 MHz
reported memory**, including under load, versus the normal loaded 1201 MHz
core observed here. Two force/release cycles passed on each core. `(16,2)`
releases the request; both cores returned to P8 at 324/324 MHz after the driver's
idle transition. The yellow **Lock P0 and max fan** action is enabled only for
PCI 1188 / subsystem 84061043 / VBIOS 80.04.1e.00.18 / driver 472.12. It does
not promise maximum core boost. UI checks covered both cores: fan 100%, Undo
to Auto while retaining P0, repeated Release, Reset all, exit, and refusing a
card switch while a hold is owned. The existing GTX 745 gate remains separate.

The [timing sweep](experiments/kepler-gtx690-timing-sweep-47212.json) queried all
33 fields on each core. RC, RFC, RAS, RP, RD_RCD, WR_RCD, CDLR, WR, R2W_BUS,
PDEX, FAW, CCDL, CCDS and RRD accepted +1 cycle. Each write was checked across
broadcast plus all four FBPA partitions, the peer core's registers stayed
unchanged, and all original registers were restored. The two sweeps completed
708 checked 64 MiB transfers with zero mismatches. CL, WL, preamble/protocol,
structural/split, inferred and warning-producing fields were not written.

[I2C discovery](experiments/kepler-gtx690-i2c-47212.md) identifies an NCP4206
at port 2 / address 0x20 through each GTX 690 core. Both return the observed
OEM identity 0x41 / 0x3298 / 0x01 and VOUT_MODE 0x20. The initial generic
survey missed them because it tested only PAGE (0x00), which these controllers
do not answer; the survey now also tries identity and voltage-read commands.
Kepler detection automatically scans ports 0–7 for NCP4206, without requiring
a particular PCI device or subsystem. Verify remains the voltage-write gate.

The first loaded Verify tests requested 1231.25 mV from a measured baseline
of 1207.03 mV. VMON rose to 1218.75–1226.56 mV across the two cores. The other
core's command/configuration registers stayed unchanged during every write,
and 0x21/0xD2/0xD3/0xDD were restored exactly. This establishes separate
controller addressing. NVAPI per-rail and private clock-domain writes remain
suppressed because their private-driver write path is unverified; the NCP4206 path is direct I2C control.

The three-GPU review corrected stale telemetry after switching and moved UI
callbacks onto the render thread so controls cannot be rebuilt concurrently
with panel refresh. Kepler NCP4206 discovery scans the selected GPU's I2C ports
without a PCI/subsystem whitelist; other generic TOML profiles retain their matching rules; MP2888A also uses automatic controller discovery.
Final test cleanup restored zero clock offsets, Auto fan policies and GPU VID control. These
are bounded functional checks; Windows sign-in replay and long-term stability
were not retested.

## Legacy P0 mechanisms on GTX 745

The [P0-path measurements](experiments/maxwell-gtx745-p0-paths-47212.json)
separate performance-state forcing from frequency locking:

- NVML GPU/memory clock locks and `nvidia-smi -lgc/-lmc` are unsupported.
  The CLI prints that warning but exits with status 0, so its exit code alone
  must not be interpreted as a successful lock.
- `NvAPI_GPU_SetForcePstate` (query ID `0x025BFB10`, handle/state/fallback)
  accepts `(0,2)` and holds idle P0 with DDR3 at 900 MHz. Two force/release
  cycles passed; `(16,2)` returns to automatic P8 idle at 135/405 MHz.
  However, the core stays at **540 MHz under the checked CUDA workload**,
  compared with 1072 MHz without the force. Fallback 1 behaves the same when
  explicitly forcing state 0. Automatic state 16/fallback 1 permits normal
  boosting but does not hold idle P0. All 95 checked transfers in the three
  loaded force/fallback tests passed without mismatches.
- Application-clock setter requests succeed and read back. A 900/1007 MHz
  memory/core request still idles at P8 and reaches 1072 MHz under load, so it
  is neither an idle P0 hold nor a fixed core lock on this board. Resetting
  application clocks restored the original default 900/1032 MHz pair.

The force signature and release sentinel are independently implemented in
[nvapioc](https://github.com/Demion/nvapioc/blob/master/Source/main.cpp) and
[NvAPIWrapper](https://github.com/falahati/NvAPIWrapper/pull/73/files).
No verified force-owner getter or companion call restoring boost was found.
`SetPstateClientLimits` limits permitted performance; `(3,0)` removes limits,
which is distinct from holding P0. Dynamic/overclocked-Pstate enable functions
have conflicting published signatures and were not called speculatively.

The full Max it action remains suppressed because the force path reduces the
loaded core clock. A separate yellow **Lock P0 and max fan** action now occupies
its place on the verified GTX 745 PCI 1382 / subsystem 6893103c, VBIOS
82.07.32.00.6a, driver 472.12. It verifies P0 and the top memory band before
setting fan duty to 100%; it does not change clock offsets, power or voltage.
The tooltip discloses the measured reduced core clock. A separate Release P0
button restores automatic states, leaving fan duty as set. Undo restores the
saved fan policy but intentionally retains P0 ownership. Reset all and exit
release this session's hold; a failed release remains recorded. Profiles do not
persist the hold. [Live UI checks](experiments/maxwell-gtx745-p0-fan-ui-47212.json)
verified P0 at 539/900 MHz from stock, fan 100%, Undo to Auto, repeated Release,
Reset all, exit and switching back to RTX. Original stock offsets and Auto fan
policy were restored. The V/F editor remains inapplicable. This force API has not
been measured on the GTX 770; Kepler needs its own installed-card retest.
Final state: automatic P8, +40 MHz core offset, stock memory offset, manual
100% fan, and original application-clock defaults. No voltage writes were made.

XBAR, SYS, VIDEO, LTC and additional memory-clock writes on GTX 770/745 have
not been verified. Read-only discovery did not establish a usable private
control mapping. They remain suppressed because the control mapping/write response has not
been verified; this does not establish that they are inert. The backend now returns unknown for unmeasured domain/architecture
combinations; the explicit measured Pascal/Turing/Blackwell results remain.

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
| GTX 770 | -105..+1001 MHz | -2695..+3505 | -5390..+7010 | -1347.5..+1752.5 |

The TITAN 472.12 checks held a 900 mV V/F point under a CUDA bandwidth workload
targeted by PCI slot. These are short functional measurements, with memory
requests subject to the card's clock quantization.

| Card | Request | Reported clock before → after | P0 maximum before → after |
|---|---|---|---|
| TITAN RTX | Core +15 MHz | 1755 → 1770 MHz | 2160 → 2175 MHz |
| TITAN RTX | Memory +10 true MHz | 6801 → 6840 MHz, approximately +9.75 true MHz | 7001 → 7041 MHz |
| TITAN Xp | Core +13 MHz | 1721 → 1733 MHz | 1911 → 1923 MHz |
| TITAN Xp | Memory +10 true MHz | 5508 → 5544 MHz, approximately +9 true MHz | 5705 → 5745 MHz |
| GTX 770 | Core +26 MHz, snapped request +27 | 1175 → 1201 MHz | Not separately recorded |
| GTX 770 | Memory +25 true MHz | 3505 → 3557 MHz, approximately +26 true MHz | Not separately recorded |

The TITAN tests first checked identity writes. Complete raw Pstates, V/F-table,
and lock buffers matched the originals after restoration. Independent
physical-clock counters also responded. The RTX captures additionally verify
that other-domain offset fields and all P-state voltage fields stayed unchanged.

On GTX 770, two load/restore cycles returned both offsets to zero. The mixed
clock-list regimes require deriving the boost grid from the contiguous upper
regime: 13.049 MHz, rather than the incorrect whole-list average of 5.523 MHz.
The tests use short workloads and do not establish maximum stable overclocks.

The local EXE/source package includes the raw measurements:

- [GTX 770 functional checks](experiments/kepler-validation-47212.json)
- [GTX 770 FAW verification](experiments/kepler-timing-writes-47212.json)
- [GTX 770 timing sweep and clock-lock attempts](experiments/kepler-timing-sweep-47212.json)
- [GTX 770 I2C identity reads](experiments/kepler-ncp4206-identity-47212.json)
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

On GTX 770, `nvmlDeviceSetGpuLockedClocks(1176, 1176)` and
`nvmlDeviceSetMemoryLockedClocks(3505, 3505)` both return Not Supported (3),
even as administrator. Application-clock and default-application-clock getters
also return Not Supported for graphics and memory; their setters were not
tested. A checked CUDA workload maintained P0/3505 MHz around timing writes;
this was not an enforced P-state lock. These results concern the tested APIs,
not every possible Kepler P-state-control mechanism.

Both TITAN cards on 472.12 reached 1112.5 mV with raised 1125 mV ceilings in two
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

GTX 770 profiles record that the V/F table is inapplicable and restore ordinary
controls without requiring a curve. Live capture and Kepler → RTX → Kepler UI
switching passed; default profile replay and rejected cross-architecture curve
payloads are covered by hardware-free tests. Missing curves on other cards
remain incomplete. Windows sign-in profile application was not tested on GTX 770.

The TITAN UI callback checks exercised Max it, its single Undo action,
named-profile restoration, and Reset all on each card. All 13 checks per
card passed, and the original profile controls, raw V/F table, and lock
buffer were restored exactly. On RTX, the ramp has 19 points from 975 to
1093.75 mV and reaches 2160 MHz at 1093.75 mV.

The RTX MP2888A I2C ladder was measured under CUDA load with a 900 mV
V/F hold. Its +75 mV request increased rail-minus-VID by 45 mV. Requests
through +50 mV did not clear the probe's detection threshold; this is why
the control reports measured response separately from its requested offset.
The regulator's original raw offset of zero was restored and read back.

### MP2888A automatic discovery

The [2026-09-07 validation record](experiments/mp2888a-discovery-47212.json)
covers read-only scans on all three installed GPUs. The TITAN RTX yielded one
MP2888A candidate at port 1/address 0x20; both GTX 690 cores still yielded their
NCP4206 at port 2/address 0x20, with no false MP candidate. Scan durations were
0.4–1.8 seconds per GPU and no discovery writes occurred. MP discovery ignores
DevID/subsystem filters and scans ports 0–7, addresses 0x08–0x77. Other addresses
and multiple candidates are covered by transport mocks, not additional boards.

The actual Verify button passed under CUDA load at GPU VID 1050 mV. The +75 mV
trial moved rail-minus-VID by +44 mV against a 37.5 mV threshold. The original
raw offset 0 was restored exactly; a subsequent +6.25 mV Apply stored raw 1,
and Stock restored raw 0. Apply and Stock requests were both refused before verification.
This proves the write response on the tested controller, not universal board
compatibility or a calibrated gain. Voltage limits were not raised in this run.

The UI exposes all candidates by port/address and scan-time telemetry. Rescan
clears verification while preserving staged V/F edits. Switching through both
GTX 690 cores and back restores the TITAN RTX's 128-point curve. MP profile
identity remains compatible at the original connection, while relocated profiles
pin the discovered bus. Failed restoration now invalidates Verify, and Reset All
cannot interrupt an active verification or make a first write to an untouched
MP candidate. Current telemetry now uses the controller's direct encoding rather
than the former LINEAR11 assumption; see [I2C profile documentation](i2c/PROFILES.md).

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


GTX 770 switching likewise clears its unavailable curve and restores all 128
RTX points on return. No private NVAPI Kepler clock or rail write was attempted; the separately
validated I2C voltage path is described below.

## Memory timing write results

All observations use nvtune on driver 472.12. A stored field alone does not
establish usable live timing control.

| Card | Result | Restoration and workload check |
| --- | --- | --- |
| TITAN RTX | FAW 16 → 17 dropped | Original field unchanged |
| TITAN Xp | FAW 24 → 25 landed | Exact restoration confirmed |
| GTX 770 | FAW 32 → 33 twice; 15 delay fields in the sweep landed across broadcast and all four partitions | Every captured register restored exactly, including after idle → P0; 207 full 64 MiB comparisons passed during the sweep, plus 49 after final recovery |
| GTX 770 | CL 18 → 19 initially verified, then caused CUDA_ERROR_LAUNCH_FAILED and Display event 4101 | Driver recovered; original registers and a fresh checked load verified afterward; not classified as an ignored write |

GTX 770 sweep values (original → requested, each restored before the next):
RC 71→72, RFC 114→115, RAS 50→51, RP 22→23, RD_RCD 25→26,
WR_RCD 18→19, CDLR 10→11, WR 19→20, R2W_BUS 8→9, PDEX 15→16,
PDEN2PDEX 7→8, FAW 32→33, CCDL 2→3, CCDS 2→3 and RRD 8→9.

CL was not retried. WL, RPRE, WPRE and WRCRC were not tested after the CL
failure. W2R_BUS 12→13 and AOND 0→1 produced range warnings and were not
committed. Structural/training fragments, split refresh fields and inferred
TIMING22 addresses remained read-only. No force or daemon mode was used.
The CL result does not establish a POST-only restriction or rule out a change
that coordinates controller and DRAM programming. None of these short tests
establishes long-term stability.

## I2C regulator discovery: NCP4206

I ported NCP4206-based I2C voltage control on GTX 770, GTX 780,
GTX 780 Ti, TITAN Black and the original TITAN.

All selected GPU generations now scan ports 0–7 and unicast addresses for
NCP4206. There is no GPU model or subsystem-ID whitelist. A detected
controller exposes its output without assuming the physical rail, and Verify must confirm a measured response
before normal voltage adjustments are enabled.

The local GTX 770 responds at 7-bit address 0x20 on NVAPI port 2: MFR_ID
(0x99, one byte) = 0x41, MFR_MODEL (0x9A, two bytes) = 0x3298 and
MFR_REVISION (0x9B, one byte) = 0x01. Ports 0, 1 and 3–7 did not respond
at that address. This confirms an accessible I2C device consistent with the
reported controller family. The manufacturer ID matches the
[onsemi NCP4206 datasheet](https://www.onsemi.com/download/data-sheet/pdf/ncp4206-d.pdf),
Table 11; the observed model/revision differ from its default 0x0208/0x03,
so discovery accepts the documented/OEM model IDs with VOUT_MODE 0x20,
records any returned byte-sized revision, and pins that identity for the
instance. The detected tuple, port and address are retained in profile identity;
the manufacturer byte alone is not
enough. The datasheet specifies the seven-bit address as 0x20 (page 15).

The previous "no matching regulator profile" observation meant Druta shipped
no matching recipe; it did not establish that this card lacked I2C support.
Subsequent bounded writes validated the absolute voltage path on this board.
`ncp4206.py` writes VOUT_COMMAND (0x21, two bytes) before enabling bit 3 in
both VR Config registers (0xD2/0xD3); other bits and VOUT_CAL remain intact.
Auto clears both VID_EN bits before clearing the inactive command. Profiles
save the target command and Auto/manual mode, not a fictitious voltage offset.
Failure during a write attempts restoration of the captured command and mode.

The UI exposes 600..1281 mV in normal mode and 600..2000 mV in XOC, as
requested. The VR11 command itself represents only 375..1600 mV: requests
outside that encoding are rejected even in XOC, never wrapped or silently
clamped. Targets snap down to the 6.25 mV grid (1281 requests 1275 mV).
XOC expands a software envelope; it does not establish a 2 V hardware path.

Live checks stayed below the owner's 1350 mV test limit: 1250 and 1262.5 mV
requests read 1248.05 and 1263.67 mV through VMON (0xD7, measured LINEAR11
volts on this board). Verify uses bounded steps under load and restores the
prior mode. It samples baseline noise and tries +25, +37.5 and +50 mV targets
relative to the measured baseline, within the normal 1281 mV ceiling. It stops
once the rail rises clearly above baseline noise. Measured voltage may remain
below the requested VID under load; the response is reported explicitly.
UI Apply/Auto and profile round trips restored 0x21/0xD2/0xD3
exactly; 0xDD remained 3. Switching cards clears session verification. Those live
round trips explicitly excluded curve restoration. New Kepler profiles omit
the V/F requirement automatically; the corresponding default replay is tested
with fake hardware.

Discovery scans NVAPI ports 2, 0, 1 and 3–7 at address 0x20. It performs no
voltage writes. Existing port-2 OEM profiles remain compatible. Ambiguous
multiple controller matches are reported instead of selecting an arbitrary
rail. Live evidence is available for [GTX 770](experiments/kepler-ncp4206-control-47212.json)
and [both GTX 690 cores](experiments/kepler-gtx690-validation-47212.json).
