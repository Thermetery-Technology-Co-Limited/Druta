# Druta

A monitor and tuner for NVIDIA cards, driven through NVAPI/NVML private
interfaces. It surfaces telemetry Afterburner doesn't show, edits the V/F curve
with planners built around how the boost arbiter actually behaves, and reads and
writes the framebuffer-partition memory timing registers.

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
machine, not of whichever order a driver happened to enumerate in. In the window
the picker is **Device → Card**, and the chosen card is in the title bar
whenever more than one is present.

**One card per window.** Choosing another card relaunches rather than
re-pointing the live one. Too much of the window is a per-card measurement fixed
at build time — slider ranges from `gfx_max` (2160 MHz on the Titan RTX, 1911 on
the Xp), the V/F editor sized by the probed table (128 entries vs 84), every
nudge a clock bin (15 MHz vs 12.657), domain names earned by correlation against
that card. A process boundary makes it impossible to miss one; re-deriving them
all live merely makes it unlikely.

Two windows on two cards is fine and is the intended way to work on both.

The switch refuses while the window is **holding** the card with either lock
mechanism: a hold lives in the driver, not in this process, so walking away
leaves the card pinned with nothing on screen saying so — and the next window,
pointed elsewhere, could not release it. Staged-but-unwritten V/F or timing
edits only ask for a confirming second click, since losing those costs nothing
but the typing.

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

## Force vs. induce

Nothing here **forces** a memory p-state, because nothing can on these cards:
`nvmlDeviceSetMemoryLockedClocks` returns `NVML_ERROR_NOT_SUPPORTED` and
`nvidia-smi -lmc` fails identically. `Induce P-state` runs a CUDA
device-to-device memcpy, waits for the clock to settle, and captures **while the
load is still running** — a capture taken after the load stops is a capture of
the card coming back down.

That reaches P2, which is enough, per the band rule above. If the card is
*already* at P0 the load is skipped: opening a CUDA context on a P0 card pulls
it **down** to P2.

A **V/F point lock holds P0 indefinitely with no load at all**, which is more
convenient than inducing when you have one.

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

**Druta does not ship nvtune.** It is a separate tool carrying its own signed
kernel driver, and redistributing it is not ours to do. It is located, in this
order:

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
