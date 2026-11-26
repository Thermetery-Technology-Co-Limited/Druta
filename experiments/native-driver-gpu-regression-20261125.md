# Native driver and GPU regression matrix

See the [issue ledger](native-driver-gpu-issues-20261126.md) for follow-up
fixes and hardware verification after this matrix run.

Started November 25, 2026, Asia/Shanghai. The table records the local Windows
test rig. The candidate is the native working tree based on
`eeb6182037464be88c0e53f6a58c77e9fa57b4d3`. The comparison uses the shipped
Druta 1.10.0 core DLL through the same current test harness. It does not
exercise the original release's GUI frontend.

| Build | Core DLL SHA-256 |
| --- | --- |
| Working-tree candidate | `135ff141a1144984d4cb88f661b36e8ab5eb35677701eff65514ab4c8cef4206` |
| Shipped 1.10.0 comparator | `47eb34691d81e2db168705c26cd642aa23094e446b2442d70fc30c19c6a92a31` |

The MSI GTX 1050 Ti Gaming X (Pascal GP107) is at `0000:03:00.0`.
The ASUS Strix RTX 3070 Ti (Ampere GA104) is at `0000:01:00.0`.
The TITAN Xp was disconnected. The RTX 3070 Ti is excluded from 375.63
and 431.36. Those drivers are tested on the GTX 1050 Ti only.

## Results

**Pass** means the target accepted the applied request, the test verified
the relevant readback, and the captured baseline was restored. For a rail
request, this does not by itself establish a physical voltage change.
**Prepared** means the active image read and the production firmware
workflow produced a plan and recovery receipt. The test did not reach a
successful driver reload of the edited image.

| GPU | Driver | Max It | V/F setter | BIOS override | Power limit | Rail limits | Shipped core comparison |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GTX 1050 Ti | 375.63 | Pass | Pass | Prepared. Registry access denied | Pass | Pass | UUID missing, 0 V/F points, configured power unavailable. Live comparison withheld. |
| GTX 1050 Ti | 431.36 | Pass. Patched candidate matched shipped curve and held clock | Pass | Prepared. Registry access denied | Pass | Pass | All four controls passed and restored. Before the fix, Max It ran 13 MHz lower and the short sample measured −0.61% throughput. |
| GTX 1050 Ti | 472.12 | Pass after clean boot | Pass after clean boot | Prepared. Registry access denied. Original verified | Pass after clean boot | Pass after clean boot | Both cores passed all four live controls. Power benchmark difference narrowed to 0.25% on repeat. |
| GTX 1050 Ti | 512.15 | Pass on repeat live bind | Pass | Prepared. Registry access denied. Original verified | Pass | Pass | Shipped V/F passed. Shipped power and Max It blocked at preflight. Shipped rail write rejected. |
| GTX 1050 Ti | 560.94 | Board power preflight blocked | Pass on repeat live bind | Running clock table read | Apply preflight blocked. Nominal-only range | Pass | Both cores had the same control outcomes. |
| GTX 1050 Ti | 581.80 | Pass on repeat live bind | Pass | Running clock table read | Pass | Pass | Both cores passed and restored all four live controls. |
| GTX 1050 Ti | 582.28 | Pass in isolated session | Pass | Running clock table read | Pass | Pass | Both cores passed and restored all four live controls. |
| GTX 1050 Ti | 582.78 | Pass in isolated session | Pass | Running clock table read | Pass | Pass | Both cores passed and restored all four live controls. |
| RTX 3070 Ti | 472.12 | Pass after clean boot | Pass after clean boot | Qualified relationship preview. Registry access denied. Original verified | Pass after clean boot | Pass after clean boot | Both cores passed all four live controls. Short throughput intervals varied between runs. |
| RTX 3070 Ti | 512.15 | Pass. Optional headroom unavailable | Pass | INFO policy definitions read. Relationship layout unknown | Pass | Apply rejected. Baseline verified | V/F passed. Max It and ordinary power blocked. Rail rejected on both. |
| RTX 3070 Ti | 560.94 | Board power preflight blocked | Pass on repeat | Relationship INFO read failed | Apply preflight blocked. Nominal-only range | Pass | Both cores had the same control outcomes. |
| RTX 3070 Ti | 581.80 | Initial V/F baseline rejection. Pass on retest | Pass, signed requests restored | INFO policy definitions read. Relationship layout unknown | Pass | Pass | The shipped core passed Max It, V/F request and Undo, board power, and NVVDD REL. |
| RTX 3070 Ti | 595.71 | Pass | Pass, including evaluated-clock repeat | INFO policy definitions read. Relationship layout unknown | Pass | Pass | Both cores applied and restored all four live controls. |
| RTX 3070 Ti | 595.97 | Pass | Pass, including evaluated-clock repeat | INFO policy definitions read. Relationship layout unknown | Pass | Pass | Both cores applied and restored all four live controls. |

