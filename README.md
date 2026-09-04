# Druta

A monitor and tuner for NVIDIA cards, driven through NVAPI/NVML private
interfaces. It edits the V/F curve
with planners built around how the boost arbiter actually behaves, and reads and
writes the framebuffer-partition memory timing registers.

> ### ⚠ Read before running
>
> Druta writes GPU voltage, clock and memory-controller registers directly into the API.
> **Memory-timing writes can hang the machine and corrupt the contents of
> VRAM.** V/F curve edits can crash the display driver. Read
> [Safety](#safety-first-what-can-go-wrong) before enabling any write path.
>
> This program comes with **ABSOLUTELY NO WARRANTY** — see sections 15 and 16
> of [COPYING](COPYING). You assume all risk of running it.

**DRUTA IS AN INDEPENDENT, UNOFFICIAL TOOL. IT IS NOT AFFILIATED WITH,
SPONSORED BY, OR ENDORSED BY NVIDIA CORPORATION, ASUSTEK COMPUTER INC., OR
MICRO-STAR INTERNATIONAL CO., LTD.**

**NVIDIA, GEFORCE, RTX, QUADRO AND TITAN ARE TRADEMARKS OF NVIDIA CORPORATION.
ASUS AND STRIX ARE TRADEMARKS OF ASUSTEK COMPUTER INC. AFTERBURNER IS A
TRADEMARK OF MICRO-STAR INTERNATIONAL CO., LTD. THESE NAMES APPEAR HERE SOLELY
TO IDENTIFY HARDWARE AND SOFTWARE THAT DRUTA WORKS WITH OR IS COMPARABLE TO.**

Developed against two cards:

| | die | arch | memory | board |
|---|---|---|---|---|
| **Titan RTX** | TU102 | Turing | GDDR6 | die transplanted onto an ASUS RTX 2080 Ti Strix PCB |
| **Titan Xp** | GP102 | Pascal | GDDR5X | stock, unmodified |

---

# Safety First: What can go wrong?

On increasing order of severity:

- **Benign, read-only:** all Monitor telemetry, and *reading* on the Timings
tab. Its driver needs admin to *start*, not to read through once running. The
induce load is an ordinary CUDA bandwidth workload — it makes the card busy for
a few seconds and writes no register.

- **Reversible, needs admin, cleared by a reboot:** clock offsets, power limit,
voltage boost, fan, both lock mechanisms, and every V/F curve edit. A V/F edit
can crash the display driver and hang the machine; a reboot clears the settings
either way. Nothing Druta applies *to the card* survives a reboot.

- **Can hang the machine and corrupt data held in VRAM, also cleared by a reboot:** memory timing writes.
They are volatile — the driver reprograms these registers per clock band, so an
edit is not expected to survive a band transition, let alone a reboot. Use
`Restore stock` when you want a deterministic revert rather than relying on
that. Four guards sit in front of every write; the fourth is a per-card stock
backup keyed by the card's UUID.
--- 
# Build

```
pip install dearpygui
python -m PyInstaller --onefile --noconsole --name Druta --collect-all dearpygui druta.py
```

Output lands in `dist\Druta.exe`.

---


# Run

- `dist\Druta.exe` — standalone, no Python needed.
- or `python druta.py` from source.
- **Run as administrator** for every write path: clock lock, fan, power limit,
  V/F curve, memory timings.
---
# Picking a card

```
Druta.exe --list-gpus          # slots and names
Druta.exe --gpu 0000:02:00.0   # open on that card
```

`Device → Open a second window on…` will start a separate process, for
watching both cards at once. That is currently the only way to hold a lock on one card
while tuning the other.

Staged-but-unwritten V/F or timing edits will ask for a
confirming second click of the same button.

Stock backups are keyed by the card's **UUID** instead of its slot/model
name. Backups written under the older name/VBIOS scheme are still found and
reused, because the alternative is taking a fresh "stock" snapshot of a card
that is currently tuned.

---

# Monitor

Should be self explainatory. 

I included seven tiles: 
- core clock, 
- XBAR clock, 
- memory clock (converted to true MHz when
the memory type is known), 
- GPU temp, 
- hotspot, 
- power, 
- vcore. 

Within the subtitle there are:
- the p-state, 
- XBAR's delta against core, (will be deprecated) 
- the memory type and Gbps, 
- the hotspot delta, and 
- the power limit.

Below them: **ALL CLOCK DOMAINS**, then the clocks-event and perf-decrease masks
(including the insufficient-aux-power bit, a canary for a transplant's power
wiring), 
- a GPU/board power split with per-domain utilisation, 
- PCIe link generation and width with AER error counters, and a 
- a state line carrying the energy counter for your curiosity, 
- fan duty/RPM, 
- applied offsets, 
- voltage boost, and **both** clock mechanisms read straight from the driver.

## All clock domains

`NvAPI_GPU_GetAllClocks` (`0x1BD69F49`) is documented by the community as
"probably deprecated". It is nevertheless very useful.

Here's how the format works: there are 288 dwords that are **two arrays over the same 32 domains, an exact partition**
— verified over a 192-sample sweep, and enforced by asserts:

| array | dwords | per domain | at | contents |
|---|---|---|---|---|
| A | 0–63 | 2 | `2*d` | `{freq_kHz, capability flags}` |
| B | 64–287 | 7 | `64 + 7*d` | `{freq_kHz, srcid, 0, 0, 0, 0, 0}` |

And the A/B is very important and useful data. 

### On column A and B:

Having this is what disabused me from believing that XBAR writing worked 
for Pascal, and I am glad that this feature exists. It also reflects thermal 
throttling or similar. 

** A is the clock target the driver programmed, and B is what's actually measured. **

Measured on TU102, GPC, under ~99% load, sampled ≥8 s after the last clock
change (40 samples per locked case, 20 free-boosting):

| state | A | B | Δ |
|---|---|---|---|
| free-boosting at 1950 | 1950.0 | 1949.90 | −0.10 MHz |
| locked at 1920 | 1920.0 | 1917.03–1921.37 | within 3 MHz |
| locked at 1350 | 1350.0 | 1364.91–1364.94 | **+14.9, dead steady** |

If values in B are substantially and consistently lower than those in A, especially at P0, then barring a thermal throttling, some of the settings probably aren't properly registered. 

### Deducement of domains:

`0x1BD69F49` reports no names to the clocks that it reports, and the formats are drastically different between Turing and Pascal. 

Each name is now correlated against figures the driver reports independently:

- A domain matching the core clock at 1× is **GPC**; at 2× it is **`GPC2CLK`**,
  named for what it actually holds rather than silently halved.
- A domain matching the memory clock is **MEM** (in Pascal, it reads as 4x the actual value under tools like GPUZ).
- A domain reading **zero on a card that is demonstrably running** is marked
  `unpopulated` and loses its name. An empty slot is not a slow clock.
- The TU102 name table is applied **only** when the card presents the TU102
  signature (GPC correlating to domain 0). GP102 gets its own, gated the same
  way.

Four grades: **CONFIRMED** (correlated against ground truth), **ALMOST CERTAIN** (amber,
with a `?` — behaviour established, the *word* is an analogy), **unnamed**
(index only), **unpopulated**.

| domain | TU102 | GP102 |
|---|---|---|
| 0 | GPC — confirmed | reads zero |
| 1 | XBAR — confirmed | reads zero |
| 4 | MEM — confirmed | MEM — confirmed |
| 15 | — | **GPC2CLK** —  (2× the core freq) |
| 16 | — | **XBAR2CLK?** — ALMOST CERTAIN (see below) |
| 21 | VIDEO — confirmed | reads zero |
| 31 | PCIe link generation, not a frequency — rendered `gen N` | same |

**GP102 domain 16** first surfaces when it consistently help 0.962–0.970 of `GPC2CLK` across the whole top half of the curve, and a +60 MHz
core offset moved it by twice the core's move while changing that ratio by
0.0006. So it is a 2× domain dependent on the core freq. Additionally, the XBar offset mentioned later would attempt to change this value (to no avail, irrelevant here). See *Why Pascal gets no per-domain advanced clock controls*.

**TU102's XBAR law**, deduced and corroboated by the later BIOS analysis:

```
XBAR = max(540, snap15(0.95 * GPC + 15))
```
---

# Control

## XBAR clock offset

`Control → Clock offsets → XBAR clock offset`. A third offset mechanism, and it
is not a variation on the other two.

**Entering The Experimental Territory**: 
`NV_GPU_PUBLIC_CLOCK_ID` has four
members — `GRAPHICS 0, MEMORY 4, PROCESSOR 7, VIDEO 8` — and no crossbar entry
at all. 

`NvAPI_GPU_GetAllClockFrequencies`
reports `bIsPresent` for slots 0 and 4 only, and
`NvAPI_GPU_GetPstates20` reports `numClocks = 2` for the same two domains. The
private per-point delta table (`0x23F1B133`) is indexed by V/F point rather
than by domain and carries GPU points only. All three are dead ends by
construction, not by accident.

The path that works is a private per-domain control block, `0xF58938F5` to read
and `0xD14B69CF` to write, both at version `0x000261A4`. It wraps the RM
`CLK_DOMAINS` controls. The layout was measured by sweeping the domain mask one
bit at a time and watching where the populated dword moved:

```
header dword 2 = DOMAIN BITMASK (bit d selects domain d; any bit the card
                 rejects fails the WHOLE call with -1, so it is probed one
                 bit at a time rather than hardcoded)
entry(d)       = 0x124 + d * 0x304
    +0x000  mode/type      reads 8, 9 or 2 per domain
    +0x10C  frequency delta, signed kHz
    +0x110  NVVDD delta, signed microvolts   — the rail slider writes this
    +0x114  MSVDD delta, also signed microvolts — never written by this app
```

This block does not naively use the clock getter's domain numbering, because that numbering was off and would throw silent errors if you just ship the rest of the code using that. The mapping below was established by writing `+45 MHz` with my Titan RTX to each control index. We did that while using the Ctrl+H "HOLD" function that pinned the clock, we then recorded WHICH CLOCK ACTUALLY MOVED. XBAR was also doubly corroborated against GPUZ's reading. 

| control | moves | notes |
|---|---|---|
| 0 | GPC **and every ratio dependent with it** | XBAR, SYS, LTC and VIDEO all shift |
| 1 | XBAR | |
| 2 | MEM | already driven through NVML; no second knob |
| 3 | SYS | |
| 5 | VIDEO | |
| 9 | LTC | coarser step — `+45` requested moved it `+30` |
| 4, 6, 7, 8 | nothing | accept a write, store it, move no clock — left unnamed |

### RTX 50-series / Blackwell diagnostic path

The Turing mapping above is not reused for Blackwell.  RTX 50-series cards
use the same `0xF58938F5` / `0xD14B69CF` interface, but the Windows control
block's frequency and MSVDD fields are at different offsets in the public
Blackwell [implementation notes](https://github.com/SHANAjam/rtx5090-xbar-control/blob/main/docs/TECHNICAL_NOTES.md).  Druta therefore selects a separate candidate
layout and only exposes the XBAR, SYSCLK and VIDEO controls when the card's
one-hot domain probe and version echo succeed.

Before testing a new RTX 50-series card or driver, collect a read-only report:

```powershell
python nvbackend.py --clkdom-debug --json > clkdom-debug.json
```

For an administrator-only, temporary mapping check, use the explicit probe:

```powershell
python nvbackend.py --clkdom-map-probe --confirm > clkdom-map.json
```

If the mapping probe reports accepted writes but no settled clock movement,
compare the two frequency-field candidates with the field-only probe:

```powershell
python nvbackend.py --clkdom-field-probe --confirm > clkdom-fields.json
```

This tests only `+0x10C` and `+0x114`; it never writes the neighbouring NVVDD
or MSVDD fields.

Repeat with `--delta -5` and compare the signs of the same observations. A
real mapping should reverse direction; a P-state transition or idle-clock
change is not evidence.

The mapping probe changes one small frequency request at a time, records a
short median/range window of the physical clock observations, and restores the
complete buffer in a `finally` block.  For a conclusive result, first use a
fixed GPU-clock/V/F hold or a steady workload; otherwise a P-state transition
is reported as unstable rather than as a mapping.  It is not run automatically
by the UI.  Include the GPU model, driver, VBIOS and both JSON reports when
reporting a driver-specific failure.  Do not use the probe on a non-RTX-50 card
or with an untrusted driver build.

### Why Pascal gets no per-domain advanced frequency control? **Because it's hard coded by the BIOS**

There are docuementations floating around the internet saying that values
of XBAR/SYSCLK/etc. are "NAFLL (noise-aware frequency locked loops)"s for Pascal. 
The name tempted me to say that the "locked loops" are why I couldn't manupulate 
the clocks, because a natural read was that it would be hard coded.

I was inveigled by that thought and was gonna write it off, but then I 
dug into the BIOS and found that Turing BIOSes also classified XBAR/SYS/etc. 
as NAFLLs in the BIOS. 

TU102's NAFLL device table has ten entries — GPC0-5 plus XBAR (id 2), SYS (id 0), VIDEO
(id 10) and LTC (id 11), at clocks indices 0/1/3/5/9, exactly the domains Druta
drives. GP102 has eight: GPC0-5, SYS, XBAR. **No VIDEO NAFLL and no LTC NAFLL
exist on the part**, which is why controls 5 and 9 are refused there and why
domains 5/6 are `FIXED 600`/`FIXED 540` instead.

GP102's clock programming table
reserves four dependent slots per program and populates two of them inside GPC's
program — `dom 1 (XBAR) = 97%`, `dom 3 (SYS) = 90%`. There are no such slots for Turing.
The same search over GP102 finds all three as a positive control. 

That is to say, on Pascal XBAR's target is *computed* from GPC in hard-coded manner.

#### The definitive mechanism for Pascal/Turing XBAR frequencies: 

We did a swept test for Pascal, querying 16 V/F across the V/F curve. 

```
private domain 15 = GPC    value/core = 2.0000, spread 0.0007
                           (the 2x unit convention mentioned earlier)
private domain 16 = XBAR   k = 0.966808 through the origin
                           affine k = 0.966370, c = +1.51, R2 = 0.99993
```

As a reminder, for TU102:

```
TU102   XBAR = max(540, snap15(0.95 x GPC + 15))
        at GPC 1800 -> 1725; a pure ratio gives 1710, which is already on the
        15 MHz grid, so the +15 is a real term and not rounding
```

The sweep also inspired the a further BIOS investigation, because the data above calls for two competing hypotheses:

| hypothesis | predicts | vs measured 0.966808 |
|---|---|---|
| dependent ratio byte `0x61` = 97% | 0.970000 | off by 0.003192 |
| `freq_max` pair 3695/3822 | 0.966771 | **off by 0.000037** |

The `freq_max` pair wins by 86x. The value XBAR is actually derived from lives at

```
0x08449   XBAR program 25 freq_max = 0x0E6F = 3695   <- the operative field
0x082F7   XBAR dependent ratio byte    = 0x61   = 97     <- not this one
```

However, with BIOS modification out of the question for Pascal, this path is completetly closed. The identification is still worth recording, because it explains what the domain does and why the knob reads target-only, but it is not a route to anything.

So on Pascal the only lever that exists is GPC — raise it and XBAR follows at
97%. 

A very deep swipe in the BAR0 also resulted in no relevant result. Furthermore, according to NVIDIA's published RM-to-PMU interface, every per-domain frequency operation exists **only** as a clk-3.5 entry point:

```
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_VOLT_TO_FREQ            0x02
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_FREQ_TO_VOLT            0x03
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_FREQ_QUANTIZE           0x04
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_CLIENT_FREQ_DELTA_ADJ   0x05
```

There is no 3X or 3.0 equivalent of any of them: 
"Apply the client's frequency delta to this domain" is an operation completely absent in pre-3.5
domain.


**Druta therefore ships no per-domain clock knobs for XBAR/SYS/etc. on Pascal because we failed to find any public or legitimate and private means to ship them.** 

## Shunt-mod corrected power

`Device → Shunt mod…`. Simply type in the new effective resistance value to correct the power reading. Planned in the next release is a better per rail calibration.

If you modify the rails with shunt resistors of different resistance, then the multipliers per
rail need per-rail power, and the driver does not report it. Per-rail telemetry does exist on boards that carry an INA3221-class shunt
monitor but is not yet implemented.

## Max it

Had enough with boring sliders to the maximum? Click "max it". It does the V/F deflatten, maxes out the voltage boost, power limit, fan, and holds at 1093mv all in one click. You click it once, and the rest is the actual part of overclocking: changing the frequency. To do it as safely as possible, it does the following in order:

100% fan > maximized power limit > maximized voltage boost > VF deflatten > hold the maximum voltage point. 
(See below for what they do)

If you undo here, it undoes the four changes but *not* the hold because the hold is a
driver state, not a profile value, so `Undo last write` still leaves the card pinned. 


Core and memory clock offsets, power limit, voltage boost, fan duty (with an
Auto button that restores the curve), and the GPU clock lock. All writes sit
behind the **Unlock controls** checkbox — untick it to make the app read-only.

Core offsets snap **down** onto the card's own grid before being sent, because
they land in the same per-point V/F delta table the curve editor uses.

---

# The V/F curve editor

This was the original reason why Druta was built: to automate a lot of tedious dot moving on the V/F curve that I have to do every time I approach a new GPU. Below I am recycling substantial amount of material from the `MANUAL.md`

## The controls

Everythings are staged first. Nothing reaches the GPU until you click the green apply button, and
the plan banner above it proactively tells you exactly what that click will write.

V/F curve editing is controlled by WASD: AD changes the point, WS changes the frequency.
Shift + one of WASD moves the points 3x faster in any given direction.
Ctrl+Z undoes any given changes on the curve for a generous 64 changes deep. Ctrl+Y redoes that change.
Ctrl+H holds a given point on the V/F curve.

## De-flatten

When two or more points on the VF curve land on the same frequency, only the one with the lowest voltage will ever be used. For example, if 1081, 1087, and 1093mv all correspond to 2000mhz, the card will always run at 1081mv, 2000mhz. Deflatten makes sure that every point on the the V/F curve between 1000mv to 1091mv (adjustable) are mathematically strictly increasing. That way, you can run 1091mv immediately without a hard voltage mod.

(Joined overclocking during the time of 4000/5000 series? It might be helpful to know that for 1000-3000 series, almost desktop every GPU can be overvolted to 1091mv by manipulating of the voltage curve. You DO NOT need to bin for voltage.)

## Limited de-flatten

The narrow transform, demoted to the **Clocks** menu: it makes only the cap
point the unique top (i.e. it makes sure that the point of maximum voltage is not on the right side of a flat line), ignore flat portions below it. Useful for severe
thermal throttling with aggressive undervolting; the full reflatten should be used for the rest of the time.

It has two mechanisms, and which one fires depends on the card:

- **Raise** the boundary above whatever shadows it — the original behaviour.
- **Lower** the shadowing points instead, when the boundary is *already at the
  hardware maximum* and there is nothing to raise into. 

It stops as soon as a point sits below the one above it. 

## Hard de-flatten 

**Mandatory hard mod required**

This mode is actually the opposite of deflatten. It flattens everything after 800mv (adjustable), overclocks the 800mv point to the standard P0 frequency, and flattens every point above that. That way, the driver will lock 800mv. This looks counterintuitive because it works in conjunction with a hardware voltage mod and a completely inoperable refin_adj (or whatever that the BIOS uses to control the voltage internally) to neutralize imperfect power limit bypasses.

Imperfect power limit bypasses are for GPUs with no XOC BIOS and don't completely work with shunt mods, such as Titan Xp, 2x8-pin 3080, 4080 Super, 3060, etc. These GPUs still power throttle after shunt mods, even at low percentage of TDP. The power estimation comes from the core and cannot be easily bypassed. By fooling the GPU core that it's at 800mv, you lower the internal power limit reading. However, because you took out refin_adj or similar, the core is actually at whatever higher voltage that you set it at with your external hard mod, which is why I made you check a box to make sure that you have both setup in place.

## The shape law: the delta table is not the curve

On Titan RTX, we discovered that two neighboring VF points cannot have a frequency delta more than 45mhz,

and the driver repairs a violation by **raising the lower** of the pair. This is now taken into account especially for the limited deflatten, because the frequency raises very quickly from the lower points. 

`evaluate_curve_law()` applies the rule in one forward and one backward pass and
reproduces every point of both experiments, so the planner predicts the reshape
instead of discovering it after the write. 

## Re-phase, and why apply does it for you

Re-phase turns a VF curve that you dragged with your mouse into the shape that will eventually be accepted by the driver WITHOUT APPLYING IT. It is a **lossy** step because it quantizes the curve into 15mhz increment of the base frequency.

If you hit apply, re-phase happens automatically by the driver anyway. 


## Undo and redo

`Ctrl+Z` / `Ctrl+Y` (and `Ctrl+Shift+Z`), 64 deep, over the **staged** working
copy. A snapshot is taken per *user action*, including:
- drag, keyboard nudge, Set MHz, planner, and per `Revert edits`, which is itself undoable because it sits
one button from apply.

**This is undo for the editor, not for the card.** It never touches hardware.
Undoing a *write* is the separate autosave mechanism behind `Profiles > Undo
last write`. 

The history is **cleared on every rebase** (`Read curve`, or the re-read after a
write). A snapshot is a set of deltas that only means anything against the
baseline it was taken under.

## The plan banner, and one-click apply

The banner directly above the apply button is recomputed on every edit and
states what the click will write: edited-point count, top ≤cap, peak, and park
point. It **reddens and takes the headline** for a plan that lowers the peak or
for a staged hard de-flatten, and the hard-de-flatten note is sticky underneath
a later ramp.

## Voltage and why it won't fry your GPU

> The NVVDD voltage offsets are *requests* and never write directly into anything low level. 

| request | vcore @ boost 0% | moved | vcore @ boost 100% | moved | |
|--------:|-----------------:|------:|-------------------:|------:|---|
|    `+0` |           781.25 | +0.00 |             781.25 | +0.00 | |
|  `+275` |          1056.25 | +275.00 |           1056.25 | +275.00 | 1:1, exact |
|  `+300` |          1068.75 | +287.50 |           1081.25 | +300.00 | 0% clips here |
|  `+325` |          1068.75 | +287.50 |           1093.75 | +312.50 | 100% clips here |
|  `+400` |          1068.75 | +287.50 |           1093.75 | +312.50 | |

Yes, I applied +400mv, and as expected, absolutely nothing bad happened. All values in mV. Clock pinned at 1500 MHz; the driver returned `ok` for every request, including `+400` (which would have been 1181.25 mV).

It is NOT hooked to any I2C or hardware. Rather, it requests the driver to apply the changes, but the driver is not the only mechanism against failure. Furthermore, 1093.75mv is the maximum value defined in the BIOS (this value can be changed if you flash any XOC BIOS, which is proof that it is also set at a BIOS level) and enforced by PMU/Falcon. The voltage is locked down at ring -1 if not doubly locked down also at ring 0 by `nvlddmkm.sys`. This is a truly decade old question, and NVIDIA's voltage lock post-Pascal has been ironclad. We will not attempt to ship a method that tries to bypass it because we cannot construe of a method. 

## Profiles and undo points

Named profiles snapshot both offsets, the power limit, the voltage boost, the
fan **policy** (not just its duty — auto-at-0% and manual-at-0% read identically,
and handing a captured duty back as a manual duty would be a thermal change) and
every V/F delta, as readable JSON in `profiles/`.

The delta table is written **last** and wins, because the core offset and the
delta table are the same driver rows. A profile from a different card or VBIOS
asks for a second, deliberate confirmation.

An automatic undo point is taken before each of: the core-offset apply, `Reset
all to stock`, the V/F apply, `Reset curve to stock`, a profile load, and **a
memory timing write**. None of these is a stock baseline — `Reset curve to
stock` zeroes the table, which is not the same as putting a previous state back.

---

# Timings

> **TIMING WRITING IS A PASCAL ONLY FEATURE.** Ceteris paribus, writings on GP102 applied, and can be verified by re-reading.
> On TU102, 25 of 25 non-structural fields
> were dropped, and a kernel ioctl doing `WRITE_REGISTER_ULONG` then
> `READ_REGISTER_ULONG` as adjacent instructions returned the old value 300/300. This means that Turing rejects writing to the memoery timing at a very low level (ring -1) with no a software revert.

Decodes and edits the framebuffer-partition memory timing registers through
`nvtune`.



## Locating nvtune

nvtune is a separate program by the wonderful Sebastian Marrufo — a C++17 CLI plus a
kernel-mode driver — and it is free software:

**<https://github.com/sebastianmarrufo/nvtune>** — GPL-3.0-or-later.

> `nvtune` is necessary for the timing tab to work. **Druta does not ship nvtune:** Druta is more beginner friendly and ships no ring-0 drivers, but nvtune ships one, and you must enable test signing for your system for `nvtune`'s driver to be supported. 

### Requirements for Pascal Timing Tuning:

`nvtunedrv.sys` is **signed with a self-signed test certificate**
(`CN=nvtune test signing`, issuer identical to subject) — not a WHQL or
attestation signature. It will not load on a stock Windows machine. Making it
load requires, per nvtune's own `install-on-target.ps1`:

1. **Secure Boot off** — in firmware; test signing cannot be enabled while it
   is on.
2. **Core Isolation → Memory Integrity (HVCI) off** — Windows Security.
3. **`bcdedit /set testsigning on`**, then reboot. You get a "Test Mode"
   desktop watermark and the machine stops enforcing normal driver signing.
4. **Its certificate imported into `LocalMachine\Root` *and*
   `LocalMachine\TrustedPublisher`.**

Putting a third-party self-signed certificate in your Trusted Root store means that certificate can sign anything
your machine will subsequently trust, and that is not just this driver.

This is your call to make. If you are chasing a world record, you should be on a burner SSD with no sensitive data, and that carries very different consequences than running this on your daily rig with personal data. The rest of Druta works without needing to lax these requirements. 

If nvtune is not found, the tab offers an **Enable test signing…** button
rather than a wall of text. Upon clicking into the button, it shows the exact commands for you to copy and paste into CMD and offers a red button that runs them automatically.
That button cannot be clicked unless you run Druta with admin and secure boot disabled (please note that this can be dangerous). BitLocker is reported if it is on, because a boot-config change can force a recovery-key prompt at next boot. The outcome will be echoced.  On failure it shows which command failed and the error message, and offers no reboot. On success it offers `Reboot now` (because nothing is affected until then), which schedules the restart ten seconds out.

Once nvtune loads, the button **is demoted to `Device → Enable test signing…`**.

nvtune is located, in this order:

1. **`DRUTA_NVTUNE`**, and it is **exclusive** — a bad override fails rather
   than quietly running some other copy. Every register offset this tab decodes
   comes out of the binary that actually runs, so "some other copy" is a wrong
   answer, not an inconvenience. The pre-rename `TITANTUNE_NVTUNE` is still
   honoured.
2. Whatever was registered through **Device → Locate nvtune…**, remembered in
   `%LOCALAPPDATA%\Thermetery\Druta\nvtune.json`. A registration made before the
   rename is carried across once from `%LOCALAPPDATA%\TitanTune\nvtune.json`
   (copied, not moved, so a downgrade still works). Registration verifies the
   file exists before recording it.
3. Derived locations — beside Druta first, then `nvtune\` under `%LOCALAPPDATA%`,
   both Program Files roots, and the current user's Desktop, Downloads and home.
4. `PATH`, last, being the least predictable.

Its driver service must be running: `sc start nvtunedrv`, elevated. With either
half missing the tab says **which**, lists every path tried, and stays read-only.

## Lock P0

Timings change with VRAM clocks. On Titan RTX for example:

```
NVML  405 (true  101 MHz)   RC=6   RFC=13   RAS=4   RP=2   CL=9   RD_RCD=2
NVML  810 (true  203 MHz)   RC=11  RFC=25   RAS=7   RP=4   CL=9   RD_RCD=4
NVML 7428 (true 1857 MHz)   RC=78  RFC=210  RAS=52  RP=26  CL=24  RD_RCD=26
```

At P0 those convert to RC 42.0 ns, RAS 28.0, RP 14.0, CL 12.9 — real GDDR6
numbers. The rest, at lower frequencies, is useless for our endeavor. 

Since reading timings at P16 is not useful, and since dropping back to the lower P states resets the timings, reading the timings locks P0 with Druta, which ships two buttons:

**`Read memory timings (will hold P0)`** — blue. It's the button you should use most of the times. It takes
the **V/F point lock, which is the same function as Ctrl+H,** on the highest point at or below the cap, waits for the
memory clock to arrive in the top band, captures, and **leaves the hold on**. No read happens until the memory clock is up.

The CUDA memcpy load is the fallback when the hold cannot be taken, such as in case there is no readable V/F curve. It captures *while the load runs* at P2. If the card is *already* at P0 the load is skipped entirely: opening a CUDA context on a P0 card pulls it
**down** to P2. This works because timings are selected per clock band, not per p-state. On Titan Xp, P2 (mem 5508) and P0 (mem 5702) are bit-identical across all 49 registers.

**`Re-read timings`** — a sanity check after you applied the settings, not a way to get a reading. Might be deprecated soon. 

## Writing


- **`timings.py` — read-only by construction.** Whitelists the read-only
  subcommands and rejects the rest.
- **`timingwrite.py` — the only module that can.** Driven from the **Edit
  timings** panel: an editable `new value` column. Timing values are green while it matches the
  register, and red once it does not. Only red rows are written. a `force`
  checkbox, `Revert edits`, and `Restore stock`.

Timing writings are quaduply guarded:

1. **The card must be in its top memory band.** Timings are per band, so a write
   in any other state will be writing into garbage and will be auto-rejected.
2. **Range and structural refusals before nvtune.**
   Druta does not allow you to write into structural fields (training and phase fragments that have no "looser" or "tigher" direction)
   and fields in a register whose offset is only *inferred* by nvtune. The `new value` column is completely empty for these fields. 
   The `force` checkbox defeats nvtune's *warning* refusal but does not add the buttons for these values.
3. **A dry run always runs first**, so a tool-side refusal is **observed**
   rather than inferred from an unchanged read-back. That inference is exactly
   what recorded four of twenty-five fields as hardware rejections in an earlier
   sweep when they had never reached BAR0.
4. **A per-card stock backup**, keyed by the card's **UUID**, NOT by PCI
   slot or by model name. nvtune's own default is `<slot>.stock.json` with
   an existence-only check, so swapping cards in one slot silently skipped the
   backup. 

Outcomes are reported as four distinct states — **landed**, **dropped** (reached
the hardware and was rejected), **refused** (nvtune declined; BAR0 never
touched), **failed**.


---

# Scope

Research software, developed against the two cards named at the top. Struct
layouts, NVAPI ids and the empirical laws above were verified against those;
nothing here should be assumed to generalise to a third card without
re-verifying. 

---

# License

Druta is free software: you can redistribute it and/or modify it under the
terms of the **GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version**.

Druta is distributed in the hope that it will be useful, but **WITHOUT ANY
WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
A PARTICULAR PURPOSE. See the [GNU General Public License](COPYING) for more
details.

Copyright (C) 2026 Thermetery Technology Co Limited.

`SPDX-License-Identifier: GPL-3.0-or-later`

## Third-party software

The prebuilt `Druta.exe` bundles Dear PyGui (MIT), CPython (PSF), OpenSSL
(Apache-2.0), libffi (MIT-style), the PyInstaller bootloader (GPL-2.0-or-later
with the Bootloader Exception) and the Microsoft C runtime. All are compatible
with GPL-3.0-or-later, and their licenses are reproduced verbatim in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Running from source redistributes none of them.

NVIDIA's NVAPI and NVML are **not** redistributed — `nvapi64.dll` and
`nvml.dll` are loaded from the installed driver at runtime.

[`nvtune`](https://github.com/sebastianmarrufo/nvtune) is a **separate
program** by Sebastian Marrufo, invoked as a subprocess. It is also
GPL-3.0-or-later, so the two licences match exactly — but it is a separate
work, not a component of Druta, and it is not bundled. See
[Locating nvtune](#locating-nvtune).

## Provenance

Every feature of Druta can be reconstructed from public knowledge. 

**Druta contains no NVIDIA proprietary source code.** 
We do not possess or have ever consulted any NVIDIA proprietary source code, or any commerically confidential material. Every fact below is either published by NVIDIA,
published by a third party, or measured here on our own cards.

**The private interface ids are facts about the shipping ABI, not inherited
knowledge.** Every private NVAPI id Druta calls is present in the `nvapi64.dll`
that comes with the driver — in `.text`, and again in the `nvapi_QueryInterface`
dispatch table in `.data` at a regular 16-byte stride.
Anyone holding the driver they are licensed to run can
enumerate that table for themselves, so these ids are independently derivable
and are treated as such here.


| material | source | status |
|---|---|---|
| That this control interface exists at all; that XBAR is domain index **1**; that one entry carries both a signed-kHz frequency offset and a µV rail offset | Loong0x00, LACT issue [#1147](https://github.com/ilya-zlobintsev/LACT/issues/1147) and the gist linked from it — Linux, R610/Blackwell, probed through NVIDIA's **open-source** kernel module | third-party public research |
| The Windows private NVAPI ids | enumerable from the shipping `nvapi64.dll` (15 of 15 verified here); also published independently by `SHANAjam/rtx5090-xbar-control` (MIT), which derives them the same way | facts about the shipping ABI |
| **The Windows control-block geometry** — header `0x124`, entry stride `0x304`, freq delta `+0x10C`, rail array from `+0x110` | **derived here.** It does not match the published Linux R610 layout (header `0x3c`, stride `0x40`, freq `+0x88`, rail `+0x90`) and self-checks against the driver's own version word: `0x124 + 32 × 0x304 = 24996` | measured here |
| `CTRL_CLK_DOMAIN_XBARCLK = BIT(1)`; boardobj class layout; `CTRL_CLK_CLK_DOMAIN_TYPE_*`; the PMU clock RPC list; VBIOS clocks-table field order | NVIDIA's own published nvgpu source | published by NVIDIA |
| GP102 instantiates no `.clk` subdevice | nouveau (in-tree Linux, MIT) | published |
| Decode of this card's VBIOS clocks / clock-programming / NAFLL tables | this project, from our own cards' ROM images | measured here |
| Every domain↔control pairing, every ratio and unit convention, and all four "does this knob actually move the card" verdicts | this project, on TU102 and GP102 | measured here |

**Tracing the experimental feature.** 

LACT #1147 was first opened by Loong0x00 and links their
own gist from its body. `SHANAjam/rtx5090-xbar-control` was created six days after that issue,
cites it in its references, and describes one of its own checks as a direct
implementation of Loong0x00's. That seems to me to be the chain of provenance.

We compared this heavily against NVIDIA's published
nvgpu for the table, which is why it is leaned on so heavily throughout this document.

Both upstream rows rest on material anyone can obtain: Loong0x00 worked against
NVIDIA's open-source kernel module, and SHANAjam's own notes describe
disassembling the `nvapi64.dll` shipped in the driver package. Druta took neither
one's code — only the knowledge that the interface exists, after which the
Windows layout was derived and measured here, and it does not match theirs.

Druta does not ship anything writable that uses a private interface could not be corroborated against something NVIDIA
published. It is either read-only here (the per-domain hard VF lock) or written
only through read-modify-write with a diff guard that refuses if any dword other
than the intended one changed.

## Source for a binary release

Every tagged release on GitHub carries the Corresponding Source for the
executable published with it — that is the tag itself, since Python source *is*
the preferred form for making modifications. `Druta.spec` and
`requirements.txt` are the scripts controlling the build. CPython and the
Microsoft C runtime are excluded as System Libraries under GPL-3.0 section 1;
Dear PyGui is not, so its pinned version in `requirements.txt` identifies the
source that goes with a given build.

## Trademarks

"Druta" and "Thermetery" are trademarks of Thermetery Technology Co Limited.
The GPL grants no rights in them: you may redistribute and fork the code, but
not use these names to brand a derivative.
