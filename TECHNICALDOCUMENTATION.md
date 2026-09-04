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
> [Safety](#safety) before enabling any write path.
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

## Run

- `dist\Druta.exe` — standalone, no Python needed.
- or `python druta.py` from source.
- **Run as administrator** for every write path: clock lock, fan, power limit,
  V/F curve, memory timings.

### Picking a card

```
Druta.exe --list-gpus          # slots and names
Druta.exe --gpu 0000:02:00.0   # open on that card
```

With no `--gpu`, Druta opens on the **lowest PCI slot** — a property of the
machine, not of whichever order a driver happened to enumerate in. In the
window the picker is the **large dropdown in the header**, and the chosen card
is in the title bar whenever more than one is present. It is not in a menu on
purpose: every control in the window is pointed at exactly one GPU and means
different numbers on a different one, so which card is selected is something
you need to *see*, not something you go looking for.

**The switch happens in place**, and works by *rebuilding* the header and the
three tabs against the new card rather than by patching the widgets that hold
per-card numbers. That distinction is the whole design. An audit found those
numbers baked into five separate build methods — the two clock sliders' bounds
and step, the nudge buttons' labels, the core-offset range, the keyboard hint,
the memory divisor on the domains panel, the row counts quoted in two tool
windows, the About box's grid figure. A patch list would have to be kept in step
with every widget added later; re-running the builders re-derives all of them
through the same code that got them right at startup, and cannot fall behind.

`Device → Open a second window on…` still starts a separate process, for
watching both cards at once — and it is the only way to hold a lock on one card
while tuning the other.

The switch refuses while the window is **holding** the card with either lock
mechanism. A hold lives in the driver, not in the window: it stays on the card
it was placed on, while the Release button in front of you would now be aimed at
a different GPU. Staged-but-unwritten V/F or timing edits only ask for a
confirming second click, since losing those costs nothing but the typing.

Two things that are not obvious and are both tested in `test_swap.py`:

- A capture or an induced load can be several seconds — up to 25 — inside a call
  that started on the *previous* card. Each worker stamps a generation counter on
  entry and drops its result if the card changed underneath it, so a snapshot
  cannot be filed under the wrong card's tab with the wrong memory divisor
  applied to its nanosecond column.
- Themes and handler registries are created *unparented*, so deleting the tabs
  does not reach them. Before that was handled, each switch leaked 129 items —
  and, worse than the memory, a second live handler registry, which made one
  press of `W` nudge the point twice and one `Ctrl+Z` walk back two edits.

`app.py` (the old Tk UI) is kept only as a parity reference for the Dear PyGui
port. It has no build target and should not be edited. See
[Why Dear PyGui](#why-dear-pygui).

## Build

```
pip install dearpygui
python -m PyInstaller --onefile --noconsole --name Druta --collect-all dearpygui druta.py
```

Output lands in `dist\Druta.exe`.

---

# Probed, not assumed

**Two hardcoded constants in this project have each been wrong twice.** The V/F
table was 103 points, then 128, then 84. The clock grid was 15 MHz, then
12.657. Each time the constant was confidently documented, and each time it was
a measurement from one card presented as a law.

So the tool now derives per-card quantities from the driver at runtime:

| quantity | how it is found | TU102 | GP102 |
|---|---|---|---|
| V/F table size | widen the point mask until the call fails | 128, all GPU | **84** = 80 GPU + 4 memory |
| GPU frequency scale | compare the table's top against `gfx_max` | direct MHz | **`GPC2CLK`**, i.e. 2× |
| clock grid | span ÷ gaps over the driver's lockable-clock table | 15.000 MHz | **12.657 MHz** |
| clock-domain names | correlate each domain against the driver's own core/memory figures | GPC at domain 0 | **GPC2CLK at domain 15** |

**Read every number in this document as a measurement on a named card, not as a
property of the architecture.** Where a section states a figure without naming
the card, it is TU102 — that is where most of this work was done.

## Which card is "this card"

Three interfaces have to agree, and by default two of them do not:

- **NVAPI and NVML enumerate in different orders.** Measured with the Titan RTX
  at bus 1 and the Titan Xp at bus 2: `NvAPI_EnumPhysicalGPUs` returns the Xp
  first, `nvmlDeviceGetHandleByIndex` returns the RTX first — exactly reversed.
  Pairing the two halves of one GPU object by index would splice one card's V/F
  curve onto the other's name, memory type and clock table.
- **nvtune's `-d` defaults to _all_ NVIDIA GPUs**, not to the first one.

Everything is therefore keyed on the **PCI slot**, in the one spelling all three
accept (`0000:02:00.0`), and the pairing is re-checked against the PCI device
and subsystem ids that NVAPI and NVML report independently. On a mismatch the
NVAPI half is dropped rather than used: reduced telemetry is a visible failure,
writing the wrong card is not.

What the un-targeted nvtune default actually did, before this was threaded
through — all verified on the two-card rig:

| call | without `-d` |
|---|---|
| `set FAW=13 --commit` | writes **both** cards; one number in the UI, two different chips |
| `get FAW` | one line per card; the parser kept whichever came **last**, so a write was graded against the wrong silicon |
| `save -o P` | card 1 writes `P`, card 2 fails `cannot replace` — so a file *named* for one card holds the other's registers |

That last one is the dangerous one: it is the stock backup. A snapshot decoded
against the wrong card is also checked now — nvtune's JSON names its own slot,
and a capture that disagrees with what was asked for is refused rather than
decoded against another card's memory clock.

Stock backups are keyed by the card's **UUID**, not its slot and not its model
name. The slot would orphan a backup the moment the card moved — which this
machine has already done, the Xp's stock file recording `0000:01:00.0` while the
card now answers at `0000:02:00.0`. The model name cannot separate two identical
cards. Backups written under the older name/VBIOS scheme are still found and
reused, because the alternative is taking a fresh "stock" snapshot of a card
that is currently tuned.

---

# Monitor

Seven tiles: core clock, XBAR clock, memory clock (converted to true MHz when
the memory type is known), edge temp, hotspot, power, vcore. Subtitles carry the
p-state, XBAR's delta against core, the memory type and Gbps, the hotspot delta
over edge, and the power limit.

Below them: **ALL CLOCK DOMAINS**, then the clocks-event and perf-decrease masks
(including the insufficient-aux-power bit, a canary for a transplant's power
wiring), a GPU/board power split with per-domain utilisation, PCIe link
generation and width with AER error counters, and a state line carrying the
energy counter, fan duty/RPM, applied offsets, voltage boost, and **both** lock
mechanisms read straight from the driver.

## All clock domains

`NvAPI_GPU_GetAllClocks` (`0x1BD69F49`) is documented by the community as
"probably deprecated". It answers with status 0 and is the only user-mode path
to several domains the public getter doesn't expose. The payload is 1156 bytes,
288 dwords.

Those 288 dwords are **two arrays over the same 32 domains, an exact partition**
— verified over a 192-sample sweep, and enforced by asserts:

| array | dwords | per domain | at | contents |
|---|---|---|---|---|
| A | 0–63 | 2 | `2*d` | `{freq_kHz, capability flags}` |
| B | 64–287 | 7 | `64 + 7*d` | `{freq_kHz, srcid, 0, 0, 0, 0, 0}` |

### A and B are not two views of one number

**A is the target the driver programmed.** Always exactly on the clock grid,
bit-identical across samples for a fixed domain. **B is a measured counter.** It
jitters and never lands on the grid. The tiles quote A; the panel shows both, so
you can see when they disagree.

Measured on TU102, GPC, under ~99% load, sampled ≥8 s after the last clock
change (40 samples per locked case, 20 free-boosting):

| state | A | B | Δ |
|---|---|---|---|
| free-boosting at 1950 | 1950.0 | 1949.90 | −0.10 MHz |
| locked at 1920 | 1920.0 | 1917.03–1921.37 | within 3 MHz |
| locked at 1350 | 1350.0 | 1364.91–1364.94 | **+14.9, dead steady** |

Settled and loaded they agree to a few MHz — and where they don't, **B reads
higher**: at the 1350 lock the card really is running one bin above what A
reports. Domain 2's own programmed word reads 1365 there too.

Two things make Δ wide and neither is a steady state:

- **A clock change in flight**, for ~1–2 s, either sign, up to 1.7 GHz (+600
  measured while locking *down* from 1950; −1700 while locking *up* from idle).
- **An idle card.** With no work the GPC clock gates and B measures the average
  of a mostly-off clock: at a 1350 lock with the card idle, B wandered 470–573
  MHz for tens of seconds.

So a wide Δ on an idle card, or in the second after a clock change, means
nothing. **A steady wide Δ on a busy card is the one that counts** — there the
tiles are optimistic. The panel colours one bin amber and three bins red.

An earlier figure in this document — "at a 1920 lock A reads 1920.0 while B
reads 1886.7" — was **wrong** and is retracted. It came from a sweep that
allowed 0.22 s to settle, so it measured the transient above.

### How a domain earns its name

Names are **not** applied by domain number. A domain-number table is exactly the
thing that moves between architectures: applied blind to GP102 it labelled four
dead rows GPC / XBAR / SYSCLK / VIDEO in confirmed styling, while the real GPU
clock sat unnamed in domain 15.

Each name is now correlated against figures the driver reports independently:

- A domain matching the core clock at 1× is **GPC**; at 2× it is **`GPC2CLK`**,
  named for what it actually holds rather than silently halved.
- A domain matching the memory clock is **MEM**.
- A domain reading **zero on a card that is demonstrably running** is marked
  `unpopulated` and loses its name. An empty slot is not a slow clock.
- The TU102 name table is applied **only** when the card presents the TU102
  signature (GPC correlating to domain 0). GP102 gets its own, gated the same
  way.

Four grades: **CONFIRMED** (correlated against ground truth), **LIKELY** (amber,
with a `?` — behaviour established, the *word* is an analogy), **unnamed**
(index only), **unpopulated**.

| domain | TU102 | GP102 |
|---|---|---|
| 0 | GPC — confirmed | reads zero |
| 1 | XBAR — confirmed | reads zero |
| 4 | MEM — confirmed | MEM — confirmed |
| 15 | — | **GPC2CLK** — confirmed, exactly 2× core |
| 16 | — | **XBAR2CLK?** — LIKELY (see below) |
| 21 | VIDEO — confirmed | reads zero |
| 31 | PCIe link generation, not a frequency — rendered `gen N` | same |

**GP102 domain 16** was identified by a 10-stop V/F lock sweep: it holds
0.962–0.970 of `GPC2CLK` across the whole top half of the curve, and a +60 MHz
core offset moved it by twice the core's move while changing that ratio by
0.0006. So it is a 2× domain riding the core clock. Calling it *XBAR* is an
analogy with TU102, where the domain in the same relationship is XBAR — hence
LIKELY, not confirmed. **Domain 17** holds 0.845–0.850 of `GPC2CLK`, even more
tightly, and has earned no name at all.

A later 16-stop sweep over a 1468-unit range tightened both: domain 16 is
`0.966808 × GPC` (R2 0.99993), domain 17 `0.8466 × GPC` (R2 0.9997). The reason
that mattered is that the original 0.962–0.970 band was wide enough to contain
**two different VBIOS fields** — the 97% slave ratio byte and the 3695/3822
`freq_max` pair — and only the tighter number tells them apart. See *Why Pascal
gets no per-domain knob*.

**TU102's XBAR law**, measured there and nowhere else: with the clock locked at
1800 MHz and vcore swept 912.5 → 1068.75 mV, XBAR never moved off 1725 MHz — it
tracks *frequency*, not the rail. Exact on every observed point and within one
bin across a 104-point sweep:

```
XBAR = max(540, snap15(0.95 * GPC + 15))
```

`XBAR = GPC − 90` is only locally true near 2 GHz. Nothing in the code computes
this; it is recorded measurement.

---

# Control

The first tab, because it is what the app is opened to do. Monitor is second,
Timings third and labelled — it is the only tab that needs a separate program
installed to do anything at all.

## XBAR clock offset

`Control → Clock offsets → XBAR clock offset`. A third offset mechanism, and it
is not a variation on the other two.

**Nothing public can reach this domain.** `NV_GPU_PUBLIC_CLOCK_ID` has four
members — `GRAPHICS 0, MEMORY 4, PROCESSOR 7, VIDEO 8` — and no crossbar entry
at all. Measured here rather than assumed: `NvAPI_GPU_GetAllClockFrequencies`
reports `bIsPresent` for slots 0 and 4 only, on all three clock types, and
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
    +0x114  MSVDD delta, signed microvolts   — never written by this app
```

**This block does not use the clock getter's domain numbering.** Assuming it did
produced a name table that was wrong for every entry but GPC and XBAR, and the
mistake hid itself: an offset written to what was labelled "SYS" landed on
memory, and the check that was supposed to catch it did not watch the memory
clock. The mapping below was established by writing `+45 MHz` to each control
index with the clock pinned and recording which clock actually moved.

| control | moves | notes |
|---|---|---|
| 0 | GPC **and every ratio slave with it** | XBAR, SYS, LTC and VIDEO all shift |
| 1 | XBAR | |
| 2 | MEM | already driven through NVML; no second knob |
| 3 | SYS | |
| 5 | VIDEO | |
| 9 | LTC | coarser step — `+45` requested moved it `+30` |
| 4, 6, 7, 8 | nothing | accept a write, store it, move no clock — left unnamed |

Two tells that the numbering had to differ, both available before the
measurement: the private getter puts VIDEO at domain 21 while this block
refuses every mask bit above 9, and a third-party tool exposes exactly five
controllable domains — core, memory, XBAR, SYS, video — which cannot be
arranged as 0/1/2/4/5.

### Why Pascal gets no per-domain knob

There are docuementations floating around the internet saying that values
of XBAR/SYSCLK/etc. are "NAFLL (noise-aware frequency locked loops)"s for Pascal. 
The name tempted me to say that the "locked loops" are why I couldn't manupulate 
the clocks, because a natural read was that it would be hard coded.

I was inveigled by that thought and was gonna write it off, but then I 
dug into the BIOS and found that for TU102 also classified XBAR/SYS/etc. 
as NAFLLs in the BIOS. 

**Because it's hard coded by the BIOS** GP102's clock programming table
reserves four slave slots per program and populates two of them inside GPC's
program — `dom 1 (XBAR) = 97%`, `dom 3 (SYS) = 90%`. There are no such slots for Turing.
The same search over GP102 finds all three as a positive control. 

That is to say, on Pascal XBAR's target is
*computed* from GPC every time the master's operating point is programmed, and
there is no independent XBAR target for a delta to modify — which is exactly
what the two arrays show: A echoes the request, B never moves.

The measured laws say the same thing, and this is measurement, not inference.
On GP102 the relationship was **swept** rather than sampled. NVML locked clocks
return NOT_SUPPORTED on this part (a Volta+ feature), so the lever is the V/F
point lock, which selects existing points on the card's own stock curve —
nothing is overvolted, it only chooses where on the curve to sit. Stepping it
700 → 1050 mV walks GPC across 2328..3796, a 1468-unit lever arm, with the card
at **0% utilisation** the whole way; no dummy load is needed. Sixteen points,
every populated private domain recorded, and GPC identified *afterwards* as the
domain tracking NVML's core clock rather than by assumption:

```
private domain 15 = GPC    value/core = 2.0000, spread 0.0007
                           (the 2x unit convention, measured not inferred)
private domain 16 = XBAR   k = 0.966808 through the origin
                           affine k = 0.966370, c = +1.51, R2 = 0.99993
```

TU102, for contrast, is affine — and that is what makes it tunable:

```
TU102   XBAR = max(540, snap15(0.95 x GPC + 15))
        at GPC 1800 -> 1725; a pure ratio gives 1710, which is already on the
        15 MHz grid, so the +15 is a real term and not rounding
```

GP102's constant is +1.5 across a 3740-unit span — 0.04%, i.e. zero. A ratio
cannot produce an additive constant, so TU102's XBAR has its own programmed
target that *tracks* GPC rather than being derived from it, and a per-domain
delta lands on that target 1:1. Pascal's has nowhere to land.

**Which VBIOS field is actually load-bearing.** The sweep separates two readings
that a single sample could not:

| hypothesis | predicts | vs measured 0.966808 |
|---|---|---|
| slave ratio byte `0x61` = 97% | 0.970000 | off by 0.003192 |
| `freq_max` pair 3695/3822 | 0.966771 | **off by 0.000037** |

The `freq_max` pair wins by 86x. **The 97% slave-ratio byte is not the number
the hardware uses.** The value XBAR is actually derived from lives at

```
0x08449   XBAR program 25 freq_max = 0x0E6F = 3695   <- the operative field
0x082F7   XBAR slave ratio byte    = 0x61   = 97     <- not this one
```

with one caveat the sweep narrows but cannot remove: 3695 looks *derived* from
97%. This card's clock grid is 12.657 MHz — 25.314 in 2x units — and 3695 is
exactly 146 grid steps, while 0.97 x 3822 = 3707.34 is not on the grid at all.
So NVIDIA plausibly computed the percentage and snapped down, which makes the
two fields related by construction. But 3695 is what is *stored*, it is what the
runtime demonstrably uses, and nothing needs to re-derive it at init.

**The route to that field is closed, and both ends were tested.** Every nvflash
we can find refuses a modified image — and refuses a card's own *unmodified*
dump as well, which says the tool wants its distribution container rather than
merely unedited content. More decisively, an independent GP102
(a GTX 1080 Ti) was written with a modified image by **hardware programmer**,
bypassing every flashing tool, and came back **Code 43 with no display output at
all** — with CSM and Secure Boot disabled to rule out host firmware policy. The
silicon validates its VBIOS at boot. A programmer changes who does the writing,
not whether the card will run the result. (Recoverable by writing the stock
image back the same way.)

So the field above is where the number lives, and it is not reachable. **On
GP102 the only lever on XBAR is GPC** — raise it and XBAR follows at 97%. The
identification is still worth recording, because it explains what the domain
does and why the knob reads target-only, but it is not a route to anything.

Two domains corroborate the table decode without having been looked for: private
domain 20 sits constant at 540, which is the clocks table's `FIXED 540 MHz`
entry, and domain 18 constant at 1296, which is clock programs 16-19's
`freq_max`.

**Unresolved, and it is the SYS half of the story.** Private domain 17 — the one
that looks like SYSCLK — fits a clean `0.8466 x GPC` in array A (R2 0.9997),
matching *neither* the declared 90% *nor* 3417/3822 = 0.894. Its measured array
plateaus badly (R2 0.80, affine slope collapsing to 0.24), consistent with
hitting a ceiling partway up the sweep. That is the same 0.845-0.850 recorded
further up as unnamed. **The XBAR result does not extend to SYS** until this is
explained.

**The ROM names the split itself.** The clocks table carries a `clocks_hal`
field — **2** on GP102, **3** on TU102 — telling the driver which clock HAL to
run, alongside the v0x10 / v0x35 table generation. The private per-domain
control block is the 3.5-era interface; the driver version-checks it by struct
size, so a hal-2 part accepts the write and stores it, and then programs XBAR
from the ratio regardless.

One thing not established: TU102's clock programming table was never located —
its `'C'+0x0C` pointer (`0xF12C`) runs past the legacy image's `0xEC00` end and
lands in compressed GOP payload. So "no ratio record on Turing" is proven for
the clock-table cluster, not for the whole 1 MB image.

The other two knobs are settled by **which NAFLLs are fitted**. TU102's NAFLL
device table has ten entries — GPC0-5 plus XBAR (id 2), SYS (id 0), VIDEO
(id 10) and LTC (id 11), at clocks indices 0/1/3/5/9, exactly the domains Druta
drives. GP102 has eight: GPC0-5, SYS, XBAR. **No VIDEO NAFLL and no LTC NAFLL
exist on the part**, which is why controls 5 and 9 are refused there and why
domains 5/6 are `FIXED 600`/`FIXED 540` instead.

So on Pascal the only lever that exists is GPC — raise it and XBAR follows at
97%. The four negative avenues stand (private control block records the offset
and the hardware ignores it; 108k BAR0 dwords surveyed across two clock states,
four moved, none a frequency; the propagation-ratio interface returns nothing;
no Maxwell-style clock-states array), and the reason all four are negative is
that on this part XBAR is not a thing you set, it is a thing that is computed.

Reading BAR0 on these parts, the `0xBADF` sentinels are worth decoding rather
than lumping together — the code is bits 31:8:

```
0xBADF5040  FECS_PRI_CLIENT_ERR   ring station alive, client refuses:
                                  priv-level-masked or power-gated
0xBADF1100  FECS_PRI_DECODE       address maps to no register
0xBADF13xx  FLOORSWEEP            unit not present on this die
0xBADF10xx  PRI_TIMEOUT
```

So `0x137000` on a GP102 is *locked*, not empty; the dwords that do answer
there are noise-aware clock **counters**, which is measurement rather than
control.

**The control indices are architecture-stable; the private domain each one
moves is not.** Control 1 is the crossbar on both cards measured, but it lands
on private domain **1** on TU102 and **16** on GP102, and control 3 lands on
**2** and **17** respectively — while control 9 isn't accepted on Pascal at
all. The pairing is therefore gated on the same signature the name tables use
(GPC at domain 0 is the TU102 shape, at 15 the GP102 one), and an unrecognised
card gets an **empty** map rather than the Turing one: a knob reading `--` is
honest, a knob quoting an unrelated domain's clock is the exact failure this
app exists to avoid. A build that hardcoded the Turing pairing put the amber
TU102 name `SYSCLK?` on a Pascal knob and read its value from a domain that is
unpopulated there.

Detect that signature through `GPU.read()`, never a bare
`read_clock_domains()` — without `core_mhz` the naming runs blind, cannot apply
the unpopulated check, names a dead domain 0 "GPC", and reports every card as
Turing.

### Blackwell / RTX 50-series adaptation

The Turing measurements above must not be applied to Blackwell by changing only
`CLKDOM_PAIR_TURING`. There are two independent namespaces: the control index
accepted by `CLK_DOMAINS`, and the private clock-getter domain index used for
telemetry. The latter is not assumed to be the same on a new architecture.

The backend now contains a guarded Blackwell candidate layout based on the
public [Windows notes](https://github.com/SHANAjam/rtx5090-xbar-control/blob/main/docs/TECHNICAL_NOTES.md)
for the same NvAPI ids:

```text
entry(d) = 0x124 + d * 0x304
frequency delta = entry + 0x114
MSVDD delta     = entry + 0x11C
```

Those field offsets are a candidate until they have been checked against the
exact GPU, VBIOS and driver. The version echo and one-hot accepted-domain probe
are read-only gates. The UI uses the accepted control indices for Blackwell and
displays the requested offsets; it does not label those values with a Turing
private-getter domain.  On the validated RTX 5080 / 610.88 path, the tested
controls are 1 = XBAR, 3 = SYSCLK and 4 = VIDEO.  Control 1 has an inverted
wire polarity, which the UI compensates for when displaying and writing its
logical offset.

For hardware validation, run:

```powershell
python nvbackend.py --clkdom-debug --json > clkdom-debug.json
python nvbackend.py --clkdom-map-probe --confirm > clkdom-map.json
```

When the candidate `+0x114` is accepted but produces no settled movement, use
the frequency-only comparison probe:

```powershell
python nvbackend.py --clkdom-field-probe --confirm > clkdom-fields.json
```

It compares `+0x10C` with `+0x114` for controls 1, 3 and 4. It deliberately
does not probe the adjacent NVVDD/MSVDD rail fields.

Run the same command with `--delta -5` and compare the signs of the reported
changes. A real field/control mapping should reverse direction; a P-state or
idle-clock transition is not evidence.

If both candidate fields accept the write but controls 1, 3 and 4 show no
settled physical effect, scan the remaining non-core/non-memory control
indices:

```powershell
python nvbackend.py --clkdom-control-probe --delta 25 --confirm > clkdom-controls.json
```

This intentionally omits control indices 0 and 2 because they may be GPC and
memory on a different driver branch.  To include those two potentially
high-impact paths on a test-only machine, pass the explicit opt-in:

```powershell
python nvbackend.py --clkdom-control-probe --include-core-memory --delta 25 --confirm > clkdom-controls-all.json
```

The scan is still one-field-at-a-time, checks the complete returned block
before each write, and restores that block in a `finally` clause after every
control.  The JSON now includes the settled `before` window as well as the
`after` window, plus an immediate GET readback of the requested frequency and
mode.  Each window also records P-state, utilization and public core/memory
clocks.  This separates a request that the setter stores from one that the
driver accepts but silently discards, and distinguishes a real loaded test
from an idle-clock ceiling that never had to move.  A physical effect is only
accepted when at least one settled observation also moves in the same direction
as the signed request; a reverse-direction or one-sided transient is reported
but is not treated as a mapping.  The final mapping verdict uses the direct
XBAR/SYS/memory/video observations; GPC is retained as an operating-point
diagnostic but is not direct evidence because it can drift by a few MHz while
the request is applied.  Private getter rows remain in the JSON for diagnosis
but cannot by themselves turn a transient into a success.  The JSON also keeps
`median_shift_candidates` for large or unsettled changes and
`reverse_directional_observations` for controls that move opposite to the
signed request; neither is a positive mapping verdict by itself.

For a large temporary probe, the minimum physical-effect threshold scales with
the request (up to 25 MHz).  This prevents a few MHz of normal counter jitter
from being reported as a successful ±200 MHz mapping while preserving the
full before/after windows for review.

The temporary diagnostic limit is `±200 MHz`. This is available for a driver
that stores `±25 MHz` correctly but does not move a clock by a measurable
amount; it is not a normal UI tuning range and should only be used with a
stable V/F hold and a high, continuous workload.

The first command never writes. The latter two are explicit administrator-only
diagnostics: they test controls 1, 3 and 4 one at a time with a small temporary
frequency delta, compares median/range windows of physical XBAR/SYS/VIDEO
observations, verifies the original requested frequency after restoration, and
restores the complete GET buffer even if sampling fails. Use a fixed GPU-clock
or V/F hold, or a steady workload, while running it; an unstable P-state is
reported as inconclusive. It is deliberately not called by the UI. A new
driver or VBIOS should not be added to a validated profile until both the field
location and the measured control effect are confirmed.

The voltage fields are a **rail array**, not one value — probing every dword
from `+0x100` to `+0x140` found exactly three consecutive refused slots on every
domain, which is what a rail array whose upper members this silicon lacks looks
like. Aligned against the frequency field, rail 0 is at `+0x110`:

- **Rail 0 is NVVDD** and it works. `+50 mV` requested moves vcore exactly
  `+50 mV`, measured with the core clock pinned at 1500 MHz. Shipped as
  *NVVDD offset (mV)*.
- **Rail 1 is MSVDD and is not reachable here.** Refused on every control domain
  that does anything, and accepted only on domain 6 — which stores frequency
  offsets it never applies either, so its acceptance means "nothing validates
  this", not "this rail exists". Read and displayed, never written.

**Measure a rail with the FREQUENCY lock, never the V/F point lock.** A held
V/F point pins the voltage, so a rail offset applies and nothing moves — which
reads as "the write does nothing". Worse, because the point lock selects "the
highest point at or below a voltage", shifting the rail changes *which* point is
held, and the resulting reading turned a true 1:1 response into an apparent
0.25:1. Both mistakes were made here before the frequency lock settled it.

All four per-domain knobs are verified end to end **against array B, the
measured counter — not array A**: with the frequency pinned, `+45 MHz`
requested moved both the programmed target *and* the measured clock by
`+45 MHz` on XBAR, SYS, VIDEO and LTC alike. LTC is the one that can land
short — `+45` has been seen arriving as `+30` at a different starting clock.

That distinction is the whole game. **A is an echo of the request** and moves
whether or not the hardware obeys; only B says what the card is doing. On
GP102 the difference is total: control 1 moves A from 3442 to 3493 while B sits
at 3290.3, and control 3 moves A from 2986 to 3037 while B sits at 2885.4. The
driver records the offset and the hardware ignores it, so the entire request
turns into programmed-vs-measured divergence — which is exactly why the delta
column goes red in proportion to the slider on that card.

**Settled: the consumer is PMU firmware, and the operation does not exist before
clk 3.5.** The open question was what *reads* the stored delta. It is not code in
`nvlddmkm`. Two findings from the shipped driver (580.97, 110,465,168 bytes, of
which only 19.3 MiB is executable) say so: the NV2080 **CLK interface
`0x208010xx` has zero entries in the RM control dispatch table** — one stray
reference in the whole binary — and the clock routines that do exist are called
directly rather than through a chip-indexed HAL table. There is no
`if (Turing)` branch to find, because per-domain clock work is not host x86.

NVIDIA's published RM-to-PMU interface carries the answer. Every per-domain
frequency operation exists **only** as a clk-3.5 entry point:

```
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_VOLT_TO_FREQ            0x02
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_FREQ_TO_VOLT            0x03
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_FREQ_QUANTIZE           0x04
NV_PMU_RPC_ID_CLK_CLK_DOMAIN_35_PROG_CLIENT_FREQ_DELTA_ADJ   0x05
```

There is no 3X or 3.0 equivalent of any of them; the other clock RPCs are counter
sampling, effective-average frequency, load, VF-change inject and mclk switch.
"Apply the client's frequency delta to this domain" is an operation a pre-3.5
domain simply does not have.

The surrounding structure agrees. `clk_domain_3x_prog` carries the `deltas` field
and `3x_slave` inherits it — which is precisely why the write is accepted and
echoed back — while slave construction pins `freq_delta_min_mhz =
freq_delta_max_mhz = 0`, and the slave frequency is computed as
`(masterclkmhz * ratio) / 100` with no delta term on either the 1X or the 3.5
ratio branch. Clk 3.5 additionally materialises per-domain VF points that each
carry a `ctrl_clk_freq_delta` — which is the object an additive `+15` needs in
order to exist at all.

The live card corroborates it: GP102's control-block type field reads `3X_FIXED`
2, `3X_MASTER` 4, `3X_SLAVE` 5 — three for three against the published enum.

*Scope of the claim.* The RPC list and struct evidence come from NVIDIA's
published nvgpu, which is Tegra; the Windows evidence is the negative space
described above. This is corroboration, not a positive identification of the
dGPU routine — no dGPU `getslaveclk` was located in `nvlddmkm`. What carries it
is the 3-for-3 type match on the actual card plus the fact that every measured
behaviour fits.

The consequence is the one that matters for this tool: VBIOS, kernel memory,
BAR0 and the control block are all *host* surfaces, and the consumer is signed
PMU firmware. The four closed routes failed for one reason, not four.

**Druta therefore ships no per-domain clock knob on Pascal.** A knob appears
only where moving it was measured to move the card's *measured* clock. The
control→domain correspondence on GP102 is real (control 1 does drive domain
16's target, control 3 domain 17's) and is kept in `nvbackend` as
`CLKDOM_PAIR_PASCAL_TARGET_ONLY` so nobody re-derives it — but it is not a
shipped control. Note also that `nvmlDeviceSetGpuLockedClocks` is unsupported
on GP102, so stabilising that card for measurement needs the V/F point lock.

The geometry self-checks: `0x124 + 32 * 0x304 = 24996`, exactly the size the
driver declares through its version word. A wrong header or stride would not
divide that size evenly. XBAR is domain **1**, corroborated four independent
ways — this card's private clock getter, the mask value that selects it,
NVIDIA's own nvgpu source (`CTRL_CLK_DOMAIN_XBARCLK = 0x00000002`, i.e.
`BIT(1)`), and the published RM layout.

**Direction was established by measurement, not by trusting a label.** The read
call and the write call share a struct and a version word, so a transposed
get/set label would have written into a signed kHz field and a microvolt rail.
Instead the applied XBAR offset was measured before and after a read: `+90 MHz`
both times, 24/24 samples each way, clock pinned. Only then was anything
written.

**The number you set and the number the card runs are different numbers**, and
both are real. This block stores the *request*; the driver floors it to whole
clock bins. A `+100` request runs as `+90`; a `-50` request runs as `-60`. The
slider floors on Apply and writes the floored value back to itself, so what is
on screen is what the card took — the same rule as the core offset. The
*applied* figure is never read from here; it comes from the private clock
getter (domain 1), which is what the Monitor tile shows.

**XBAR is a ratio slave of GPC.** NVIDIA's clock model governs it by a ratio
against the core, and past roughly 1:1 the governor either clamps the offset or
drags GPC up to keep the ratio legal. On TU102 the untouched relationship is

```
XBAR = max(540, snap15(0.95 * GPC + 15))
```

confirmed to the bin at 1455 / 1950 / 2010 / 2040 / 2115 MHz, and independently
on a second TU102 (2250 core → 2160 XBAR). `+90` is the offset that produces
1:1 across roughly 1950–2130 MHz on this card, which is why it is a common
landing point rather than a coincidence.

Writes are read-modify-write: the buffer sent is the one the getter produced
with exactly one dword changed, and the write is refused outright if a
re-read shows any other dword differing. 768 of the 772 bytes in an entry are
fields we have not identified, and the app does not synthesise them.

## Shunt-mod corrected power

`Device → Shunt mod…`. A board measures rail current as the voltage across a
sense resistor and divides by the resistance it was *built* to expect. Lower
that resistance and the card believes it is drawing less than it is — which is
the point, because the power limit is enforced on the believed number.

Give each rail its **original** and its **effective** resistance; the
multiplier is `R_orig / R_eff`. A resistor soldered on top of an existing shunt
bridges the same two pads, so the two are in **parallel** and the value
*halves*: 5 → 2.5 mΩ is ×2. (In series it would rise and the card would
*over*-report, which is the opposite of what a shunt mod is for.) Replacing the
part outright works the same way — only the two numbers matter.

The POWER tile then shows the corrected figure, with the raw reading and the
multiplier beneath it, and the limit restated in real watts — a 320 W limit on
a ×2 board is really 640 W.

**Uniform mods are exact.** If every rail ends at the same multiplier, the
board total is scaled by one number and how the load divides between rails is
irrelevant.

**Mixed mods are an estimate, and are labelled one.** Different multipliers per
rail need per-rail power, and the driver does not report it — probed on a Titan
RTX, NVAPI's power topology returns two domains (GPU and board) as per-mille of
the limit, and NVML's `POWER_AVERAGE` answers only for scope 0 while
`POWER_INSTANT` is unsupported. A mixed configuration is weighted by each
rail's rated capacity and the reading turns red with `est` beside it.

Per-rail telemetry does exist on boards that carry an INA3221-class shunt
monitor — that is where HWiNFO's *PCIe Slot Power* and *8-Pin #1 Power* come
from, and the per-rail *voltage* alongside them is the giveaway. It is read
over the card's I2C bus rather than from the driver. Reaching it from here is
possible in principle (all four `NvAPI_I2C*` entry points resolve) but is not
implemented.

## Max it

One button beside `Reset all to stock`, doing the four things people do by hand
at the start of a session:

| order | knob | to |
|---|---|---|
| 1 | fan | 100% |
| 2 | power limit | this card's maximum |
| 3 | voltage boost | 100% |
| 4 | V/F curve | de-flatten, apply, then hold the cap point |

**Headroom first, clocks last.** Cooling before the power budget rises, budget
before the extra voltage spends it, curve last because it is the only step
asking for more clock. Reversed, each step spends headroom the next one is
about to provide.

**Everything is validated before anything is written**, so a refusal costs
nothing. It refuses outright if V/F edits are already staged — it will not
write a plan it did not make, and a *hard* de-flatten left staged for a look is
the case that makes that a safety rule rather than tidiness — and it refuses if
the de-flatten would *lower* the peak clock, which is definitionally not what a
button called Max it is for. Both leave the card untouched.

**The hold is what makes the rest stick.** A de-flattened curve is a *shape*;
without a hold the arbiter still chooses where to sit on it, and it picks the
lowest voltage of any peak-frequency flat run — the very behaviour de-flatten
exists to work around. The final step is the same V/F point lock `Ctrl+H`
applies, on the highest point at or below the cap.

**One undo point covers the four writes** — but *not* the hold. A lock is
driver state, not a profile value, so `Undo last write` leaves the card pinned;
`Ctrl+H`, Release, or `Reset all to stock` drop it. `vf_apply` is called with `autosave=False`
so it does not take a second one: `Profiles → Undo last write` loads the
*newest* snapshot, so a second point taken after the three knobs had moved
would undo only the curve and leave fan, power and voltage maxed.

Two things it does not do: the fans stay at **100% manual** until `Auto` or
`Reset all to stock` — they do not ramp back down — and de-flatten works
*below* the voltage cap in the V/F editor's cap box, so that box bounds what
"max" means. The log names the cap it used.


Core and memory clock offsets, power limit, voltage boost, fan duty (with an
Auto button that restores the curve), and the GPU clock lock. All writes sit
behind the **Unlock controls** checkbox — untick it to make the app read-only.

Core offsets snap **down** onto the card's own grid before being sent, because
they land in the same per-point V/F delta table the curve editor uses.

## Lockable clocks are not a ceiling

`nvmlDeviceGetSupportedGraphicsClocks` reports a table *per memory clock*, and
the top row is what a naive read quotes as the card's maximum. It isn't one. The
curve editor goes above it and the card runs there — 2175 MHz observed on TU102
against a 2160 lock ceiling.

The lock also needs snapping. **Measured:** a 1234 MHz request (between the valid
1230 and 1245) ran at **1245**, while both the API and the driver's own lock
record still claimed 1234. Range-checking is not enough — the driver accepts any
in-range value, reports success at the value asked for, and then runs the next
enumerated clock *up*. So requests are snapped down into the table first.

## Two lock mechanisms, one table

`0xE440B867` (getter) and `0x39442CFB` (setter) share a 780-byte struct, and
**only `lockMode` separates two entirely different mechanisms**:

- **mode 2** — the NVML clock-range lock. Its `volt_uV` field is a **frequency
  in kHz**, not a voltage: domain 0 takes `hi`, domain 1 takes `lo`.
- **mode 3** — the V/F point lock (Ctrl+H).

Every lookup matches on **mode**, never on `lockMode != 0`. Reading a mode-2
entry as a voltage yields a confident and entirely wrong "1350.00 mV", and
clearing one from the V/F side would silently release the other mechanism.

**The struct stores the request, never what is held.** A 900000 µV lock reads
straight back as 900000 while the rail sits at 893.75. Mode 3 means *lock to the
highest V/F point at or below the request* — resolution has to be derived
against the curve, which `resolve_vf_point()` does in two stages: resolve down
to a point, then apply the flat rule below.

Measured on TU102's rail: requesting 900.00 mV held **1740 MHz at 893.75 mV**,
because idx 71 is the other half of a 1740 MHz flat. Requesting 950.00 mV held
950.00 / 1830, idx 80 being the lowest member of its own flat.

Both locks are **readable back** from the driver, so a lock left by a killed
instance or another tool is visible. `VF_LOCK_DOMAIN = 6` is only a fallback: an
existing lock is re-targeted at whatever domain the driver already has it on.

---

# The V/F curve editor

## Why it exists: the arbiter parks at the bottom of a flat

**The boost arbiter can only occupy, for each distinct frequency, the lowest
voltage carrying it.** A flat run in the curve is therefore not merely wasted
headroom — it is a voltage band the card **cannot sit in at all**.

That matters most when throttling, because a throttling card does not sit at its
park point: it walks **left** down the curve until it is under budget, and what
decides performance then is how many operating points it has to choose from.

Measured on TU102's stock curve:

| from | to | voltage dropped |
|---|---|---|
| below 1050 mV | uniform | 12.50 mV per 15 MHz |
| 1175.00 / 2010 | 1137.50 / 1995 | **37.50 mV** |
| 1137.50 / 1995 | 1106.25 / 1980 | **31.25 mV** |
| 1106.25 / 1980 | 1050.00 / 1965 | **56.25 mV** |

Between 1050 and 1175 mV there are **21 voltage points and only 4 are usable** —
17 are shadowed. Power goes roughly as `f·V²`, so shedding 56 mV to give up 15
MHz dumps far more power than the budget asked for and the card undershoots.
Thermetery internal testing measures **up to 7% of a benchmark** lost this way
with an imperfect power-limit bypass (shunt mods, where the card's own
current-sensing still throttles).

**GP102 has the same pathology with different numbers**: 17 of its 80 GPU points
hold 1911 MHz from 1081.25 mV upward, while the rail stops near 1062.5. Note
that the *consequence* there is inferred from the TU102 rule — the park rule
itself has not been separately measured on Pascal.

## The controls

Everything **stages**. Nothing reaches the GPU until the green apply button, and
the plan banner above it says exactly what that click will write — before the
click, not after.

| control | what it does |
|---|---|
| `Read curve` | re-read the hardware curve, discarding staged edits |
| `Re-phase to N MHz increments` | stage a phase correction (N is the card's grid) |
| `De-flatten` | **the main transform** — rebuild the band as a strictly increasing ramp |
| `Fit view` | put the whole curve back on screen |
| `Reset curve to stock` | zero every GPU-row delta |
| `Limited de-flatten ≤ cap` | *(Clocks menu)* make only the cap point the unique top |
| `Hard de-flatten` | *(collapsed header)* needs an external voltage mod |
| `Re-phase and apply V/F curve to GPU` | the only thing here that writes |

Keyboard: `W`/`S` nudge one bin (`Shift` for three), `A`/`D` select, `Ctrl+H`
holds the selected point, **`Ctrl+Z`/`Ctrl+Y` undo and redo staged edits**.
Drag a dot to move it; drag anywhere else to pan. The grab is a **pixel** hit
test — the Tk-era "nearest by voltage anywhere on the plot" rule made empty sky
a drag handle for whatever point shared that column.

## De-flatten

Rebuilds the band between the ramp floor and the voltage cap as a strictly
increasing ramp: one distinct frequency per voltage point, no ties. On TU102's
default 1000 mV band that takes the band from **5 usable operating points to
16**.

**Top-anchored, deliberately.** The cap point takes the highest allowed
frequency and each point below drops one bin. Ascending from the floor instead
*clips*: from 800 mV the unclipped top would exceed the card's max, so the top
rungs get clipped onto it and a flat run reappears exactly where it hurts most.

**Where there is no headroom, it shrinks the band.** A clipped ramp used to keep
its rung count and slide the whole descent down — which put the bottom rungs
*under* the untouched point below the band, where the shape law raised them all
back onto it as one flat, the exact pathology the ramp exists to remove.
Measured on GP102: a 10-rung band from 1000 mV delivered 8 distinct frequencies,
with idx 55/56/57 collapsed onto 1822.5. It now drops rungs from the bottom
until the floor clears its neighbour, and reports how many and why. Dropped
points keep their stock values, which are already increasing.

**The granularity gain and the overclock are the same edit.** Every rung asks
for more clock at its voltage than stock did. There is no version of this that
improves granularity without also being an overclock, so **every rung has to be
stable in its own right**.

## Limited de-flatten

The narrow transform, demoted to the **Clocks** menu: it makes only the cap
point the unique top and leaves every flat below it alone. Useful for severe
thermal throttling with aggressive undervolting; the full rebuild is what you
want the rest of the time.

It has two mechanisms, and which one fires depends on the card:

- **Raise** the boundary above whatever shadows it — the original behaviour.
- **Lower** the shadowing points instead, when the boundary is *already at the
  hardware maximum* and there is nothing to raise into. GP102 stock peaks at
  1911 with the point below it also at 1911, so this is the path there. The
  arbiter rule only cares that the boundary is the lowest voltage carrying the
  peak, and lowering a shadowing point costs nothing at the park point — the
  card could never occupy it anyway.

It stops as soon as a point sits below the one above it. A naive bin-per-point
descent never terminates early on a stock curve, because the curve descends at
almost exactly one bin per point too — it cost 24 points reaching down to 850 mV
to fix a two-point flat before that was fixed.

## Hard de-flatten — the opposite transform, and it needs a hardware mod

**This is the opposite of De-flatten and deliberately so.** De-flatten *removes*
flats so a throttling card has fine steps to descend through. This one *builds*
the largest flat it can, to exploit the arbiter's lowest-voltage rule instead of
working around it:

- Set a floor (default **800.00 mV**, adjustable, allowed lower).
- Every point at or above it is set to **one** target frequency.
- The arbiter runs the lowest voltage of any peak-frequency flat, so the card
  parks **at the floor**.
- The target must hold P0, so it is seeded from the curve's own peak.

The point is **deceiving the power estimator**: the GPU believes it is at 800
mV, computes a low power figure from that belief, and stops throttling — while
the real rail is driven externally and is invisible to all GPU software,
including this app.

> **It requires `refin_adj` (or the board equivalent) rendered completely
> nonoperational and voltage driven externally.** Without that it is a driver
> crash, not an overclock. It sits behind an explicit acknowledgement checkbox
> that is never persisted and clears itself after one use.

**The shape law makes it reach below the floor.** Nothing is *written* below the
floor, which is not the same as nothing *changing* there: on TU102 the max-rise
repair drags **16 points** up with the flat top — idx 40 (700.00 mV) through idx
55 (793.75 mV), worst case **1530 → 1965 MHz at a nominal 793.75 mV** — with no
delta written to any of them. The staged plan states the count, the lowest
voltage affected and the worst rise, before the click.

The numbers this mode reports are **planner predictions**. There is no modded
card here, so no part of it has been executed on hardware.

## The shape law: the delta table is not the curve

The delta table takes whatever you write — verified, a 14-row ramp read back
with **zero** mismatches. But the curve the driver *evaluates* is not free-form.
Measured on TU102, it always satisfies

```
0  ≤  f[i] − f[i−1]  ≤  45 MHz          (points in voltage order)
```

and the driver repairs a violation by **raising the lower** of the pair. Both
halves bite, and neither is visible in the table you read back:

- **Lower bound.** idx 60 (825.00 mV, 1605 MHz) written to 1545 read back as
  **1590** — its left neighbour's value.
- **Upper bound.** idx 60 written +150 to 1755 pulled idx 56–59 up to
  1575/1620/1665/1710, each exactly 45 MHz under the next, stopping the instant
  idx 55's untouched 1530 was within 45 of idx 56. **An edit can move points
  outside the range it wrote.**

`evaluate_curve_law()` applies the rule in one forward and one backward pass and
reproduces every point of both experiments, so the planner predicts the reshape
instead of discovering it after the write.

Consequence: **a band can never deliver more than `(top − the point below it) /
bin + 1` distinct rungs**, however many points it spans.

> **Scope.** The 45 MHz bound was measured on **TU102 only**, over two edits at
> one index. `max_rise_khz()` returns **three grid bins**, which reproduces 45
> MHz exactly on Turing — but what was measured is "45 megahertz", and treating
> it as "three bins" is an **assumption**. On GP102 the lower half
> (non-decreasing, repaired by raising) *is* corroborated; the upper bound is
> not. On any non-TU102 card the reshape prediction is unverified.

## Re-phase, and why apply does it for you

A V/F point evaluates as `floor((base + delta) / bin) * bin`, and **`base` is
not readable** — the reported frequency is already floored, so `base` carries an
unknowable remainder. Deltas must therefore never be computed from an absolute
target; only whole-bin changes are reliable. Asking for 2150 on a 15 MHz grid
yields 2145, which collides with the point below and re-creates the flat you
were removing.

Two points only move together under a uniform offset if their deltas share a
remainder mod the bin. A point on another phase crosses bin boundaries at a
different offset and silently re-creates a flat. **Re-phase** forces them onto
one phase, rounding off-phase deltas **down**.

Everything done in the editor is already on-phase, because edits move whole
bins. What is *not* is the **core offset slider**, which lands in the same delta
table in whole MHz: on GP102 a +64 MHz offset is 64000 kHz against a 12657 kHz
grid. Those arrive through `Read curve` — points you never touched.

So the apply button **re-phases the staged plan first, in the same click**, and
says so in its label. The order is forced: re-phase plans off the deltas about
to be written, so doing it after the write would mean two writes and a banner
that described the first. It is a **lossy** step when it bites — the original
remainders are gone — and it logs that.

## Undo and redo

`Ctrl+Z` / `Ctrl+Y` (and `Ctrl+Shift+Z`), 64 deep, over the **staged** working
copy. A snapshot is taken per *user action*, not per point: one entry per drag
(taken on grab — the drag callback fires every frame), per nudge, per Set MHz,
per planner, and per `Revert edits`, which is itself undoable because it sits
one button from apply.

**This is undo for the editor, not for the card.** It never touches hardware.
Undoing a *write* is the separate autosave mechanism behind `Profiles > Undo
last write`. A single merged history would mean Ctrl+Z after an apply silently
re-writing the table.

The history is **cleared on every rebase** (`Read curve`, or the re-read after a
write). A snapshot is a set of deltas that only means anything against the
baseline it was taken under.

## The plan banner, and one-click apply

The banner directly above the apply button is recomputed on every edit and
states what the click will write: edited-point count, top ≤cap, peak, and park
point. It **reddens and takes the headline** for a plan that lowers the peak or
for a staged hard de-flatten, and the hard-de-flatten note is sticky underneath
a later ramp.

Apply is **one click**, not two. The press-again arm it replaced only described
the plan once the user had already committed to pressing; the banner describes
it continuously, and `autosave_before` makes the write recoverable. `Reset all
to stock` still takes two, because it drops every knob at once.

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

Decodes and edits the framebuffer-partition memory timing registers through
`nvtune`.

## A cycle count is not a time until you know its clock

A timing register holds a **cycle count**, and a cycle count without the clock
it was counted against is not a number. The same registers on TU102 at three
memory states (NVML figure → true clock = NVML ÷ 4):

```
NVML  405 (true  101 MHz)   RC=6   RFC=13   RAS=4   RP=2   CL=9   RD_RCD=2
NVML  810 (true  203 MHz)   RC=11  RFC=25   RAS=7   RP=4   CL=9   RD_RCD=4
NVML 7428 (true 1857 MHz)   RC=78  RFC=210  RAS=52  RP=26  CL=24  RD_RCD=26
```

At P0 those convert to RC 42.0 ns, RAS 28.0, RP 14.0, CL 12.9 — real GDDR6
numbers. The same registers at idle look like garbage, and that is what made the
first two dumps of this investigation unreadable. GP102 at its top band (mem
5702 → 1425.5 MHz true) gives RC 58 = 40.69 ns, RAS 36 = 25.25, RP 22 = 15.43,
CL 19 = 13.33.

Every capture **brackets the register read with a memory-clock read on each
side** and refuses to print a nanosecond column if the clock moved. A capture
straddling an 810 → 7428 reclock turned RC's 42 ns into 385 ns; that number must
not reach the screen.

**Two fields do not convert** and are refused a nanosecond figure: **RFC** (210
cycles = 113 ns against a 240–350 ns spec, so it is a multiplier or its range
splits with `TIMING22`) and **WL** (GDDR6 write latency is expressed relative to
CL, not as an absolute delay).

## Timings are selected per clock band, not per p-state

Measured on TU102, `CONFIG0..CONFIG5` are **bit-identical** at 7228 (P2, CUDA
load) and 7428 (P0, 3D load) — 50 MHz of true clock does not cross a VBIOS band
boundary. But 405 and 810 program genuinely slacker values.

**Re-confirmed on GP102**, with a wider gap: P2 (mem 5508) and P0 (mem 5702) are
bit-identical across all 49 registers.

So the axis that matters is the **band**, not the p-state — an idle capture is
worthless, a P2 capture is not. The comparison view puts each field's cycle
ratio beside the clock ratio; two ratios agreeing is what turns the decode from
plausible into proven.

## Hold, don't induce

Nothing here **forces** a memory p-state, because nothing can on these cards:
`nvmlDeviceSetMemoryLockedClocks` returns `NVML_ERROR_NOT_SUPPORTED` and
`nvidia-smi -lmc` fails identically.

So the tab has two buttons, and only one of them is how you get a reading.

**`Read memory timings (will hold P0)`** — blue, the primary action. It takes
the **V/F point lock** on the highest point at or below the cap, waits for the
memory clock to arrive in the top band, captures, and **leaves the hold on**.
The lock is a voltage request that the clocks follow, so the wait is real; a
capture taken inside that gap would file idle timings under a P0 heading.

Leaving it held is the point: the band is still up afterwards, which is what
makes the second button cheap.

**`Re-read timings`** — a sanity check, not a way to get a reading. On its own,
on an idle card, it returns idle timings, which is the exact error this tab
exists to prevent. Once the blue button has the band held it costs about
**0.2 s** and confirms the reading is stable. It is also how you capture the
*second* state that proves the decode: read held at P0, release with `Ctrl+H`,
let the card idle, re-read.

The **CUDA memcpy load is the fallback**, used when the hold cannot be taken —
controls locked, no readable V/F curve. It captures *while the load runs*, since
a capture after it stops is a capture of the card coming back down, and it
reaches the band only for as long as it lasts. If the card is *already* at P0
the load is skipped entirely: opening a CUDA context on a P0 card pulls it
**down** to P2.

## Writing

**The tab can write, and the write path is isolated in one file.**

- **`timings.py` — read-only by construction.** Whitelists the read-only
  subcommands and rejects `set`, `restore`, `apply`, `daemon`, `--commit` and
  `--force` *before a process is created*. No flag, no disabled button and no
  dead branch in it can change a timing.
- **`timingwrite.py` — the only module that can.** Driven from the **Edit
  timings** panel: an editable `new value` column (green while it matches the
  register, red once it does not — only red rows are written), a `force`
  checkbox, `Revert edits`, and `Restore stock`.

Four guards sit in front of every write:

1. **The card must be in its top memory band.** Timings are per band, so a write
   in any other state edits a band you are not tuning.
2. **Range and structural refusals happen here, before nvtune is consulted.**
   Structural fields (training and phase fragments, with no "looser" direction)
   and fields in a register whose offset is only *inferred* get no input at all.
   The `force` checkbox defeats nvtune's *warning* refusal — not this check.
3. **A dry run always runs first**, so a tool-side refusal is **observed**
   rather than inferred from an unchanged read-back. That inference is exactly
   what recorded four of twenty-five fields as hardware rejections in an earlier
   sweep when they had never reached BAR0.
4. **A per-card stock backup**, keyed by the card's **UUID** — *not* by PCI
   slot, and not by model name. nvtune's own default is `<slot>.stock.json` with
   an existence-only check, so swapping cards in one slot silently skipped the
   backup. Found live: a TU102 backup was occupying the Titan Xp's path. See
   [Which card is "this card"](#which-card-is-this-card) for why the UUID rather
   than the slot, and for the `-d` targeting every one of these calls now
   carries.

Outcomes are reported as four distinct states — **landed**, **dropped** (reached
the hardware and was rejected), **refused** (nvtune declined; BAR0 never
touched), **failed** — because conflating the middle two produces a confident
wrong conclusion.

> **Measured: GP102 accepts these writes; TU102 rejects every one of them at the
> hardware.** Same tool, same driver, same slot. `FAW 24→25` on GP102 applied,
> verified, held and restored clean. On TU102, 25 of 25 non-structural fields
> were dropped, and a kernel ioctl doing `WRITE_REGISTER_ULONG` then
> `READ_REGISTER_ULONG` as adjacent instructions returned the old value 300/300
> — a hardware rejection, not a software revert.

## Locating nvtune

nvtune is a separate program by Sebastian Marrufo — a C++17 CLI plus a
kernel-mode driver — and it is free software:

**<https://github.com/sebastianmarrufo/nvtune>** — GPL-3.0-or-later.

**Druta does not ship it**, and the reason is not a licensing one — both
projects are GPL-3.0-or-later, so redistribution would be permitted. The reason
is what installing its driver costs you.

### What the Timings tab actually costs

If nvtune is not found, the tab offers an **Enable test signing…** button
rather than a wall of text. It shows the exact commands, copiable, says which
one actually does the work, and only then offers a red button that runs them.
That button is **dead unless the machine can take them** — Secure Boot is
checked with `Confirm-SecureBootUEFI` rather than asserted by the user, and
elevation is checked too, both again at the click. BitLocker is reported if it
is on, because a boot-config change can force a recovery-key prompt at next
boot.

The outcome gets its own prompt. On failure it shows which command failed and
what it said, and offers no reboot. On success it asks the question that
matters — **nothing has taken effect yet** — and offers `Reboot now`, which
schedules the restart ten seconds out so `shutdown /a` can still abort it.

Once nvtune loads, the button **retires to `Device → Enable test signing…`**.
It stays reachable — test signing can be turned back off, or need re-applying
after a Windows update — but it is out of the way of a tab that now works.

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

Step 4 is the one to think hardest about. Putting a third-party self-signed
certificate in your Trusted Root store means that certificate can sign anything
your machine will subsequently trust — not just this driver.

That is a deliberate, machine-wide reduction in your security posture, and it
should be a decision you make about *nvtune*, taken from nvtune's author, with
nvtune's own instructions in front of you. It should not arrive as a side
effect of installing a GPU monitor. That is why Druta locates the tool instead
of carrying it, and why the Timings tab is inert until you have done the above
yourself.

If none of that is acceptable on your machine — and it is entirely reasonable
for it not to be — every other tab works without it. Only Timings needs BAR0.

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

---

# Safety

**Reversible, read-only:** all Monitor telemetry, and *reading* on the Timings
tab. Its driver needs admin to *start*, not to read through once running. The
induce load is an ordinary CUDA workload — it makes the card busy for a few
seconds, writes no register, and releases its context in a `finally`.

**Reversible, needs admin, cleared by a reboot:** clock offsets, power limit,
voltage boost, fan, both lock mechanisms, and every V/F curve edit. No delta
survives a reboot.

**Reversible only by restoring:** memory timing writes. They are volatile — the
driver reprograms these registers per clock band — but the recovery path you
should rely on is `Restore stock`, backed by the per-card backup.

**Reversible is not the same as harmless.** A V/F edit asks the card for a clock
at a voltage; if the silicon cannot hold it the machine crashes. `De-flatten` is
an overclock by construction. `Hard de-flatten` is an overclock that *assumes a
hardware modification exists* — on an unmodified card the 800 mV floor is not a
trick played on the power estimator, it is simply 800 mV, and both the flat top
and the shape-law cascade below it will crash the driver.

**Can hang the machine or corrupt VRAM:** memory timing writes. This is the one
control here with that property, which is why it carries four guards and a
per-card backup rather than a warning.

## Deliberately not wired to a button

- **VBIOS flashing** and anything writing `0x001850` / PROM beyond nvtune's own
  restored read.
- **`nvidia-smi -dm 1`** (driver model TCC) — **drops display output** on these
  cards. Never run blind.
- **NvAPI `SetForcePstate` (`0x025BFB10`)** — genuinely absent from the code.
- **FBPA privilege-mask writes.** `0x9A0148` reads `0xFFFFFFCF` on TU102 and is
  fully read-gated (`0xBADF1002`) on GP102 — the architecture where writes
  *work*. Direct host writes to it are dropped, as expected if a PLM protects
  itself. The identification is by analogy with published GA100 work and is
  **unconfirmed**; the Pascal reading makes it less certain, not more.

Memory timing writes **used to be on this list and have come off it.** They are
now wired, gated as described above.

---

# Why Dear PyGui

The original UI was Tk, and dragging the window stalled the whole desktop.
Measured root cause: Tk creates one native child HWND per widget (~50 on the
control page), and on `WM_ENTERSIZEMOVE` Windows' modal window-move loop pumps
messages synchronously — so Tk's entire idle queue and `after()` timers drain
*inside* that loop instead of after it, and the desktop hangs for as long as the
drag lasts.

Dear PyGui (Dear ImGui + DirectX 11) renders the whole UI as GPU geometry inside
a single window: zero child HWNDs, so there is nothing for the modal loop to
stall on.

---

# Scope

Research software, developed against the two cards named at the top. Struct
layouts, NVAPI ids and the empirical laws above were verified against those;
nothing here should be assumed to generalise to a third card without
re-verifying — and the per-card machinery described under
[Probed, not assumed](#probed-not-assumed) exists because assuming is exactly
what went wrong the first three times.

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

**Druta contains no NVIDIA proprietary source code.** None has been obtained,
sought out, or consulted, and no NVIDIA binary has been disassembled in order to
build any feature in this tool. Every fact below is either published by NVIDIA,
published by a third party, or measured here on our own cards.

**The private interface ids are facts about the shipping ABI, not inherited
knowledge.** Every private NVAPI id Druta calls is present in the `nvapi64.dll`
that comes with the driver — in `.text`, and again in the `nvapi_QueryInterface`
dispatch table in `.data` at a regular 16-byte stride. Fifteen of fifteen
checked, all present. Anyone holding the driver they are licensed to run can
enumerate that table for themselves, so these ids are independently derivable
and are treated as such here.

| what | where it came from | status |
|---|---|---|
| That this control interface exists at all; that XBAR is domain index **1**; that one entry carries both a signed-kHz frequency offset and a µV rail offset | Loong0x00, LACT issue [#1147](https://github.com/ilya-zlobintsev/LACT/issues/1147) and the gist linked from it — Linux, R610/Blackwell, probed through NVIDIA's **open-source** kernel module | third-party public research |
| The Windows private NVAPI ids | enumerable from the shipping `nvapi64.dll` (15 of 15 verified here); also published independently by `SHANAjam/rtx5090-xbar-control` (MIT), which derives them the same way | facts about the shipping ABI |
| **The Windows control-block geometry** — header `0x124`, entry stride `0x304`, freq delta `+0x10C`, rail array from `+0x110` | **derived here.** It does not match the published Linux R610 layout (header `0x3c`, stride `0x40`, freq `+0x88`, rail `+0x90`) and self-checks against the driver's own version word: `0x124 + 32 × 0x304 = 24996` | measured here |
| `CTRL_CLK_DOMAIN_XBARCLK = BIT(1)`; boardobj class layout; `CTRL_CLK_CLK_DOMAIN_TYPE_*`; the PMU clock RPC list; VBIOS clocks-table field order | NVIDIA's own published nvgpu source | published by NVIDIA |
| GP102 instantiates no `.clk` subdevice | nouveau (in-tree Linux, MIT) | published |
| Decode of this card's VBIOS clocks / clock-programming / NAFLL tables | this project, from our own cards' ROM images | measured here |
| Every domain↔control pairing, every ratio and unit convention, and all four "does this knob actually move the card" verdicts | this project, on TU102 and GP102 | measured here |

**The upstream lineage, stated plainly.** The two third-party rows are **not
independent of one another.** LACT #1147 was opened by Loong0x00 and links their
own gist from its body, so those are one submission by one author — not two
sources. `SHANAjam/rtx5090-xbar-control` was created six days after that issue,
cites it in its references, and describes one of its own checks as a direct
implementation of Loong0x00's. Count that chain as **one upstream result plus one
downstream re-derivation**, not three corroborating sources. NVIDIA's published
nvgpu is the only genuinely independent corroboration in the table, which is why
it is leaned on so heavily throughout this document.

Both upstream rows rest on material anyone can obtain: Loong0x00 worked against
NVIDIA's open-source kernel module, and SHANAjam's own notes describe
disassembling the `nvapi64.dll` shipped in the driver package. Druta took neither
one's code — only the knowledge that the interface exists, after which the
Windows layout was derived and measured here, and it does not match theirs.

Where a private interface could not be corroborated against something NVIDIA
published, it is either read-only here (the per-domain hard VF lock) or written
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
