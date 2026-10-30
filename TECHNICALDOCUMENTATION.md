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

- `dist\Druta\Druta.exe` — bundled application, no Python needed.
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

Two things that are not obvious and are both tested in `tests/test_swap.py`:

- A capture or an induced load can be several seconds — up to 25 — inside a call
  that started on the *previous* card. Each worker stamps a generation counter on
  entry and drops its result if the card changed underneath it, so a snapshot
  cannot be filed under the wrong card's tab with the wrong memory divisor
  applied to its nanosecond column.
- Themes and handler registries are created *unparented*, so deleting the tabs
  does not reach them. Before that was handled, each switch leaked 129 items —
  and, worse than the memory, a second live handler registry, which made one
  press of `W` nudge the point twice and one `Ctrl+Z` walk back two edits.

`src/druta/app.py` (the old Tk UI) is kept only as a parity reference for the Dear PyGui
port. It has no build target and should not be edited. See
[Why Dear PyGui](#why-dear-pygui).

## Build

```
python -m pip install -r requirements.txt
.\build.ps1
```

Output lands in `dist\Druta\Druta.exe` and the matching ZIP. Distribute the whole bundle, including `_internal` and `source`.

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

### Programmed and measured clocks in the tiles

The three clock tiles' big numbers are the **programmed** clock: the driver's
current figure (public NVAPI, or private array A where that is missing). On one
TITAN RTX, NVML and GPU-Z showed this same figure. Under each one is a
**measured** line: array B of the same private row, and its distance from A,
e.g. `measured 2087  Δ -27.9`. The core line uses the row confirmed as GPC
(GPC2CLK rows are halved to core MHz); XBAR uses the private slot the tile's
number is read from; memory uses the row confirmed as MEM, divided like the tile.

B earns the word "measured" on the card in front of you, from its own readings,
not from a list of cards:

- it must **turn back** on its own twice - move up after moving down, or down
  after up - while A holds still, landing on values A never reported. B's first
  two moves after each change of A are not looked at (it may still be catching
  up: on one TU102, B trailed a change by 1-2 s). A counter keeps jittering
  both ways. A copy of A only moves one way once A holds: an exact copy at any
  lag lands on values A reported, and in simulation, offset, smoothed and
  slew-limited copies up to three reads late earned nothing. What this cannot
  rule out: a copy that is not exact and four or more reads late, or an array
  computed from A that overshoots and rings. No card has shown one: on the
  cards tested, B was identical to A (GK104/GM107), constant while A moved (one
  GP102 row), or a jittering counter (TU102);
- a B that equalled A exactly for five readings through a change of A has not
  shown itself to be independent (GK104/GM107 did this in every tested state):
  `measured: none (B = A)`; with A never having changed yet, `measured: same as A`;
- a B that differs from A but has not turned back on its own is shown as
  `unproven`. It takes A holding still for five readings or more: while GPU
  Boost changes A every four readings or sooner, a real counter stays
  `unproven` until the clock holds (on a steady clock, a simulated TU102-like
  counter earned it in about 9-11 readings). Until 12 readings are in, the
  line says `checking`.

Only the **core** line is colour-judged, and only when a reading is comparable:
at ≥ 90 % GPU load (a chosen threshold, released below 85 %; at idle the clock
gates between bursts and B reads low), after B has refreshed twice since A last
changed, with B still refreshing, and after five such readings with A
unchanged. The colour comes from the median of up to the last 15 of them, in
this card's clock bins with 0.5 MHz of tolerance, either sign (above the
programmed clock counts too): plain within one bin, amber at one bin or more,
red at three or more. When the last five spread over more than one bin (a
transient or an outlier), the line is `varying` and dim.

A colour once reached is **held** until the median falls back past its edge by
the tolerance plus three standard errors of the median. The scatter behind
that comes from the steps between successive readings, so a real change of the
gap does not count as noise. While a colour is held, that release margin only
widens, because a single quiet window would otherwise release it. The tooltip
says when a colour is held and by how much. One TU102 held its ceiling at about
-14.7 MHz, 0.2 MHz from the amber edge. In a simulation at the ±2.2 MHz of
jitter the same card showed at a 1920 MHz lock, a five-reading median changed
colour 70-490 times an hour at true gaps from -13.0 to -14.7. This rule changes
it a few times at most (the worst simulated hour: 7, at -13.0).

The price is history. Once amber, a counter jittering ±2.2 MHz has to come back
to about 2-2.5 MHz inside the edge (-12 to -12.5 MHz) before the line turns
plain. The same steady -13.5 is amber when the card came down from -14.7, and
plain when it started there. A change of A, a load drop below 85 %, or a break
in the readings starts the judgement over.

No colour is claimed when this card's clock step could not be measured. The core
subtitle says why a line is dim (`checking`, `same as A`, `settling`,
`clock moving`, `not refreshing`, `load 3 %`, `vs A 2115`, `read failed`,
`stale`), and the tooltip gives both raw arrays, the Δ in bins and the evidence.
When the private read fails after good ones, each tile keeps its last reading
for up to 3 s (a number is marked `last …`), then reads `measured: n/a`.

XBAR and memory are shown but never judged: XBAR follows the core clock and its
own bin is not known here, and on one TITAN RTX the two memory arrays differed
by a fixed -6.8 MHz (raw) in every state, loaded or idle. The log's clock check
reports what the core tile judged: once, after five judged readings in a row
coloured with B **below** A, and recovery once after five judged plain ones. It
counts each reading once (not each redraw, and never a stale snapshot), so it
shares the tile's trust, load gate and release, pairing, refresh check and
clock bin. It reports GPC rows only, not GPC2CLK.

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

**On TU102, A is the target the driver programmed.** Always exactly on the
clock grid, bit-identical across samples for a fixed domain. **On TU102, B is a
measured counter.** It jitters and never lands on the grid. (GK104/GM107 return
identical A and B, so this is not assumed of any card; the tiles check each card
at runtime.) The tiles' big numbers quote A and the line under them shows B; the
panel shows both for every domain, so you can see when they disagree.

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
    +0x114  MSVDD request delta, signed microvolts
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

### Blackwell adaptation

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

Those field offsets are selected only for the Blackwell generation, then checked
against the live runtime ABI. The version echo and one-hot accepted-domain probe
are read-only gates. The UI uses the accepted control indices for Blackwell and
displays the requested offsets; it does not label those values with a Turing
private-getter domain.  On the validated RTX 5080 / 610.88 path, the tested
controls are 1 = XBAR, 3 = SYSCLK and 4 = VIDEO. Their logical UI signs are
direct on the validated 610.88 path, including XBAR. This was confirmed by
an end-to-end test of the built UI; the earlier raw-probe direction must not
be reused as the UI sign without repeating that test.

For hardware validation, run:

```powershell
python -m druta.nvbackend --clkdom-debug --json > clkdom-debug.json
python -m druta.nvbackend --clkdom-map-probe --confirm > clkdom-map.json
```

When the candidate `+0x114` is accepted but produces no settled movement, use
the frequency-only comparison probe:

```powershell
python -m druta.nvbackend --clkdom-field-probe --confirm > clkdom-fields.json
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
python -m druta.nvbackend --clkdom-control-probe --delta 25 --confirm > clkdom-controls.json
```

This intentionally omits control indices 0 and 2 because they may be GPC and
memory on a different driver branch.  To include those two potentially
high-impact paths on a test-only machine, pass the explicit opt-in:

