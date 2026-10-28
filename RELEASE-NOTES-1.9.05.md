# Druta 1.9.05

Supersedes published [1.9.03](RELEASE-NOTES-1.9.03.md) and carries forward the
Turing power-interface fixes from the intermediate
[1.9.04 build](RELEASE-NOTES-1.9.04.md). No 1.9.04 release or tag was published.
This remains a prerelease on the registry-controls development line.

## Clock-offset capability fallback

An exported `nvmlDeviceSetClockOffsets` can be a stub returning
`NVML_ERROR_NOT_SUPPORTED` (status 3). Druta now tries the independently
available NVAPI Pstates20 path after that definite capability refusal. The
fallback validates the adapter's P0 domain, editable range, representable units
and exact offset readback. Other NVML failures, including permission and
transient errors, do not dispatch another setter. Support depends on current
capabilities, not driver-version or board-identity lists.

On the shunt-modified Gigabyte RTX 2080 SUPER AORUS Xtreme / TU104 with its
16 GB VRAM modification, driver 431.60 accepted a -15 MHz core offset and a
-1 MHz true memory offset through this path. The captured offsets, graphics
curve and 1062.5 mV P0 hold were restored exactly. The observed memory clock
changed from 8000 to 7750 MHz despite both offsets returning to zero; that
physical clock difference remains under investigation. This is not a claim of
complete physical-state restoration. See the
[431.60 clock report](experiments/turing-43160-clock-compat-20261028.json) and
[validation script](experiments/validate_turing_clock_compat.py).

Driver 472.12 was installed through PnP without a Windows reboot and P0 was
verified. Both offset writes passed. A combined trial also verified that a core
write preserves a nonzero memory offset. Captured offsets, graphics curve and
hold were restored, and the observed memory clock returned to its captured
7750 MHz baseline. These tests do not explain the earlier 431.60 clock change.
The public evidence is recorded in the
[472.12 comparison](experiments/turing-47212-compatibility-20261028.md),
[clock report](experiments/turing-47212-clock-compat-20261028.json), and
[power report](experiments/turing-47212-live-power-20261028.json).

The installed loader retained the System32 NVML runtime **8.17.16.2101**,
whose clock-offset exports exist but report unsupported capability. A separate
test process explicitly loaded the supplied 472.12 NVML **8.17.14.7212**, whose
clock-offset exports are absent, without changing installed files. Both
runtimes passed the clock and combined-offset checks, fan 100% and voltage
boost 1% writes, and restoration checks, retaining the captured 7750 MHz
memory baseline and P0 hold. The alternate result is embedded as
`alternate_nvml_runtime` in the linked clock report. These runtime identities
describe the tested processes; they do not select support.

## Power-slider readback

- A rejected or unmatched request returns both the slider and numeric input
  to the configured value read from the card. A request for 400 W that reads
  back as 350 W therefore displays 350 W and reports the mismatch.
- When post-write readback is unavailable, Druta reports the current setting
  as unverified. It restores this adapter's last verified slider value when
  available and labels it as last known, not a confirmed current setting.
  A later valid poll resolves the warning and synchronizes the displayed value.
- Accepted configured requests remain separate from the NVML-reported
  enforced limit. Configured 400 W with an enforced 350 W remains a 400 W
  configured request, accompanied by an explicit discrepancy warning.
- The independently read native effective limit is shown alongside both
  values. On 472.12, NVML reports configured 400 W and enforced 400 W while
  native status settles at 360.938 W. Druta warns that the native limit is
  below the configured request. Missing or failed native reads remain
  explicitly unavailable and do not disable the ordinary slider. NVML
  agreement does not verify native status or actual power draw.

## Retained Turing power qualification

The [live driver comparison](experiments/turing-driver-live-comparison-20261028.md)
records idle 349/351/400 W configured requests and exact client/native
restoration on 431.60 and 621.01, alongside the prior 581.42 evidence. The short
RM `26xx` geometry and modern packets carrying v30 firmware definitions remain
independently validated. Role binding, units, unknown bytes, current bounds,
coupled peer changes, pin replay and compensation retain their checks. The
[431.60 static evidence](experiments/turing-43160-static-layout-20261028.md)
and historical [offline ABI review](experiments/turing-driver-abi-review-20261028.md)
explain the observed interfaces without becoming compatibility lists.

On 431.60, initial native effective status samples of 349/350/350 W settled
within about 0.3 seconds at **359.906/360.938/360.938 W**, with core current
remaining at **324 A**. The plateau is consistent with a 350 W clamp plus a
driver status uplift; downstream override remains unqualified. On 621.01,
native effective limits were **359.906/361.969/412.5 W**, with core current of
**359.640/360.360/378 A**. These are software status values, not physical-power
measurements. Physical enforcement under load on these drivers remains
unmeasured; the inherited 1999 W XOC range is a software request envelope.

On 472.12, 349/351/400 W configured requests passed exact request readback and
full client/native restoration to the captured 250 W baseline. The new native
effective getter first returned **349/350/350 W**, then four settled samples
per request returned **359.906/360.938/360.938 W**. Independent raw STATUS was
read after the getter and was already settled in all five samples. NVML's
enforced getter echoed **349/351/400 W** throughout. For these same requests,
core-current requests were **359.640/360.360/378 A**, while effective current
was **359.640/360/360 A**. Druta now keeps these driver-derived clamped current
values visible and can re-apply a separately bounded 350 A pin after repeated
board requests. All four power trials restored the complete captured 250 W
client/native state and exact P0 hold. The native plateau leaves downstream
override unqualified, and physical power enforcement under load remains
unmeasured.

The separate [owner FurMark A/B report](experiments/turing-owner-furmark-ab-20261028.md)
describes an operative limit increase during the prior 581.42 session on this
board. Its shunt-modified TDP telemetry and estimated 900 W+ transient do not
establish calibrated GPU input watts or a universal physical ceiling.

`Druta-1.9.05-win64.zip` includes the application, adjacent I2C profiles,
licenses, matching source, tests and public validation material. Extract the
complete folder and run `Druta.exe`. The build retains CPython 3.13.8 and the
existing pinned dependencies.

Validation: the complete hardware-free suite passed **6672 tests in 149.75
seconds**. The new and changed clock, native-status, power-UI and current tests
pass Ruff. Existing whole-module lint findings remain unchanged.
