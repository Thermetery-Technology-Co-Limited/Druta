# Ada / RTX 4080 SUPER validation

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
were zero and disabled. Read-only register `0x39` was `0x94`. Idle controller
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