## Method

On each driver, the harness checked the PCI slot, expected GPU UUID, and
reported loaded driver. It ran the production backend against the explicitly
chosen adapter. A short adapter-bound rendering workload supplied steady
P0 for live trials. The test records contain operating counters and
configuration readbacks. Instrumented trials use a fixed D3D11 workload
and include only complete intervals inside the baseline, applied and
restored windows. A frame-rate sample requires confirmed P0 and a
completed functional round trip. The initial 375.63 and 431.36 runs did
not produce gradeable frame-rate samples.

- **Max It** captured the starting configuration, confirmed the result
  for fan, board power, boost, curve and point hold, then ran Undo.
- **V/F setter** held an existing 950 mV P0 point, lowered its curve
  request by one bin, and read the full request table and operating
  counters. It undid the curve change and released the test hold.
- **Power limit** lowered the configured board request by 1 W through
  the ordinary control. The test compared the configured request and,
  where available, the driver-reported effective limit separately.
- **Rail limits** changed only the NVVDD reliability request by
  −6.25 mV. The test checked the stored delta, absolute limit status,
  neighboring requests and boost, then restored the captured delta.
- **BIOS override** read the running firmware clock table and prepared
  a bounded downward edit of a single P0 GPC maximum. Windows denied
  registry access on both attempted versions. The test cleared the
  preparation receipts. No BIOS-test driver restart occurred, so the
  behavior of the edited running image remains unverified.

The live tests checked restoration of captured request values, curve
deltas and lock ownership before moving to the next control. The driver
can reevaluate frequencies independently of those requests. The results
apply to the tested adapters, driver versions, builds and operating points.

## Initial results and differences

### GTX 1050 Ti on 375.63

The candidate paired both driver APIs and exposed 80 curve points.
Max It reached 113 W configured power, 100% fan and 100% boost.
The 1100 mV point hold read back, while live voltage in that sample was
1081.25 mV. The separate power test read 90 → 89 → 90 W.
The rail trial moved reported effective NVVDD from
1056.25 → 1050 → 1056.25 mV. Its live voltage stayed at 1043.75 mV.
The legacy XOC discovery range stayed nominal at 66–113 W.

The first V/F verifier rejected the sub-bin difference between the
calculated and evaluated frequency. The active-point rerun independently
checked the exact signed request and verified graphics
1657 → 1645 → 1657 MHz at P0. Driver-reported memory stayed at 3504 MHz. All peer deltas
matched and the hold released. The disk Undo snapshot still reported
incomplete broader coverage. Session Undo restored the test.

The shipped core returned an empty UUID, zero V/F points, and no configured
power request. Those discovery results blocked a safe live comparison on
this driver. BIOS inventory and preparation succeeded through the candidate,
with a 1329 MHz test GPC maximum in the prepared plan.

### GTX 1050 Ti on 431.36

Both cores restored Max It, the one-bin V/F edit, the 90 → 89 → 90 W
ordinary request, and the NVVDD REL trial. At the 950 mV point, both
cores moved graphics 1708 → 1695 → 1708 MHz and left driver-reported
memory at 3504 MHz.
Both cores reported the 0.001–1999 W XOC request envelope.

The candidate used inferred V/F spacing and sent −25,312 raw kHz for
the point edit. The shipped core sent −30,000 raw kHz. Both landed at
the same evaluated point. Max It changed four curve points on the
candidate and five on the shipped core. A single P0 reading at the
1100 mV hold showed 1898 MHz on the candidate and 1911 MHz on the
shipped core. That clock difference needs repeat measurements before
attribution, and provides no frame-rate result by itself.

