# Ada / RTX 4080 SUPER validation

**2026-10-09 update:** the private [disabled-offset storage test](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-I2C-DISABLED-OFFSET-WITNESS.md)
proved reversible uP9512R `0A: 00 -> 01 -> 00` storage through the combined-prefix
route on both 617.42 and 581.42. Each run made one change, one restoration and
28 independent register reads; `2A=20` and all other sampled bytes stayed exact.
All 26 runner tests passed, and root plus independent evidence review passed.
Both driver switches needed no reboot; 617.42 and exact controller state were
restored, with Druta open and responding. The recorded interval had no selected
fault event or query error. This proves register storage, while active voltage
response and a production fallback remain unverified. Application code and the
packaged EXE are unchanged. Earlier results below retain their original scope.

The completed 2026-10-07 captures cover one user-described Manli RTX 4080
SUPER on NVIDIA **617.42 and 581.42**. Both NVAPI and NVML identified device
`10de:2702`, subsystem `41401458`, VBIOS `95.03.44.40.09`; nvtune identified
AD103. These identifiers document the tested adapter, not compatibility gates.
The compact [public evidence](experiments/ada-ad103-validation.json) contains
selected requests, readbacks, restoration results and source-report hashes.
It excludes UUIDs, raw control buffers, client handles and local paths.

Generation selects candidate interfaces. The current adapter must still
pass handle pairing, packet geometry, field and capability checks. Results
on this board do not establish another board's voltage references, limits,
rail topology or physical response. Captured entry state and quantized
first-read references are not factory defaults.

## Ordinary controls

Each operation below passed selected-request readback and restoration on
both drivers:

| Control | Temporary request |
| --- | --- |
| Core offset | -15 MHz |
| Memory offset | -12.5 MHz true, encoded as -200 wire units |
| Configured power | 320 W to 319 W |
| Fans | Both fans to manual 50%, then original automatic policy |
| Voltage boost | 0% to 5% |
| V/F point | 900 mV point down one 15 MHz bin |
| V/F hold | 900 mV |
| NVML frequency lock | 1500 MHz minimum and maximum |

The V/F table contained 128 entries: 127 GPU points and one trailing row
matching a memory clock. The measured graphics grid was 15 MHz. This
GDDR6X adapter reported memory divisor 8 and a public memory-offset scale
of 16 wire units per MHz true. The power range was 150-400 W, with a reported
320 W default. These values come from the tested adapter.

Profile capture had no incomplete sections and survived JSON serialization
on both drivers. The separate integration runs below exercised production
profile replay. The ordinary-state comparison passed at the end of each run. Fan restoration
compared policy and manual targets; automatic duty can change with temperature.

## Production profiles and hold headroom

Separate integration runs on both drivers exercised production profile
preflight, replay, recapture and restoration with capability gates unchanged.
One profile combined core/VF -15 MHz, memory -12.5 MHz true, fans 50%,
SYS? -15 MHz, NVVDD +5 mV, policy 13 current -1 A and reliability -5 mV.
Every production replay result and the final entry-state comparison passed.

Both runs exercised the default 25 mV hold-headroom path. The test first
lowered the reliability, alternate and overvoltage ceilings to 900 mV,
verified refusal of a held point above that ceiling without a rail write,
then applied 925 mV temporary ceilings for a 900 mV hold. These temporary
ceilings stayed below the captured entry ceilings. Live guards passed:
reported samples were 915-920 mV on 581.42 and 915 mV on 617.42. Profile
capture excluded the temporary raise. The owned headroom and complete entry
state were restored. This validates the production workflow and stored
state, without establishing performance or voltage behavior at other
operating points.

## Additional clocks

Both drivers accepted version `0x000261A4`, size `0x61A4`, header `0x124`
and entry stride `0x304`. The frequency request is at `+0x10C`; the NVVDD
offset is at `+0x110`. Candidate entries 0-9 were readable. Readability alone
does not establish every field's meaning or writability.

With a 1500 MHz core lock and bounded CUDA copy workload, controls 1/3/5/9
stored -60 MHz requests and control 2 stored -64 MHz. Complete original
control packets were restored after every probe. The table gives medians of
the **programmed** target row, before/during/after each request:

| Control | Tentative name | Programmed row | 617.42, MHz | 581.42, MHz |
| --- | --- | ---: | --- | --- |
| 1 | Crossbar? | 1 | 1380 / 1320 / 1380 | 1380 / 1320 / 1380 |
| 3 | SYS? | 2 | 1320 / 1260 / 1320 | 1320 / 1260 / 1320 |
| 5 | Video? | 21 | 1320 / 1260 / 1320 | 1320 / 1260 / 1320 |
| 9 | LTC? | 5 | 1350 / 1290 / 1350 | 1350 / 1275 / 1335 |
| 2 | Memory | 4 | 11251.968 / 11187.960 / 11251.968 | 11251.968 / 11187.960 / 11251.968 |

Control 1 also moved programmed rows 2 and 5. On 581.42, the LTC? row's
running clock varied even though the request and restored packet were exact.
The separate XBAR/SYS counter probes did not follow the programmed shifts;
their values do not independently confirm those engine names or physical
clock changes. The names in those original probe records were tentative;
the later GPU-Z comparison below corroborates three of them. Druta does not
apply Blackwell's physical-counter interpretation to Ada. The memory counter
samples did move lower, but the table's memory-domain MHz are distinct from
the public editor's MHz true.

Additional exploration on 617.42 found that controls 4, 7 and 8 stored their
requests without an observed programmed-clock effect in this workload.
Control 6 did not retain the request despite a successful setter status.
Those results do not establish additional user-facing tuning controls.

### GPU-Z name corroboration

A later user-supplied screenshot on 2026-10-07 shows GPU-Z **2.71.0** beside
Druta **1.7.0** on this RTX 4080 SUPER, driver **617.42**. The displayed
frequencies corroborate three engine names:

| Private domain | GPU-Z name | Druta array A, MHz | GPU-Z, MHz |
| --- | --- | ---: | ---: |
| 1 | Crossbar Clock | 2355.0 | 2357.3 |
| 2 | SYS Clock | 2250.0 | 2246.7 |
| 21 | Video Clock | 2175.0 | 2180.2 |

Combined with the restored control-to-domain probes, this supports plain
**Crossbar**, **SYS** and **Video** names in the monitor and controls. The
applications poll separately; the screenshot is one operating point on one
board and driver, not a synchronized frequency trace or a performance test.
It does not validate the separate clock-measurement API or establish either
GetAllClocks array as an independent physical counter on Ada.

Domain 5 was 1335 MHz, with no named GPU-Z counterpart. Its **LTC?** label
remains inferred: control 9's routing is established, but elimination among
the named rows does not identify the engine independently. Domains 3, 6,
20 and 22 also remain unnamed. The public evidence records the screenshot
hash and readings separately, preserving the original probe labels.

## NVVDD and current policies

At a 2400 MHz frequency lock on both drivers, a +25 mV NVVDD request stored
exactly +25000 microvolts. Median reported core voltage moved
**915 / 940 / 915 mV**, while the reported core clock stayed at 2400 MHz.
The separate rail-status voltage medians were **925 / 950 / 930 mV**.
These are different software telemetry channels, not external electrical
measurements. On 617.42, a separate 1000 mV V/F hold kept reported voltage at
1000 mV while the same offset moved the core clock **2640 / 2580 / 2640 MHz**.
An offset's effect depends on the operating point.

All four NVVDD limit fields accepted small changes with exact selected-field
readback and restoration on both drivers: reliability 1070 mV, alternate
reliability 1095 mV, overvoltage 1195 mV, and a minimum-voltage increase of
5 mV. That floor request was 910 mV on 617.42 and 905 mV on 581.42, reflecting
their session references. These are not Ada-wide reference values. Rail 1's
getters rejected requests on this adapter; other Ada boards must be assessed
through their own getters.

Policy 13 reported type `0x0F`, channel 19 and current units, with a 1 mA
minimum and 400000 mA default/maximum. Both drivers accepted **400 / 399 /
400 A** with matching requested and effective-limit readbacks. This validates
the policy interface, not a measured 399 A load or physical current threshold.

