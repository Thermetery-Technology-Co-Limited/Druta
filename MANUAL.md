How to use Druta:

Druta is from Sanskrit *druta* meaning fast. In Hindu performing art, it can also denote a “rapid shift (of expressions)", which is perfect for what this project is. 

# I. Hotkeys built for gamers:

- V/F curve editing is controlled by WASD: AD changes the point, WS changes the frequency.
- Shift + one of WASD moves the points 3x faster in any given direction. 
- Ctrl+Z undoes any given changes on the curve for a generous 64 changes deep. Ctrl+Y redoes that change. 
- Ctrl+H *holds* a given point on the V/F curve.


# II. A couple useful functions:

## Save a tune and load it at Windows sign-in

**Profiles > Save profile** captures the applied curve, clocks, power, fan policy,
confirmed per-rail limits and voltage offsets, the Additional Memory Clock
Offset, the identified I2C regulator's offset, and (since 1.7.0) every
writable power policy with the values set by hand and the channel notes. The
Load profile list shows
these settings. Loading also restores XOC mode and enables the rail controls
needed by the tune; I2C verification runs under load in each new session.

Saving a profile is unavailable until verification and profile application have finished,
so temporary verification voltages cannot become a saved tune. Loading checks
the complete saved payload before changing any control. Fractional memory
offsets are preserved in the driver's supported units, and replaying an
already-correct clock-domain offset is a successful no-op.

Voltage fields preserve fractional millivolts when read, edited and reapplied.
For example, 12.5 mV NVVDD offset and a 1068.75 mV reliability limit keep those
values in the input boxes. Driver voltage requests use integer microvolts;
I2C requests use the identified controller's step. Core offsets snap to the
card's physical frequency bin on Apply, and the displayed result can be
reapplied without moving to another bin.

Choose **Load at startup** beside a named profile to apply a saved copy at
Windows sign-in. Configure this while running Druta as administrator. Selecting
it again updates that copy; **Disable startup loading** turns it off. The card,
VBIOS and driver must still match. After changing drivers, save a fresh profile.

If Windows or Druta did not shut down normally, or the shutdown record cannot
be verified, automatic loading is skipped for that boot. You can still load a
profile manually. Reopening Druta does not bypass the skipped boot. After a
clean shutdown and boot, automatic loading can resume.

## EVGA iCX voltage and controller settings

Select the EVGA iCX controller on the target Ampere adapter and check its
identity. The displayed controller requests are not physical voltage readings.
Close other tools that write to the same controller before applying changes.
Enable the I2C rail controls, Unlock, edit the desired request, then Apply.
Druta enables the controller's write gate if necessary and checks the returned
request before reporting success. A successful driver call alone is not enough.

NVVDD requests are 750-1150 mV, extended to 1600 mV with XOC; FBVDD requests
are 1250-1450 mV. Both use 5 mV steps. **NVVDD Auto affects only NVVDD.**
You can adjust FBVDD while NVVDD remains on Auto. The Auto action does not
change FBVDD, and its placeholder count is not a measured 750 mV rail.
FBVDD has no inferred Auto or factory-reset action. Use an external meter to
confirm physical voltage.

Five additional controls have their own staged choice and Apply button:

| Control | Choices |
| --- | --- |
| NVVDD switching frequency | 300-1000 kHz in 100 kHz steps |
| FBVDD switching frequency | 300-1000 kHz in 100 kHz steps |
| NVVDD overcurrent protection (OCP) | Enable or Disable |
| FBVDD overcurrent protection (OCP) | Enable or Disable |
| NVVDD loadline | Level0 through Level15 |

Selecting a choice does not write it. Each Apply verifies that setting and
preserves its sibling and other readable controller settings. Missing readback
in one group does not remove the others. A previously confirmed value marked
`*` is stale; check the readback status before applying. Unknown selected
encodings or malformed responses refuse the operation. Transient read failures
have bounded recovery; an uncertain write is never automatically resent.

Voltage Apply, NVVDD Auto, setting Apply, and preset Apply first hold the
graphics clock at the driver's minimum lockable frequency. Release or quitting
clears that clock lock. Closing Druta does not undo the controller requests;
use NVVDD Auto explicitly and confirm FBVDD and the other settings separately.

The preset controls use Druta's JSON format:

- **Save current iCX preset** saves complete current controller readback to a
  JSON file. It does not save unapplied row edits.
- **Load iCX preset** validates the file and displays all eight setting fields
  in a separate preview. Loading does not write the controller or replace row
  edits.