BIOS inventory and preparation succeeded through the candidate, with
a 1506 MHz test GPC maximum in the prepared plan.

### Live switch to 472.12

Windows bound both devices to signed `oem2.inf`, reported no required
reboot, and loaded the 472.12 display driver. Both devices reported
`CM_PROB_NONE`. The System32 NVML file still reported 431.36, and the
472.12 package's own `nvidia-smi` also failed to get a GPU handle after
both devices were restarted. Druta could see the RTX 3070 Ti through
NVAPI but could not establish its UUID, and could not select the GTX
1050 Ti. No control writes were attempted on this session. A clean boot
is needed before interpreting this as a Druta regression.

### BIOS override access failure

The error on 375.63 and 431.36 came from Windows denying the open of the
adapter registry key with `KEY_SET_VALUE`. Both test receipts recorded
`clock_unstaged` and `refresh_requested=false`. The registry writer and
production transaction code match the shipped 1.10.0 sources, so the
denial alone does not show a candidate-specific regression.

### RTX 3070 Ti on 512.15

The live switch loaded NVIDIA 512.15 and recovered this adapter with
full UUID pairing. Windows reported the GTX 1050 Ti present and started,
but NVIDIA's runtime did not enumerate it by UUID on this session.

The candidate moved configured board power 310 → 309 → 310 W. NVML
reported the same sequence. The independently reported native effective
policy moved 319.688 → 318.656 → 319.688 W. The candidate's read-only XOC
envelope was 0.001–1999 W. The shipped core exposed 100–380 W after its
ClientPower read was rejected with RM `0x3A`. This checks range discovery.
No 1999 W request was applied in this matrix.

The candidate accepted and restored all five Max It steps, including
380 W configured power and a V/F hold. The optional headroom step and
the isolated NVVDD REL request both reported an unrecognized rail-control
write packet. No rail request or ceiling moved in the isolated trial.
The shipped core returned the same rail-write error. The trial verified
the starting requests after Undo.

Both cores verified the same evaluated 950 mV V/F entry, 1845 → 1830 → 1845 MHz,
with an exact signed delta of 0 → −15,000 → 0 kHz and no peer deltas.
An earlier shipped trial restored the signed delta and released the hold,
but the evaluated frequency ended at 1845 MHz instead of its 1815 MHz
starting value. Individual live graphics samples also varied. The
firmware INFO read provided policy definitions but did not identify its
relationship-row layout, so there was no qualified weight preview or
BIOS Apply attempt. The clock-specific override is scoped to Pascal.

One short, P0-qualified candidate performance trial measured Max It at
1705.0 → 1834.3 → 1713.0 workload frames/s, and the ordinary power trial
at 1703.0 → 1704.7 → 1702.0 frames/s. These are within-build results on
the controlled workload. The shipped core could not preflight an ordinary
power write or a restorable Max It board target, so an applied-to-applied
performance comparison was unavailable.

### RTX 3070 Ti on 560.94

Both cores could read the nominal 310 W board request. ClientPower
rejected the live extended range with RM `0x1F` and the XOC range fell
back to 100–380 W. The policy GET also returned `0x1F`, so the harness
could not preflight an ordinary request or a restorable Max It board
target. The relationship INFO read also failed. Neither result verifies
what the writes would do on a clean driver session.

The first candidate V/F sample verified the new signed request, and the
live frequency stayed at 1830 MHz. The shipped core's first sample showed
1830 → 1815 → 1830 MHz. Two repeats on each core then measured
1845 → 1830 → 1845 MHz at the 950 mV entry, with matching exact peer
deltas and released holds. Both cores moved the NVVDD REL and live rail
readbacks 1081.25 → 1075 → 1081.25 mV. The rail trial measured a 0.02%
applied versus baseline throughput change on the candidate and a −0.56%
change on the shipped core. These are single short functional benchmark
runs and do not establish a performance regression.

The 1050 Ti stayed absent from NVIDIA's runtime after a targeted device
restart. No tuning was attempted on that pairing.

### RTX 3070 Ti on 581.80

