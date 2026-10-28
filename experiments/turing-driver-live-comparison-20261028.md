# Turing live driver comparison: 431.60 and 621.01

Both supplied drivers were installed and exercised on the owner's Gigabyte
RTX 2080 SUPER AORUS Xtreme / TU104 on 2026-10-28. The live comparison found
two additional packet families and an important enforcement difference:
**431.60 accepts extended configured requests but its settled effective
status plateaus at 360.938 W; 621.01 follows the higher requests.**
An effective override on 431.60 remains unresolved. Neither newly installed
driver received a physical power-under-load qualification in this session.

## Device and operating point

The selected adapter was PCI `0000:0a:00.0`, architecture 6, with VBIOS
`90.04.7A.40.3D`. The owner reports shunt modification and a 16 GB VRAM
modification that is unstable in P5/P8. A domain-6 mode-3 V/F hold at
1062500 uV was retained or reacquired as the driver interface became
available. Every qualified sample verified P0. Driver-reset intervals
were not continuously observable, so this is not a claim that every
transitional P-state was measured.

The post-interruption captured request was 250 W. This is the restored
session baseline, not an independently identified factory default.
Live metadata reported an ordinary 105/250/350 W minimum/rated/maximum.

## Activation and method

431.60 Standard initially encountered the installed DCH stack's installer
compatibility checks. An initial PnP attempt failed a coinstaller step and
left mismatched user-mode components; no power trial was conducted in that
state. Installing 621.01 repaired the stack, then removing its display
package and installing 431.60 through PnP activated a consistent 431.60
kernel/NVAPI/NVML stack. The final PnP operation reported no reboot needed.
After the 431.60 trials, PnP activated 621.01 again and reported no reboot
needed. Installer post-processing errors were distinguished from actual
activation by fresh API processes and the loaded display-driver version.
Windows was never rebooted; its recorded boot time stayed unchanged.

Static inspection of the supplied binaries identified candidate geometry;
live GETs then established transport, role binding, units, existing caller
and full baseline before any SET. No BAR0 scanner, registry patch or VBIOS
write was needed. Driver version strings identify the tests but do not
select runtime compatibility.

Each driver received independent 349, 351 and 400 W client-request trials,
plus a production-backend trial at 351 W. Every trial used a durable private
undo capture and independent restoration watchdog, bounded idle checks
(P0, utilization at most 5%, reported power at most 80 W, temperature at
most 55 C), exact configured-request readback, paired native status and
full captured client/native restoration. One preliminary 431.60 attempt
aborted on desktop utilization and verified restoration before the successful
rerun. No watchdog restoration was required during the successful trials.
Public JSON records decoded observations and buffer hashes; private undo
payloads and driver binaries are excluded from the distribution.

## Live results

| Configured request | 431.60 settled native effective board limit | 431.60 core current | 621.01 native effective board limit | 621.01 core current |
| ---: | ---: | ---: | ---: | ---: |
| 349 W | 359.906 W | 324 A | 359.906 W | 359.640 A |
| 351 W | 360.938 W | 324 A | 361.969 W | 360.360 A |
| 400 W | 360.938 W | 324 A | 412.5 W | 378 A |

Configured requests matched exactly on both drivers. The effective values
above come from native STATUS, separately from CONTROL's configured vote.
431.60's first sample reported 349/350/350 W respectively; its next four
samples, starting about 0.3 seconds later, all reported the settled values
shown above. Summarizing only that first sample would miss this transition.
The settled values are consistent with a 350 W request clamp followed by
the same approximately 3.125% status uplift seen on 621.01; that relationship
is an inference, not a decoded physical-power calibration or clamp mechanism.
They are driver-reported software quantities, not measured electrical power.
The 621.01 coupled core-current values may exceed that row's independent
360 A SET bound as a driver-derived response. This does not authorize an
independent out-of-range current write.

The production setter accepted 351 W on both drivers, set a separate 350 A
core-current pin, and replayed that pin after repeating the board request.
It refused an independent 360.001 A request. All four trials per driver
restored the complete captured 250 W client/native baseline, including
peer requests, and verified the original 1062.5 mV hold. Detailed evidence:
[431.60 JSON](turing-43160-live-power-20261028.json) and
[621.01 JSON](turing-62101-live-power-20261028.json).

## Fixes and remaining scope

431.60 uses the short RM26 geometry, including a first-word native CONTROL
mask and a 120-byte role definition. Its existing raw RM caller was FE;
the public NVAPI wrapper's F8 conversion did not justify changing that
caller. See the [static evidence](turing-43160-static-layout-20261028.md).
621.01 carries a v30 firmware definition inside the modern large packets;
equating packet capacity with v40 firmware had incorrectly refused it.
Druta now selects the understood geometry, validates the actual firmware
definition and handles coupled requests, current-pin replay and exact
rollback across these families. The control view separately displays the
configured request and NVML's reported enforced limit when available;
native policy rows retain their separate requested/effective values.

431.60's accepted 400 W vote does not establish an operative override:
settled native effective status is the same 360.938 W as at 351 W, and core
current remains 324 A. Static tracing found no understood
reversible bypass in the existing native/client setter paths. This limit
is documented rather than hidden by treating successful request readback
as enforcement. 621.01 demonstrates reported software headroom; a new
FurMark or PSU-trip experiment was not performed.

The owner's earlier [FurMark A/B observation](turing-owner-furmark-ab-20261028.md)
on 581.42 supports an operative limit increase in that separate session.
It does not establish physical behavior on 431.60/621.01 or calibrated watts
on a shunt-modified board. Four-digit Turing requests, a universal physical
ceiling, and other devices remain unqualified. The 1999 W XOC range is a
software request envelope. The final installed driver for handback is 621.01.
