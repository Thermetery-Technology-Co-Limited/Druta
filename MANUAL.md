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
The 1200 mV normal limit (1500 mV with XOC) applies before adding headroom,
so a 1200 mV hold can temporarily raise the rail ceiling to 1225 mV without
XOC. The held voltage must already be at or below the current rail ceiling.

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
2004 or later are required; on an older Windows version Druta stays open and
logs that requirement.

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