The candidate's first Max It applied fan, board power and boost, then
stopped at `V/F baseline changed; refresh the curve`. Undo verified the
original settings. A benchmark retest captured the curve immediately
before dispatch and both cores completed Max It. A shorter second retest
also passed on both cores. The initial refusal remains an observed
intermittent failure. The runs do not isolate a candidate-specific cause.

The matched retest measured 1721.7 → 1861.0 → 1723.7 workload frames/s on
the candidate and 1723.7 → 1863.3 → 1724.3 on the shipped core. The
applied samples differed by 0.13% and cover three seconds each. Both
cores also applied and restored the ordinary power request, the signed
V/F entry and NVVDD reliability. The candidate read the firmware INFO
policy definitions without finding a recognized relationship layout.
The GTX 1050 Ti was still absent from NVIDIA's runtime.

### GTX 1050 Ti on 582.28 in the initial mixed-driver session

Windows staged and bound the signed package and returned a required
reboot. The RTX 3070 Ti stayed bound to the preceding package and
reported a PnP initialization error while the system used the Pascal
package. `pnputil /disable-device` refused to change the GTX 1050 Ti
because its driver installation was already pending a system reboot.
The card remained enabled. The matching NVIDIA utility could not
initialize NVML, so no control writes were attempted.

### Driver and test state at the initial pause

On 582.78, the targeted GTX 1050 Ti disable and enable both succeeded.
PnP moved from `CM_PROB_DISABLED` back to `CM_PROB_NONE`, but NVIDIA's
matching utility still reported `Failed to initialize NVML: Not Found`.

The RTX 3070 Ti had already started switching to 595.71 when the matrix
was paused. Windows reported a reboot required, but the package's own
utility returned the exact RTX 3070 Ti UUID and version 595.71 with a
310 W request at P8. The GTX 1050 Ti remains enabled and bound to its
Pascal-only 582.78 package, with a PnP initialization error while the
Ampere package is loaded. No test load or tuning request is active.

### RTX 3070 Ti on 595.71

The exact PCI slot, GPU UUID and loaded driver matched on the candidate
and the shipped 1.10.0 core. Both completed and restored Max It, the
ordinary 310 → 309 → 310 W request, NVVDD REL and the signed V/F edit.
Max It reached 380 W configured power and an 1100 mV hold on both builds.

| Workload frames/s | Candidate baseline → applied → restored | Shipped baseline → applied → restored | Candidate applied vs. shipped |
| --- | --- | --- | --- |
| Max It | 1710.00 → 1848.67 → 1709.00 | 1716.33 → 1862.33 → 1712.67 | −0.73% |
| Ordinary power | 1710.67 → 1712.33 → 1713.67 | 1711.33 → 1714.67 → 1715.67 | −0.14% |
| NVVDD REL | 1711.67 → 1703.67 → 1710.33 | 1715.33 → 1715.33 → 1716.67 | −0.68% |

Each measurement covers three complete seconds at P0. The single short
samples did not establish a throughput regression. The initial V/F run
restored signed requests and released the hold on both cores. The
candidate's evaluated table drifted during that run. On the repeat both
cores measured 1815 → 1800 → 1815 MHz at 950 mV, with stable peer entries,
exact signed restoration and released holds. The firmware INFO read
decoded policy definitions. The relationship row layout remains unknown,
so the BIOS override did not reach preparation or Apply.

### RTX 3070 Ti on 595.97

Windows loaded the signed 595.97 package in a live switch. Both cores
paired the exact GPU on that driver and completed and restored Max It,
V/F, ordinary power and NVVDD REL. The BIOS preflight decoded policy
definitions and found no known relationship row layout. No BIOS edit
was prepared or applied.

| Workload frames/s | Candidate baseline → applied → restored | Shipped baseline → applied → restored | Candidate applied vs. shipped |
| --- | --- | --- | --- |
| Max It | 1713.33 → 1847.67 → 1710.00 | 1716.00 → 1861.00 → 1715.67 | −0.72% |
| Ordinary power | 1711.67 → 1715.67 → 1713.67 | 1711.67 → 1703.67 → 1710.33 | +0.70% |
| NVVDD REL | 1715.33 → 1715.00 → 1716.00 | 1714.00 → 1714.33 → 1715.67 | +0.04% |
| Max It repeat | 1722.33 → 1862.00 → 1723.00 | 1721.67 → 1861.67 → 1723.00 | +0.02% |