- **Apply iCX preset** deliberately applies the preview after fresh identity,
  write-gate, XOC-range, and clock-clamp checks. Only settings that differ from
  complete current readback are sent, and the final settings are verified.
- **Restore captured connection settings** stages the first complete read from
  this controller connection through the same preview and Apply path. These
  are captured settings, not factory defaults. A controller or GPU change
  clears the captured and pending presets.

A partially failed preset stops immediately. Earlier verified changes remain
applied, and the failed operation may also have applied. Druta reports the
partial result without automatically retrying or rolling back. Refresh the
readback before choosing the next action. These presets are never automatically
replayed at startup, and saving a preset does not write controller EEPROM.

The command formats and returned states were established with black-box
Classified captures on one EVGA RTX 3090 Ti Kingpin. This verifies controller
setting readback, not physical PWM frequency, OCP trip behavior, or the
loadline's electrical effect. The displayed ranges are input policy, not
measured safe operating ranges.

## 1. `De-flatten`:
When two or more points on the VF curve land on the same frequency, only the one with the lowest voltage will ever be used. For example, if 1081, 1087, and 1093mv all correspond to 2000mhz, the card will always run at 1081mv, 2000mhz. Deflatten makes sure that every point on the the V/F curve between 1000mv to 1091mv (adjustable) are *mathematically strictly increasing*. That way, you can run 1091mv immediately without a hard voltage mod. 

The TITAN RTX and TITAN Xp default cap is 1093.75 mV. On the confirmed
boards, **Rail limits** can raise the NVVDD ceiling; the V/F planning cap
follows it. Both cards were measured at 1112.5 mV with a 1125 mV ceiling and
a de-flattened curve requesting that point. Raising the ceiling alone may
leave the card at the beginning of its existing flat. MSVDD controls are not
available on these TITANs. See [the measured procedure](VOLTAGE-RAILS-TITAN.md).

(Joined overclocking during the time of 4000/5000 series? It might be helpful to know that for 1000-3000 series, almost desktop every GPU can be overvolted to 1091mv by manipulating of the voltage curve. You DO NOT need to bin for voltage.)

## 2. `Hard deflattern`
**Mandatory hard mod required**
This mode is actually the opposite of deflatten. It flattens everything after 800mv (adjustable), overclocks the 800mv point to the standard P0 frequency, and flattens every point above that. That way, the driver will lock 800mv. This looks counterintuitive because it works in conjunction with a hardware voltage mod and a completely inoperable `refin_adj` (or whatever that the BIOS uses to control the voltage internally) to neutralize *imperfect power limit bypasses*. 

*Imperfect power limit bypasses* are for GPUs with no XOC BIOS and don't completely work with shunt mods, such as Titan Xp, 2x8-pin 3080, 4080 Super, 3060, etc. These GPUs still power throttle after shunt mods, even at low percentage of TDP. The power estimation comes from the core and cannot be easily bypassed. By fooling the GPU core that it's at 800mv, you lower the internal power limit reading. However, because you took out `refin_adj` or similar, the core is actually at whatever higher voltage that you set it at with your external hard mod, which is why I made you check a box to make sure that you have both setup in place. 

## 3. (my favorite function) `Max it`: 

Had enough with boring sliders to the maximum? Click "max it". It does the V/F deflatten, maxes out the voltage boost, power limit, fan, and holds at 1093mv all in one click. You click it once, and the rest is the actual part of overclocking: changing the frequency. 

While the hold lasts, Druta also raises the voltage limits to the held point +
25 mV (Clocks > Keep headroom above a held point, on by default), because a
point held on the voltage ceiling runs below the clock it shows. 25 mV is a
fixed safe margin for every card. The limits go back when the hold ends.

Kepler and Maxwell cards with the legacy P0 API instead have a yellow
**Lock P0 and max fan** button here. Availability is based on generation and API support, without device, subsystem,
driver-version or VBIOS allowlists. The button confirms P0 and the top memory
band before setting manual fan duty to 100%. Loaded core clocks were about
540 MHz on GTX 745 and 705 MHz on GTX 690; this keeps memory in its top band
without maximizing core boost.
The GTX 690 shares one blower between its two GPU cores.
**Release P0** drops the hold; **Auto** restores automatic fan control.
**Undo last write** restores the saved fan policy while retaining the P0 hold.
**Reset all to stock** releases the hold and restores Auto; closing Druta
releases the hold but leaves fan duty as set. Clock, power and voltage settings
are not changed by this button.