The two drivers exercised different current-policy geometries:

| Capture | Info/status/control/set bytes | Info header/stride | Status header/stride | Control header/stride |
| --- | --- | --- | --- | --- |
| 581.42 | 8620 / 172080 / 4432 / 4432 | `0x58 / 0xE4` | `0x70 / 0x1454` | `0x14 / 0x7C` |
| 617.42 | 20512 / 397048 / 13876 / 13876 | `0xCC / 0x104` | `0x9C / 0x1730` | `0x14 / 0xC4` |

The 617.42 implementation DLL's command dispatch provided the new sizes;
runtime getters then agreed on occupied policy types, masks and geometry.
Earlier candidate info sizes were rejected with RM `0x1F`. The implementation
selects the accepted geometry from packets, without checking driver strings.
The inspected DLL's SHA-256 is recorded in the public evidence.

## nvtune and memory timings

nvtune v1.0.2-alpha identified AD103 and read timing registers successfully
on both drivers. Captures cover idle, CUDA-loaded and held-P0 states. Druta
calculated a read-only RFC preview and attempted one raw-field increment,
**460 to 461**, in `CONFIG0` at `0x9A0290`, bits 8-16. RFC nanoseconds remain
untrusted; this was one encoding increment, not an established timing duration.

On both drivers, the helper exited 1: immediate readback returned original
`0x2C91CC74` instead of requested `0x2C91CD74`. Druta recorded failed Apply
with RFC still 460. All active partitions remained unchanged in subsequent
captures. Timing-register restoration and original lock packets were verified.
The helper attributed frequent reversions to GSP memory-training ownership;
this test did not independently prove that mechanism. No force flag, daemon,
structural timing field or inferred register was used.

Timing reads and preview worked on these configurations. Persistent timing
adjustment was not demonstrated and is not represented as successful.

## uP9512R I2C discovery and verification

The original production read-only I2C sweeps completed 2736 route/controller
probes on each driver with no recognized candidates. Those sweeps predated
the uP9512R adapter and did not test that controller's identification recipe.
No I2C writes were attempted during those original sweeps.

After adding the [uP9512R adapter](i2c/UP9512R.md), production discovery with
that controller selected completed **896/896** probes on **each driver**.
Both found one candidate: NVAPI port **2**, seven-bit address **0x25**,
vendor/device IDs **0x00/0x2B**. Entry control bytes at
`0x0A/0x0B/0x0C/0x2A` were `00/00/00/20`, meaning all five offset fields
were zero and disabled. Register `0x39` was read as `0x94`; it was not written. Idle controller
FB was 930-940 mV and IMON was 110 mV. These are controller ADC voltages,
not calibrated rail-voltage or current measurements. The route and readings
are observations on this board, not discovery gates or universal references.

At held P0 operating points, production Verify failed on the first +10 mV
rung on **both drivers**. Its first byte write, `0x0A = 0x11`, returned
**NVAPI -1**, byte count 1, with both extra 32-bit words zero. All four entry
control bytes, the SMBus lock value and the temporary GPU holds were confirmed
restored. A separate 617.42 same-value write, `0x0A = 0x00`, at explicit
100 kHz also returned -1 and left the controls unchanged. The failure's cause
was not established; an unlocked register value alone did not make these
NVAPI writes succeed. Druta never wrote the SMBus lock.

An independent documented RM SMBus-byte operation at 100 kHz also read the
IDs, all control bytes and lock successfully on both drivers, using the native
I2C object matched to the selected GPU. Its same-value `0x0A = 0x00` write
returned **`0x16` (`NV_ERR_ILLEGAL_ACTION`)** on both. Independent readback
confirmed the controller state was unchanged. The internal refusal reason
remains unknown; this diagnostic transport is not shipped as a fallback.

