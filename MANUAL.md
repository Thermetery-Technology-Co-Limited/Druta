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
Offset, and the identified I2C regulator's offset. The Load profile list shows
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

On the verified GTX 745 and GTX 690 with driver 472.12, this position instead
has a yellow **Lock P0 and max fan** button. It holds P0 and sets manual fan
duty to 100%. Loaded core clocks were about 540 MHz on GTX 745 and 705 MHz
on GTX 690; this keeps memory in its top band without maximizing core boost.
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

# III. How to load `nvtune`?

The power-limit knob and tune profiles preserve the configured request,
including fractional watts. Enforced power telemetry can lag behind that
request and remains a separate live reading.

On the measured RTX 5080 / VBIOS 98.03.3b.c0.6f / driver 580.97, memory-offset
Apply snaps half-MHz requests toward zero to the whole-MHz values the driver
can retain. Other cards keep their existing offset precision. This board's
memory command-clock divisor remains unknown; no timing-nanosecond conversion
is inferred from its reported memory clock.

`nvtune` is shipped by Seby. You must enable test signing for it to work on your machine. Druta can hunt for it on your desktop and will load it automatically. Druta is an offline tool. It does not download or upload anything.  

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

Once `nvtune` EXE is loaded, these buttons move up to the `Device` menus on the taskbar. 

## I2C controller selection

MP2888A is discovered automatically by scanning the selected GPU's I2C ports
and addresses. Open I2C regulator to see each candidate's port, address and
scan-time telemetry. Choose a candidate when several respond, enable I2C rail,
and press Verify before Apply. Rescan I2C refreshes discovery and clears the
verification result; it preserves staged curve edits. Verification is repeated
after changing GPUs or controllers, and cannot pass if restoration fails.

While Verify is running, the selected controller and risk modes stay fixed.
Closing Druta cancels verification and waits for the original control state's
restoration attempt to finish before the process exits. Cancellation cannot
authorize Apply, and a failed restoration leaves the session marked unclean for
automatic startup loading. A forced process termination or power loss cannot
run this cleanup. Ordinary reboot does not necessarily clear I2C settings;
use the controller's Stock/Auto action or a full power cycle as appropriate.