```powershell
python -m druta.nvbackend --clkdom-control-probe --include-core-memory --delta 25 --confirm > clkdom-controls-all.json
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
reported as inconclusive. It is deliberately not called by the UI. A changed runtime layout must not be treated as validated until both the field
location and the measured control effect are confirmed. No device/VBIOS allowlist
participates in slider eligibility.

The voltage fields are a **rail array**, not one value — probing every dword
from `+0x100` to `+0x140` found exactly three consecutive refused slots on every
domain, which is what a rail array whose upper members this silicon lacks looks
like. Aligned against the frequency field, rail 0 is at `+0x110`:

- **Rail 0 is NVVDD** and it works. `+50 mV` requested moves vcore exactly
  `+50 mV`, measured with the core clock pinned at 1500 MHz. Shipped as
  *NVVDD offset (mV)*.
- **The tested MSVDD request was ineffective on this board.** It was refused
  on the measured active domains and accepted on domain 6 without establishing
  a voltage response. This experiment does not suppress other boards' controls.
  The current UI exposes readable MSVDD request fields in understood layouts
  as an XOC experiment. It validates stored-field readback, explicitly reports
  physical voltage response as unverified, and provides Zero after leaving XOC.

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

With **Keep headroom above a held point** on (the default), the hold in step 4
also raises reliability, alt-reliability and overvoltage to the held point plus
25 mV for as long as it lasts, so the cap point does not sit on the voltage
ceiling, where it would run below the clock it shows. That raise does not need
the Rail limits box; it is written only upward, to exactly hold + margin, and
restored when the hold ends (`GPU.apply_hold_headroom`).

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


On Pascal, Turing, Ampere and Blackwell, the Control tab exposes current
policies only after the generation descriptor agrees with the live masks,
record types, channels and mA unit. Pascal, Turing and Ampere expose the
validated core current (on Ampere, the info, status and control buffer sizes
are chosen by which info GET the adapter accepts); Blackwell
also exposes its other rail. Blackwell normal caps are 500 A / 200 A and XOC
follows the API maxima. Limits participate in profiles and Reset all. See
[current-limit ABI and validation](CURRENT-LIMITS-RTX5080.md).

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

## I2C discovery and contribution interfaces

`railctl.discover()` returns all controller candidates on the selected GPU.
An optional `controller` name limits discovery to that family; optional
`routes` restrict it to exact `(port, addr7)` pairs within the family's
supported routes. `controller_names()` includes built-in controllers and
loaded TOML recipe regulator names. The GUI starts discovery only on an
explicit scan-button click. Its UUID-bound route cache stores the selected
controller and recipe identity, never settings or write verification. Reconnect
performs fresh identity and settings reads through the same scoped scanner,
falling back to the selected scope if the hint cannot be verified.

NCP4206 uses controller-model discovery and an absolute-VID adapter; MP2888A
checks a repeated register fingerprint; MP29816 checks its source-backed model
ID and binds its already-selected PAGE/scaling. All scan ports and unicast
addresses independently of GPU generation or board IDs, and identify the
controller output without assuming its physical rail. These scanners do not use board-ID gates;
other generic TOML recipes retain optional PCI matching and fixed bus settings.

Candidate selection and Verify are separate steps. Verify makes bounded writes
at the recipe's required operating point, measures response and checks
restoration. The original MP2888A offset recipe uses its existing loaded Verify;
manual PMBus Verify uses an owned, observed minimum graphics-clock clamp.
Failed restoration cannot authorize Apply. Verification is bound to the
current GPU/controller/recipe;
rescans and selection changes invalidate it. Tuning profiles also capture
per-rail limits and controller state, including NCP target/Auto or MP offset,
with the exact connection and recipe fingerprint. An exact unique saved identity
can resolve multiple candidates but does not replace fresh verification.
MP2888A manual PMBus requests are session-only and are omitted from profiles;
profile loading requires returning that method to Auto first. Existing offset
profiles retain their original identity and register-23h replay path.

See [CONTRIBUTING.md](i2c/CONTRIBUTING.md) for evidence requirements and
[PROFILES.md](i2c/PROFILES.md) for recipe fields, adapter responsibilities and
hardware-free checks. Discovery, decode and write-path validation are separate
claims; a successful read does not establish an effective or restorable write.

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

1. **Controls must be unlocked and the current card must be in its top memory
   band.** Apply checks fresh P0/P2 and offset-normalized memory-clock readings
   before preparation and again immediately before commit. Missing state, clock
   or offset refuses the write. Both reads bracketing a capture must qualify.
   Only the measured GP102/TU102 nearby P2/P0 clock pairs share a band; other
   chips use their highest enumerated clock. The GTX 745's [405, 900] clock list
   therefore has a 900 MHz floor, with the same 5 MHz quantization allowance used
   for matching reported clocks to enumerated states.
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

A nonzero helper exit, timeout, missing pre-write value or missing post-write
readback is **failed**, with the diagnostic retained. A partial commit can have
actual readback values and still be failed. Missing readback is never proof of a
hardware rejection. `force` only overrides nvtune's warning refusal; it does not
override the current-state or Unlock checks.

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

## Registry override transactions

`registry_overrides.py` binds a paired NVAPI/NVML adapter to SetupAPI's PCI
instance and its current `Enum\...\Driver` class-key mapping. A per-adapter
Windows mutex serializes staging, restart and verification. A durable receipt
and discovery pointer precede mutation; `registry_recovery.py` provides
case-insensitive three-way undo of transaction-owned values, including partial
writes. Original types and missing values are retained.

`romOverrideXX` records use a big-endian four-byte VBIOS selector, one-byte OEM
selector, little-endian full-image offset, byte length, and payload. Records
are contiguous; payloads are limited to 246 bytes by the traced 256-byte driver
buffer. `vbios_fields.py` resolves actual image ownership and table pointers,
preserves all unselected bytes, and rejects unknown geometry. Existing record
payloads must reconcile with the running image before another transaction can
be composed. New transactions preserve `EnableGpuFirmware` exactly, including
absence and value type. A 450 -> 451 -> 450 W maximum trial succeeded with that
value absent throughout; the production path then verified three 999 W maxima
and undo with it still absent. Old prepared receipts retain their saved explicit
firmware plan for compatibility. NVIDIA's shared firmware-registry header defines
0x11 as enable-with-fallback; it is not a power-limit value. The NVML GSP-mode
getter returned unsupported in all trial phases, so these results do not identify
the active GSP mode.

The helper restarts only the selected device through PnP. A separate child
process reads the new image to avoid retained NVAPI/NVML state. Power policy
contracts additionally bind the selected table index, units, channel and all
three initial bounds to driver metadata, then verify all resulting bounds and
unchanged understood peer policies. `power_runtime.py` also handles the traced
GB20x class-17/18 reconstruction: effective maximum follows the rated/default
limit. This requires the actual architectural implementation, recognized V4
layout and exact baseline relationship. An isolated BIOS maximum edit is rejected
with an explanation; the preview shows the coupled active maximum. Old receipts
retain their original selected-field restoration contract.
`bios_runtime.py` binds decoded GPC/DRAM offset ranges to public NVAPI P0 clock
domains and kHz units. It checks both bounds before mutation, after activation
and during undo. Other clock domains retain image-level verification only.
The operating-temperature contract uses the public NVML absolute Celsius
threshold getter, with independent capability checks and bounded retries for
each threshold. It requires an exact BIOS/runtime baseline match, whole Celsius
requests, and unchanged supported slowdown/shutdown thresholds. Relative T.Limit
values are not substituted for absolute temperatures.
Decoded BIOS fields without a separate runtime semantic readback remain marked
experimental. An exact image match proves activation, not hardware behavior.

`vfe_inputs.py` uses read-only VFE getters with explicit native packet geometry,
typed fuse identities and bounded transient retries. Direct Speedo registry
substitution is verified through active/override fields while raw/default
values remain unchanged. IDDQ is displayed as a raw model integer; its physical
units and internal firmware implementation remain unresolved. Named-only transactions do not
require a BIOS parser or image getter. No mvolt implementation is used.

On the tested RTX 5080 / 621.01 configuration, three power-policy maxima accepted
999000 mW with exact restoration, and Speedo accepted 2187 -> 2188 -> 2187.
`EnableCoreVoltage=0` left the active flag at 1 after restart; it is not exposed
as a working registry control. Static follow-up identifies a conditional Windows
initialization path that could overwrite a registry-derived zero with 1, but
does not establish that the tested GPU took that path. Both A030 and A031 have
a route-to-physical flag; their actual route and the loaded firmware are not
established. A candidate firmware image has a same-byte initializer and
getter/setter, but its input provenance and lifecycle order remain unresolved.
One read-only GSP-features query returned success with `bValid=0` and an
unchanged zero-filled response. Its feature mask is invalid, so this does not
establish the current offload mode or command route. The pinned NVIDIA source
also includes a kernel implementation that returns success with `bValid=0`.
The [transport evidence](experiments/driver-transport-followup-20261028.json)
records the prior negative and the static boundaries. These observations
describe the tested configuration.

Core/memory offset ceilings also accepted 1000 -> 1001 MHz and 3000 -> 3001 MHz.
Public NVAPI returned 1001000/3001000 kHz; NVML returned 1001/6002 in its core and
doubled-memory units. Applied offsets stayed zero and exact initial metadata was
restored. This establishes range control on that configuration, not stability
at an offset within the expanded range.

The operating-temperature limit accepted 88 -> 87 -> 88 C through the production
transaction. NVML GPU-max readback followed it, while slowdown/shutdown remained
90/93 C and the memory threshold was unsupported. Registry values, image and
all supported thresholds were restored exactly; no workload was started.

`perf_diagnostics.py` independently reads PERF limit, continuous-feedback
controller and policy INFO/CONTROL groups through a captured paired RM transport.
GET command/size pairs, masks, row types and adapter identity are validated;
transient failures have bounded retries and prior snapshots remain explicitly
historical. Registered named slots are not an active-throttle report. The tested
configuration exposes 244 limit slots, nine feedback controllers and five
feedback policies. Most registry flag and policy semantics remain under
investigation; the diagnostics reader itself has no setters.

`registry_flags.py` supports two traced fields of `RmPerfLimitsOverride`. The cache
field uses
bits 3:2 encode inherit=0, disabled=1 and enabled=2; encoding 3 is reserved.
This field reaches PERF CONTROL byte 0x4D and controls reuse of cached limit
calculations. The production false transaction changed its active flag from
1 to 0 after GPU restart, and restoring the original absent value returned it
to 1. Other flags, the BIOS image and the 360000 mW request stayed unchanged.
Explicit true also passed a nested false -> true -> false -> original-absence
transaction using DWORD values 4 and 8. Both active states and both undo steps
were verified. No performance improvement or power-enforcement bypass is
claimed. Transactions reconcile existing named
bits, preserve unrelated state, and verify stable paired identity, geometry,
inventory, row types and peer flags. Undecoded pre-existing bits must be
reconciled before a transaction can restart the GPU.

Bits 1:0 use the same inherit/false/true/reserved encoding for skipping host
performance-limit updates. The driver getter returns the consumed object flag
at PERF CONTROL byte 0x4C. Static host consumers bypass both immediate and queued
processing before the firmware-facing update request. A production idle trial
using DWORD 2 verified false -> true -> false, exact registry and BIOS restoration,
unchanged peer flags and a 360 W board request. Firmware enforcement effects are
unverified. Combined cache/skip transactions reconcile both saved peer baselines
and verify their independent expected states. Legacy cache-only receipts remain
compatible. Bits 5:4 have a traced deferral/coalescing consumer but no independent
getter; bits 7:6 have a getter but no proven consumer semantics. Neither is writable.

`power_arbitration.py` decodes the independently traced modern policy STATUS
prefix. It separates CONTROL request, host arbitration, returned PMU arbitration,
effective limit and signed serialization adjustment. Each bounded vote vector has
a method, output and numeric client/value pairs; unknown clients are not assigned
policy or physical names. Other packet geometries remain undecoded without
removing existing power controls. `GPU.read_power_arbitration()` checks paired
identity before and after fresh GETs and retries transient failures once.
The independent UI read preserves previous snapshots as explicitly historical.
All 18 policies decoded on the tested adapter. Policy 7 requested 450 W while
the firmware minimum vote was 360 W; policy 9 requested 45 A while its firmware
minimum vote was 36.002 A. These are idle observations, not workload enforcement
measurements. Malformed vectors remain unavailable independently of readable peers.

A separate 360 -> 361 -> 360 W request trial changed only policy 8's CONTROL
request. Its firmware client-8 votes on policies 2 and 7 followed the change,
matching independently decoded relationship edges on this BIOS image. All 18
INFO rows and all other CONTROL fields remained exact. Policy 7's effective
limit followed the vote; policy 2's settled effective field was separately
derived (370811 -> 371841 -> 370811 mW). Downstream current-policy values also
changed, so raising the board request does not imply that finer limits remain
independent or that every effective value copies the board request.

Immediate post-write reads sometimes contained the new local request and stale
downstream votes. Three consecutive matching reads confirmed propagation and
undo. These sequential captures bound observed transitions, not firmware
execution latency or a universal settling time. No workload was started; this
establishes request propagation at the tested idle operating point, not physical
enforcement. The UI therefore identifies reads as sequential and keeps client
labels numeric. Raw packets and a reproducible offline analysis are retained in
`experiments/registry-overrides/vbios-fields/arbitration-propagation-analysis.json`.

`linked_current_runtime.py` verifies the BIOS kind13/runtime32 linked-current
configuration independently of its source power request. The WORD at BIOS +33
is a relationship index, resolved through an understood kind1 relationship to
its target policy. It is not universally the target policy index. Source INFO
+0x34 exposes that relationship reference; the target policy must independently
expose the expected runtime30 class and mA units.

The UInt32 at BIOS +61 appears in source CONTROL +0x28. An independent firmware
STATUS float at +0x148 followed the same value in isolated trials. Host and GSP
serializers forward the configured field; their ordinary SET callbacks ignore
these extension words, so the control uses the registry image and GPU restart.
The original firmware member name and numerical enforcement algorithm remain
unresolved. Selected loaded cache and reported value must match exactly, while
full INFO and every other CONTROL byte are preserved. Dynamic status peers are
observations, not configuration that can be compared byte-for-byte on Undo.

On the tested 5080, 45 -> 44 -> 45 A changed exactly the selected cached DWORD
and firmware-reported value. The target's client7 vote also followed the change;
its effective limit stayed at 36.002 A because another vote was lower. The first
research comparator incorrectly required every CONTROL word to stay unchanged;
it rejected the expected cache change and restored the full baseline. The
corrected exact-projection repeat passed and restored the full baseline again.
Neither trial started a workload or measured electrical enforcement. Existing
image-only linked-current receipts remain restore-only; new Apply requires the
versioned runtime contract.

The read-only modern power-equation getter also links DTCS 1.3 equation 0 to
the NVVDD IDDQ VFE input and equation 1 to MSVDD IDDQ on this configuration.
Active values are 26000 and 10300, versus raw values 520 and 206, with a Q12
hardware scale of 50. Subsequent selected-adapter DWORD trials established
direct effective substitution on this configuration: NVVDD 26000 -> 26001 and
MSVDD 10300 -> 10301, with exact cached override and evaluated active readback.
Writing the current active integer first also preserved that active value.
Thus these registry integers are not additionally multiplied by the reported
hardware scale in the observed path. Staging alone left the model unchanged;
activation required the selected-GPU restart. Full VFE INFO outside the selected
active/override fields, complete VFE CONTROL, voltage/power policies, clocks,
VF tables, BIOS and peer registry values were preserved, with exact undo.
Physical IDDQ units and the internal evaluator implementation remain unresolved.

`model_registry.py` binds direct IDDQ transactions to evaluated values, cached
overrides, typed fuse metadata, the complete variable inventory, CONTROL hash
and a static INFO fingerprint. Only understood mutable input fields are masked
from that fingerprint; unknown INFO bytes remain covered. Known active/raw
scalar mirrors must agree with their typed unions. Receipts validate before
apply or recovery; effective readback and preserved peers must match exactly
after restart and undo. Existing Speedo receipts remain recoverable.

`fan_runtime.py` reads Fan2X INFO (0x2080852E/0x598 bytes) and CONTROL
(0x20808530/0x3D4 bytes), using two stable paired observations. The BIOS cooler
header supplies the client-ID-to-row map; indices are never inferred from fan
IDs. Typed CONTROL rows expose duty bounds as Q16 fractions and RPM bounds as
integers. Contracts compare all understood peer bounds, inventory, role map and
adapter identity before activation, after restart and during undo. Unknown peer
types stay diagnostic. Dynamic requests and telemetry are not frozen by this
metadata comparison.

On the tested adapter, production trials changed cooler row 0's duty minimum
30 -> 31 -> 30 percent (Q16 19661 -> 20316 -> 19661) and RPM minimum
500 -> 499 -> 500. Both selected readbacks and exact restoration passed. A
500 -> 501 RPM preview was rejected before writing because an existing curve
point would lie below the proposed minimum. Public NVAPI/NVML still reported
30-100 percent; this adapter's selector 2 normalizes RPM bounds into that range.
These trials establish stored bounds, not measured fan response under load.

The same Fan2X INFO getter exposes loaded PWM Hz at row+0x18 for types 2 and 3.
The BIOS parser copies the unsigned 24-bit cooler value without scaling; the
constructor and getter preserve it as a 32-bit Hz value. New contracts retain
the actual client-role mapping, raw routing identity, all understood peer
frequencies and duty/RPM bounds. Missing PWM data leaves valid bound controls
usable, and malformed row bounds do not remove a separately understood PWM
field. The transport still requires both paired INFO and CONTROL GETs. Legacy
bound receipts remain valid without the new optional PWM metadata.

The production transaction changed row 0 from 25000 -> 25001 -> 25000 Hz.
During activation the complete INFO packet changed only at the selected
frequency DWORD; the complete CONTROL packet and row 1's 25000 Hz remained
unchanged. Both packets, the registry and BIOS image restored exactly, and the
board request remained 360 W. Initialization converts Hz into an integer
hardware period, so this proves loaded configuration rather than a measured
one-Hz waveform change. Evidence is retained in
`experiments/registry-overrides/production-probe-1792813474579849500.json`.

The separate `RmFan2XOverride` selector is consumed on the tested 5080. Writing
DWORD 1 and restarting the GPU changed the previously populated modern Fan2X
INFO/CONTROL observations into empty results; public NVAPI/NVML fan-control
readbacks also became unavailable. Exact absence restoration and a second GPU
restart restored both modern fan rows and automatic-control metadata. Board
request and BIOS image were unchanged. The available readbacks do not establish
physical fan movement during the disabled interval. This selector is not exposed
as a production toggle.

The all-state editability experiment wrote `RMEnableOverclockingAllPstates=1`.
Staging alone changed no tables; after GPU restart, P1/P3/P5/P8 exposed core and
memory offset editability. P0 and video-clock editability were unchanged. The
extended Pstates20 getter retained the same underlying offset values, ranges,
clock metadata, voltage rows and domain map. The public getter had previously
hidden the noneditable delta triples as zero; their appearance is not an offset
change. Exact registry absence and full table restoration passed. This proves
capability exposure on the tested configuration, not successful offset writes
in every exposed state.

`all_pstates_registry.py` integrates that override with exact public/native/map
packet contracts. The production apply/undo transaction repeated the successful
activation and restored the full original observation. Nonzero DWORDs force the
gate true; zero and absence leave the initialization default. Disabling removes
an existing nonzero entry and verifies disabled capability, rolling back if the
driver default stays enabled. A naturally enabled default with no effective
override is not presented as a proven disable path. BIOS clock-range changes
require a separate transaction because the editability contract preserves their
underlying values exactly. No per-state clock/voltage SET is part of this module.

A separate sparse public Pstates20 SET requested +1000 kHz on the newly editable
P1 core domain. NVAPI returned success, but the requested delta remained zero;
the complete public/native/domain-map and VF tables were unchanged. Restoring
the request and removing the override restored the original tables and registry.
The immediate final power GET was unavailable; three fresh reads subsequently
confirmed the original 360000 mW, with the complete original snapshot exact.
The journal preserves both the failed immediate observation and this recovery.
This is a successful no-op on the tested configuration, so advertised editability
alone is not used to enable per-state offset writers.

`voltage_request_gate.py` uses traced RM commands 0x2080A030 (four-byte GET) and
0x2080A031 (eight-byte SET). SET returns the prior Boolean in word 0 and takes
the new Boolean in word 1. It controls host voltage-request admission; it does
not set a voltage or change a ceiling. Two fresh GETs, a sentinel overwrite,
validated transport-header geometry and sticky paired identity are required.
The NVDA envelope has two traced output words: word 5 receives a Boolean caller
context flag and word 11 must report escape success 1. These outputs are cleared
for each call; the other header words must remain exact, and the independent RM
status must be zero. A SET
is never retried as the requested change. Failed verification attempts restore
the returned atomic previous state, or the captured baseline when a valid
previous-state response is unavailable. Target changes prohibit compensating
writes. On the tested 5080, true -> false -> true and an identity write passed
without registry changes or driver restart; no voltage request was sent and
the 360 W board request and BIOS image remained unchanged.

The class-18 current-policy experiment changed only policy 13's BIOS default
from 300000 to 301000 mA, leaving its BIOS maximum at 5001000 mA. Runtime default,
maximum, requested and active limits all moved from 300000 to 301000 mA. The
original registry, running image and complete policy INFO/CONTROL blocks were
restored exactly. This is an idle metadata experiment, not a measured 301 A
workload. The reconstruction is now independently traced immediately after
successful BIOS parsing in host RM and GSP implementations. The production
transaction repeated the same 300 -> 301 -> 300 A sequence, verified the coupled
maximum and exact undo, and left the board request at 360 W. Runtime classes 17
and 18 alone do not identify input versus output power; both can reference the
same channel and units. Names require separate channel/topology evidence.

Further GSP factory/assertion tracing identifies class 17 as workload physical
single 2x and class 18 as logical single 2x, with the latter referencing a range
of physical-single relationships. This describes their model roles, not rail
placement. Configured directed relationships correlate with several numeric PMU
votes in the saved capture, but the actual PMU vote producer remains unresolved;
the arbitration UI therefore retains numeric client labels.

The selected GSP class17/class18 STATUS implementations call the common policy
serializer and an interface-owner identity check. They do not populate the
additional workload-result fields. Saved zero extension bytes therefore do not
prove zero modeled workload, zero leakage or an inactive estimator. Class17's
clock selector travels unchanged from its BIOS parameter through object+0x38A,
INFO+0x38 and PMU+0xEC. No traced consumer connects it to the separate clock
inventory ordinal namespace; the UI retains the numeric selector.

Additional named registry paths have been traced in the installed driver:

| Registry value | Traced role | Current evidence |
| --- | --- | --- |
| `RMHwSpeedoOverride`, `RMHwSpeedo1Override`, `RMHwSpeedo2Override` | Substitute selected silicon-model inputs | Primary Speedo active readback and exact undo verified; peers require their own live input identity |
| `RmPmgrSpeedoCoefficient`, `RmPmgrSpeedoOffset` | Q12 scaling and signed offset in the legacy VDT/CVB voltage-model path | Legacy VDT getter returned unsupported twice on the tested 5080; no working coefficient/offset control claimed |
| `RmPmgrIddqNvvddOverride`, `RmPmgrIddqMsvddOverride` | Substitute leakage-model inputs | Constructor traced; firmware correction order and physical units unresolved |
| `RMEnableOverclockingAllPstates` | Expose non-P0 overclocking editability | DWORD 1 activation and exact undo verified; offset setters remain separately qualified |
| `RmFan2XOverride` | Select a fan initialization path with capability checks | DWORD 1 removed modern/public fan-control readbacks; exact undo verified; physical fan behavior unresolved |
| `RmPerfLimitsOverride` | Override four performance-limit processing flags | Cache bits 3:2 and host-update skip bits 1:0 have active readback and exact undo; other fields remain research candidates |
| `RmPerfCfPolicyOverrides`, `RmPerfCfControllersOverrides` | Continuous-feedback policy/controller initialization | Selected policy indices and some raw flags readable; semantic roles under investigation |
| `RMClkVfOverride` | Clock/VF initialization flags with more than one consumer | Monotonicity and smoothing flags independently activated and restored; remaining fields unresolved |
| `RmVoltThresholdCtrlCtrl` | Voltage-threshold controller initialization bits | Bit fields, not a numeric voltage ceiling |
| `EnableCoreVoltage` | Initialize a flag gating voltage requests | Registry zero did not change the tested flag; conditional initialization and command routing remain unresolved. Separate live A030/A031 GET/SET verified both states and undo |

These names are research results, not a recommendation to write arbitrary
DWORDs. Their consumers, activation rules and supported GPU paths differ.

The threshold-controller investigation now binds two selectors to bits 12/13
of the modern PMU initialization payload: 0xA8 bytes, flag DWORD at offset
0xA0. An independent architecture GET reports RISCV_EB on the tested adapter.
Host RM and GSP encode the same selectors, but matching firmware meanings and
authoritative active-flag readback remain unresolved. Public nvgpu bootarg
layouts differ from the installed payload and cannot supply those names by
offset analogy. No threshold-control mutation is exposed. A further offline
object-identity audit excludes a similarly located field in a different GSP
class, resolves an eight-byte registry output-pointer base bias, and finds no
direct exports on the identified PMU class. These exclusions supply neither
named bit semantics nor an active readback; a matching readable PMU consumer or
typed loaded-initialization diagnostic remains necessary.

For example, `RmPerfCfControllersOverrides` has subtype-specific fields whose
two-bit encodings are opposite those of `RmPerfLimitsOverride`. The relevant
controller type 5 is absent from the current adapter's returned inventory.
The policy STATUS getter (0x2080A0CD, 400 bytes) also returned five fresh rows
matching the current INFO/CONTROL inventory, but its numeric status fields have
not been assigned physical meanings. Agreement with host flags alone does not
establish which policies currently limit a workload.

The first two `RMClkVfOverride` fields map to clock-domain members serialized in
the same order and with the same defaults as NVIDIA's
[monotonicity and smoothing fields](https://github.com/OE4T/linux-nvgpu/blob/patches-rel-36/drivers/gpu/nvgpu/common/pmu/clk/ucode_clk_inf.h#L282-L297).
This naming is a correlation with NVIDIA-authored source, not a public Windows
registry specification. The independently traced CONTROL getter is
0x2080901B with 0x83C bytes; an empty selection mask reads the group header
without per-domain entries. Two paired baseline reads returned both flags true,
with intact buffer guards and exact envelope validation. This establishes the
active host configuration; firmware enforcement is a separate question.

Two isolated registry trials verified the individual selectors: DWORD 3 disabled
monotonicity, and DWORD 12 disabled smoothing. Neither staging operation changed
the live flags; each target changed after restarting the selected GPU. Exact
registry absence restoration and another GPU restart restored both flags true.
Full public/extended P-state and VF/boost-table payloads, BIOS image and 360 W
request were preserved in each trial. The driver also derives an internal mode
flag from the two checks, so disabling either is not a claim that every internal
field stays unchanged. No curve, clock or voltage setter was used, and no
firmware algorithm or workload effect was measured.

The production transaction then disabled both checks together (DWORD 15) and
restored their original absence and active true states. Both apply and undo
verified the selected flags, paired adapter identity and every preserved header
field. Independent full P-state/VF captures, BIOS image and registry snapshots
matched the baseline after undo; the board request remained 360 W. Receipts
bind each selected Boolean to its complete captured contract before either
apply or recovery can modify the registry. This structural recovery check does
not require a working GPU getter before restoring the registry.

The separate live CONTROL SET is **0x2080D01C**, also 0x83C bytes.
The adjacent 0x2080901C command is STATUS, not SET. `clock_vf_runtime.py`
captures two stable empty-mask headers and changes only selected bytes A/B.
The remaining header, including the opaque 40-byte block, is preserved; the
explicit zero domain mask skips all per-domain setters. A configured PMU update
still serializes the group flags, which establishes packet propagation rather
than firmware execution. Any dispatched failure or mismatched readback gets one
exact-original compensating SET and a fresh verification; SET is not retried
through a different transport.

The live setter does not recompute the startup-derived group member +181.
When the PMU capability at +4F4 is enabled and that mode is false, requesting
either check false returns RM56 before the header writes. An unchanged SET
succeeded on the original RTX 5080 configuration while monotonicity=false was
rejected with that status. After the existing registry monotonicity=false
setting entered its derived startup mode, separate live monotonicity=true and
smoothing=false trials and their exact undos succeeded without further driver
restarts. Both public and extended P-state tables and complete VF/boost payloads
were preserved. The full user-clock GET mirrors the same host flags: V2
0x261A4 maps native A/B to public bytes E/F; these are aliases rather than an
independent firmware observation. Registry Undo and another GPU restart restored
the complete original configuration. No workload or Windows reboot occurred.
Support is established from current paired identity, packet layout and actual
responses; the tested board and driver are not compatibility allowlists.

`voltage_device_runtime.py` adds a separate live voltage-device switch-delay
request. Native INFO **0x2080B205** uses 1800 bytes (8-byte header, 32 rows of
56 bytes); CONTROL **0x2080B207** and SET **0x2080F208** use 392 bytes
(8-byte header, 32 rows of 12 bytes). The PWM type is 3. CONTROL row+4 is the
`switchDelayus` uint32, copied to host device+5C and PMU row+8 without conversion.
This is distinct from the registry power-up/down WORDs at device+880/+882,
serialized at PMU row+18/+1A. Those separate words still have no qualified active
readback or scheduler consumer in this investigation.

Two stable INFO/CONTROL captures establish each selected ordinal, type,
voltage domain and operation. SET and its compensating restore use a singleton
mask; full-inventory GET then verifies all INFO and the exact CONTROL projection,
including unknown bytes and peer requests. No SET retry or transport fallback
occurs after dispatch. Unknown or malformed peer devices remain diagnostic;
the selected target must have its own understood binding. Positive whole
microseconds use the uint32 request format, with no board-specific delay value
or driver-version gate.

On the tested RTX 5080, the four returned PWM devices had LOGIC/MSVDD domains,
each with DEFAULT and IPC_VMIN operations. All four independently accepted
128→129→128 us with exact full INFO/CONTROL and broader configuration
restoration, without a registry write, driver restart or workload. Current rail
registration puts the DEFAULT selector in rail+2C and its normal device mask;
IPC_VMIN uses the separate selector+2D. This distinguishes their roles and does
not establish that both operations execute in every firmware mode. PMU
serialization/submission is traced, but the actual timer/wait consumer and
physical settling latency remain unproved.

A later trace identifies the legacy PWM timing consumer. Windows PWM class3
binds device+8A0 to C40680; after changed PWM programming succeeds, C4081D
multiplies device+5C by1000 and calls the OBJTMR delay method. Unchanged duty
and period skip both programming and this wait. The corresponding GSP class3
factory binds 1680A18, which performs the same multiplication and calls the
PTIMER-style timer at16F4ED8. This corroborates a real microsecond-to-nanosecond
consumer, not a timing measurement. The identified Windows caller5009A0 and
GSP caller16848A8 reject VOLT+16A!=0, whereas the working modern B207 getter
requires that member nonzero. These opposite gates prevent attributing the
legacy consumer to ordinary voltage switches on the tested modern path.
Its actual PMU consumer, composition with policy delay, and separate power-up
and power-down WORD consumers/readbacks remain unresolved.

`lpwr_runtime.py` adds a configuration contract for disabling captured enabled
DIFR entries. Native **0x2080A0A7** is the callable loaded P-state INFO getter:
17676 bytes, a 12-byte header and 32 rows of 552 bytes. The output inventory mask
is at +4 and version at +8; no caller subset is consumed. Common row fields are
P-state-ID mask +4, flags +8, dense level +C, LPWR selector +D and duplicate type
+E. The serializer at 94C700 has a different selector-at-+4 PMU layout and is not
this INFO record. A successful RM status with version zero is capability
absence, not an initialized inventory.

The contract joins the actual loaded selectors to the bounded v10 LPWR index
table at BIT P+74, then its row byte10 to the DIFR table at BIT P+E8. The supported
owned layouts have header/row sizes 6/18 and 11/18 respectively. Invalid selectors
choose runtime fallback slot5. The traced fresh host construction leaves that
slot's DIFR flags zero: allocation, constructors, separate table loaders and both
HAL alternatives have been checked. A consumed index below6 that lacks a loaded
table row is not guessed. The aggregation model requires the understood
populated PERF v70 path; the producer of that format's raw P-state selector is
not inferred from a matching ROM offset.

For an enabled DIFR record, subfeature bits1/2/3 govern the configured support
of DFPR9, MS DIFR SW-ASR10 and MS DIFR CG11. Disabling the record skips its
construction and clears its contribution. Native **0x2080A801** type3 attribute39
reads support; attribute36 reads configured P-state-ID masks only where support
is available. A false support value is itself a valid readback. Its gated state
mask is unavailable, whereas a supported zero mask is a distinct valid value.
The model accounts for all currently loaded routes and enabled records rather
than assuming a particular entry index or P-state ID.

The root research trial disabled the sole enabled DIFR0 on the current RTX5080
by changing BF→BE at its flags byte. The loaded INFO map independently showed
P8→LPWR0→DIFR0; other active states routed to fallback5. After a GPU-only restart,
all three controllers reported support false and their masks were unavailable.
The complete INFO payload, remaining ROM bytes and independent clock, power,
voltage and fan configuration matched the projected state. Registry/image Undo
and another GPU restart restored the full baseline. Registry staging alone did
not alter the loaded readbacks. This establishes configuration activation;
firmware execution, residency and measured power savings remain unproved.

The same single-bit trial also passed through production `prepare`/`execute`
and durable-receipt Undo. Both transactions reported the selected field as
runtime verified. Independent complete INFO and attribute captures, plus the
full image/registry/clock/power/voltage/fan baseline, matched after activation
and restoration. The production diagnostic callback was exercised against
real driver GETs before, during and after the trial; its rendered text
distinguished unsupported controllers from a supported zero state mask.

In the captured image, factory-disabled DIFR rows 1-4 have raw flags 0xFFFFF800, zero
subfeature bits and no loaded P-state routes; enabling bit 0 alone leaves the
understood controller 9/10/11 support and mask projection unchanged. The enabled-row
loader also copies six 16-bit parameters: routed internal consumers return the
captured 0xFFFF values as 65535, and one caller multiplies a returned value by 1000.
Their units and valid ranges remain unproved; 0xFFFF is not identified as a sentinel
or as invalid. No direct native getter of an arbitrary loaded row has been
established, and no new factory-disabled enable or compound routing control is
qualified.

A separate static path for RmForceEnableDIFR can, subject to controller presence and
other conditions, call the controller-9 enable helper with reason 6. The public
0x20802801 DIFR control uses reason 0 and separate counter/bit handling. Attribute
0x37 is a conjunction involving controller presence, record fields and a callback,
rather than a direct reason-mask read; attributes 0x3A/0x3B serialize scaled callback
values, not raw reason bits. Actual same-owner binding, the subsequent owner+0x270
callback, selected callback/runtime path and physical effect are unproved. These
traces do not add a control or establish that the disabled BIOS entries became active.
See the [transport evidence](experiments/driver-transport-followup-20261028.json).

New DIFR previews carry a versioned structural contract bound to their image
and paired adapter. Apply, post-restart verification, Undo and automatic rollback
check the selected configuration. Contract validation before registry cleanup
does not require a working GPU getter; historical receipts remain recoverable.
The new reads are scoped to DIFR operations and the optional diagnostic view,
so an unsupported LPWR getter does not gate unrelated registry controls.
Raw INFO bytes are retained diagnostically, while the activation contract uses
the loaded routing fields and controller states; it does not turn evaluated
clock data into universal frequency-equality requirements.

The adjacent `grpg_runtime.py` contract covers disable-only GR PG entries. Its
original schema retains shared capability contributions; scoped schemas2/3
qualify removal of the final raw-bit3 or raw-bit1 contributor respectively,
schema4 verifies their combined removal, and schema5 covers removal of all
enabled main entries in the development continuation.
The owned layouts are BIT P+74
index v10/header6/stride18/owner24 and P+80 GR PG v10/header10/stride5/owner39,
each with1..5 rows. Actual A7 selectors route through index-row byte1:
7A9230 reads LPWR+2FD+index*18h;770A19 reads the selected GR PG enable byte at
LPWR+39C+entry*0Ch. Configured controller0 P-state IDs are read through A801
attribute36, separately from attribute39 support.

The host HAL at AE8C90 can populate all six GR PG slots, including fallback5,
on the constructor branches that install it. The traced generated bindings
select that callback for ChipHal indices3D/3E, whereas the current GB203 mapping
is42; the overwrite is not a universal active path. These are static evidence,
not runtime device allowlists.
The contract therefore retains captured fallback and unselected membership;
it does not assume that fallback is zero. Selected entries must be enabled,
consumed by the actual loaded map, and present in controller0's before mask.
After-mask is the captured mask with only those routed P-state ID bits removed.
Ignored activation fails the exact readback and invokes Undo, without using
GPU or driver identity allowlists. The populated PERF v70 aggregation path,
loaded routing, support and other available controller masks remain checked.

Clearing the last enabled entry calls771730 for controller0 at BEEC17..27,
which recursively clears its group+F8 dependency
mask. Constructor76E4E0 adds controller4 when group+E1 is clear and controller2
when LPWR+2D8 bit100h is set. Its traced group+C0 callbacks write software-filter
data rather than additional dependency edges. The configuration getters do not
expose the dependency mask or these constructor predicates. No universal
controller4 disappearance is inferred. Schema5 accepts finite, fully specified
profiles for the traced conditional loss of controller2 (GRRG), controller4
(MSCG), or both, and rejects any other peer changes. It requires all enabled
entries to be disabled, including enabled entries unused by the loaded map.
Main controller0 must become unsupported and its configured mask unavailable.
All controller feature words and type7 configuration are independently read
and checked along with the auxiliary and bit1 blocks. DFPR controller9 feature
mask0x8 clears in a profile only when no surviving class0 provider (1, 2 or 20)
supplies feature0x200000; an unresolved provider prevents that profile. Activation must match
one complete profile; Undo verifies the captured before state. The dependency
graph is not exhaustively known, and broader hardware qualification remains
pending. Those main-loss profiles still require the OR-presence of raw bit4
among enabled GR PG entries to remain unchanged.
Raw bits1/3/4 feed separate feature-group operations92A650,927470 and92B350
respectively; their numeric role0 is not controller0.

On one RTX 5080 GB203 with driver 621.01, a schema5 production trial used
normal preparation, durable reload, Apply and Undo. Controller0 became
unsupported with its mask unavailable; controller2 was already unsupported,
controller4 stayed supported with mask120h, and DFPR9 feature Fh became 7h.
The complete controller and auxiliary projection, image, registry and other
requests matched, and the final baseline was restored exactly. This trial did
not test the conditional loss of controller2 or controller4, establish broader
hardware qualification, or measure physical residency.

A later packaged trial disabled all three enabled GRPG rows using the ordinary
panel, durable helper and Undo. Main0 became unsupported with mask and feature
unavailable; controller2 was already unsupported and controller4 remained
supported. The standalone auxiliary feature row was marked unavailable, with
its raw value uninterpreted; the bulk row was absent. Controller9 feature Fh became
7h, type1 role0 support/mask became valid zero, type4 role0 became unavailable
and type7 remained available through the MS peer. Full LPWR packet and opaque
peer projections, P-state, client/native requests and original restoration
were independently audited. This does not qualify the conditional loss of
controller2 or 4.

Root and Luna inspected all four frames: three checked boxes before and after,
three blank boxes and change lines in Preview, and three read-only False labels
active. The Preview shows the main support and P5/P8 unavailable clauses; the
far-right generic caption tail has no separate legibility qualification.
Before/restored images are byte-identical. All eight before samples matched;
final samples 0-2 retained BOOST_LOW request/hash differences and 3-7 matched.
All sixteen before/final curves matched without exclusions. There was no
independent active broad window, physical input or physical effect measurement.
This packaged trial used two GPU-only restarts. No-peer UI, failure/recovery
and other configurations remain pending.
[Packaged GRPG evidence](experiments/ui-lpwr-configured-qualified-20261029.json).

The initial schema6 qualification covered aggregate loss10h: disabling the last
routed raw-bit4 contributor while main controller0 and an independently
supported type7 peer remain. It reads the complete dependency scope and verifies role0
support/mask removal and controller0 feature8 clearing. Both available feature
copies must agree; an unavailable optional duplicate stays independently
unavailable. Other controllers and roles are retained. When selected entries
also contribute raw bit3, their type1 mask806:0 loses only those routed IDs,
while surviving support and feature10h stay active. Schema7 composes the
witnessed mixed losses described below. The no-peer follow-up below extends
these schemas without changing their previously accepted projections.

A temporary singleton raw-bit4 setup on the captured RTX 5080/621.01 established
a distinguishing before-state. Normal production preparation, durable reload,
Apply and Undo then passed with schema6: main and type1 masks120h→100h→120h,
type7 role0 support1→0→1 and mask20h→0→20h, and feature8 removal/restoration.
Full A801 packets matched independently patched projections, raw A7 stayed
exact, and all captured peers and the complete original registry/image/requests
were restored after removing the setup. Intermediate broad GET differences
were retained and bounded GET-only recovery reached each exact projection.
The full suite passed 6,537 tests, including 399 focused tests. This qualifies
configured transitions on that setup, not a product enable action, physical
residency or power savings. [Bit4 qualification and remaining scope](experiments/grpg-bit4-qualified-20261029.json).

Schema7 adds the exact aggregate losses 12h, 18h and 1Ah while main support
remains. It composes the existing type1 bit3 projection, type4 bit1 support
query, and type7 bit4 projection under the same complete dependency readback.
For bit3 loss, role0 support/mask and controller0 feature10h clear; group-any
is derived from independently read peer roles. For bit1 loss, role0 becomes
unavailable; the initial qualification retained another supported type4 role
as a witness. The later full-state availability contracts are described below. The initial bit4
profiles clear role0 support/mask and feature8 while retaining a type7 peer. Both available
feature copies receive the complete combined projection; an unavailable
optional duplicate stays independently unavailable. If bit3 survives, only its
selected contributing routes contract. Schemas1-6 remain valid.

Three source executions of the production workflow on the captured RTX 5080/621.01 passed normal
Prepare, durable reload, Apply and Undo from distinguishing temporary setups:

| Aggregate loss | Main mask before/after | Type1 role0 after | Type4 role0 after |
| --- | --- | --- | --- |
| 12h (bit1+bit4) | 120h / 100h | supported, mask100h | unavailable |
| 18h (bit3+bit4) | 120h / 0 | unsupported, valid mask0 | supported |
| 1Ah (bit1+bit3+bit4) | 120h / 0 | unsupported, valid mask0 | unavailable |

Main controller0 remained supported with an available mask in all cases,
including the valid-zero results. Type7 role0 support/mask cleared while its
supported peer and group remained. Both feature copies and all remaining A801
packet bytes matched independent projections; the selected unavailable type4
value DWORD is unspecified, not assigned a false value. Full raw A7 remained
identical in these idle captures. That raw equality is diagnostic evidence for
these trials, not a universal gate on evaluated clock tails. Every successful
Undo recovered its exact setup, and ownership-aware setup removal recovered
the complete original registry, image, requests and peer configuration.
Intermediate broad GET differences remain recorded; bounded GET-only recovery
reached each expected state.

The first 12h attempt passed Apply but recorded AccessDenied during cleanup
before an Undo result and failed final restoration verification. A separate
durable Undo and setup removal restored the full original; a fresh complete 12h
trial then passed. The failure location/cause was not captured and remains
unresolved, rather than being treated as successful restoration. The retained
failure, recovery and three successful trials used sixteen GPU-only restarts
in total, without Windows reboot or workload. The full suite passed 6,563 tests,
including 425 focused tests. Those trials did not qualify type7 whole-group
disappearance, conditional controller2/4 losses, physical residency/savings or
broader adapters. The separate no-peer qualification follows. [Mixed-loss evidence and exact limits](experiments/grpg-bit4-combined-qualified-20261029.json).

**Last type7 role, with independent main readback (2026-10-29).**

Schemas6/7 now also accept no-peer before-states when both type7 roles1/2
independently return available/value0. Unknown peers without a supported
witness remain refused. Clearing role0 at92B350 clears controller0 feature8
and the group's support bit0; the A801 outer92B3A0 gate then skips the handler.
Every type7 query becomes unavailable, including group-any and role support.
The main controller's separate support/mask/feature GETs remain readable and
must match the complete projection. Existing peer-bearing receipts recompute
the same after-state and retain their original read scope.

Four source production trials used temporary understood MS enabled-entry
shutdown plus known GRPG raw-bit1/bit4 setup flags. Production then cleared
only selected GRPG bit0 through normal Prepare, durable reload, Apply and Undo.
All four removals ended with an unavailable type7 group:

| Loss | Main mask before/after | Type1 group-any after | Type4 role0 after |
| --- | --- | --- | --- |
| 10h | 120h / 100h | valid1 | supported |
| 12h | 120h / 100h | valid1 | unavailable |
| 18h | 120h / 0 | valid0 | supported |
| 1Ah | 120h / 0 | valid0 | unavailable |

Main support remained true, including both zero-mask cases. This also
qualifies type1 empty support as valid0 alongside type7 unavailability.
Independent raw-packet checks cover all controller support/masks/features,
type1/type4/type7 groups, duplicate GETs, routing and opaque bytes. Complete
client/native power records and requests, selected-bit-only images and the
compared configured peers are retained. Each qualified Undo restored its exact
setup; ownership-aware setup removal recovered the complete captured original.
A7 common identity/routes are required; its evaluated clock tails remain
diagnostic rather than becoming a universal byte-equality gate.

The first1Ah trial passed LPWR/client verification and normal Apply/Undo, but
its active and UndoSetup broad comparisons differed in50 evaluated V/F
frequency words. The original image, registry and complete curve were verified
restored after setup removal. That run remains a negative full qualification
with its cleanup warning; the recalculation cause is unproven. A separate
repeat reached the original strict broad comparator at every settled phase,
and all its captured curves equaled the original. No frequency exception was
needed for qualification. The diagnostic probe's broader frequency mask is
not a portable qualification helper: the current read-only layout and captured
grids classify127 GPU rows and trailing other_idx127, which remained exact.

The final suite passed6,633 tests in117.14 seconds. The focused controller/receipt
cases passed405 tests, and the Preview panel suite passed76 including the new
unavailable-group messages. Ruff passed for changed source and tests.

Twenty GPU-only restarts include all four qualified cases and the retained
negative attempt. No Windows reboot, workload, F219 SET or product enable path
was used. Type4 whole-group disappearance, conditional controller2/4 losses,
physical residency/savings, wider adapters and a dedicated packaged no-peer
visual walkthrough remain pending. [Exact no-peer evidence and limits](experiments/grpg-bit4-no-peer-qualified-20261029.json).

For the raw-bit3 transition,927470 clears controller0's available feature bit10h
at group+138 and role0's bit in the separate LPWR+12F8 group support mask+5C.
A801 type3 attribute38h reads the controller feature word directly. Type1
attribute1 reads whether the auxiliary group has any support;800h reads a
bounded role's support;806h returns its configured P-state-ID mask. Unsupported
roles return a valid zero here, so support and configured mask are captured
separately. These getters do not invoke the26h/28h statistics refresh hooks.
Nearby type1 attributes802h..804h are not used for this configuration readback.
Schema2 captures these extra fields only for the selected auxiliary transition,
preserves the other roles and verifies the exact projected feature/support/mask
change through Apply, Undo and recovery. Schema1 receipts retain their original
read scope and Undo behavior.

For the raw-bit1 transition,92A650 clears role0 in the separate LPWR+12F0
support mask. A801 type4 attribute9 can read supported roles0..10, but the
driver gates the query both on group support and on the individual role.
An unavailable row is therefore not a reported false value. Schema3 requires
role0 and another role to be readable before the change; it verifies role0's
row becomes unavailable while the witness and all other observed role rows
remain unchanged. This legacy schema does not qualify all-unavailable
replies; the later schemas8/9 add a full-state availability contract. No reliable
per-P-state mask for this auxiliary role has been identified; its configured
membership is not claimed. These additional reads apply only to schema3,
leaving schema1/2 transactions and older Undo receipts at their existing scope.
BEFD81 can set a runtime bit1 subflag after BIOS loading, and the HAL can also
overwrite entries. The post-restart role readback determines whether the
projected support disappearance actually occurred; the BIOS aggregate alone
does not establish it.

On the current5080, a reversible research setup cleared bit1 in enabled rows0
and2 while leaving their other flags intact. Disabling row1 then removed the
last bit1 contributor while main support and bit3 support remained. Controller0
and bit3 role0 configured masks changed120h to100h; bit1 role0's query became
unavailable and roles4/5/6/7 remained readable. All captured controller peers,
feature words and the other configuration matched the projection. The original
image, registry and requests were restored exactly. The setup patches are
research-only, not exposed as new raw-bit controls.
The subsequent schema3 production Apply and durable Undo verified the selected
entry through the normal transaction path. Undo restored the temporary setup,
then the experiment restored and independently checked the full original image,
registry and requests. No temporary setup patches remained active.

Schema4 composes the independently established bit1 and bit3 projections for
enabled-row aggregate XOR0Ah while retaining main support and bit4 presence.
Only this schema reads both auxiliary blocks. Both projections are recomputed
from the captured before state and bounded BIOS tables; Apply, Undo and recovery
verify both independently. The bit3 contract still refuses unowned/fallback
configured membership, and the bit1 contract still requires a supported peer
as a query witness. A change that affects only one group is not accepted as a
successful combined activation. Existing schema1/2/3 receipts keep their scopes.

On the current5080, a temporary research setup cleared only row2's bit1, leaving
all main and other flags intact. Disabling rows0/1 then changed the aggregate
0Bh to01h: controller0 eligibility120h to0, bit3 role0 support/mask1/120h to0/0,
controller feature10h cleared, and bit1 role0 became unavailable while its
supported peers remained readable. Main controller support and all other
captured peers/configuration matched the projection. The full original image,
registry and requests were restored. Raw setup patches remain research-only;
these observations do not establish physical residency or a bit4 enable path.
The subsequent schema4 production Apply and durable Undo verified both selected
entries. Independent captures checked both auxiliary groups and all controller
peers, first after activation, then after Undo to the temporary baseline, and
finally after restoration of the full original baseline. All checks passed.

On the current5080, a separate research trial cleared rows0/1 while row2 retained
main support and rawbit1. Controller0 eligibility changed120h to0, feature10h
cleared and auxiliary role0 support/mask changed1/120h to0/0. Role1 retained
support1 and mask120h. All22 controller support/other masks, other captured
feature words, image, registry and remaining configuration matched the planned
projection; Undo restored the complete baseline. This is configuration evidence
on that adapter, not an Ampere mapping or a physical-residency measurement.
The subsequent schema2 production preview, Apply and durable Undo passed the
same independent projection. Both selected fields were reported as runtime
verified on Apply and Undo; the complete original baseline was restored.

On the tested5080, clearing only GR PG row1 bit0 changed its configured mask
120h(P5/P8) to100h(P8). Independent full A7 and all22 support/mask reply bytes
matched the projection, as did the image, registry and other configured
requests. Undo restored the original mask and full baseline. This research
trial used the generic image transaction; its success is separate from the
production GR PG contract's automatic verification and does not establish
physical power-gating execution or savings.

The subsequent production GR PG trial passed normal preview/Apply and durable
Undo with the same independent complete-packet projection. Both transactions
reported `lpwr.gr_pg.1.enabled` as runtime verified, and the full original
baseline was restored. The actual production preview callback also used real
driver GETs with substituted text storage and showed P5/P8 to P8 eligibility;
it changed neither registry nor active transaction while preparing that view.

The candidate A801 activity attributes26h/28h are not used as residency
telemetry. They select DWORDs at statistics offsets28h/2Ch and can include
host accumulation. Their pre/post hooks perform per-query cache refresh, not
an established sampling-registration protocol. A failed firmware-memory read
at76FD2A is logged without propagating failure to the row-valid result; the
refresh hook also ignores its helper's status. Outer RM success and a stable
valid row cannot distinguish a fresh zero from an internal read failure.
Zero retention requires the later host-carry contribution to be absent or
zero; a nonzero carry can instead leave a valid-looking accumulated value.
Producer units, reset epoch and reliable freshness remain unresolved. These
limitations do not apply to the separately traced39h/36h configuration paths.
[Updated PMU provenance and remaining boundaries](experiments/lpwr-telemetry-provenance-20261029.json).

`voltage_bios_runtime.py` adds independent effective-policy contracts for
constant VMIN equations. It binds each BIOS rail to the existing singleton-mask
absolute rail getter and signed-offset getter, takes two stable policy samples,
and retains paired identity, boost, offsets and every available peer policy.
The live-voltage field is allowed to vary. A missing unrelated rail does not
disable a valid selected rail. New receipts validate their complete contract
before registry cleanup; older receipts remain recoverable without this field.

On the tested 5080, BIOS VMIN 800000 -> 801000 uV produced an effective minimum
of 805000 uV. That is evidence of quantization on this configuration, not a
universal 5 mV rule. Production trials on NVVDD and MSVDD separately verified
800000 -> 805000 -> 800000 uV, with all other reported policies, existing
offsets, BIOS bytes and registry values restored exactly. A production 801000 uV
request was then rejected by exact readback and automatically rolled back.
The VMIN parser no longer treats a theoretical REL-chain minimum as the active
upper limit: this adapter's original MSVDD VMIN was already above that
conservative polynomial bound. It still requires an unshared constant and a
complete understood REL chain; paired runtime limits qualify each operation.
No workload or physical voltage measurement was used in these trials.

Independent voltage-device INFO (0x2080B205) and rail INFO (0x2080B201)
captures linked the two rails to their default devices. Both devices reported
a first table interval of 5000 uV on this adapter. The installed driver sorts
a device voltage table and supports ceiling/floor searches through its entries;
the first interval alone does not establish a uniform grid or authorize a
universal step-size rule. These INFO values remain research evidence; VMIN
activation continues to require exact effective readback.

`fan_policy_runtime.py` adds independent contracts for v20 fan-policy curve
temperatures and fan-stop thresholds. Guarded INFO (0x20808532, 336 bytes),
CONTROL GET (0x20808534, 1556 bytes) and routing INFO (0x20808547, 332 bytes)
bind the policy group, class, current stop settings and both routing masks to
the BIOS metadata and Fan2X cooler inventory. Two stable fresh reads retain
all understood peer temperatures, duty/RPM values, stop flags and cooler
bounds/PWM settings. Unrelated unknown policy classes remain inspectable;
missing routing disables writes without discarding readable policy data.

BIOS temperatures are unsigned 1/32 C units; current policy temperatures are
signed Q8 Celsius, so each BIOS step is eight Q8 units. A policy flag can replace
point 2's stored temperature with a thermal-table value plus signed offset;
that point is preserved and cannot be written through its ignored field.
Temperature and duty/RPM curve coordinates can be selected together; stop-enable
and cooler metadata changes remain separate. Pure receipt validation binds selected values and
peer state before activation or recovery, and legacy receipts without the new
contract remain recoverable. Readback establishes loaded configuration, not
physical fan response or the active winning policy.

Production trials on the tested 5080 changed policy 0's stop-low temperature
50 -> 50.03125 C, policy 1's middle curve temperature 70 -> 70.03125 C, and
shared policy 4's final temperature 103 -> 103.03125 C. Each independent raw
CONTROL capture showed exactly the expected eight-Q8-unit change and no peer
policy or cooler change. Each undo restored all captured fan configuration,
BIOS bytes, registry values and the 360 W board request. No workload was run.

Curve-coordinate receipts additionally bind the output-mode selector and every
mask-A cooler route. INFO row byte13 selects duty when 1 and RPM when 0; this
matches the traced route/cooler mode consumers. All shared mask-A policies must
agree, and selected coordinates must remain within every routed cooler's bounds.
Mask-B references alone are not treated as output-mode relationships. BIOS duty
coordinates use integer percent converted to rounded Q16 fractions, and RPM uses
whole integers. The preview identifies a stored coordinate that is inactive in
the current policy mode. Exact configured-coordinate readback does not establish
physical fan response or which policy wins arbitration.

On the tested 5080, policy0's middle RPM coordinate accepted 900 -> 901 -> 900,
and its stored duty coordinate accepted 39 -> 40 -> 39 percent (Q16 25559 ->
26214 -> 25559). Its RPM mode stayed unchanged. Independent raw captures changed
only the selected CONTROL dword; all other fan metadata and cooler settings were
preserved. New receipts require these coordinate contracts; earlier image-only
coordinate receipts retain their exact registry undo path.

Global zero-RPM research on the tested 5080 changed BIOS fan-policy flags bits
1 and 2 across policies 0, 1, 4 and 5. After GPU restart, only INFO bytes 14/15
and current CONTROL byte 0x48 changed in each policy; complete raw policy,
routing and cooler packets otherwise matched. Undo restored the original bytes,
registry state, image and 360 W board request. Platform stop capability is a
separate loader condition; valid stored thresholds alone do not prove it.

Auxiliary clock research binds v50 BIOS low-five-bit type 2 rows to native
CLK_DOMAINS INFO type 16 using their constructor ordinal. INFO command
0x20809019 has a 48-byte header and 32 allocated 384-byte rows; its correct
NVAPI export-presence identifier is 0x57B5A5DF. Type 16 signed minimum/maximum
MHz values occupy row offsets 0x26/0x28. The BIOS HAL selector and runtime
header HAL identity are separate fields and are not required to be equal.
The tested XBAR minimum accepted -1000 -> -999 -> -1000 MHz with exactly one
INFO word changed, and full user-clock CONTROL, P-state, VF and board-request
state preserved. This experiment establishes loaded bounds without measuring
physical clock behavior under load.

`clock_bios_runtime.py` retains full INFO and user-clock CONTROL packets and
validates every selected v50 bound. The understood NVAPI CONTROL wrapper keeps
the native ordinal, maps native type16 to public type15, and copies the signed
kHz request into public row+0x114. Tightening checks only the selected domain's
retained request; unrelated memory offsets do not constrain an XBAR range.
New receipts bind exact v50 selection coverage and actual BIOS table metadata;
legacy image-only receipts remain recoverable. GPC/DRAM additionally retain
their existing independent public range checks.

Production repeats verified minimum -1000 -> -999 -> -1000 MHz for XBAR, SYS,
video, GPC and DRAM on this adapter. GPC/DRAM also update the corresponding
native P-state bounds and their enabled public projections; an initial core
comparison correctly detected those changes before its expectation was extended
to the already traced P-state projection. The repeat checked those exact words
while retaining every other packet byte, user request, VF field and power request.

`fan_stop_runtime.py` saves all five complete policy/routing/cooler packets and
projects only support/default/current flag bytes for the BIOS-selected policies.
Every new global-stop receipt requires a typed contract; preflight compares its
affected inventory against the running BIOS. A production nested transaction
verified true -> false -> true, followed by both undo steps to the original
configuration. Both enablement directions passed exact independent readback.
An absent loaded support flag leaves platform capability unobserved until the
driver accepts activation; it does not create a universal board support claim.

Factory clock research connects v10 array-A records at BIT P+0x98 to the typed
v50 clock-domain factory delta. In the inspected driver, loader 0x605AB0 reads
an enable flag and signed-kHz value; the type16 branch stores the delta at
object+0x15C/+0x160. INFO serializer 0x899F20 exposes these at row+0x2C/+0x30,
separately from the BIOS-domain delta at row+0x34/+0x38. The core factory delta
also propagates to XBAR when its enabled subentries include type1. Predicate
0x89A770 uses object+0x194 and subentry types at +0x198+40*i; INFO exposes them
at +0x3C and +0x40+24*i. Its serializer has seven slots, explicitly bounded by
the loop at 0x89A229. These addresses are research evidence only; production
uses the validated API layout, current BIOS binding and live domain inventory.
The generic descriptor producer linking raw BIT P+0x98 to internal BIOS+0x448
has not been completely traced; both selected records have independent
change/readback/restore evidence.

`factory_clock_runtime.py` binds each selected BIOS record to its current GPC
or DRAM API domain and derives XBAR eligibility from the loaded INFO packet.
It preserves every unselected INFO byte, the complete user-clock CONTROL packet
and all 9248 bytes of the understood VF tuning-request packet. Both observations
must be stable, identity-paired and bounded by allocation guards. New receipts
require typed activation and restoration contracts; older image-only factory
receipts remain recoverable. Factory changes are separate from other registry
and BIOS edits so their overlapping readback expectations cannot conflict.

Production trials on this 5080 verified core 75 -> 76 -> 75 MHz, memory
0 -> 1 -> 0 MHz and core 75 -> 0 -> 75 MHz. Core changes affected both GPC and
eligible XBAR factory deltas; memory affected DRAM. The +1 MHz memory trial
also changed public/native P0 and P1 reported memory frequencies by 1000 kHz.
Every production trial restored the complete independent baseline, original
image, empty registry and 360 W board request. No workload was run.

The first exploratory core trial recorded a temporary difference in 50
evaluated VF frequencies after undo while all saved configuration and tuning
requests already matched. A later read-only capture matched the entire original
snapshot without another write or restart. The cause of that transient is not
established. A subsequent +1 MHz core trial changed no evaluated curve points,
whereas removing the 75 MHz factory offset changed derived frequencies. Thus
the production contract verifies factory metadata and user requests; it does
not assume an exact linear projection of evaluated VF/P-state frequencies.

Rated base/boost research traces the v10 array-B records at BIT P+0x98.
Loader 0x605FC0 maps records 0/1 to VPstate selectors 16/18, divides unsigned
kHz by 1000 and passes whole MHz to 0x606F40. That function replaces the GPC
frequency at word+0/+6 of each eight-byte domain entry in both active and
original-copy groups. Selector17 follows only when its word+4 original
frequency equals the selected record's original frequency. Equality of current
frequencies is insufficient. All twelve inspected HAL family maps used these
selectors; production uses current bindings and packet validation, not those
binary addresses or a driver/device allowlist.

Public `NvAPI_GPU_GetAllClockFrequencies` V2 modes 1/2 return base/boost.
The inspected implementation reaches pointer-based RM 2080A016, marshalled to
flat GET 2080A026 (0x214 bytes), with original-clock flag 0x10000000 and selectors
0x800/0x400. Selector0x200 exposes the linked record. The actual GET handler is
0x423A80 -> 0x420350 -> the VPstate serializer 0x7FE500; the NVOC descriptor's
handler lies 16 bytes before its command ID. An earlier exploratory trace used
the adjacent handler and is superseded by the corrected GET trace. No native
setter or undocumented public clock mode is used.

`rated_clock_runtime.py` retains full VPstate INFO (2080A0A4/0x10744) and active
CONTROL (2080A0A6/0x87A0). INFO supplies the type3/v0x30 inventory, selector map
and four-word original/current frequency tuple; CONTROL supplies the active
copy. These are tied to the real GPC ordinal from clock INFO and its BIOS
binding. Native/public queries must agree on GPC kHz. Exact contracts project
only the selected/linked words and query fields, retaining every other byte,
all user-clock CONTROL requests and the full VF request packet. Two stable,
identity-paired, allocation-guarded reads precede preparation. Whole MHz in
1..65535 avoids the loader's unsigned16 truncation; zero is displayed only as
an inactive stored record, and undo restores the original enable flag and bytes.
Legacy image-only receipts remain eligible for undo but require fresh typed
contracts for activation.

Independent root research on this 5080 observed base 2295 -> 2296 -> 2295 MHz
and boost 2760 -> 2761 -> 2760 MHz. Base selected row1 alone; boost changed
rows0/2 because their original GPC words matched. Both trials showed no live
metadata change after registry staging; activation followed a GPU-only restart.
The full original configuration was restored. Immediately after some restarts,
a BOOST_LOW diagnostic control word differed transiently and then matched on
bounded read-only followups; the original mismatches remain in the evidence.
No workload or physical frequency/performance measurement was performed.

Both rated changes were then repeated through production prepare/apply/restore,
with independent full INFO/CONTROL/native/public packet projections. Base
changed row1 and boost changed rows0/2, exactly as predicted. All original
clock INFO, user offsets and VF tuning requests were preserved; evaluated
VF/P-state snapshots also matched in these two small trials. Undo restored the
original image, empty managed registry, no active receipt and 360 W board
request; bounded read-only followups recovered the full original diagnostic
snapshot. These results establish this adapter's metadata path only.

## Constant OV and ALT equation controls

`voltage_ceiling_runtime.py` binds compound BIOS `OV.constant` and `ALT.addend`
fields to native loaded VFE definitions and independent absolute rail status.
The parser updates the pinned constant's low clamp, high clamp and coefficient
c0 together (BIOS equation offsets +3/+7/+11). OV must terminate; ALT must link
to the same rail's complete REL equation. Native quadratic type3 corresponds
to BIOS type1. INFO supplies the ROM ordinal, variable, output units and next
link; CONTROL supplies float32 clamps at +4/+8 and c0 at +16. The two slopes
must be zero. Whole microvolts must be exactly representable by float32.

`vfe_equation_runtime.py` uses guarded E8192 INFO GET 2080A0B5/0xE0408 and
CONTROL GET 2080A0B6/0x100408. Both have a 0x408 header with a 1024-byte mask;
row strides are 0x70 and 0x80. Corresponding public export presence, paired
identity, transport geometry and response envelopes are checked. All packet
bytes are retained, including inactive and unknown peer rows. A saved projection
changes exactly the selected constant's three CONTROL floats; INFO stays exact.

Sharing analysis includes known VFE links and multivariate variable/coefficient
term references, all understood rail selectors, and C+0C main clock roots.
Type7 is a multivariate polynomial, not an edge-free equation: five-byte BIOS
term type3 contains a u16 equation index. Unknown term kinds, truncated tables,
cycles or unknown present clock geometry prevent compound constant writes.
This scopes the sharing proof to those consumers; other BIOS consumers remain
unverified. No device, subsystem, BIOS hash or driver version is an allowlist.

`voltage_rail_control_runtime.py` independently preserves complete public rail
CONTROL replies (0xAC8 bytes) for each available rail with a singleton mask.
V2 falls back to V1 only on INCOMPATIBLE_STRUCT_VERSION. The four decoded signed
offsets must agree with policy capture; additional offsets and opaque returned
bytes remain exact across activation and undo. The separate 40-byte boost GET
9DF23CA1 writes only byte+4. Bytes +5..39 are caller memory, not returned driver
state. Druta uses a sentinel, validates caller-owned bytes and allocation guards,
and captures only the meaningful percentage byte. Explicit unsupported status
keeps OV available; ALT needs the shared request getter.

Absolute rail status uses B202 (public 5D0634EE), whereas CONTROL uses B213
(public A3070DB0). OV verifies constant plus retained offset. ALT verifies the
previous reported ALT plus the requested addend delta; quantization mismatch
triggers rollback. All unselected ALT/OV/VMIN values and complete control requests
remain exact. ALT may recompute shared boost and all REL/effective status values;
OV may recompute only the selected effective status. These derived outputs are
recorded instead of predicting an unproven effective=min formula. Undo checks
the complete captured baseline, with registry cleanup preceding GPU queries.

The static host path derives shared boost from cached, voltage-table-quantized
REL/ALT monitor gaps across rails. OV does not enter that calculation. Cached
monitor values have not been proven interchangeable with public absolute status,
so the implementation does not substitute them into a guessed formula. Trials
verify loaded configuration and driver-reported limits without a workload or
physical regulator-voltage measurement.

Production tests on the RTX 5080 (621.01, BIOS 98.03.3b.c0.6f) independently
changed and restored OV 1200000 -> 1195000 uV on each rail and ALT addend
20000 -> 15000 uV on each rail. Each selected ALT status moved from 1060000 to
1055000 uV. A further NVVDD OV 1200000 -> 1035000 uV trial reduced its reported
effective ceiling 1040000 -> 1035000 uV, while REL remained 1040000 uV. This
establishes a firmware-reported response at that operating point, not universal
arbitration semantics. OV 1199000 uV was quantized to 1195000 uV and production
verification automatically rolled back. Every trial restored the original
registry, image and full captured baseline; some diagnostic flags needed bounded
read-only settling after restart. User clock/VF requests and the 360 W power
request were unchanged. These earlier constant-equation trials used 0% requested
voltage boost. A subsequent production REL trial exercised nonzero boost and
verified its restoration across Apply and Undo, as described below.

The separate **Live OV and ALT values** panel uses
`voltage_constants_live.VoltageConstantsController` and
`vfe_equation_runtime.write_constants` for a session-only type3 write through
`0x2080E0B7`. It binds the current loaded constant to the stored graph, variable,
output units and next link, permitting repeated live edits without equating the
stored value with the current value. Each call selects one complete row and
changes only its two bounds and c0. The writer shares the guarded transport
with the type6 writer, retains all other bytes, verifies complete fresh
INFO/CONTROL, and never retries SET. The controller checks reported policy,
complete rail requests, boost and readable clock requests, with the existing
ALT shared-output allowances. Entered failures receive one original-row
compensation and bounded GET-only verification of the full original state,
including the evaluated curve. Optional absent peer APIs are listed separately
from failures of present getters. Capability is evaluated per rail and role.

Four source-product live trials on the same RTX 5080 changed and restored OV
1200000 to 1195000 uV and ALT addend 20000 to 15000 uV independently on both
rails. Each reported OV changed to 1195000 uV or reported ALT to 1055000 uV.
Eight independent active snapshots over at least 20 seconds in each trial
matched the full definition projection and configured peers. The full legacy
rail packets and all six signed request words per rail stayed unchanged.
The first evaluated-curve snapshot differed; the next and subsequent snapshots
matched the initial curve. This bounds an observed transient rather than an
exact settling time. All four trials recovered the complete original baseline
after one explicit restoration. A separate live 1199000 uV OV request loaded
that constant but reported 1195000 uV; exact policy verification rejected it and
one automatic compensation restored the full baseline. These tests used 0%
requested boost; nonzero retained-boost cases have offline coverage. There were
no registry writes, driver restarts, workloads or physical voltage measurements.
The [sanitized live-constant evidence](experiments/voltage-live-constants-20261028.json)
records the tested scope. Arbitrary six-word replay, nonzero variable operand
semantics, modern F219 effects and physical enforcement remain unqualified.

The **Live voltage constants** panel also accepts the distinct VMIN role on
each independently supported rail. Its terminal constant binding uses the
current loaded value, preserving the stored value for display. The live branch
builds the existing `voltage_bios_runtime` policy contract without changing
persistent VMIN routing or receipt schemas. Loaded VMIN plus the retained signed
VMIN offset must equal reported VMIN; the requested result must be positive and
must not exceed any currently reported REL, ALT, OV or effective upper limit.
The displayed maximum input subtracts the retained offset from that bound.
Unlike ALT's shared-output allowances, VMIN verifies the complete policy with
only the selected expected VMIN changed. Native packets, rail requests, boost,
image and readable clock requests retain the same exact checks and compensation.

Source-product trials independently raised NVVDD and MSVDD VMIN from 800000 to
805000 uV without a restart. Eight independent captures per rail over at least
20 seconds matched the complete definition projection, reported VMIN and all
policy/request peers. The first evaluated-curve capture differed; later active
captures matched. Each original-row restoration recovered the complete baseline,
including the curve, after bounded GET-only settling. A further NVVDD request
loaded 801000 uV but reported 805000 uV, so verification rejected it and one
automatic compensation restored the original state. This is one rounding
observation, not a universal step. Hardware trials used 0% requested boost and
zero VMIN offset; signed-offset and nonzero retained-boost cases have offline
coverage. The [VMIN and consumer follow-up](experiments/voltage-vmin-live-20261028.json)
records scope and remaining requirements. No physical voltage measurement,
workload, registry write or driver restart was involved.

## REL upper-clamp controls

`voltage_rel_runtime.py` uses the same combined voltage observation as OV/ALT,
with a separate versioned receipt. BIOS type7 maps to native type6. Only the
upper float at BIOS +7 / CONTROL +8 changes. INFO, lower clamp, active and
inactive descriptor bytes, all coefficients and every peer row remain exact.
One, two or three variables require three, six or ten coefficient descriptors.
The current ROM supplies typed literal/variable/equation terms, ordinal, next
link and microvolt units; no numerical evaluator is assumed.

The parser computes reverse dependency closure across all understood VFE edges
and binds every affected rail and documented clock root. The initial runtime
contract admits affected REL/ALT roles with their own available policy/control
readbacks; unknown affected roles or clock roots remain unsupported. An unrelated
missing rail does not remove the selected capability. Full sharing scope remains
limited to understood VFE edges, rail selectors and C+0C main roots.

At zero requested boost, only affected REL/ALT outputs and their rails' effective
limits may recompute. All boost contributions, unrelated rail limits, signed
offsets and full raw control packets must stay exact. Nonzero boost has a wider
derived dependency set. New OV/ALT/REL transactions capture complete replay
contracts when the current interface supports them; a nonzero boost request
requires that contract before registry writes. Legacy receipt cleanup remains
available. Offsets are not classified as user requests merely because they are
nonzero: driver initialization can supply an offset. Exact readback remains
required for all captured requests.

A loaded upper clamp does not predict the evaluated REL voltage: the polynomial
may lie below it; next-equation accumulation, signed offsets and VID quantization
also affect status. Clamp/chain evaluation order remains unproved. Verification
accepts unchanged evaluated REL with an exact loaded clamp, records current
firmware outputs separately, and requires the complete original state on undo.
Legacy REL image-only receipts remain restore-only.

Root research on RTX5080 / 621.01 lowered 1120000 uV to 1035000 uV separately on
each rail. NVVDD reported REL/effective 1035000 and ALT 1055000 uV; MSVDD reported
REL/effective 985000 and ALT 1055000 uV while retaining its -50000 uV REL offset.
A 1036000 uV NVVDD clamp loaded exactly and reported REL 1035000 uV. The complete
configuration was restored after each trial. These observations cover the tested
adapter and operating point, without a workload or physical-voltage measurement.

Production repeats verified NVVDD 1036000 uV and MSVDD 1035000 uV through
prepare, persisted active-receipt reload, exact native projection and undo. A
third production trial set NVVDD to 1100000 uV: the loaded upper clamp changed
while every reported rail policy remained unchanged. This directly demonstrates
a nonbinding clamp on this adapter. All three trials independently restored the
full baseline; transient diagnostics settled through bounded read-only capture.

An initial production trial exposed duplicated native packet data that exceeded
the existing 8 MiB receipt-reader bound. Apply/undo and hardware restoration
succeeded, but active receipt reload failed. The compact contract now stores
INFO/CONTROL once per before/after observation and reconstructs the native
contract from those packets. Regression coverage includes real atomic JSON
prepare/active/undo reload under the unchanged size bound.

## Fixed REL equation bounds

`voltage.[01].REL.value` writes the same whole-microvolt float to BIOS type7
lower/upper +3/+7 and native type6 CONTROL +4/+8. The typed descriptors,
coefficients, INFO, next links and all peer bytes remain exact. Its initial
input is the stored upper bound; an explicit UI selection is required even
when only the lower bound will change. The preview displays both old bounds.

`voltage_bounds` native contracts save `after_lower_uv` alongside the existing
upper-clamp metadata. Mixed selections can pin one equation and change only
another equation's upper clamp. Same-equation aliases are rejected. Existing
upper-only receipts keep their original format. Full packet data remains
stored once per before/after observation under the existing receipt size bound.

The driver already accepts equal bounds on 49 type7 equations in the tested
baseline. Independent root trials on the RTX5080 moved terminal NVVDD REL from
1040000 to 1035000 and 1045000 uV, with ALT remaining 20000 uV higher. MSVDD
head bounds of 1045000 uV reported REL 995000 and ALT 1065000 uV while retaining
the -50000 uV REL offset and its temperature/IDDQ-dependent tail. All loaded
definitions and retained requests matched the exact projection, and complete
baseline restoration passed after bounded read-only settling. No workload or
physical-voltage measurement was used. These observations establish the tested
configuration; firmware clamp/chain evaluation order remains unresolved.

The control fixes the selected equation's bounds. It does not claim that the
whole linked equation list or reported rail REL equals the entered value.
Reported REL/ALT/effective limits are recorded separately using the existing
affected-root contract, while unrelated policy and all request bytes remain
exact. Undo requires the complete captured original state.

Production Apply/Undo repeated the 1045000 uV change on each rail, with exact
independent native packets and active receipt reload. The MSVDD repeat retained
a 1% boost request through both restarts; final cleanup restored 0% and the full
original baseline. Both production receipts fit the unchanged 8 MiB reader
bound. The complete regression suite passed 5330 tests after integration.

## Voltage request restoration after GPU restart

`voltage_request_replay.py` captures native rail INFO (B201/0x98C), native
CONTROL (B213/0x40C), independent V2 singleton NVAPI CONTROL packets, the small
boost getter and reported voltage policy. Complete replay requires every active
INFO rail to have an understood current identity and matching native type5 /
public type3 requests. Partial captures remain readable. The full INFO payload,
routing, types and opaque CONTROL bytes must match; no driver or board list is
used. All six signed microvolt request words are preserved. NVIDIA's legacy
`voltDeltauV[6]` ABI names them REL, ALT REL, OV, VMIN, VCRIT low and VCRIT high;
the installed public/native adapters preserve that ordering. Those names do not
establish the physical threshold effects or make a suppressed SET operative.

NVAPI export `0x87C55C8A` accepts a zero-initialized V2 0xAC8 request containing
the complete rail mask, BYTE8 percentage and each selected type plus six signed
offsets. It wraps F214. Native GET header BYTE0 is a group discriminator, not an
inventory mask; the row-tail byte is GET-only. Neither is copied as a SET input.
The setter can update boost or some rows before failing. Success status can also
accompany ignored offsets. Every attempt therefore requires independent complete
request readback; status and caller-buffer integrity alone are insufficient.
Readback errors identify each differing rail and offset, requested and observed
values, whether a zero-status SET left that word unchanged, and separate boost
or metadata differences. All six fields use their legacy ABI names and signed
microvolt values. This diagnostic does not establish that arbitrary offset replay works.

`voltage_replay_worker.py` performs each capture or replay in a fresh process.
It verifies the Windows device mapping and exact running BIOS hash before SET,
saves exclusive request/attempt/result records, and attempts SET once. The
registry transaction keeps the adapter mutex, journals the attempt, and retries
only fresh observations. Apply and Undo replay captured requests after the new
image is verified; rollback restarts the original image and replays the original
requests. A failed or uncertain replay retains its recovery files. Receipts keep
one compact capture and event links rather than duplicating worker observations;
the existing 8 MiB receipt limit remains unchanged.

On the tested RTX5080 / 621.01, 1% boost reported a 200 uV shared contribution.
A GPU restart cleared the request to 0%; replay restored 1% and the complete
previous policy. A production NVVDD REL clamp of 1035000 uV applied and undid
with 1% retained after both restarts. Original 0%, all six offsets, registry,
image, clock/VF requests and the full independent baseline were then restored.
These observations establish request and reported-policy behavior at the tested
operating point; no workload or physical-voltage measurement was used.

The first broad comparison also detected changed PERF reliability client
requests. The traced monitor refresh stores rail REL offset plus shared boost
at CONTROL +0x0C. Research binds these rows by voltage request type, current rail
ordinal and the independently captured equation root, preserving every other
byte. It does not predict boost from a percentage or identify clients by labels.

A separate -5000 uV offset request returned success but was ignored; exact
readback rejected it and restored the original boost. The inspected local F214
row setter skips offset copies when PERF+0x32C is set, and generated HAL initialization
selects that flag for the inspected chip family. No qualified active flag readback
was established. The captured call was not traced into this local implementation,
so that guard is not established as the cause of the ignored request.
This is distinct from the existing A030/A031 admission switch at PERF+0x2E5;
changing that switch is not a demonstrated way to enable the offset copies.

In a later no-restart trial on the same RTX5080 / 621.01, a SET returned zero
while NVVDD REL remained 0 instead of the requested -5000 uV; boost changed
from 0% to the requested 1%. The updated error identified the ignored word.
The original requests, policy, image, registry and peers were restored exactly;
there was no registry write or workload. The [sanitized report](experiments/voltage-replay-routing-20261027.json)
records the trial and its scope. Arbitrary changing-offset replay and recovery
remain unqualified.

In the inspected local implementation, public and direct native rail SET routes
converge on the same gated row callback. Pstates20 writes different records.
Native legacy arbiter controls own different requests and reject the modern VOLT+0x16A path;
the public escape's association with those native commands is not proven.
F208 controls voltage-device fields and F212 controls voltage-policy fields;
neither is established as an equivalent six-word replay. The modern group's
B212 INFO and B218 CONTROL readbacks were separately captured
on the current adapter. Its five entries have CONTROL types2,2,2,2,4, with zero
current flags and values. These are adapter observations, not fixed indices or
rail names. Their units, relationship to the legacy requests and F219 firmware
SET effects remain unresolved; no modern SET has been attempted.

On the inspected RTX 5080 / 621.01, a separate research no-op sent native
VFE equation SET `0x2080E0B7` (size `0x100408`) with the unchanged type6 REL
row3 and its known counts. Transport and RM returned zero, and full equation
INFO and CONTROL matched exactly. However, 78 evaluated V/F frequency words
(rows49–126) differed by -315 to -75 MHz. V/F voltage words, the complete
per-point BoostTable delta requests and all other configured snapshot components
remained byte-exact. One original-row restoration and fresh capture returned the
full configured baseline exactly. In a timed repeat, eight GET-only control
captures remained exact over 20.77 seconds. The first post-SET capture
(0.003–0.798 seconds) contained the same changed curve; the next
(2.858–3.650 seconds), all subsequent captures through 20.77 seconds, and the
final comparison were fully exact without a restoration SET. This establishes
a transient in that trial, not an exact settling time or its mechanism. The
earlier restoration cannot be credited as the cause of curve recovery.

A subsequent upper-only live trial changed row3's stored runtime upper float
from 1120000 to 1035000 uV. Both independent CONTROL packets matched that
one-word projection, while both policy readbacks reported NVVDD REL/effective
1040000→1035000 uV and ALT 1060000→1055000 uV. Eight captures through
20.74 seconds retained the new definition and policy values, unchanged peer
requests, and unchanged OV/VMIN and MSVDD policy. The initial evaluated curve
transient had disappeared by the next capture (2.852–3.651 seconds). One
original-row SET and bounded GET-only recovery restored the full original
baseline, including the curve. An earlier shorter trial stopped at the first
observation because its comparator did not project mirrored diagnostic copies;
the raw copies matched the intended change and its full baseline was restored.
The interrupted result is retained separately from the completed timed trial.

`vfe_equation_runtime.write_upper_clamps` dispatches one selected type6 row
through `0x2080E0B7`, preserving all counts, terms and opaque bytes. It verifies
fresh full INFO/CONTROL and delegates compensation to its caller.
`voltage_rel_live.VoltageRelController` binds the ROM graph and terms to the
current loaded upper bound, supporting repeated session edits without treating
the original stored upper as the current value. It uses the existing REL policy
contract, retains rail controls and readable clock requests, and reports
evaluated policy separately. Dispatched errors get one original-row
compensation; bounded GET-only recovery includes the original captured curve.
Genuinely absent optional peer readbacks are listed; failures from present
getters remain read failures. The Controls panel exposes read/apply/restore
separately from the persistent BIOS/restart transaction.

These operations used no registry change, restart, workload or physical
measurement. The inspected callback through PERF+0x588 remains unnamed. Live
upper-clamp behavior does not establish legacy six-word request replay, other
equation writers, or physical voltage enforcement. See the
[sanitized follow-up](experiments/voltage-replay-followup-20261028.json).

A separate candidate copy path now has a conditional static ownership trace:
the engine loader dispatches the constructed ClockManager interface through
table slot `+0x30`; its thunk subtracts eight from the interface pointer before
registering key `0x200` on the same root-relative list used by the later lookup.
The dispatcher passes record destinations within a separately allocated
`0x2A708`-byte buffer.
An offset within that buffer does not identify the destination as PERF+`0x588`.
Root reproduced the twelve bounded instruction windows and dispatch table;
current runtime invocation, the actual PERF callback and voltage-request
consumption remain unproved. See the [dispatch evidence](experiments/driver-transport-followup-20261028.json).

The real PERF owner's base is separately bound on the enumerated class
`0xED0B65` construction path. Successful construction returns its allocation
root; the loader's exact-class lookup stores that pointer unchanged in
GPU+`0x23C8`. The local equation handler loads its target from `[root+0x588]`,
passing that same root in RDX and the GPU in RCX.
The engine-dispatch interface is a distinct adjusted pointer. This conditional
base proof does not observe the active runtime descriptor or identify the
callback assignment. The traced equation group and child allocations retain
their separate identities.

An outer RM routing layer must be distinguished from those local handlers.
The ten inspected voltage-command export records carry
`RMCTRL_FLAGS_ROUTE_TO_PHYSICAL` (`0x40`). Under the installed runtime forwarding
predicate, successful forwarding returns `NV_WARN_NOTHING_TO_DO` (`0x10006`);
the dispatcher converts it to success and skips the local exported handler.
The conditional endpoint builds a message whose ID `76` and payload layout
match NVIDIA's `GSP_RM_CONTROL` schema. This outer control envelope is distinct
from the VOLT group's downstream submissions and response-directory selectors.
Neither the captured calls' live route nor their entry into the local F214
`+0x32C` or E0B7 `+0x588` paths was observed. Documented GSP-mode and firmware
queries returned unavailable values here; they do not establish a false
forwarding predicate. Existing changed-definition, readback and Undo evidence
remains interface-level evidence. See the [routing qualification](experiments/driver-transport-followup-20261028.json).

The matching DriverStore firmware file also contains an embedded RV64 image
with a validated Subdevice export array. Four entries match E0B7, F214, F219
and B214, including their parameter sizes and executable handler pointers.
The file is a candidate: its loaded identity, actual dispatch and handler
effects are unproved. These records provide concrete firmware counterparts
for further analysis without identifying the active route or qualifying a new SET.

Further candidate-image tracing reaches concrete row operations. The legacy
F214 construction path installs a setter which, after a successful type check,
either copies six DWORDs from the selected packet row or skips all six while
returning success. It tests byte `+0x334` of the pointer at GPU `+0x20C8`.
A conditional child-creation path now joins the Perf class request, successful
creation/cast and that slot. The constructor derives byte `+0x334` from a selector
halfword on its reached-store paths. This does not observe the current selector,
stored instance, flag value or later retention.
The row accessor's runtime table and returned interface identity also remain unproved;
this conditional body is not established as the cause of the captured no-op.

The modern F219 candidate type2 path maps input rows at
`request + 0x28 + 0x24 * index`. Its traced callback copies the input type byte
and three additional bytes, plus four DWORDs, into its destination row. Exact
source/destination offsets and the constructor/callback chain are retained in the
[candidate-consumer evidence](experiments/driver-transport-followup-20261028.json).
Those copies establish field transport on that path, not numerical units, rail
identity or voltage application. Further construction tracing gives a `0x2C`
serialized header and `0x24` row stride, submitted with selector `0x1F`. Three
conditional backends copy that buffer or store its words at unclassified
context-derived addresses; the active receiver and eventual consumption remain
unbound. A default response path also copies selected fields into object state,
subject to validation and callback retention. Neither image loading nor live
handler selection was observed, and no modern F219 SET was performed.

The candidate equation parser maps source type7 to native type6 and installs
the selected-row setter. That setter can copy both bounds before rejecting a
later descriptor-count mismatch. Entered-attempt compensation and exact
restoration therefore remain necessary even after an error. On one guarded
same-Perf construction path, direct member `+0x590` is assigned a leaf that
only returns success; this particular callback is not a numerical evaluator.
Its live retention and invocation remain unobserved.

An offline comparison independently decoded the saved native INFO/CONTROL
packets and matched both REL term/bound bindings to the ROM-derived fields.
This establishes definition equality, not coefficient order, polynomial
evaluation, clamp/next composition or quantization. A true additive REL
control remains unqualified. Known ALT sharing and the second REL chain are
effects to account for, rather than automatic disqualifications.

The candidate B202 effective-policy refresh now connects its destination
buffer to a selected group entry and backing descriptor on a conditional
same-instance path. The group is a separate allocation; an earlier private
alias to an embedded object was corrected after following intervening register
assignments. A guarded descriptor branch reaches a MemoryManager class-key
lookup, but the original mapped-value writer and live route remain unknown.
Separate receiver lifecycle callbacks update their own provider descriptors;
they do not establish population of `EA=*(VOLT+0x118)`, `receiver=*(EA+0x60)`.
These findings do not qualify modern voltage SET or arbitrary replay. The
[evaluator/source checkpoint](experiments/driver-transport-followup-20261028.json)
records the exact bindings, corrections, byte audits and remaining boundaries.

A distinct candidate equation-load path now packs the typed descriptors into
a serialized page with a `0x110` header, `0x100`-byte selection mask and `0x80`
row stride. Its observed extent is `0x40110`; the mapper's separate `0x2000`
index check does not establish that many rows in this backing. Native API
capacity and serialized page geometry remain separate. The trace does not
establish that earlier live E0B7 requests triggered this load or that firmware
evaluated the equations.

One constructor-bound alternate channel copies requests into mapped backing,
then publishes an incremented control word modulo 16. A separate response
path validates a header checksum and length, advances its response control
word and updates state checked by the sender's wait predicate. The bus call
between copying and publication is traced separately; its argument is not
treated as proof of notification. The response-driving caller, producer and
live channel selection remain unresolved.

The documented read-only `GPU_GET_CONSTRUCTED_FALCON_INFO` command
(`0x208001B0`, `0x504` parameter bytes) returned 15 entries on the tested
adapter. Pinned NVIDIA descriptor definitions decode one entry as Pmu
instance 0, with reported register base `0x8F4000`. The candidate request
control offsets are `0x2800` and `0x2820` relative to that reported base;
the response offsets are `0x24C8` and `0x24CC`. This arithmetic links the
candidate constants to a current adapter report, rather than establishing
the earlier calls' route, processor ISA, loaded firmware or numerical
evaluation. No device-specific register base becomes a product support gate.

The GET used a zero-initialized parameter buffer, preserved its paired adapter binding
and passed envelope/guard checks. The complete compared configuration, curve,
LPWR, Pstate and client requests matched before and after. No SET, registry
change or driver restart was performed. The
[equation queue and adapter record](experiments/driver-transport-followup-20261028.json)
retains raw output, source provenance, conditional paths and the remaining
evaluator/replay boundaries. No new voltage control follows from this GET.

A separate variable investigation matched native INFO `0x2080A0B1`
(`0xC364` bytes, header `0x28`, 255 rows of `0xC4`) and CONTROL
`0x2080A0B3` (`0x2BFC` bytes, rows of `0x2C`) to the installed NVAPI
converters. Both native packet rounds were byte-identical; native/public masks
and translated type observations agreed for all 75 entries. For the 53
type8/9/13 rows, the traced mode and two raw DWORDs matched their converted
public fields; the other 22 rows had no additional field comparison.
Native mode is at row+`0x20`,
serialized to public row+`0x2C`; an older SINGLE structure's row+`0x0C` is
not the installed native mode location. Native type10 at ordinal48 becomes
public type15, so the earlier public-only inventory could not establish that
native type10 was absent. Its selector byte2 remains semantically unclassified.

INFO header byte `0x26` was 1, while CONTROL byte `0x26` was 20 and matched
public CONTROL byte `0x24`. They have distinct sources. Variable SET
`0x2080E0B4` stores the CONTROL polling byte before selected-row dispatch,
so a request must preserve that captured byte even when a later row fails.
The native type9 selected callback copies the actual mode and full operand
container; the prior older-layout conclusion that it skipped mode was withdrawn.

A root-run request changed only variable44's mode0 to mode2 while preserving
all 16 zero operand bytes, bounds, other row bytes, header and peers. Eight
captures through 20.87 seconds matched the full native/public one-byte
projection. The initial 78-word evaluated-curve transient was absent in the
next capture, completed at 3.71 seconds. Both complete legacy rail CONTROL
packets, including all six signed request words per rail, stayed exact. One
original-mode SET and bounded GET-only settling restored every captured
definition, request and the full curve. This verifies zero-valued mode storage
and recovery, not firmware consumption or a nonzero voltage effect.

Current variable44 and variable48 feed recognized clock-programming roots;
their parsed closures contain no recognized P+`0x68` rail roots. Unknown edges
and consumers remain open. The installed public variable SET wrapper copies
two raw DWORDs without establishing their numeric representation or units.
Those facts do not qualify a nonzero operand or a substitute for six-word rail
request replay. See the [native-variable evidence and remaining boundaries](experiments/voltage-variable-native-followup-20261028.json).

Installed-NVAPI tracing binds public query `0x3FC4D4C8` to native B212 INFO
GET and `0xE6AAED18` to native B218 CONTROL GET. The inspected local B218 body
reads cached RM state; the captures do not establish local-cache servicing.
The separate public
query `0xC268BC22` dispatches native F219 SET; after successful SET transport,
C98050 on the local path fetches its response, which may conditionally update
the modern RM cache. The read-only captures exercised B212 and B218; no F219 SET or response
capture was made. Native B214 is a distinct GET bound to public query
`0xBE94281E`; one GET on the inspected adapter succeeded with status 0 and a
populated type2 result. This is a narrow single-adapter read qualification; a
separate typed VMIN comparison did not establish an active B214 correlation or
field units.

A subsequent GET-only control captured eight B214 responses whose call starts
spanned 21.01 seconds. All sixteen per-call configured-state brackets, the
baseline/final brackets and eight initial/eight final broad captures matched;
all 34 compared full curves were exact. Every traced type2 record varied across
the eight responses, exclusively in its code byte and associated DWORD. The
finite relation `DWORD = 6250 * code` held for all 56 returned pairs, without
establishing units or rail identity. No VMIN edit was made. This demonstrates
response variability despite exact saved configuration checks and prevents
treating the earlier single VMIN triplet as a stable configuration mapping.
The producer, physical meaning and consumed write path remain unresolved.
Raw inputs/outputs, timings, scope and independent audits are recorded in the
[GET-only control](experiments/voltage-replay-followup-20261028.json).

Further static tracing follows both sampled fields through the host copies.
The inspected local type2 serializer copies its input byte and DWORD independently, and the
complete public-wrapper path independently copies all seven sampled pairs.
Those operations do not create the observed `6250` relationship: on this
traced path it already exists at the serializer's input. The preceding transfer
has runtime-selected backends whose source and callback behavior remain
unbound, so this does not locate the original producer or establish units,
rail identity or a write format. Root reproduced 31 response/transfer windows
and 27 PERF instruction/data windows. Exact mappings, coverage limits and the
remaining boundaries are in the [transport evidence](experiments/driver-transport-followup-20261028.json).

The B212/B218 public getters use 32-entry masks: INFO is
`0x10D48` bytes with version `0x10D48`, header `0x48`
and stride `0x868`; CONTROL is `0x2348` bytes with version `0x12348`, header
`0x48` and stride `0x118`. The complete INFO allocation cannot be derived from
the version's low 16 bits. The B212/B218 native group remains E255; equivalence
requires all native mask bits above 31 to be clear. Two guarded public/native
B212/B218 captures were byte-identical within each format, with mask `0x1F` and
matching traced type2 fields. Within those B212/B218 captures, the fifth native
INFO type0 became public `0xFE`, while its CONTROL remained type4; those
independent observations do not imply absence.
The initial comparison used native CONTROL row+4 for its type. Re-analysis of
the same saved packets corrected this to row+0, preserving the failed journal.
Flags remain at native row+5..7 and four DWORDs at +8/+C/+10/+14. The observed
zero fields cannot independently prove their offsets; the wrapper supplies
that mapping. INFO's two enum converters establish a bounded 0/1/2 domain,
without semantic names. Raw copies establish no numeric representation or units.
No modern SET was called and the full configured before/after state matched.

Further installed-image tracing conditionally connects B214's receiver to the
same backing entry selected by the typed B202 rail-status path. Registration
and iteration use the same F33840-owned list and install the section receiver
into the exact voltage owner's config. Its kind2 item refers through the
section's stored base pointer to the same base container returned to B202;
both successful paths select its category0 entry. This requires the same
successful setup calls and retained owners/pointers; active class and execution
were not observed. The original section-constructor descriptor holds that base
pointer at `+0x10`; a later item descriptor separately uses `+0x8`.

The B202 constructor initializes offset `0x238970`, length `0xC10` and a zero
flag selecting its alternate read helper. These are host constants, not a
capture of live descriptor state. B214 GET instead selects section `0x1E`,
distinct from F219 SET's post-transport response selector `0x1B`. Its mapped
region starts at the parsed kind2 item's offset and is bounded by that item's
extent; those directory values remain uncaptured. Shared backing therefore
establishes neither overlapping regions nor a typed record correspondence.
B214 units, rail identity, producer, F219 effects and arbitrary offset replay
remain unresolved. The conditional path and arithmetic corrections are recorded
in the [transport evidence](experiments/driver-transport-followup-20261028.json).

The variable-group transport trace also found a separate short command after
the initial large-buffer check. Under the documented constructor path it reaches
the same endpoint interface as the modern group, but runtime instance selection,
receiving processor and numeric consumer remain unproven. Independent PERF
constructor and overlapping-store checks did not bind its post-group callback.
Older NVIDIA-authored Tegra single-u32 structures differ from the current
16-byte operand container and adjacent flag. These findings leave nonzero
variable operands and six-word request replay unqualified; see the
[follow-up evidence](experiments/voltage-vmin-live-20261028.json).

A static trace identifies the deferred assignment of the modern group's
receiver: a list consumer recovers the registered configuration owner and fills
the `config+0x60` slot after construction. The constructor installs a generic
indexed accessor. The selected population callbacks and insertion are bound;
each created section's owner reference points back through the constructed
container to the earlier base container used by the second lookup. The base
population constructs kind-3 backing entries, while a separate category field
determines selection: the reader selects category zero. The selected entry
leads to a bounded child MemoryDescriptor. Window construction and the later
bulk read are distinct operations; the inspected generic read path establishes
no voltage conversion. The source wrapper has conditional independent-allocation
and original-descriptor subview factories. Its original data writer remains
unbound. Exact public NVOC class IDs and the reserved-offset branch structure
identify the conditional source calculation as KernelPmu reserved-memory
backing, with KernelFsp and MemoryManager dependencies. That ownership does not
identify the writer of each voltage record. These are conditional host paths,
not proof of execution on the captured GPU or the mapped values' types, units,
rails or firmware effects.
The power-status transport descriptor and response parser use the same group
buffer, at a distinct offset within a common selected mapping on the traced
direct path. A candidate manager was excluded after resolving a class-specific
accessor to a different field. The correct manager's construction, population,
category-zero selection and later mapping call are now connected conditionally.
The metadata descriptor also reaches an upstream base-and-size provider, but
actual runtime selection, original data production and the numerical conversion
remain unresolved. A 0x700-byte metadata read is separate from the full backing
allocation. No new tuning operation follows from these transport findings. See the
[transport checkpoint and exact remaining boundaries](experiments/driver-transport-followup-20261028.json).

The reviewed constructor paths also bind logical directory geometry and final
read callbacks:

| Backing payload | Mask | Logical header | Row stride | Buffers | Callback offsets |
| --- | --- | --- | --- | --- | --- |
| Base directory | 32 bits | `0x10` | `0x28` | 1 | `0x100` |
| Section directory | 256 bits | `0x2C` | `0x18` | 1 | `0x100` |
| F219 post-SET response backing (static path) | Unclassified here | `0x2C` | `0x24` | 2 | `0x100`, `0x2600` |

These offsets are added to the logical requested offset; source-context
offsets apply separately. Base and section directory read lengths span through
the highest set mask bit, including unset rows within that span. The static
post-SET F219 response fetch uses directory selector `0x1B`; its wire group
byte is `0x17`. The installed callbacks replace the generic failure defaults,
and this response-fetch path uses before/after DWORD samples
for buffer selection and consistency. Header kind, source-row kind, category
and selector remain separate fields. Actual runtime records, unclassified
header bytes, writer identity and numerical voltage semantics remain open.

The public [mVolt v0.50.8 release notes](https://github.com/b00nz/mVolt/releases/tag/v0.50.8)
describe an experimental BIOS voltage-offset option, without specifying its
mechanism. Our inspected PERF+0x32C writer and type4/type5 rail routes have not
established a BIOS switch for this behavior.

## FixBlackScreenIssue startup adjustment research

The inspected driver reads `FixBlackScreenIssue` from the adapter software key
before its global fallbacks. On current Windows the global path uses the driver
PersistentState and Parameters keys; the legacy service-root path is conditional
on older OS branches. Explicit 1 forces the startup adjustment; 2 suppresses it.
Absent values can inherit another scope or defer to BIOS matching. Explicit 0
is not identical to absence: a successful lookup stops fallback before applying
the BIOS-match decision.

The traced initializer clears the six rail offsets, then the selected HAL
callback subtracts 50000 uV from the REL offset of each rail whose active INFO
domain selector at row+0x33 is 3. Getter support and that selector alone do not
prove callback activation. Current runtime offsets also do not establish their
origin, because other paths can replace them.

A root-owned 1 -> 2 -> 1 -> original-absence trial on RTX5080 / 621.01 verified
the complete startup differential. Value1 retained MSVDD REL offset -50000 uV;
value2 changed it to 0. Reported MSVDD REL/effective moved 990000 -> 1040000 uV
and returned. The five remaining offsets, the other rail, ALT/OV/VMIN, loaded
equations and other configuration remained exact. Staging alone changed no live
voltage state. Original registry absence and the full baseline were restored.
Transient BOOST_LOW diagnostics settled through bounded read-only captures.

`voltage_startup_runtime.py` now supplies the direct control contract. It owns
only the first REL offset of current INFO-selector3 rails. Complete VFE
definitions are stored once; all other rail words, INFO and opaque bytes remain
exact. At zero boost only affected REL/effective policy can recompute; nonzero
boost can also change shared boost and other REL/effective policy. ALT/OV/VMIN
remain preserved. The runtime policy is observed without a predicted formula.

The registry transaction applies an explicit DWORD1 or2 separately from other
changes. It proves the initializer effect before replay, then checks the full
projected requests. Undo restores the original registry type/value or absence,
replays the original capture and requires exact original definitions and policy.
A successful SET that ignored an offset fails readback and leaves recovery
required if the original requests cannot be reproduced. This control does not
claim that arbitrary preexisting runtime offsets are always recoverable.

The UI uses explicit force/suppress choices and a leave-unchanged choice; it does
not infer the active registry selection from a voltage number.

## Live client power requests

When the modern INFO packet is rejected with INVALID_ARGUMENT, an Ampere
adapter may expose a smaller 32-row client layout. Druta tries that known
read-only layout and requires an exact semantic-role/native-INFO binding, a
stable restorable FE request, matching native CONTROL, and PCI pairing before
enabling a SET. Packet sizes are capability checks, not driver-version or
device-ID allowlists. Ada's smaller INFO packet has an independent trace, but
the complete alternate layout and SET have only been validated here on the
RTX 3070 Ti; packet rejection remains an independent unsupported result.

For the smaller layout, a board SET can recalculate other occupied policies.
Druta validates their understood type, units, bounds and changed request words
while preserving all other bytes; it re-applies this session's user-pinned
values. A failed SET compensates the board request and restores any changed
peer requests, reporting whether the complete captured state was recovered.
Legacy-layout arbitration votes and original-BIOS-range provenance may be
unavailable without disabling the independent request path.

On the ASUS Strix RTX 3070 Ti / 595.97, the production setter accepted and
read back 381 W against a reported 380 W maximum at idle. A 65.9 W pinned
peer survived the board request after reapplication. The original 310 W board
request and complete captured controls were restored. No physical board-power
or downstream firmware enforcement measurement was made; this board is shunt
modified. See `experiments/ampere-live-client-power-20261027.md`.

`client_power_runtime.py` binds the unique milliwatt client-role1 inventory to
semantic-role INFO and an identical occupied native policy row. The external
client index and native policy index are distinct namespaces. Two complete
stable INFO/CONTROL observations establish the existing FE request. The live
`0x2080E633` setter changes that request once and verifies the exact client and
selected native CONTROL; other native bytes are preserved except for the
validated legacy peer request words described above. A dispatched failure
attempts one compensating original SET and exact restoration readback. Optional
power STATUS is diagnostic; it never replaces accepted-request readback.

`GPU.set_power_limit_mw` retains NVML for ordinary requests. Established live
extensions, a return from an active extended request, and an unavailable NVML
setter use the paired live controller. No second setter is attempted after a
live dispatch fails. The existing XOC switch admits positive whole mW requests
up to Druta's 1999 W software cap, widened to retain a higher ordinary maximum
or observed endpoint. This is not a physical ceiling or a claim of PMU bypass.
With XOC off, only the normal range and the observed extended endpoint remain.
A prior capability can retain its UI range after a transient observation failure;
any SET still requires fresh complete readback. Missing NVML range metadata can
be populated from the live binding; absent unrelated STATUS does not remove it.

The modern layout preserves full peer CONTROL and existing pin bookkeeping
without adding pin writes. A policy Stock action under an extended board request writes
its selected default and verifies the board request stayed exact; it reports
that host-derived limits were not recomputed. It does not send an intermediate
ordinary board request. Profiles validate against the same bounds, and capture
marks an extended carryover as requiring XOC for later replay.

The 1999 W request envelope does not imply a monotonic native effective limit.
An eight-point idle sweep on one RTX 3070 Ti exactly fit
`round_half_up(request_mW * 33 / 32) modulo 2^20` for settled native board
status, while NVML retained the complete request and eight coupled peers
continued extrapolating from their advertised endpoints. The 1999 W hold lasted
thirty seconds and restored the complete baseline. This is evidence of a wrap
along the native status path; the conversion site and physical enforcement
remain unproven. The observations do not select an architecture-wide cap or
alter decoder widths. See `experiments/ampere-live-client-power-20261027.md`
and its sanitized numerical summary for the full scope and timings.

The root production controller trial on RTX 5080 / 621.01 accepted 249 W,
451 W and 999 W and restored the exact original 360 W request after every
round. Independent full BIOS, registry and retained configuration matched the
original baseline. At 999 W, source PMU request was 999 W while downstream
policy2/7 arbitration remained 450 W. No GPU restart, registry write, workload
or Windows reboot was used. These observations do not measure electrical
power enforcement or identify mVolt's implementation.

The same-slider frontend trial additionally verified XOC-on 999 W, XOC-off
movement to 950 W, rejection of a subsequent upward request, exact 451.001 W
profile replay, and capture of extended carryover with XOC required. Returning
to 360 W restored the complete independent baseline without a driver restart.


## Original power range coloring

`power_range_baseline.py` resolves the original minimum/maximum for the existing
power knob from the dynamically bound client-role1 policy. Its read-only metadata
path requires client INFO, semantic-role INFO and matching native INFO; it does
not depend on an editable current request or power STATUS. The current BIOS must
match that binding. With active Druta overrides, the existing receipt chain and
hashed ROM backups establish the earliest original image. Preexisting external
BIOS overrides without a trustworthy original remain unknown. Missing provenance
affects coloring only; it does not remove the existing power control.

The reader also understands the existing smaller client/role/native INFO
layout. BIOS table version and native packet format are checked separately.
For supported v30 table geometries, the full policy class combines the low
nibble at row+0 with the low nibble at row+0x42. Only full class 1 has a traced
translation to runtime class 0; other v30 classes remain unmapped. The
[signed-driver trace](experiments/power-v30-class1-20261028.json) follows that
class through construction and both native INFO serializers. Exact policy
index, class, channel, units and all three bounds must still match. The same
class binding can satisfy the existing persistent power contract for editable
layouts. The RTX 3070 Ti read-only capture identified a shorter
`0x30/0x2C/0x47` table. Its bounded common fields support inspection and
original-range comparison. The persistent planning follow-up below remains
separate from this read-only target qualification.
The selected full class 1 row, channel, units and all three limits matched the
actual native/client policy; the original-range getter returned 100–380 W with
`live-bios` provenance and unchanged image, registry and configured requests.
This establishes the current live-image baseline, not a manufacturing-stock
flash or persistent activation/restart behavior. Original receipts and ROM
hashes remain required when an image override is active.
[Target qualification and limits](experiments/ampere-provenance-qualified-20261029.json).

The shorter row is explicitly accepted by the same signed driver: 5A4402
selects descriptor `2b8d3b2w1b6d1b1d`, and 5A466E calls the decoder directly.
It consumes 0x47 source bytes into 24 DWORD slots (0x60 bytes); the caller's
0x74-byte initialization is buffer capacity, not decoded size. The decoder
checks the row against the backing image and the caller rejects its error.
The planner retains stricter complete-table bounds. Full class1 uses the same
class0 constructor and native INFO mapping for its three limit DWORDs. The
separately consumed row+43 DWORD and every other byte remain unchanged.

Only that shorter-row class's minimum/rated/maximum fields now accept plans.
Other full classes remain read-only. Existing typed contracts require the
current adapter's complete native class, channel, units and bounds to match;
Apply verifies the exact loaded image and selected/peer metadata, and Undo
retains its existing durable recovery path. This source implementation and its
offline tests do not qualify activation/restart on the 3070 Ti. The remote task
uses `experiments/validate_short_power_override.py`: default GET-only preflight,
then explicit `--run` for one 1000 mW maximum increase and durable Undo. Complete
client/native buffers are projected independently and full fresh snapshots are
retained for peer review. No workload or physical enforcement is claimed.
[Short-row consumer and pending target validation](experiments/power-v30-short-row-20261029.json).

`power_slider_style.py` tints only the portions of the native slider outside
those original bounds and colors the adjacent numeric input amber for an
out-of-range requested value. Original endpoints are inclusive. The native slider
still handles input; the drawing follows its bounds, position, clipping and UI
scale. Per-frame and input/mirror refreshes cover programmatic values and rebuilds.
Colors describe the original range, not electrical safety or measured power.

The opt-in developer command `--ui-capture NEW_DIRECTORY` exports the actual
DearPyGUI framebuffer to PNG without the Windows screen-capture service. The
parent directory must exist; the output directory must be new. Startup-profile
and recovery-relaunch flags cannot be combined with this mode. Normal launches
are unchanged. After one ordinary rendered iteration, capture pauses ordinary
callback dispatch and periodic refresh. It captures the Control page and stages
widget-only power values and ranges using independently established original
bounds. It then calls the live REL and voltage-constant read and selection
methods, capturing supported rows independently. Two error frames pass the
nonnumeric string `not a number` to the existing Apply callbacks; parsing
rejects it before either writer is reached. No tuning or registry write is
performed by the sequence. Synthetic frames carry a developer notice.

Power widgets are restored before the live reads. The final frame and cleanup
restore the original tab, scroll position, header expansion and clock text;
live selections and inputs return to legitimate read values. The panel retains
its ordinary read and initial-value caches. The manifest records stage type,
selected live label, value and status, dimensions, frame, hashes, provenance,
skips and errors. Source capture has exercised the two rails' REL, OV, ALT and
VMIN rows and the two parser errors. Repeat capture and independent state
comparison for each packaged build. This checks rendering and readback, not
physical power or voltage enforcement.

The separate read-only `--ui-capture-recovery NEW_DIRECTORY` requires an
accepted `--recovery-result` and captures the RegistryPanel receipt/Undo state
and actual Control log in separate frames. It does not invoke Apply, Undo or
live-voltage callbacks. A packaged presentation used a root-created result
envelope from a real source rollback after a deliberately injected application
exception. The complete existing failure log and Details path were visible;
metadata bound the accepted result and recorded no active receipt and Undo disabled. The panel
pixels alone do not establish the button state. The source trial verified the
active NVDEC row0 change before the exception and restored the original image
and state through two GPU-only restarts. Early driver-flag differences remain
recorded; before/final curves matched. This does not qualify a native fault or
helper-created relaunch, active recovery-required Undo, physical input or other
adapters. [Scoped rollback and presentation evidence](experiments/ui-recovery-result-qualified-20261029.json).

A later source trial injected two application exceptions after the real active
and rollback NVDEC verifiers succeeded, leaving a durable recovery-required
receipt. The original registry and image had already been restored. The actual
packaged `--registry-recover` helper then verified restoration and relaunched a
normal GUI using its own success result. Two source restarts and one helper
restart were GPU-only. The original helper-relaunched GUI was identified from
its OS command line; the pending and recovered screenshots came from separate
read-only launches. The pending screenshot used a root-created result and its
long log line was clipped; this negative remains recorded. The success result
was generated by the actual helper.

A later read-only replay reused that unchanged historical pending result after
recovery, with no active receipt or new restart. The wrapped Registry panel
showed the complete historical message and Details path; the original log line
remained clipped. Receipt and Undo availability come from capture metadata,
not button appearance. Early driver-flag differences remain recorded; all
source, helper-final and replay curves matched. Actual failed helper-origin
Apply or recovery errors, other recovery-required states, native faults,
physical input and other adapters remain pending.
[Scoped helper recovery and replay evidence](experiments/ui-recovery-result-qualified-20261029.json).

A subsequent packaged `--ui-capture-recovery-undo NEW_DIRECTORY` trial used
the actual RegistryPanel Restore callback on a separately created pending
receipt. Two labeled application exceptions followed successful real active
and rollback verifiers; the original registry and image were already restored
when the recovery-required pointer remained. The panel launched the normal
restore helper and its actual resumed GUI captured the verified restored
receipt and cleared pointer. Two source GPU-only restarts and one helper
restart are counted separately from the earlier CLI case.

Root and Luna inspected the four actual initial and resumed frames. The pending
panel shows the complete wrapped failure and Details; its raw log still clips
the long line. The restored panel and log show the complete helper success
message. Receipt state and Undo availability are established by metadata,
not button styling. All sixteen source and eight GUI-final curves matched;
early driver-flag differences remain recorded. This does not qualify native
faults, other recovery states, physical input or physical effects.
[Scoped GUI Restore evidence](experiments/ui-recovery-result-qualified-20261029.json).

Two later packaged helper trials exercised receipt-load errors before any
registry transaction. In each, the helper was observed waiting for the normal
parent GUI to exit, then produced its own failure result and relaunched a GUI
on the selected adapter. The first rejected an invalid receipt path before file
access. The second used a valid receipt path whose file was absent and returned
WinError 2. Its wrapped panel message and Details path were fully readable;
the raw log clipped the long error-line tail, while its separate Details line
remained visible. Receipt absence and disabled Undo come from capture metadata.
Both trials retained an early driver-flag difference; all eighteen saved curves
matched. No GPU restart or registry transaction was requested. These load-failure
cases do not qualify post-execution helper errors, native faults, other recovery
states or physical input. [Scoped helper-load evidence](experiments/ui-recovery-result-qualified-20261029.json).

A further packaged helper trial deliberately supplied an incorrect expected
image digest in a copy of an otherwise valid NVDEC receipt. After the registry
write and GPU-only restart, the helper's image comparison failed against that
test value; the helper restored the original registry and image through a second
GPU-only restart and relaunched the selected adapter's GUI with its own failure
result. This was a controlled incorrect expectation, not a spontaneous driver
or native fault. The failure occurred before active NVDEC verification; no
independent complete active image or peer snapshot was saved. Before and final raw peers
matched; early final driver-flag differences are retained, and all seventeen
saved curves matched. Root and Luna viewed the complete failure, restored suffix
and Details path in both captured frames without clipping at this viewport. Other failure modes,
failed restart and physical input remain unqualified.
[Scoped post-execution evidence](experiments/ui-recovery-result-qualified-20261029.json).

The separate opt-in developer mode `--ui-capture-live-vmin NEW_DIRECTORY`
performs one +5 mV change to rail 0 VMIN through the panel's real Apply and
Restore callbacks. It selects the unique current ready row, uses its current
device-derived maximum, and tracks the retained reported offset through active
and restored readback. It saves raw `before.json`, `active.json` and
`restored.json` controller captures, three live framebuffer stages and the
final restored display. If capture or restoration is interrupted, cleanup
attempts the original value through the existing typed controller and records
failures and recovery attempts. This mode performs no registry write, GPU
restart or workload. Ordinary `--ui-capture` remains display-only; the two
capture modes cannot be combined with each other, `--startup-profile` or
`--recovery-result`. This supplies one scoped VMIN Apply/Restore walkthrough;
qualification requires recorded captures of each tested adapter and build.
Other real Apply/Undo and registry preview/recovery checks
remain pending, along with packaged-build comparisons, other DPI/viewports and
adapters.

On the captured RTX 5080/621.01, source and packaged runs each produced four
valid 1528×1261 frames. The actual panel callbacks applied and restored VMIN
800→805→800 mV; independent native equation/policy projections and complete
retained requests matched, and the final independent configuration including
the evaluated curve returned to the original baseline. Stage labels, displayed
values and statuses agreed across source and package. The active frames show
a collapsed V/F plot. Active and immediate restored native curve snapshots
match each other, although the restored frame shows the full plot; both native
snapshots differ from baseline. Those captures did not record plot series or
axis limits and do not establish the cause of the display change or a settling
time. Native screen capture failed twice, so these are
application-framebuffer/callback checks rather than physical mouse or keyboard
interaction. The complete suite passed 6,503 tests, including 22 capture tests.
[Power capture evidence](experiments/ui-render-capture-20261028.json) and
[live-panel source evidence](experiments/ui-live-panels-capture-20261028.json).

A read-only follow-up reproduced the collapsed plot on its first visible
render: all 127 editor/series points stayed unchanged while the axes fitted
their data on the next render, without another curve read or axis-clamp call.
The capture now postpones framebuffer output by one render when the plot first
becomes visible. It preserves the editor, current view and ordinary hardware
behavior. Source and packaged VMIN 800→805→800 mV walkthroughs then showed the
full fitted editor graph in the active frame and passed exact active policy,
retained request and complete final original-configuration checks. Matching
stage/value metadata and all four PNGs were checked in each run. This graph
uses the editor's cached curve; its freshness after a voltage change, native
curve settling and physical voltage enforcement remain unqualified. The full
suite passed 6,521 tests, including 40 capture tests; the package passed 427
committed-source hashes and twelve compiled-module comparisons. The earlier
collapsed frames and native-curve observations remain separate historical
evidence in the same JSON report.

The opt-in developer command `--ui-capture-pstates-trial NEW_DIRECTORY` runs one
mutating all-P-state editability Boolean trial, returning it to the captured
original value with two selected-GPU restarts. Windows is not rebooted. Unlike ordinary
display-only `--ui-capture`, it uses the registry panel's normal Prepare, Apply
and Undo callbacks and existing receipt/helper path; there is no alternate
registry mutator. Before, preview, active and restored frames are captured while
a durable trial record and each helper's recovery result carry state across
fresh-process continuations. The command rejects `--startup-profile`,
`--ui-capture` and `--ui-capture-live-vmin`. Source and packaged runs on the
captured RTX 5080/621.01 each produced four valid 1528×1261 frames and completed
false→true→false through two helper-driven GPU restart/relaunch cycles. The
checkbox and normalized preview/status metadata agreed across the builds.
Independent exact P-state contracts, restored receipt, original registry,
BIOS and complete configured peer state passed. A separate real Preview with
the launcher deliberately blocked preserved the full independent baseline.
The complete suite passed 6,517 tests; 187 focused UI/registry cases include
mocked continuation and failure reporting. Other adapters, physical input,
broader fields and post-restart failure/recovery cases remain unqualified.

The separate opt-in `--ui-capture-power-trial NEW_DIRECTORY` extends this
workflow to a numeric board-policy maximum. Current role1 metadata selects the
policy and the panel must expose independently matching editable mW bounds.
The trial stages maximum +5000 mW through the real numeric input and normal
Prepare callback. Native maximum contracts are required; transformed maximum
policies are rejected before Apply. A saved kind keeps the numeric workflow
across the existing helper/relaunch path while older Boolean records remain
compatible. No separate registry mutator or live client-power SET is added.

Source and packaged runs on the captured RTX 5080/621.01 each produced four
valid 1528x1261 before/preview/active/restored frames. The selected input showed
450/455/455/450 W; before, active and restored images matched across builds,
and initial/restored images were byte-identical. Both Apply and Undo passed,
with exact independent singleton-ROM and whole client/ROLE/native INFO
projections. Both complete CONTROL buffers and the 360000 mW request stayed
unchanged, typed policy peers matched, and the full original configuration,
registry, BIOS, requests and evaluated curve returned exactly after each trial.

The first source attempt passed runtime/Undo checks but clipped the selected
field and is not visual qualification. A launcher-blocked diagnostic reproduced
first-layout scroll_max 643 followed by 2778; clamping against the first value
left the field outside the page. Power-only capture now waits for settled
layout, uses the input's screen rectangle and child-window position, and lets
the scroll render before output. The diagnostic moved the field from Y1911 to
Y344/visible. Boolean/default scrolling is unchanged. The intermediate child
rect_min error and all original captures remain recorded separately.

The final suite passed 6,586 tests, including 128 focused UI/power cases; Ruff
passed for the trial/tests and the large app module has no new diagnostics.
Six GPU-only restarts include the initial runtime-only attempt plus the two
qualified source/package trials. No Windows reboot or workload occurred.
Physical mouse/keyboard input, other field types, post-restart failure/recovery
visual states, other DPI/viewports/adapters and physical enforcement remain
unqualified. [Scoped numeric qualification and retained evidence](experiments/ui-registry-power-qualified-20261029.json).

## Scoped LPWR row-0 UI trial (2026-10-29)

The opt-in developer command `--ui-capture-lpwr-trial FAMILY NEW_DIRECTORY`
covers row 0 of `gr_pg`, `ms`, `nvdec`, `nvenc`, `ofa` or `nvjpg`. It proceeds
only when that row is currently enabled and editable, and its BIOS plan changes
only the understood enable mask: 0x1 for five families and 0x2 for NVJPG.
The workflow stages the Boolean in DearPyGUI and calls the registry panel's
normal Prepare, Apply and Restore callbacks through the existing helper and
durable receipt path. The saved family and field carry across helper relaunch;
schema-specific contracts and fresh readbacks verify each phase. The additional
opt-in command `--ui-capture-lpwr-rows FAMILY ROWS NEW_DIRECTORY` accepts distinct
comma-separated row indices 0 through 4, stages exactly those enabled fields and
preserves the selection through both helper relaunches.

The trial records before, preview, active and restored framebuffer frames,
complete main LPWR observations, client-power state and image evidence. A
terminal snapshot is published after the final frame or a failure, retaining
receipt and recovery context. This is programmatic widget staging and callback
execution, not physical mouse or keyboard input. The ordinary display-only
capture and other registry workflows are separate.

Six packaged row-0 sequences completed through the ordinary callbacks and
both helper relaunches. Independent audits, reproduced by root, reconstruct
whole-ROM enable-bit-only changes and loaded routes: GRPG main/type1 role0
masks 0x120 to 0x20; MS controller1/4 and type1/type7 role1 masks to valid zero;
NVDEC/NVENC/OFA/NVJPG primary masks 0x129 to 0x009. Raw packet routing/opaque
bytes, full client requests, receipts and 24 PNG hashes/CRC/dimensions passed.
The selected active False rows and Preview text were visually inspected;
before/restored checkboxes were also inspected as checked and legible.

Four trials passed their initial strict external original comparison. OFA's
eight post-Undo samples failed on the curve after initial flag differences;
a later GET-only capture matched the full original, without a restart. NVJPG's
initial broad observer raised a stability error without returning the
disagreeing pair; a later GET-only capture matched the full original. These
negative/error observations remain separate from the later matches. Curve
freshness remains unresolved and no frequency exclusions were introduced.

The first GRPG trial exposed read-only False displayed as 0.0 and obsolete
dependency guidance. The fixed display uses False/True/unavailable for Boolean
fields, retains numeric scaling, and describes understood Preview contracts.
The corrected GRPG trial was repeated. Fourteen GPU-only restarts cover the
seven UI sequences; no Windows reboot, workload or F219 SET occurred. Windows
computer-use capture failed twice with 0x80040154, so framebuffer evidence
does not establish physical input. The final suite passed 6,823 tests in
119.23 seconds; 206 focused cases passed. Runner/panel/test lint and changed
CLI lines are clean; the app file retains its 26 pre-existing lint findings.

The original packaged qualification is scoped to one row-0 transition per
family. Later packaged MS trials additionally exercised rows 0 and 1 together
and all three currently enabled rows; they do not extend the other families'
row-0 coverage. Full-table shutdown and no-peer profiles for other families,
failure/recovery screens, physical input, multiple displays/DPI/adapters and
physical effects remain unqualified.
The later OFA source full-phase repeat is separate; its two
earlier curve-negative records remain retained. Counts and per-family outcomes belong in
[the scoped LPWR UI qualification record](experiments/ui-lpwr-configured-qualified-20261029.json).

## MS configured-state contracts (2026-10-29)

The MS v10 table at BIT P+0x84/object0x3A is now bound to its loader and
six runtime flag/feature slots. The reviewed allocator zeroes the same object;
the row loader populates indices0-4, and the known constructor/HAL callbacks
preserve those flag/feature fields. The actual loaded Pstate LPWR index selects
byte2 of the understood 18-byte index row; invalid/unassigned indices use
slot5. A controller mask follows enabled slots, while the type1 role1 mask
tests MS feature0x2000 and the type7 role1 mask tests feature0x100. Their
support aggregates are separate from their configured Pstate masks.

The initial row0 research prediction omitted controller1, which shares the
MS route when controller4 or6 is supported. That trial failed its exact
projection and restored the original; its negative evidence is retained. The
corrected research and normal production Prepare/durable Apply/Undo trials
qualified two distinct cases on the current RTX5080/621.01 configuration:

- Disabling row0 changes controller1/4 and type1/type7 role1 masks from0x120
  to valid0 while support remains, supplied by unconsumed enabled MS rows.
- Disabling all enabled rows0/1/2 removes controller4 support, keeps
  controller1 supported with mask0, removes shared type1 role1, and makes
  the entire type7 group unavailable because its sole supported role is gone.

MS contracts capture the understood tables and full independent LPWR
observation, recompute the durable projection, and verify selected fields
after restart and on Undo. Other controller support/masks/features, type4
roles, auxiliary peers, identity and loaded routing must remain exact. The
normal transaction still verifies the complete image and preserves unknown
bytes. Older image-only MS receipts retain Undo but need a fresh preview for
new Apply. Preview distinguishes valid-zero eligibility from removed support.

The complete suite passed 6,607 tests in 117.26 seconds, including 218 focused
MS/registry/LPWR cases. An independent raw-packet/ROM/client audit reproduced
the production projections and settled broad-state comparisons; earlier
non-exact driver-flag samples remain retained.

The qualified profiles require controller4 supported and controllers6/16
independently unsupported. Schema1 retains its original aggregate-preserving
partial disables and complete shutdown behavior. Schema2 additionally supports
a bounded partial loss within raw mask0x37C that removes the last enabled MS
raw0x10 contributor while another MS entry remains enabled. Its durable proof
adds the understood DIFR table and current feature/role witnesses; missing or
contradictory witnesses and other aggregate losses are refused. The feature
projection starts with the current reported word, removes mapped contributions
and applies the traced clear-only dependency filter. No tested board's flags,
feature word or identity are admission constants.

In the captured case, disabling rows0/1 leaves row2 enabled, contracts native1/4
and type1/type7 role1 masks0x120 to0, and changes native4 available features
0x023971EF to0x020910CC. Type1 role1 remains readable and reports valid0;
type7 role1 remains supported through the independent DIFR contributor.
The first research prediction missed the type1 role change and failed; it then
restored the original exactly. The later same-object trace resolves it:
after aggregate support and native feature filtering, 928470 intersects the
enabled MS slots' +45C values. The last raw0x10 contributor is gone, so it
clears role1 despite DIFR's earlier aggregate contribution. The corrected
research repeat and normal product Prepare/Apply/Undo passed independent raw
audits and root reproduction, including all six keyed value changes, other
packet bytes/opaque tails, complete images, client requests and final original.
Early BOOST_LOW request differences remain recorded; the final two broad
samples matched in each active/final window, and every captured curve matched.
The new profile passed the 6,907-test suite in 120.17 seconds. After a Preview
caption clarification, 143 focused panel/MS tests passed in 0.82 seconds;
the backend was unchanged after the full suite.

Unresolved mixed feature loss, nondiscriminating changes, ambiguous routes,
support6 cascades and other layouts remain pending. Capability/layout/readback checks govern
eligibility; tested device IDs and driver versions do not gate support.
Physical residency/savings remain unqualified. A packaged compound MS trial
used `--ui-capture-lpwr-rows ms 0,1 NEW_DIRECTORY` through the normal panel
callbacks and helper relaunches. All four framebuffer frames were visually
inspected: rows 0 and 1 were checked before, unchecked in Preview, read-only
False when active, and checked after Undo; row 2 remained checked. Initial,
active and restored captures retained full native LPWR and client readbacks.
Root's separate before/final broad comparison confirmed the original state;
final samples 0-2 differed only in the BOOST_LOW request/hash and samples 3-7 matched, with the final two strict
matches required and no curve exclusions. There was no separate active broad
observation window. All eight full curves matched. An independent audit reconstructed
the six native value DWORD changes, two ROM-bit changes, preserved packet/client
bytes, receipt events and observed process handoffs; root reproduced both audit
outputs exactly. The earlier negative research result remains retained.
The suite passed 6,963 tests in 121.94 seconds and 144 focused tests in 1.16
seconds; the package check covered 459 committed source files and 20 compiled
modules.

A subsequent packaged MS full-shutdown trial selected all three currently
enabled rows through the same panel callbacks and helper relaunches. The
corrected four frames showed all three checked before, unchecked in Preview,
read-only False when active, and checked after Undo. Preview described support
removal and unavailable readbacks. Controller 1's mask changed from 0x120 to
zero; controller 4 support was removed and its mask and feature became
unavailable. Type1 role1 support and mask cleared, and all six queried type7
fields became unavailable. This does not independently establish an empty group
mask. Type4 roles, other native peers, client requests, P-state data and opaque
packet bytes remained exact; the ROM changed only the three selected enable
bits and Undo restored the original. Initial, active and restored UI snapshots
included full native and client readbacks. There was no independent active
broad-state window in this UI trial; the earlier source qualification covered
full phases separately.

The first packaged attempt passed backend Apply/Undo and original restoration,
but its active frame left the Idle section collapsed and is retained as a
visual qualification failure. The read-only value widgets now retain their
field tags, so capture can open and scroll to the selected section; read-only
False and unavailable values remain outside the writable inputs. In that first
attempt all eight before samples and final samples 2-7 matched strictly; final
samples 0-1 differed in BOOST_LOW request/hash. In the corrected trial all eight
before samples and final samples 3-7 matched; final samples 0-2 differed in that
request/hash. All 16 full curves matched in each attempt, with no comparison
exclusions. The corrected result and independent raw audits were reproduced by
root. Both attempts together used four GPU-only restarts. The full suite passed
6,965 tests in 120.88 seconds, with 104 focused tests passing and Ruff clean for
the four changed source/test files; the package check covered 459 committed
source files and 20 compiled modules before the trial.

These programmatic framebuffer checks do not exercise physical input, recovery
screens, other adapters, or full-table shutdown and no-peer profiles for other
families. Ten GPU-only restarts include the retained initial negative trial, corrected
research and both production cases. The later partial-aggregate series adds
six restarts across its retained negative, corrected research and production
trial. The packaged compound callback trial adds two separately counted
GPU-only restarts; every trial restored its complete original compared state. No Windows
reboot, workload or F219 SET was used. [Scoped MS evidence](experiments/ms-configured-qualified-20261029.json).

## NVDEC configured-state contracts (2026-10-29)

The NVDEC table at BIT P+0x11C/object 0xB4 uses v10, a ten-byte header and
one WORD per row. The loader accepts fewer than six rows and tests bit 0;
bit 1 selects controller 12, otherwise bit 2 selects controller 17. Raw bit 3
contributes the associated type4 roles 4/8 support. Bit 4 changes the class17
feature word, not its support. Disable edits clear only bit 0 and preserve
all other bits. The runtime records occupy LPWR+AD8..B07; the traced zero
allocation, defaults and adjacent loader boundaries establish the bounded
six-slot candidate. This is not a whole-binary proof against every alias.

Actual loaded PERF Pstate IDs select LPWR index rows, whose raw byte 13
selects the NVDEC record; fallback and unloaded slots use the zero sixth
slot. Contracts require the complete before mask to equal that route
projection. Duplicate IDs retain eligibility if a surviving route contributes.
They capture the understood index/NVDEC/PERF ownership and full raw/typed
LPWR observation, recompute the saved prediction and verify every captured
controller, mask, feature and auxiliary peer after restart and on Undo.
Unexpected dependency effects fail verification and invoke restoration.

Two normal source Prepare/durable Apply/Undo cases passed on the captured
RTX 5080/621.01 configuration:

- Row 0 disable retained controller 12 and its feature 0x3E005, while its
  configured mask contracted from 0x129 to 0x009. P8/P5 both select NVDEC row 0.
- All five enabled rows disabled removed controller 12 support, making its
  mask/feature unavailable and removing type4 role 4. Other type4 roles
  supplied independent surviving support; all remaining peers stayed exact.

Both trials independently checked full LPWR packets, the selected-WORD-only
image plan, complete native/client power records and requests, registry and
compared configured state including the full curve. Both restored the complete
original compared state. Earlier driver-flag differences and, after all-row
Undo, curve differences remain retained; bounded GET retries reached strict
equality without excluding curve fields. Their cause is not inferred.
Four GPU-only restarts were used; no Windows reboot,
workload, F219 SET or physical-residency measurement occurred.

The current product profile requires available controller 12 support/mask/
feature, independently unsupported controller 17 and the traced role 4 aggregate.
Partial disables retain the enabled-row aggregate flags. Full shutdown keeps
the peer-bearing projection when another type4 role remains supported. If role 4
is the only valid type4 support query before Apply, the no-peer projection
requires all eleven support queries to become unavailable, with controller 12
support, mask and feature readbacks matching the predicted unsupported state.
Unavailable rows are not valid false values and do not prove an empty group
mask. Other controller 17 profiles, mixed aggregate losses and other table
layouts remain pending. Tested identities and driver versions identify
evidence, not compatibility gates. Older image-only NVDEC receipts retain Undo;
a new Apply requires a fresh runtime preview.

The full suite passed 6,667 tests in 116.54 seconds; 110 focused NVDEC/Preview
cases and changed-file Ruff also passed. An independent raw-packet audit was
reproduced by root for both terminal trials.

The original hardware evidence covers source production transactions. The
earlier packaged LPWR follow-up covers row 0 through panel callbacks and
framebuffer capture. A later packaged trial disables all five enabled NVDEC
rows through the ordinary panel, durable helper and app relaunch, then uses
Undo. Its unchanged executable matches the current committed runtime; 460
packaged sources and 20 compiled modules were checked before execution.

The independent audit reconstructs the five enable-bit changes and complete
LPWR packet projection. Controller 12 loses support; its mask and feature become
unavailable, while type4 role4 becomes unavailable with independently supported
peers retained. Other captured LPWR fields, opaque packet bytes, complete client
requests and the full P-state packet match. Undo restores the original image and
configuration. OS observations bind the three GUI processes and both helpers.
All eight before samples match strictly. Final samples 0-2 retain the observed
BOOST_LOW request/hash difference; samples 3-7 match strictly. All sixteen
before/final full V/F curves match, with no exclusions. No independent active
broad window was captured in this packaged run.

Root and Luna inspected all four frames: five checked rows before, five visible
enabled-to-disabled Preview lines, five read-only False labels active and five
checked rows restored. The Preview checkbox rows are below the viewport; their
unchecked appearance is not visually qualified. Before/restored PNGs are
byte-identical. This trial used two GPU-only restarts. Other type4 no-peer UI,
post-restart failure/recovery, physical input and physical savings remain
pending; the later NVDEC no-peer trial is recorded separately. [Scoped source and packaged NVDEC evidence](experiments/nvdec-configured-qualified-20261029.json).

## NVENC, OFA and NVJPG configured-state contracts (2026-10-29)

The three media tables share v10, a ten-byte header and two-byte WORD rows,
but their selectors and enable fields differ. The understood bindings are:

| Table | BIT P / object | Enable mask | LPWR-index raw byte | Native controller / alternate | Type 4 role |
| --- | --- | --- | --- | --- | --- |
| NVENC | +0x120 / 0xB5 | 0x1 | 14 | 13 / 18 | 5 / 9 |
| OFA | +0x124 / 0xB6 | 0x1 | 15 | 14 / 19 | 7 / 10 |
| NVJPG | +0x128 / 0xB7 | 0x2 | 16 | 15 / none | 6 |

NVENC and OFA give raw bit 1 priority over bit 2 for their primary/alternate
classes. Raw bit 3 supplies the corresponding auxiliary role, while bit 4 changes
the alternate feature word rather than support. NVJPG instead tests raw bit 1
directly for controller 15 and bit 2 for role 6; its loader does not test bit 0.
The former generic field therefore changed an ignored bit on this path. The
field parser/planner now uses mask 0x2 for NVJPG, preserving bit 0 and all other
WORD bits. NVENC/OFA retain mask 0x1. This binds an actual consumer rather than
interpreting a changed BIOS byte as a configured effect.

Media contracts use actual loaded PERF P-state IDs and each table's own
LPWR-index selector. Full before masks must match all routed contributions;
duplicate IDs retain eligibility if another surviving route contributes.
Fallback slots follow the bounded zero-allocation/default/loader evidence,
not an exhaustive alias proof. The product requires supported primary
controller/mask/feature, an independently unsupported alternate for NVENC/OFA,
and agreement between the enabled-row role aggregate and independent role
readback. Partial changes preserve the enabled-row flag union. Full shutdown
keeps the peer-bearing projection when another type4 role remains supported. If
the selected role is the only valid type4 support query before Apply, the
no-peer projection requires all eleven support queries to become unavailable
and the engine controller's support, mask and feature readbacks to match the
predicted unsupported state. Unavailable rows are not valid false values and
do not prove an empty group mask. Unknown words are preserved; originally
disabled entries cannot be enabled, and nondiscriminating selections remain
unqualified.

The following six normal source Prepare/durable Apply/Undo trials use the
peer-bearing profile; these results remain separate from the no-peer contract.
They passed on the captured RTX 5080/621.01 configuration: one row 0 disable
and one complete five-row shutdown for each family. Each partial disable
changed its primary configured mask from 0x129 to 0x009 while support, feature
and auxiliary role remained.
Each complete shutdown made its primary controller unsupported with an
unavailable mask/feature, and removed its corresponding auxiliary role.
All other captured controllers, masks, features and auxiliary groups matched.
NVJPG's complete ROM comparison verified mask 0x2-only changes with bit 0 intact.
Both source receipts verified every selected field, and each clean trial
restored the complete original compared configuration including the curve.

One earlier NVJPG shutdown attempt passed the full targeted LPWR/client
and source Apply/Undo checks but failed strict active/final broad comparison.
After driver flags settled, the remaining difference was 110 bytes in 50
GPU frequency words of the evaluated V/F curve; voltages, headers, other
entry words and tail stayed equal. This run is retained as negative, with a
cleanup warning rather than an active recovery receipt. A later GET-only
capture verified the complete original state without another restart. A
repeat using the unchanged strict harness passed; no frequency exceptions
were introduced. Earlier non-exact samples in the clean runs are retained,
and the cause of curve recalculation remains unproven.

The independent offline audit checks complete 1028-byte raw LPWR packets,
typed fields and opaque bytes, exact selected-mask ROM changes, actual
routes, complete client/native power records and requests, saved receipts
and every retained broad-comparison sample. Root reproduces that audit.
Fourteen GPU-only restarts cover the six clean trials and retained negative;
no Windows reboot, workload, F219 SET or physical measurement occurred.

The full suite passed 6,721 tests in 117.49 seconds, and 176 focused media,
NVDEC/MS and Preview cases passed after new-test import cleanup. Changed-file
Ruff passed.

The original hardware evidence covers source transactions. Later packaged
trials disabled all five enabled rows separately for NVENC, OFA and NVJPG
through ordinary panel callbacks, the durable helper and Undo. Independent
audits verified the exact enable-bit changes (NVJPG uses bit 1), complete
LPWR packet projections including opaque bytes, P-state and client/native
power records, receipts and restored original configuration. Each primary
controller became unsupported with its mask and feature unavailable; its type4
role became unavailable with supported peers retained.

Root and Luna inspected all twelve frames. The five selected rows are checked
before and restored, and show read-only False values active. Each Preview
shows five change lines and the availability caption; its checkbox rows are
below the viewport and their unchecked pixels are not qualified. Before and
restored images are byte-identical per case. All eight before samples per case
match strictly; final samples 0-2 retain BOOST_LOW request/hash differences and
samples 3-7 match. All 48 before/final full curves match without exclusions.
No independent active broad window was captured. These trials used six GPU-only
restarts; physical input and effects were not measured.

Other dependency no-peer UI and post-restart failure/recovery remain pending,
as do other profiles, layouts and physical residency/savings. Device
and driver identities identify evidence, not product compatibility lists.
Legacy image-only receipts retain Undo; fresh runtime contracts are required
for a new Apply.
[Scoped media evidence](experiments/media-lpwr-configured-qualified-20261029.json).

## Last type 4 role availability for NVDEC and media LPWR (2026-10-29)

The source contracts also model a shutdown where the selected NVDEC/media role
is the only valid type4 support query before Apply. The full prestate must show
that target role as available/value 1 and all other type4 rows unavailable. The
projected result makes all eleven support queries unavailable. It does not
reinterpret invalid rows as valid false values or claim an independently read
empty group mask.

For full engine shutdown, the selected native controller changes from
supported to unsupported, and its configured mask and feature become
unavailable. All other captured controller masks/features, auxiliary peers,
loaded routing and adapter identity must match the recomputed projection. The
existing peer-bearing projections are unchanged; no schema or receipt change is
required. Legacy GRPG schemas 3/4 retain their prior positive-peer witness and
read-scope rules.

Three complete source trials passed for NVDEC, NVENC and NVJPG on the captured
RTX 5080/621.01 configuration. Each normal Prepare/durable Apply/Undo verified
all five selected entries. Temporary setup, active state, Undo-to-setup and
final original matched the complete LPWR/client and strict broad comparison,
including the full curve. Independent audits were reproduced by root.

Two earlier OFA attempts retain partial configured-state evidence.
An initial setup-only attempt failed the strict curve comparison before
product Prepare, then restored the original exactly. One unchanged repeat
passed setup and active broad checks, and both product Apply/Undo verified
the images, LPWR packets, full client requests and all captured peers. Its
Undo-to-setup broad comparison failed all eight attempts on the curve after
initial driver-flag differences; final original equality, including the curve,
passed. The retained research recovery warning is an intermediate comparison
failure, not an active product recovery receipt. Both OFA records remain
negative for their failed phases. The cause is unproven; no curve exclusions
or comparator changes were introduced.

A later OFA repeat keeps the same product Prepare/Apply/Undo, temporary setup
and full broad comparator. It records eight complete broad snapshots per phase
at three-second target intervals and requires the final two to match exactly.
The separate eight-sample pre-window also matched the transaction's original
capture. Setup, active, Undo-to-setup and final original all passed this
criterion, including complete LPWR/client packets and durable receipt bindings.
The full V/F curve matched in every saved sample. Setup samples 0-3 and the
other phases' samples 0-2 still differed in the BOOST_LOW request and its packet
hash; these negatives remain recorded. Later samples matched without another
write within the phase. This qualifies the final-two full-state observation
criterion, not all-eight equality or immediate restoration. The request's actual
producer and physical effect remain unresolved.

Audits independently rebuild the complete images, owned patches and actual
routes, all raw packet rows/opaque bytes and independent copies, durable
receipts and every broad sample. The harness checked a second initial stable
capture but persisted only the first in each trial; audits do not claim to
inspect the unsaved second capture. Eighteen GPU-only restarts cover these
five attempts; the later OFA repeat adds four, for twenty-two source-trial
restarts total. No Windows reboot, workload or physical measurement occurred.

The full suite passed 6,786 tests in 119.24 seconds; 432 focused runtime,
transaction and Preview cases plus changed-file Ruff passed. Package parity
and baseline GETs remain separate from these source transactions. The later
evidence-only OFA repeat changes no runtime source; the latest unchanged-source
suite remains 6,890 passed in 121.76 seconds. Curve freshness, the BOOST_LOW
producer, packaged input/recovery and physical effects remain pending.
[Last-role NVDEC/media qualification](experiments/media-type4-last-role-qualified-20261029.json).

## GRPG type 4 availability contracts (2026-10-29)

New bit1-loss previews use schemas 8/9 for aggregate losses 0x02/0x0A and
capture the complete main-controller, feature, type 1, type 4 and type 7 state.
Legacy schemas 3/4 keep their narrow reads, positive-peer witness requirements
and original projections. Schema 7, already full-scope, also accepts the new
finite availability profile for combined 0x12/0x1A losses; previously accepted
peer-bearing projections remain unchanged.

The exact A801 type 4 attribute 9 path is gated by group presence/nonempty
support and then the queried role. Unsupported roles produce invalid rows,
not valid false values; the row's status is not preserved in the successful
aggregate command status. After the role check, attribute 9 reads the support
bit without another fallible helper. An all-invalid packet cannot independently
distinguish null group from zero mask. None of the already traced attributes
bypasses both gates to expose a status-preserving support mask.

The new contract therefore verifies the exposed availability outcome. Before
role 0 is valid 1 in the same packet, with all other queried roles unavailable;
afterward all eleven support queries are unavailable. No unavailable value
is converted to false, and no independently read empty mask is claimed.
Controller 0 must remain supported with available configured-mask and feature
readbacks. Actual loaded P-state IDs, selected entry bytes, native support,
all captured features and all other groups/peers must match the recomputed
projection. Identity, global errors, packet geometry and opaque-byte checks
remain enforced. This is configured-state evidence, not physical residency.

The temporary setup disables all enabled NVDEC/NVENC/OFA/NVJPG entries using
their understood enable masks 0x1/0x1/0x1/0x2, then isolates known GRPG raw flag masks 0x02/0x08/0x10 around an
enabled carrier. No unknown bits or initially disabled entry enable bits are
changed. Normal production Prepare/durable Apply/Undo clears only the carrier's
bit 0; Undo restores the setup and cleanup removes it. Four cases passed on the
captured RTX 5080/621.01 configuration:

| Removed aggregate bits | Schema | Main mask setup / active | Type1 role 0 mask setup / active | Type4 support replies after |
| --- | --- | --- | --- | --- |
| 02h | 8 | 120h / 100h | 120h / 100h | all unavailable |
| 0Ah | 9 | 120h / 100h | 20h / 0 | all unavailable |
| 12h | 7 | 120h / 100h | 120h / 100h | all unavailable |
| 1Ah | 7 | 120h / 100h | 20h / 0 | all unavailable |

Main support stays true. Last raw bit 3 losses clear feature mask 10h and type 1 role 0
support to valid 0; otherwise that role retains support with its contracted
mask. Raw bit 4 losses clear feature mask 8h and type 7 role 0 support/mask while the
independent MS peer remains. All untouched controller/group state stays exact.
Four normal source Apply/Undo pairs verified the selected field and full
readback, and the complete original compared configuration including the curve
was restored after each clean case.

An initial 1Ah setup passed full targeted LPWR/client checks but failed strict
setup/final broad comparisons. It never reached production Prepare or Apply.
After driver flags settled, only 110 bytes in 50 evaluated GPU frequency words
differed; other curve fields stayed equal. The setup was removed, and a later
GET-only capture matched the complete original state without another restart.
The negative remains retained separately; the unchanged strict repeat passed.
No frequency exclusions were used, and the recalculation cause is unproven.

An independent offline audit, reproduced by root, reconstructs all complete
ROM plans, actual-ID masks and role/feature changes, and checks full 1028-byte
raw packet pairs/opaque bytes, complete client/native requests, receipts and
every retained broad-comparison sample. Eighteen logical GPU-only restarts
cover the four clean cases and two negative setup/cleanup operations. No
Windows reboot, workload, F219 SET or physical measurement occurred.

The full suite passed 6,750 tests in 117.17 seconds; 492 focused runtime,
transaction and Preview cases plus changed-file Ruff passed.

A later packaged NVDEC no-peer sequence used a temporary setup that changed
18 understood bits in the other media and GRPG rows. The ordinary panel then
disabled five NVDEC entries and Undo returned to that setup; the supervisor
removed it and verified the original image, registry and state. Native12 became
unsupported and all eleven type4 replies unavailable. This does not establish
an independently read empty group mask. Full packet/opaque peer, P-state and
client/native request projections were independently audited.

Three attempts are retained. The first restored original state but did not
capture every GUI process in the strict OS observer. The second passed the
strict audit, but clipped the extra no-peer Preview line. After increasing the
existing Preview height, the third passed strict and visual review: five change
lines, the full no-peer explanation, restart and receipt are visible. The
checkbox rows remain below that viewport; active shows five read-only False
labels, and before/Undo-to-setup show five checked boxes with identical PNGs.

All four eight-sample before/setup/Undo/final windows per attempt retained
strict last-two matches and all 96 full curves matched. Earlier driver-flag
differences remain recorded; no independent active broad window was captured.
These attempts used twelve GPU-only restarts. Other dependency no-peer UI, physical
input and recovery, independent group-mask/presence readback, curve freshness,
other dependencies and physical effects remain unqualified.
[Packaged NVDEC no-peer evidence](experiments/media-type4-last-role-qualified-20261029.json).

Separate packaged NVENC, OFA and NVJPG no-peer trials used the same temporary
setup method and ordinary panel/helper Undo path. Each selected five enabled rows,
made its native controller unsupported and returned all eleven type4 replies
unavailable; this does not prove an independently read empty group mask.
Undo returned to the temporary setup, then the supervisor restored and audited
the exact original image, registry and full peer state. Root and Luna inspected
all twelve frames: five checked rows before and after Undo, five read-only
False labels active, and five change lines plus the no-peer explanation,
restart and receipt in Preview. Target checkboxes were below the Preview
viewport; before/restored images depict the setup, not the original image.

Each case retained four eight-sample broad windows with strict final-two
matches. Setup samples 0-3, Undo sample 0 and final samples 0-2 retained
driver-flag differences; all 96 full curves matched without exclusions. No
independent active broad window was captured. These three cases used twelve
separately counted GPU-only restarts. GRPG no-peer and other dependency
profiles, recovery, physical input, telemetry, other layouts and physical
effects remain pending.
[Packaged media no-peer evidence](experiments/media-type4-last-role-qualified-20261029.json).
[Scoped type 4 availability evidence](experiments/grpg-bit1-availability-qualified-20261029.json).

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

## PMU statistics provenance (2026-10-29)

The pinned host trace now identifies the statistics address handoff. A
controller-specific request with prefix bytes `00 06 13` returns a DWORD at
reply +0x10, stored at controller S+0x74. The existing scalar/64-byte readers
consume that address. The separate refresh request uses prefix `00 03 05`.
Neither receiver's producer, units, reset/latch behavior or freshness contract
has been established, and neither request was invoked in this follow-up.

The factory's requested base is NVIDIA's Pmu class ID F3D722. Its table
selection is now bound to the same constructed OBJGPU root as +0xF00 and
+0x2480. A paired public GetArchInfo capture returned 1B0/3/A1 twice, from the
hash-matched installed implementation. On the traced normal shared-source
initialization, public architecture equals constructor architecture shifted
left four bits and implementation is unchanged. The two traced compatibility
rewrites cannot produce this observed pair.

That normal construction projects tuple1B/3 to HAL42, callback680CC0 and
Pmu descriptor RVA F33840. Accepted profile zero/lookup-failure fallback and
RmPmuUcodeProfile4/5 overrides converge on that descriptor for this projection.
These numbers identify this binary's static paths, not compatibility gates.
The public getter can use a cache; no live HAL word, class or retained endpoint
pointer was read. This narrows the earlier B202/B214 investigation but does not
establish firmware receiver behavior or implement arbitrary voltage replay.

The installed NVUCODES v1 bundle has 1,223 descriptors, 864 nonempty and 359
empty. Bounds/nonoverlap and the metadata layout were independently reproduced.
Its fingerprint/order differs from the cached public generated index, so that
index's names cannot be imported by ordinal. Five bounded metadata comparisons
did not establish a matching GSP table; relocated or transformed tables remain
possible. No PMU payload or statistics writer has been identified.

Root reproduced pinned instruction anchors, table records, metadata and module
provenance; Luna independently audited the status/validity gaps. The research
probe added only two existing public architecture GETs, with identity reads;
routine package validation uses the existing configured-state GETs.
No candidate statistics request, registry mutation, GPU restart or workload
was performed. Current diagnostics remain configured support/masks, and the
telemetry producer, units, epoch/reset/wrap and independent freshness remain
pending. [Source identities, evidence and limits](experiments/lpwr-telemetry-provenance-20261029.json).

## Live fixed REL equation bounds (2026-10-29)

The live controller now extends the existing persistent `REL.value` field contract
without changing equation types or graph structure. `set_fixed` selects the
understood type7/native type6 root and changes only its lower/upper CONTROL
floats. `restore_bounds` restores both values from the current ROM definition;
the existing `set` remains upper-only. Repeated edits bind fresh loaded bounds
while checking the original graph, terms, identity and complete peer packets.
Fixed-head availability is independent of the upper-only capability.

The singleton two-bound writer reuses the existing E0B7 transport and complete
definition readback. A dispatched failure attempts one compensation to the
captured pre-write row, retaining the original error and verified recovery
status. Both Apply and Restore require the independent boost and affected-rail
policy readbacks. The panel distinguishes current bounds, ROM bounds and
reported policy; neither a fixed head nor exact definition readback establishes
the final chained limit or physical voltage.

Two source product-controller trials set roots3/265 from900000/1120000 to
1045000/1045000 uV, with eight independent active captures per rail spanning
at least21 seconds. NVVDD REL/effective changed1040000 to1045000 uV; MSVDD
changed990000 to995000 uV. ALT changed1060000 to1065000 uV in each selected
rail's trial. Full native INFO/CONTROL, retained requests and configured peers,
LPWR observations and client power requests matched their exact projections.
Computed arbitration telemetry remains separate from request equality.

For both trials, the first active curve and first two post-Restore curves
differed from the original by230 bytes. Later active captures and the third
strict final capture matched the original curve. These negatives are retained;
no curve exclusion or universal settling delay follows. Independent raw audit
and root reproduction confirm the changes and complete original restoration.
No registry write, GPU restart, Windows reboot, workload or physical measurement
was used. The source trials do not cover packaged callbacks or physical input.

The subsequent opt-in `--ui-capture-live-rel-bounds RAIL VALUE_UV DIRECTORY`
runner exercises the existing panel callbacks in the packaged EXE. It requires
the understood original bounds, rejects a fixed no-op, captures before/active/
restored/display frames and exits after restoration. If the panel guard blocks
cleanup, one identity-bound controller restoration remains available; a changed
target, image or selected equation prevents that fallback. The original failure
is retained.

Initial packaged trials retained a first-sample BOOST_LOW request change and
therefore did not qualify. A display-only control reproduced the same request
change while all equation definitions remained unchanged. This identifies a
startup/read-path confound, not its exact producer or physical effect. On this
configuration the observed type6 request changes from inactive to type3 with
vP-state index0, then returns to the original request. No comparison exclusion
or harmlessness assumption was added.

A later isolated GET-only trial did not reproduce that request change. After
the ordinary GUI closed, it captured eight PERF-only pre-samples, then invoked
no-call control, VfpCurve and BoostTableGet once each with eight PERF-only
post-samples per step, followed by full broad/LPWR/client checks and eight final
PERF samples. All 40 timed samples retained the original INFO/CONTROL hashes
and inactive BOOST_LOW request; the full final state matched. This was one
order and operating point in a warmed process, with samples taken after calls
returned. It does not qualify cold startup or prove no internal SET occurred.
No comparison was relaxed.

A separate static DearPyGUI child, importing no Druta or NVIDIA API code,
reproduced the transition. Its 24-second render loop completed 714 frames.
The parent retained eight PERF-only samples before launch, 48 during the child
process and eight after exit. During samples 1-6, the complete CONTROL packet
differed only in the BOOST_LOW tag byte, 0 to 3; its union DWORD remained zero.
Every INFO byte and all other CONTROL bytes matched. The first changed sample
completed 0.513 seconds after process launch; the first reverted sample completed
at 3.562 seconds. Those times do not establish a viewport-ready event or a
general settling interval. Final broad state, full curve, LPWR and client
requests matched the original. The test used ordinary GUI rendering and no
explicit configuration SET, registry change or GPU restart. It identifies a
display-only reproduction without proving the driver writer, physical effect,
exclusive cause or absence of side effects in other getters.

In the inspected driver, native VfpCurve GET `0x2080902B` selects an interface
whose later constructor branches assign `0x8ADA60` or `0x8ADE20` to callback
`+38`; these callbacks obtain their `+108` receivers from different bases.
The actual branch, selected-row clock receiver and BOOST_LOW producer remain
unknown. Driver identity and RVAs identify this evidence, not compatibility
gates. [Isolated GET investigation](experiments/voltage-live-bounds-20261029.json).

The inspected NVAPI wrapper sends a zeroed row area and selected-point header
before `0x2080902B`, then maps returned rows to the caller. It also invokes
`0x20809029` and `0x700010B` helpers whose driver-side semantics remain unknown.
The host buffer allocation and the kernel command's payload-size descriptor
are separately evidenced; their nested pointer/length binding remains unproven.

The repeated trials provide 27-second acquisition windows before Apply and
before Restore. The independent observer requires the last two of eight
pre-Apply samples to match the full original state, then all eight active
packet/retained-peer projections to pass. Both rails passed at 1045000 uV fixed
heads, including LPWR and client requests and all four GUI frames. The first
active and first final curves retained 230-byte differences; later observations
and the second final capture matched exactly, including the curve. The windows
are observation opportunities, not firmware settling guarantees. Root reproduced
the independent raw audits and visually reviewed all eight frames.

These packaged tests cover programmatic callbacks on one configuration.
Physical input, failure/recovery walkthroughs, other adapters/operating points
and arbitrary six-word rail-offset replay remain pending.
[Source trials and exact qualification](experiments/voltage-live-bounds-20261029.json).