Each phase covers three complete seconds at P0. The initial Max It
sample had a 15 MHz core-clock difference at baseline and under load
with Max It applied. In the repeat, both cores measured the same
1980 → 2145 → 1980 MHz sequence and nearly the same throughput. The
initial V/F run restored signed requests and released holds on both
cores. On the repeat the assessed 950 mV entry measured
1845 → 1830 → 1845 MHz on both cores, with stable peer entries and exact
restoration. These runs did not establish a reproducible performance
regression for the tested RTX 3070 Ti controls.

### Checkpoint for clean driver testing

At the handoff, the RTX 3070 Ti was on 595.97 at P8 and the original
310 W configured request. The GTX 1050 Ti was bound to 582.78 with PnP
Code 31 and was absent from NVML. The test processes had exited.

The handoff plan called for a clean 472.12 boot to retry both rows, then
the blocked GTX 1050 Ti pairings and an instrumented Max It repeat on
431.36. BIOS activation remained unverified at the handoff.

### Clean 472.12 restart preparation

The restart was authorized on November 26. The default Boot Manager
entry points to a loader absent from its store. The one-time boot
sequence selects the running Windows 10 installation on C:. A backup
of the boot store is in `before-cleanboot-47212.bcd` in the run artifacts.
Windows bound both adapters to signed 472.12 (`oem2.inf`). The GTX
1050 Ti bind required a restart. The pre-boot binding receipt is
`bind-47212-cleanboot-r1.json`.

The `Druta-Matrix-47212-Once` task runs at the first Administrator
sign-in after a new boot. It records Windows binding and the matching
NVIDIA runtime, then reuses the matrix harness for qualified live
controls. The task removes its registration after that attempt. Read
`cleanboot-47212-r1-status.json`, `cleanboot-47212-r1-log.txt` and the
matching per-control exit files before starting any further live run.
The login task covers the four reversible controls. BIOS activation
remains a separate test with its own adapter and recovery checks.

### Both GPUs after a clean 472.12 restart

Windows started on November 26 at 17:29:54 China time. Both adapters
reported PnP Code 0 and the exact expected PCI slots and UUIDs through
the package's NVML tool. The login task ran all 16 live trials across the
two cards and builds. Every trial exited successfully and verified its
restoration. The task removed its registration after the run.

On the GTX 1050 Ti, both evaluated V/F tables measured
1708.5 → 1695.5 → 1708.5 MHz with exact signed-request restoration and
released holds. The candidate ordinary-power sample initially measured
523.00 workload frames/s versus 532.00 on the shipped core. The matched
repeat measured 531.00 versus 532.33 from the same 531.33 baseline, a
0.25% difference. The initial candidate Max It sample measured
544.00 versus 546.33 workload frames/s.

On the RTX 3070 Ti the evaluated target changed from 1845 to 1830 MHz
and restored to 1845 MHz in the candidate run. The shipped sample
stayed at 1830 MHz. Both verified the exact signed request, the original
evaluated table and hold release. The candidate Max It sample reached
1862.00 workload frames/s versus 1769.67 on the shipped core, from
different starting clock and frame-rate states. The short samples and
within-run drift leave the cause of that difference undetermined.

The first Pascal firmware preparation stopped at the helper's live
telemetry check. With sustained renderer work at P0, preparation
succeeded for a 1822 MHz P0 GPC maximum. The Ampere preflight identified
the 4096 relationship value and prepared a 4095 preview. On both cards
the production writer returned registry access denied before a driver
refresh. Fresh readback verified the original image and the absence of
staged registry values. The evidence is in the `bios-*-47212-*.json`
reports, including the untouched historical Pascal receipt check.

### Both GPUs on the 512.15 repeat bind

After the clean 472.12 session, Windows bound the signed 512.15 package
to both adapters. NVML confirmed both intended PCI slots, UUIDs and the
loaded driver. The successful GTX 1050 Ti pairing resolved the earlier
runtime enumeration failure for this session.