The 2026-10-08 policy follow-up tested NVAPI/RM caller privilege and alternate
driver-owned transports on both drivers. Privileged reads worked, but the
unchanged byte still returned NVAPI `-1` or RM `0x16`. Known-register reads
failed with CPU software I2C, so the guard performed no data write. Those
reads succeeded with PMU software I2C, which still rejected the unchanged byte.
Six same-value requests were made across both drivers; none established
successful physical write traffic.
All temporary registry settings were removed, eight device restarts and both
driver switches completed without requesting a reboot, and independent reads
confirmed the original seven identity/lock/control bytes. Driver 617.42 was
restored. No new monitored crash/TDR/dump-error event occurred in this round.
The exact host/firmware rejection condition remains unresolved; the separate
[policy research record](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/I2C-POLICY.md)
distinguishes static Falcon-status translation from live response evidence.
Independent binary reviews exclude normal ownership/restore returns as the
direct `0x16` source on both drivers. Further 617.42 queue-path exclusions are
conditional on runtime state that was not captured. The installed Ada HAL also
confirms the earlier BAR0 register and `07` command, but does not require the
mode bit to echo its written value; the observed `07 -> 33` mismatch is not
proof of a BIOS lock. No additional controller write follows from these
offline findings.

A subsequent separate line diagnostic on both drivers requested `07` and
`06`, receiving `33` after each, with no SCL-low observation. It stopped at its
output guard before intended byte traffic. The probe did not confirm cleanup;
separate device reinitialization preceded independent controller postflight,
which matched the original captured register state. Same-context CPU-mode port-info
queries stayed valid (`11`) around identity-read failures (`FFFF`). No new
controller-data write request or working write path was established. Both
overrides were removed and 617.42 was restored after the six additional device
restarts and two switches, without a reboot request or new monitored crash event.

A later ETW diagnostic round positively located both drivers' CPU identity-read
failures at NVIDIA's SCL-high guard. Normal unchanged `0A=00` writes reached the
PMU send routine, which returned `16` to its caller. This strengthens the path
evidence but does not capture a raw firmware response or supported unlock.
Two further unchanged-byte requests were rejected; independent seven-register
readback remained exact. Five bounded traces reported no loss and preserved
existing trace sessions. Both overrides were removed, four mode restarts and
two driver switches completed, and 617.42 plus the verified EXE were restored.
No new monitored crash/TDR/dump-error event occurred in the recorded interval.
The [return-site research](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-I2C-ETW.md)
records positive observations separately from the conditional firmware-origin
inference. No application write-capability claim or production fallback changed.

A subsequent passive diagnostic tested the private host RPC statistics getter
`0x20803126` once on each driver. Both returned RM `0x56` with all 120 parameter
bytes unchanged, so no valid counters or raw PMU status were obtained. The
rejection does not identify the handler branch or runtime eligibility state.
This round made no controller-data writes or direct research MMIO requests.
Independent seven-register readback remained exact after both driver switches;
617.42, nvtunedrv and the verified EXE were restored. See the
[completion-diagnostics record](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-PMU-COMPLETION-DIAGNOSTICS.md).
This binary-derived interface is research only; application write support
remains unverified.

On 617.42, an earlier loaded point reporting GPU VID 1195 mV was refused by
the 1200 mV controller-FB guard before any write. The subsequent verification
attempt used a 1000 mV GPU hold. VID and FB are separate telemetry channels.
No successful physical offset response was demonstrated. Common Apply,
I2C profile replay and Reset were therefore **not exercised on hardware**;
their transport, profile and UI behavior has mocked regression coverage.

## Scope and restoration

Every ordinary-control step restored its captured entry state. Every private
clock, NVVDD, rail and current probe passed its restoration check, including
complete clock/lock packet comparisons where applicable. Exploratory clock
probes also restored state when requests had no effect or failed readback.

During the later uP9512R validation, switching to 581.42 reported installer
success with no reboot required, followed by an unexpected PC shutdown.
Windows recorded Event 6008; crash dumps and crash logging were disabled,
so the cause was not established. A second switch to 581.42, with tests and
the Druta UI closed, was interrupted by another unexpected shutdown before
the installer return was captured. After reboot, 581.42 was active and the
controller bytes were unchanged. Both returns to 617.42 completed successfully
without a reboot. The original GPU profile's raw requests and holds were
restored. Its estimated minimum-voltage first-read reference changed from
925 to 920 mV despite the identical raw zero-delta request, so the derived
absolute profile values were not byte-for-byte equal across sessions. No
voltage offset was left applied.