## 4. What about XBAR? 

On 10, I have found no manipulable software knobs to tune XBAR. To help you cope and seethe, you can read the the 1000 series XBAR frequence inside the monitor tab of Druta but not change it, because NVIDIA simply doesn't expose any way to change it. The good news:

### For 10 and 20 series, XBAR strictly scales with CORE FREQUENCY, NOT NVVDD VOTLAGE. 

That means you should just overclock the crap out of your core like everyone else instead of worrying about NVVDD. 

## 5. Shunt mod corrected power

It currently lives under taskbar > Device > `Shunt mod corrected power`. Simply type in the new effective resistance value to correct the power reading. Planned in the next release is a better per rail calibration.

## Clock controls after startup

If the driver cannot answer a clock-control check at startup, Druta retries the
failed checks twice in the background. Controls that recover appear automatically
when you finish any active edit or operation. Existing controls and staged values
are retained, and discovery does not change GPU settings or scan I2C.

After those retries, use **Device > Refresh capabilities** to check again. A
control is shown only after the current adapter returns a supported, validated
response; retrying does not make an unsupported clock layout writable.

# III. How to load `nvtune`?

The power-limit knob and tune profiles preserve the configured request,
including fractional watts. Enforced power telemetry can lag behind that
request and remains a separate live reading.

On the measured RTX 5080 / VBIOS 98.03.3b.c0.6f / driver 580.97, memory-offset
Apply snaps half-MHz requests toward zero to the whole-MHz values the driver
can retain. Other cards keep their existing offset precision. This board's
memory command-clock divisor remains unknown; no timing-nanosecond conversion
is inferred from its reported memory clock.

`nvtune` is an external tool distributed by Seby; Druta does not bundle it. You
must enable test signing for it to work on your machine. Druta can locate an
existing copy, but does not download tools or install updates. Druta is an
offline tool. It does not download or upload anything.

You should almost always use `Read memory timings (will hold P0)` (blue) because changing P states can change timings, and reading/changing memory timing when the card is idling at P16 is useless for your endeavors. `read timing` is for sanity checks after you have applied your changes. 

`Load nvtune` and `Enable Test Signing` are conspicuously displayed when nvtune isn't loaded.

Timing Apply requires Unlock controls and a fresh confirmed performance band
immediately before writing. An old capture cannot authorize a write after the
card returns to idle. The GTX 745 must reach its 900 MHz memory band; 405 MHz
idle does not qualify. Negative memory offsets are accounted for when checking
the band. A failed helper or unreadable register is reported as a failure, not
as proof that the hardware rejected the value.

If the helper reports an unknown chip layout, Druta retains raw register
captures and disables decoded timing Apply and Restore. RTX 5080 reported
`UNKNOWN_1B3` with the helper tested here. Druta also checks the helper's help
text before previewing a change: newer helpers require explicit `--dry-run`,
while the recognized legacy convention requires `--commit` for writes.
Unrecognized command conventions refuse the preview.

### Upcoming GPU recovery

Use the red **Panic Button (PnP Reset, Deeper than Shift+Ctrl+B)** in the
upper-right shared header to attempt recovery from a driver or timing failure.
It remains visible while the tab page scrolls; **Device > Restart GPU device
(PnP)...** starts the same recovery immediately, without a confirmation. The
display may go blank and other GPU applications may lose their device. Close
other workloads and save staged edits first.

Druta closes, restarts that exact Windows device, and opens a fresh window. It
does not reapply a profile or reboot Windows. The result appears in the log and
is saved in `%LOCALAPPDATA%\Druta\device-recovery`. If Windows needs a reboot,
Druta reports that requirement. This is not a guarantee of recovery from a
hardware hang or of stock settings. Administrator rights and Windows 10 version
2004 or later are required.

From an elevated shell, after closing existing Druta windows, the same operation
is available as `Druta.exe --restart-gpu 0000:01:00.0`; replace the PCI slot with
the card shown by `Druta.exe --list-gpus`.

### Upcoming timing profiles

Druta 1.6.0 adds **Save timing profile...** and **Load timing profile...** to the
Timings tab. Save records the current decoded broadcast timing fields together
with any red, staged edits. It only exports a capture taken in the top memory
band whose active framebuffer partitions agree; raw registers, inferred fields,
and structural training fields are never profile values.