On the GTX 1050 Ti, the candidate applied and restored all four live
controls. Ordinary power moved 90 → 89 → 90 W. The reported effective
NVVDD limit moved 1056.25 → 1050 → 1056.25 mV. Both cores applied and
restored the V/F request and measured 1708.5 → 1695.5 → 1708.5 MHz at the
selected entry. The shipped core could not preflight a restorable power
request for the ordinary power and Max It paths. Its rail writer
rejected the packet, and the baseline readback matched.

On the RTX 3070 Ti, the repeat reproduced the earlier functional
results. Both cores passed V/F and rejected the NVVDD packet, with the
pre-trial request verified. The candidate passed ordinary power and
Max It, while the shipped core stopped at their power preflight with
RM `0x3A`. There is no paired applied-throughput measurement for the
controls blocked on the shipped core. The BIOS relationship layout
remained unknown on the RTX 3070 Ti.

The Pascal 512.15 firmware trial prepared a 1816 MHz P0 GPC limit. The
test launched the production helper directly from the elevated process.
Registry access was denied again, and the test verified original ROM
and registry state. The 472.12 retry from a highest-privilege interactive
task had produced the same Pascal result. The registry writer and
production launcher match the shipped 1.10.0 source, so the evidence
does not establish a candidate-specific BIOS regression.

### Both GPUs on the 560.94 repeat bind

The signed 560.94 bind recovered both runtime UUIDs. Both cores on
both cards passed V/F and NVVDD REL and restored the original requests
and holds. The board power preflight on every pairing hit the same
policy GET error, RM `0x1F` for control `0x2080A618`. That left Max It
and ordinary power without a verified restorable write path. The
candidate read the Pascal clock table. The Ampere relationship INFO
read failed before preview.

After a clean restart into the same 560.94 package, both UUIDs paired
again. Each of the eight candidate and shipped board-power or Max It
retries stopped at the same preflight check. The Ampere INFO read still
failed. A live-switch residue alone does not account for the result.

### GTX 1050 Ti on the 581.80 repeat bind

Both adapters bound to signed 581.80 and paired with their expected
NVML UUIDs. Both cores on the GTX 1050 Ti passed and restored all four
live controls. Max It reached 113 W and the 1100 mV hold, with the same
six changed curve points and restoration on both builds. The ordinary
power request measured 90 → 89 → 90 W. The NVVDD ceiling measured
1056.25 → 1050 → 1056.25 mV. The evaluated V/F entry measured
1708.5 → 1695.5 → 1708.5 MHz with signed restoration and hold release.

Both Max It samples reached 545.33 workload frames/s. The power and
rail applied-to-baseline throughput changes differed by less than
0.2 percentage points between the builds. The candidate read the
running Pascal firmware clock table. BIOS activation was not attempted
on this version after the production permission failure on 512.15.

### Isolated GTX 1050 Ti on 431.36

With the monitor connected to the GTX 1050 Ti, Windows showed that card
as the primary display and reported the temporarily disabled RTX 3070 Ti
as PnP Code 22. The GTX 1050 Ti used signed 431.36 `oem45.inf` and was the
only adapter visible to the matching NVML tool.

The instrumented Max It repeat reproduced the earlier clock difference.
Both builds started and restored at 1847 MHz. The candidate changed four
curve points and measured 1898 MHz with Max It applied. The shipped core
changed five points and measured 1911 MHz. Candidate throughput was
530.67 → 545.33 → 530.00 workload frames/s. Shipped throughput was
530.00 → 548.67 → 531.33. The applied difference was −0.61% in this short
sample, with exact restoration in both trials.

The captured baseline curves and Max It caps matched. The candidate
inferred a 12.656 MHz grid where the shipped core used its 15 MHz
fallback. At the held 1100 mV point the shared ramp planner therefore
requested +25,312 raw kHz on the candidate and +60,000 on 1.10.0.
Replaying the planner against the recorded baseline reproduced both
sets of requests. The recorded held frequencies follow their evaluated
curve entries.

### Max It clock-grid compatibility fix