Stored requests, programmed values and physical effects are distinct. These
captures do not establish performance improvements, long-term stability,
memory integrity, other operating points, successful I2C adjustment or final UI
callback behavior. The bounded CUDA copy workload exercised
bandwidth; it was not a memory-integrity checker.

The final hardware-free regression run after the uP9512R changes passed
**1649 tests**, including the synthetic DearPyGui renderer checks, following
the return to 617.42. After restoring the stripped Windows capture components,
native desktop inspection on 617.42 confirmed the control and monitor views and
the Clocks menu's enabled "Keep headroom above a held point (strongly recommended)"
option, with a 25 mV margin. This was visual inspection; complete desktop UI
callback testing was not performed. Source-distribution and Windows bundle
packaging include this document and its public evidence summary.

## Raw response capture on both drivers

A later bounded entry trace positively captured the response to one unchanged
`0A=00` WriteEx request on each of 581.42 and 617.42. Each driver's known
supersurface response caller supplied phase `0`, I2C category `1`, write
operation `0`, raw status byte `03`, and translated RM status `0x16`; NVAPI
returned `-1`. A successful identity-read response validated the same observation
path before each write capture. This supersedes the earlier uncertainty about
whether a raw rejection response could be observed.

The specific firmware policy predicate and a supported unlock remain unknown.
The captures do not establish a controller password requirement, a BIOS bit to
clear, or an accepted write. All seven controller registers remained exact after
each attempt; `39` and `2A` were never written. The round added two explicit
unchanged-byte requests and no direct research MMIO requests. Two driver switches
completed without a reboot request. Independent readback confirmed the original
state after restoring 617.42; the tracer was removed, research services stopped,
nvtunedrv running, and the verified EXE reopened. The monitored interval contains
no new crash/TDR/dump-error event and no event-query errors. See the
[raw-completion evidence](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-RAW-I2C-COMPLETION.md) for exact binary bindings, capture checks,
and the remaining interpretation limits. Application write support is unchanged.

An offline [firmware preparation follow-up](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-PMU-IMAGE-PREPARATION.md)
confirmed the static Ada PMU archive bindings in 581.42, supplementing the
617.42 image identification. Neither older stored image contains ELF magic.
The 617.42 Ada preparation and upload method bindings are also statically
resolved. Its normal conditional supplier replaces a 12 KiB prefix ending
before the bootloader and image body; simulating that replacement reveals no
ELF image. A post-copy PMU callback is a no-op. Further independent review
resolved the additional helper as a Confidential Compute path: AD103's inspected
normal initialization cannot enable that feature, and this binary's helper
cannot return success to its caller under normal call/return behavior. It may
still update bookkeeping before failing. Live eligibility, transfer mode and
other possible image transformations remain unobserved. This static work made
no hardware requests or runtime changes and did not recover the I2C predicate.

A separate [bounded PMU status observation](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-PMU-STATUS-VISIBILITY.md)
on 617.42 read three statically derived BAR0 locations through the signed default
reader: BCR_CTRL `111`, CPUCTL `80`, and ICD_CMD `7`. The decoded fields show
RISC-V selected and active, HALTED clear, and ICD BUSY/ERROR clear. All replies
had valid geometry and no poison. Fourteen controller reads before/after matched
the seven-register state; there were no MMIO writes, controller-data writes,
halt requests, driver switches or display restarts. The reader unloaded, Druta
remained responsive, boot time was unchanged, and the monitored interval had no
selected crash/TDR/dump-error event or event-query error. This new status test
was performed on 617.42 only. Readable status does not establish debugger-memory
permission or an SMBus unlock; production write support remains unverified.

### Isolated PMU debugger-status follow-up (617.42 only)

The separate ring0 research branch now records one fixed RSTAT4 status
command. `ICD_CMD` changed from `7` to `40E`, with BUSY/ERROR clear and a raw
64-bit result of zero; ACTIVE remained set and HALTED clear. The first poll
was already idle, so no BUSY transition or exclusive result ownership was
observed. No meaning is assigned to the zero result. One direct MMIO command
write and 19 direct reads occurred; seven controller reads before and seven
after matched, the helper unloaded, and boot time stayed unchanged.