Load accepts Druta timing profiles and nvtune-compatible `fields` profiles. It
replaces the staged timing edits only: it never writes the GPU. Review the
normal preview and use **Apply to memory controller** to make a write; the
existing fresh-band and write checks still apply. A raw `nvtune save -o`
register backup is for nvtune restore, not a timing profile, and Druta refuses
to turn it into broadcast writes. Upstream nvtune `save --profile` is fixed in
[v1.0.2-alpha](https://github.com/sebastianmarrufo/nvtune/releases/tag/v1.0.2-alpha);
use its emitted profile with that version or newer installed.

Once `nvtune` EXE is loaded, these buttons move up to the `Device` menus on the taskbar. 

## I2C controller selection

Check **I2C rail** to reveal discovery controls; checking it does not scan.
The **IC to scan** dropdown defaults to **Unknown -- Full Scan**. Select a
known controller to limit discovery to that IC, then press **Connect / Scan**.
Druta first checks a compatible remembered route using fresh identity and
settings reads; if that fails, it scans the selected scope. **Full scan**
always searches all supported controllers, bypassing the remembered route.

Open I2C regulator to see each candidate's port, address and scan-time
telemetry. Choose a candidate when several respond. For voltage controllers
such as MP2888A, press Verify before Apply. Scanning clears verification but
preserves staged curve edits. Verification is repeated after changing GPUs or
controllers, and cannot pass if restoration fails. NCT3933U current-DAC controls
instead use **Read settings** and register readback; they cannot measure rail
voltage. See [NCT3933U.md](i2c/NCT3933U.md).

MP2888A has two selectable control recipes for the same physical controller:

- **MP2888A (ASUS Turing)** retains the original GPU VID offset method through
  register 23h, including Stock and existing saved offset profiles. It remains
  the default. This is the recorded working method on the TITAN RTX die fitted
  to an ASUS RTX 2080 Ti Strix PCB.
- **MP2888A (ASUS Ampere)** uses manual PMBus voltage through nominal command
  21h and the E1h mode bit. This method was measured on the ASUS Strix RTX 3070 Ti.
  Its value is a nominal command in 6.25 mV steps; existing coarse and fine
  trims are retained.

The names identify control recipes, without restricting discovery by GPU or
board identity. Selecting a recipe changes no controller register. Each needs
its own Verify. Controller-reported voltage is displayed separately; it is not
a calibrated physical reading. Use an independent meter to establish physical
core voltage.

Before manual PMBus Apply or Verify, Druta takes its own minimum graphics-clock
clamp and checks that the clock has settled. It refuses an unreadable lock state or a
lock held by another application. Verify checks two PMBus commands and their
reversal at that clock. It does not validate stability at an overclocked point.

**Auto** selects GPU PWMVID control. Release, exit and controller changes restore
the captured controller state before releasing Druta's clock clamp. A failed
restoration keeps the clamp and the recovery state. Other controller fields,
protection, calibration, EEPROM, phase count and switching frequency are retained.
Manual PMBus voltage is session-only: profiles omit an active manual request.
Return to Auto before loading a profile. Existing MP2888A offset profiles still
replay through the original offset recipe, with fresh verification as needed.

While Verify is running, the selected controller and risk modes stay fixed.
Closing Druta cancels verification and waits for the original control state's
restoration attempt to finish before the process exits. Cancellation cannot
authorize Apply, and a failed restoration leaves the session marked unclean for
automatic startup loading. A forced process termination or power loss cannot
run this cleanup. Ordinary reboot does not necessarily clear I2C settings;
use the controller's Stock/Auto action or a full power cycle as appropriate.

## Registry and BIOS overrides

Open **Controls > Registry / BIOS overrides**, then **Read current fields**.
The panel shows understood BIOS fields and sensed silicon-model inputs for the
selected adapter. Direct Speedo and IDDQ substitutions are writable when their
live input identity, range and effective readback are available. IDDQ values
are raw model integers; their physical units remain unresolved. They are not
current-limit settings or measured current.

Edit values, choose **Preview changes**, and review the saved transaction before
**Apply and restart GPU**. Apply closes Druta, stages the selected adapter's
registry values, restarts that GPU through Windows PnP, and reopens Druta with
fresh driver handles. It never restarts Windows or reapplies a startup tuning
profile. Changes persist across application exits until restored.
New BIOS patch transactions preserve the existing GPU-firmware selector.

**Restore previous registry settings** undoes the latest transaction. Undo keeps
the original value types and absence of values, preserves unrelated external
changes, and reports conflicts instead of replacing externally changed values.
Receipts are stored under `%LOCALAPPDATA%\Druta\registry-overrides`. If the GPU
backend cannot start, run the independent recovery command as administrator:

```powershell
.\Druta.exe --registry-recover 0000:01:00.0
```

Use your adapter's PCI slot. The saved Windows device identity must still match.
The helper can restore registry values without a live NVAPI/NVML connection;
success also requires the GPU to restart and the selected readbacks to return.

BIOS activation is checked against the exact expected running image. Understood
power/current policy metadata, core/memory offset ranges, fan duty/RPM bounds, PWM frequency and
the operating temperature limit also require matching driver readback. Some
Blackwell power/current policies derive their active maximum from the default
limit; the preview explains this relationship and rejects ineffective maximum
edits. Fan bounds use the driver's underlying duty/RPM fields; its public
30-100 percent range can remain unchanged. PWM readback verifies the loaded Hz
setting; the hardware derives an integer period, so it is not an electrical
frequency measurement. The operating-temperature
control accepts whole Celsius values and currently supports lowering the limit;
supported slowdown and shutdown thresholds must remain unchanged. Other decoded
BIOS fields are experimental: loading their bytes does not establish a physical
effect. Increasing a board-power maximum does not automatically increase every
other power/current policy or prove that the board draws the requested wattage.
Speedo and IDDQ change software model inputs while the raw hardware fuses stay
intact. IDDQ transactions also verify the other model inputs and configuration;
an ignored, rounded or otherwise mismatched value triggers undo.

**Linked current limits** configure a current limit carried by another power
policy. Druta resolves the BIOS relationship to its target policy and verifies
the loaded cache and firmware-reported value after a GPU restart. Other power
definitions and requests must remain exact. A lower firmware vote can keep the
effective current below this limit. Apply these separately from other registry
controls. Undo checks the original cache, reported value and requests; an
unverified restoration stays marked **recovery required**. These checks do not
measure physical overcurrent protection or enforcement under load.

**Driver behavior > Cache limit-conversion results** controls reuse of cached limit
calculations. Disabling it requests recalculation; it does not disable power-limit
enforcement. Its selected value, peer flags and registered limit inventory must
agree with fresh driver readback after activation and undo.

**Skip driver performance-limit updates** suppresses host-initiated updates.
The active switch and exact undo have been verified; its effect on autonomous
firmware enforcement remains unverified. Both driver-behavior controls require
a GPU restart and can be changed independently or together.

**Expose all-P-state overclocking editability** enables the driver's override for
supported non-P0 clock domains. Clearing it removes the override and requires
fresh readback to show disabled editability; if the driver default remains
enabled, the transaction rolls back. The override itself changes no offset.
Reported editability does not establish that every state-specific setter works.
Apply this control separately from BIOS clock-range edits. Underlying offsets,
ranges, peer metadata and adapter identity are checked after activation and undo.

**Enforce VF curve monotonicity** and **Enforce VF curve smoothing** control two
driver policy flags through `RMClkVfOverride`. Apply restarts the selected GPU
and verifies both flags and the other returned configuration fields. Disabling
either also changes a derived VF processing mode. These controls do not write
a curve or request a voltage. Their effect on the firmware's curve-processing
algorithm remains uncharacterized. Apply them separately from BIOS clock-range
edits; **Restore previous registry settings** restores the captured flags.

**Constant VMIN** changes an understood, unshared BIOS minimum-voltage equation
for the selected rail. Preview requires agreement with its independently
reported effective VMIN and retains existing voltage offsets. The target must
fit the rail's current reported upper limits. After restart, the effective VMIN
must match exactly and the other captured rail policies must remain unchanged.
If the driver rounds to another voltage step, Druta restores the previous
settings. The supported step is device-specific; the current upper limits do
not establish behavior at every operating point or measure physical voltage.

**OV constants and ALT addends** change understood constant voltage equations.
OV is an absolute BIOS constant, with the current rail offset retained. ALT is
an addend to that rail's complete REL equation; its displayed value is not an
absolute ceiling. Druta edits all three constant words together and checks the
entire loaded equation configuration after a GPU restart. The selected reported
limit must match exactly; rounding or an unexpected change triggers undo.
Complete rail-control requests must read back unchanged. When complete request
replay is available, Druta restores the captured rail offsets and voltage-boost
percentage after each GPU restart, including Undo and rollback. Each replay is
verified independently. If complete replay is unavailable, set boost to 0% before
applying OV, ALT or REL BIOS changes. Arbitrary offset replay is not yet
qualified: a driver can report SET success while leaving an offset unchanged.
Druta reports the rail, affected field, requested and observed values, and any
separate boost or metadata discrepancy. An ignored request fails verification;
if the captured requests cannot be restored, recovery remains required.

**Live voltage constants** changes loaded constants without restarting the
GPU. Read the available rails and select an **OV constant**, **ALT addend** or
**VMIN constant**. Enter a value in mV and choose **Apply now**. The panel shows
the current constant, stored value and independently reported limit separately.
For example, changing an ALT addend from 20 to 15 mV can change a reported ALT
limit from 1060 to 1055 mV. **Restore initial value** returns that rail and role
to the value first read for this adapter and image in the current Druta session.
Live values last for the driver session; use BIOS fields for persistent changes.
Voltage boost, rail offsets and readable clock requests are retained. A rounded
reported value or unexpected peer change fails verification and attempts one
restoration of the original constant, with the recovery result shown. The
supported voltage step is device-specific.

VMIN also shows the maximum input allowed by that rail's currently reported
upper limits and retained signed VMIN offset. Druta checks the loaded constant
against the reported minimum before changing it. The new reported VMIN must
match exactly, while other policy values and requests remain unchanged.

**Live REL upper bounds** provides a separate control for the loaded REL
equation's upper bound. Read the supported rails, enter a value in mV, and use
**Apply now** without restarting the GPU. The panel shows the loaded and
stored upper bounds separately from reported REL, ALT and effective limits.
The reported limits can differ from the bound. **Restore initial bound** uses
the value first read for this adapter and loaded image in this Druta session.
Live edits last for the driver session; use the BIOS fields for persistent edits.
The evaluated V/F curve can briefly refresh after a write. A timed research
trial returned to its original curve without a second SET. Retained voltage and
readable clock requests are verified independently; unavailable optional
readbacks are listed. Failed dispatched changes attempt one original-row
restoration and report whether recovery was verified. Arbitrary rail-offset
replay remains unqualified.
Changing ALT can change the shared boost contribution and the computed REL and
effective limits on both rails. OV can change the selected rail's effective
limit. These computed outputs are recorded without assuming a firmware formula.
This checks loaded settings and reported limits, not measured regulator voltage
or enforcement under load. Apply these separately from other registry controls.

**REL upper clamps** change the maximum of an understood multivariate voltage
equation. Druta checks the complete loaded definition and changes only its upper
clamp; the lower bound, coefficients and linked equations stay unchanged.
The reported REL can remain below the clamp or be rounded to a voltage step.
Linked ALT limits and effective limits can change. With 0% requested boost,
unrelated rail limits and the boost contribution must stay unchanged. A missing
readback for an affected rail prevents applying; an unrelated missing rail does
not. Apply REL separately from other BIOS or registry controls. Undo restores
the captured definition and reported state. These checks establish loaded
configuration, without measuring physical voltage or enforcement under load.

**Fixed REL equation bounds** set the selected equation's lower and upper bounds
to the same value. Check **Set** to include this control in the preview; editing
its number alone does not select it. The initial number is the stored upper
bound, and the preview shows both original bounds. Polynomial terms, linked
equations and rail offsets are retained, so the reported REL can differ from
this equation value. Druta verifies both loaded bounds exactly and records the
reported result separately. Apply and Undo use the same request preservation
and GPU restart checks as the REL upper clamp.

**Startup REL adjustment** selects an explicit driver startup setting for the
currently affected rails. **Force -50 mV startup adjustment** requests that REL
offset; **Suppress startup adjustment** requests zero. **Leave unchanged** keeps
the current registry selection. An inherited setting is not labeled enabled or
disabled from its current offset alone.

Apply this separately from other controls. After GPU restart, Druta checks the
startup effect before replaying captured rail requests and boost. Undo restores
the exact previous registry value or absence and verifies the original requests.
Current drivers can ignore runtime offset writes; if a captured custom offset
cannot be restored, the transaction stays marked **recovery required**. A saved
registry value alone is not reported as complete restoration.

**Fan curves and zero-RPM thresholds** use the driver's current per-policy
configuration for readback. Temperatures use steps of 1/32 C; duty coordinates
use whole percent and RPM coordinates use whole RPM. The preview identifies a
stored coordinate that is inactive in the current RPM or duty mode.
Druta binds each policy to its reported cooler routes and preserves other
curve points, stop flags, duty/RPM values, cooler bounds and PWM settings.
Zero-RPM thresholds require an already supported and enabled stop policy.
A final curve point derived from a thermal-table limit cannot be changed
through its unused stored temperature. Temperatures and curve coordinates can
be changed together; apply them separately from zero-RPM enablement and
cooler-setting changes. These checks
verify loaded configuration; they do not measure physical fan response or
identify which of several routed policies currently controls fan speed.

**Global zero-RPM enablement** changes the support and enable flags in every
affected fan policy. Preview requires an understood binding for every edited
policy. Apply verifies the full loaded policy, routing and cooler configuration;
unsupported activation or any unexpected change triggers undo. Apply this
separately from fan-curve, threshold and cooler edits. This verifies the loaded
configuration; physical fan-stop behavior remains dependent on the active policy.

**Clock offset ranges** on understood v50 BIOS tables also use the driver's
typed domain table. This covers auxiliary domains such as XBAR, SYS and video
when their current bindings are available. Apply preserves other domain metadata
and current user-clock requests, and requires exact bound readback after restart.
Changing a range does not itself request a new clock offset.

**Factory clock offsets** change the loaded core or memory factory delta on
understood v10 factory/v50 clock tables. Apply checks the exact factory delta
after the GPU restart, including the core offset copied to an eligible XBAR
domain. Existing per-domain offsets, VF tuning requests and other clock metadata
are preserved. Zero disables the selected factory record. Apply factory offsets
separately from other registry or BIOS controls. Evaluated VF and P-state
frequencies can change; readback verifies the loaded configuration, without
establishing clock behavior under load.

**Rated base/boost clocks** change the stored absolute core frequency on
understood v10 factory/v50 clock tables. Apply one rated clock per transaction,
in whole MHz. A stored value of zero means that record is inactive; enter a
positive value to enable it and use **Restore** to undo the saved change.
After the GPU restart, Druta checks the public rated-clock query and both loaded
copies of every affected record. It derives linked-record changes from the
original frequencies reported by the current adapter, while preserving existing
clock offsets and VF tuning requests. These values describe rated metadata;
they do not force an operating frequency or prove a performance increase.

**GR power-gating entry disables** use the existing **Idle power management**
registry fields in a separate transaction. Preview shows the configured
P-state eligibility before and after the change. For ordinary entry disables,
Apply checks that selected P-state memberships were removed while loaded
routing, controller support and other available low-power masks remain
unchanged. Restore checks the original configuration.

Ordinary entry disables require enabled entries consumed by the loaded P-state
map. Two shared auxiliary capabilities can lose
their last contributor when the corresponding driver readbacks are available.
For one, Druta checks its support, P-state mask and controller feature. For the
other, it checks that the role's support query becomes unavailable while another
supported role remains readable; no separate per-P-state mask is established.
These two removals may be combined when both checks are available. Preview
explains the applicable checks, and Restore verifies the original state.
The development continuation also supports disabling every enabled entry in one
transaction, including enabled entries unused by the loaded map. It checks that
main controller 0 becomes unsupported with an unavailable configured mask,
and independently checks all controller features and type 7 configuration.
Known profiles can include loss of GRRG controller 2 or MSCG controller 4;
other peer changes fail verification and trigger restoration. Restore checks
the captured original configuration. A separate path supports removing the final
raw bit 4 contributor when the selected entries have verified P-state routes,
main GR power gating stays supported, and another auxiliary role remains
supported. It checks the affected feature/support/masks and the other captured
controllers and roles, including the mask contraction of any surviving role
that also used those entries. Combined bit 4 capability losses and whole-group
disappearance remain unavailable. The complete dependency graph and broader
hardware qualification remain open; configured eligibility does not establish
actual power-gating residency or power savings.

**DIFR entry disables** use the existing **Idle power management** registry
fields. Disable a currently enabled DIFR entry in its own transaction, preview,
then apply with a GPU restart. The supported path checks the loaded P-state to
LPWR routing and the resulting configured DFPR/MS DIFR support and state masks.
**Restore previous registry settings** restores the captured original entry and
verifies its original configuration. Enabling an originally disabled entry
remains unavailable because its captured bytes do not establish support.

**DIFR configured state > Read DIFR configuration** shows the loaded mapping
and controller support. A supported controller with a zero mask has no configured
P-states; an unsupported controller has an unavailable mask. These are distinct
results. The read does not measure the executing P-state, feature residency or
physical power savings. Table layouts and routing that cannot be qualified are
reported before a DIFR write; unrelated controls retain their own capabilities.

**Runtime VF policy control** reads the active monotonicity and smoothing checks.
After **Read VF policies**, use **Apply policies now** to change either check
without restarting the GPU. **Restore initial policies** restores the values
from the first successful read for this adapter in the current application
session. A failed change attempts to restore the immediately preceding values
and reports whether that restoration was verified.

Some startup modes reject disabling these checks. In that case, apply the
corresponding registry setting with a GPU restart first. A live change retains
the startup processing mode and leaves the stored registry settings unchanged;
restarting the driver reloads the registry policies. Restore the initial live
values before using registry Undo. The controls verify
the driver's active policy flags. They do not write a VF curve or request a
voltage, and physical firmware enforcement remains unmeasured.

**Runtime voltage-request control** reads the driver's current admission switch.
After reading it, **Apply gate now** allows or suppresses driver voltage requests
immediately. **Restore initial gate** returns to the state captured by the first
successful read for this adapter in the application session. Each change checks
fresh readback; a failed change attempts to restore the state immediately
preceding it. This switch does
not request a voltage or raise a voltage ceiling. It uses no registry entry and
does not restart the GPU. A failed read disables its Apply action until a fresh
read succeeds.

**Voltage switch-delay requests** reads the supported PWM devices and their
current delay requests in microseconds. Select the device by its displayed
domain and operation, enter a positive whole number, then use **Apply delay
now**. DEFAULT and IPC_VMIN are separate device roles. **Restore initial delay**
returns the selected device to the value from its first successful read in this
application session. Other devices' requests are preserved; a failed write
attempts an exact restoration and reports its verified result.

These live changes use no registry entry or GPU restart and are reloaded when
the driver restarts. The field is a positive 32-bit request; its representable
range is not a measured timing range. Readback verifies configuration, while
physical switching latency and firmware scheduling remain unmeasured. The
separate power-up/down delay words are not controlled here. A missing or
unrecognized peer device does not remove an independently supported control.

**Power/current arbitration > Read arbitration** separates the requested limit,
host votes, firmware votes and effective limit for each understood policy. A
lower firmware vote can keep the effective limit below the host request. Client
numbers remain numeric until their roles are established. These reads do not
change settings; a failed refresh labels any retained result as historical.
The fields are read sequentially, and firmware votes can lag a recent change.
Refresh again before concluding that a request has propagated to dependent limits.

**Driver policy diagnostics > Read driver policies** shows registered performance
limit slots, feedback-controller flags and registry-selected feedback policies.
Registered slots are an inventory; their presence does not show that they are
currently limiting the GPU. Unknown flags stay labeled as raw values. Each group
is read independently, and retained values after a failed refresh are labeled
historical.

## Power slider extensions

On GPUs exposing the live client-power interface, **XOC** extends the same
slider to positive requests up to 1999 W (or the current BIOS maximum if
higher). This is a software request range. It does not change BIOS limits,
and firmware can still enforce a lower limit. Ordinary requests keep the
existing driver path. Extended requests take effect without restarting the
GPU or Windows. The configured request and measured draw remain separate;
**Registry / BIOS overrides > Power/current arbitration** shows host and
firmware votes and effective limits.

On adapters using the older client-power layout, changing the board request can
also recalculate other power and current requests. Druta checks the resulting
control records and re-applies values pinned in this session. Arbitration-vote
details and original-BIOS-range coloring may be unavailable independently of
the live request control. The reported limit is not a measurement of actual
power draw.

Very high requests can produce a lower native effective limit. On one RTX
3070 Ti, a 1,024 W request reported a 7.424 W native board limit, while a
1,999 W request reported 1,012.893 W; NVML retained the complete requests and
the linked current limits kept increasing. These observations fit a wrap in
the native status path. Raising the request therefore does not guarantee a
higher effective limit. See the measured policy table in
`experiments/ampere-live-client-power-20261027.md` for scope and details.

Turning XOC off retains an already configured extended request and allows
movement back toward the normal range. Profiles that capture such a request
carry XOC for replay. Stock beside Power limit restores its rated request.
Stock beside another power policy restores that policy's default while
retaining an extended board request; its result states that host-derived
limits were not recomputed.

The existing **Power limit (W)** slider keeps the original BIOS range in its
usual color. When a BIOS override extends the range, the added portion is tinted
amber. The number in the adjacent input box also turns amber whenever it is
outside the original range; values exactly on the original endpoints keep the
normal color. Typing, dragging, profile updates and Stock use the same rule.

Druta reads the original limits from its saved original BIOS when an override is
active. If the original image cannot be established, the slider tooltip explains
that extension coloring is unavailable. A higher requested value can still be
limited by another power or current policy.