Max It uses the released ramp fallback when NVML cannot measure a
lockable clock grid. A measured grid still goes to the ramp planner.
The inferred physical grid remains available for other curve operations,
and the predictor still receives the physical model bound. The captured
431.36 band replay again produces the shipped five edits and +60,000
raw kHz at 1100 mV. All 170 CTest cases passed. [Max It planner](../native/ui/tuning_actions.cpp)

Two live Apply and Undo pairs on 431.36 ran the rebuilt UI harness
against both cores. The second pair ran the shipped core first. Each
pair started from an 1835 MHz live clock and matched all baseline,
applied and restored curve entries across the builds. With this
baseline both produced six edits, requested +90,000 raw kHz at
1100 mV and held 1911 MHz. Each trial restored 1835 MHz, the original
90 W request and the complete curve, and released the hold.

The applied workload measured 533.67 and 478.33 frames/s in the first
pair and 548.67 and 459.33 frames/s in the second, candidate then
shipped in each pair of values. Baseline and restored frame samples
also varied. The exact curve and 1911 MHz clock match reproduced in
both orders. The throughput samples are limited to three seconds per
phase. The raw artifacts use the `max-it-grid-fix-r1` and
`max-it-grid-fix-r2` labels.

On 581.80 the GTX reported a measured 12.657 MHz step. Both cores
produced three curve edits with +25,314 raw kHz at 1100 mV, measured
1847 → 1898 → 1847 MHz and restored the original 90 W request. Both
adapters returned healthy on signed 581.80, and the rebuilt Druta
desktop window reopened against the RTX 3070 Ti.

### Isolated GTX 1050 Ti on 582.28

With the RTX 3070 Ti disabled, signed `oem52.inf` bound without a
reboot requirement. The driver reported the exact GTX UUID at its
expected slot. Both cores passed and restored all four live controls.
Max It used six curve edits and measured 1835 → 1885 → 1835 MHz at the
1100 mV hold under a 90 → 113 → 90 W board power request. V/F, ordinary
power and rail-limit readbacks matched between the builds.

Max It measured 544.33 and 544.50 applied workload frames/s on the
candidate and shipped cores. Baseline-adjusted throughput changes
differed by less than 0.1 percentage points in all three gradeable
controls. The Pascal firmware clock table was readable.

### Isolated GTX 1050 Ti on 582.78

Signed `oem53.inf` bound without a reboot requirement. Windows reported
PnP Code 0 on the GTX 1050 Ti, and the matching NVIDIA runtime saw its
exact slot and UUID. Both builds applied and restored all four live
controls. Max It changed five curve points and measured
1835 → 1873 → 1835 MHz on both builds. Reported applied throughput
was 541.33 and 541.00 workload frames/s on the candidate and shipped
cores. Applied power and rail samples differed by less than 0.5%.
The candidate's first power baseline ran slower than its restored
baseline, limiting the within-run comparison. The Pascal firmware
clock table was readable.

### Restored workstation state

The shared 581.80 rebind returned both GPUs to signed `oem50.inf` and
PnP Code 0. NVIDIA's runtime reported both expected UUIDs, the RTX
3070 Ti at its initial 310 W request and the GTX 1050 Ti at its
initial 90 W request. Both were at P8 and all test processes had
exited. The GTX 1050 Ti was the active primary display, and the candidate
Druta desktop window opened against the RTX 3070 Ti.

## Evidence

The local run artifacts are under
`native/build/clock-discovery/driver-gpu-regression-20261125/`.
The `gp107-37563-*` and `gp107-43136-*` files contain the initial
capabilities and the baseline, applied, and restored states for each
trial. The `bind-*.json` files record the signed INF binding and loaded
driver evidence. The unchanged candidate passed 170 of 170 tests in
the host-independent CTest suite. The public
[adapter matrix](../ADAPTER-MATRIX.md) is historical context.

For 595.71 and 595.97, use the `ga104-*-takeover1-*` and repeat artifacts.
The `bios-ga104-*` JSON reports capture the read-only firmware checks.
The November 26 results are in the `*cleanboot-47212*`,
`*common-matrix-r1*`, `*clean560-board-r1*` and `*pascal-isolated*`
artifacts. The final device and runtime receipts are
`pnp-58180-both-after-restore.json` and `runtime-58180-both-after-restore.txt`.