This does not establish debugger-memory access, a firmware policy unlock or
accepted uP9512R writes. It changes no application controls or packaged
binaries. See the [RSTAT4 research record](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-PMU-RSTAT4.md)
for the exact static proof, scoped live evidence and ownership limitations.

### Raw I2C block follow-up

One native raw-block request containing unchanged `0A 00` returned NTSTATUS
zero / RM `0x16` on each of 617.42 and 581.42. Both native identity reads and
independent seven-register baseline/readback passed. The raw layout has zero
index bytes and two data bytes, unlike the indexed byte request; this format
also failed to establish accepted writes. The round made two unchanged write
requests, zero direct research MMIO requests, and no lock/enable writes.

617.42 and the verified EXE were restored with exact controller state and
research services stopped. The user manually rebooted during the interruption;
boot continuity applies only to the resumed test interval. No application code
or packaged binary changed. See the [alternative-route evidence](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-I2C-ALTERNATE-ROUTES.md)
for the response-format validation, static route limits and PMU memory-read
boundary. The exact firmware predicate and a supported unlock remain unresolved.

### Ownership callback follow-up (offline)

An exact-image static trace on 617.42 binds the AD103 I2C ownership callback
to PMGR token and per-port hardware mutex operations. Its token-acquire
register read is stateful. The live owner, mapped port, and firmware policy
remain unobserved; this is not an unlock or successful-write result. No
hardware operation, application change, or packaged-binary change was made
for this follow-up. See the [ownership effects and limits](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/I2C-POLICY.md#ownership-callback-hardware-effects-61742-offline).

### Native read ownership observation (617.42)

A subsequent one-shot capture observed owner `0x66`, mapped port `2`, host
resource `0x0C`, acquired mask `0x04` and saved token `0xDA` during a successful
identity read. The actual owner selects the PMU wrapper's existing-owner
release/reacquisition branch. These are host fields, not physical mutex
readback, write-time state, or a firmware unlock.

An initial attempt read the identity successfully but failed its caller guard;
that capture remains inconclusive. The corrected native indexed-read caller
was statically verified and independently reviewed before a fresh attempt.
Across both attempts: two explicit identity reads, no controller write, no
direct research MMIO, no explicit token acquisition and no driver switch.
Tracer removal and restoration passed for both, with the verified EXE reopened,
boot/service state unchanged and no selected event or event-query error in the
test windows. No full controller-state readback or sustained RAM-stability
claim follows. Application code and the packaged EXE are unchanged. See the
[capture evidence and limitations](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-I2C-OWNER-OBSERVATION.md).

### Write-time ownership comparison, both drivers

One unchanged `0A=00` write per driver captured owner `0x66`, mapped index `2`,
resource `0x0C` and acquired mask `0x04`, selecting the existing-owner branch.
Those host fields match the earlier successful 617.42 read, while both writes
still return NVAPI `-1`. Physical mutex contents, later ownership completion,
firmware permission and accepted writing remain unverified. This round did
not capture a fresh RM or raw firmware response.

All seven baseline/readback registers matched on each driver. After two driver
switches, 617.42 and the verified EXE were restored, a final independent
seven-register readback matched, and the tracer was absent. Totals: two rejected
unchanged-value writes, 35 explicit reads, no direct research MMIO or explicit
token acquisition. No reboot was required or observed, and the full monitored
interval had no selected fault event or query error. Application code and the
packaged EXE are unchanged. See the [write ownership evidence](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/ADA-I2C-WRITE-OWNERSHIP.md).

### Request configuration follow-up, 617.42

An offline follow-up identifies the previously unnamed request field at
`I2C + 0xF2C` as the driver's clock-stretch timeout override. A separate
per-port association mask controls request bit 19; its firmware meaning
remains unresolved. Neither finding establishes a supported write unlock.
No hardware request, registry change, driver switch or runtime code change
was made for this follow-up. See the [configuration analysis](https://github.com/Thermetery-Technology-Co-Limited/drutadrv/blob/ada-up9512r-bar0-probe/research/I2C-POLICY.md#request-configuration-follow-up-61742-offline).
