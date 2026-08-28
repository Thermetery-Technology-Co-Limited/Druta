# TitanTune

An Afterburner-style monitor/tuner for the Titan-RTX-die-on-2080Ti-Strix-PCB
frankencard: it surfaces Turing telemetry Afterburner doesn't show and drives
the reversible tuning knobs directly through NVAPI/NVML.

The GPU layer (`nvbackend.py`) is shared unchanged between UI builds: every
NVAPI id, struct layout, and the 15 MHz quantisation law in it was verified
against this specific card.

## Run

- `dist\TitanTune2.exe` — the current build (Dear PyGui). Standalone, no
  Python needed.
- or `python titantune_dpg.py` from source.
- Run **as administrator** for clock-lock, fan, and power-limit writes.

`app.py` / `dist\TitanTune.exe` (Tk) still exists, but it is not a build you
should reach for — see "Why Dear PyGui" below. Do not edit `app.py`; it is
kept only as a parity reference for the Dear PyGui port.

## Why Dear PyGui

The original UI was Tk. Dragging the window stalled the whole desktop.
Measured root cause: Tk creates one native child HWND per widget (~50 of them
on the control page), and on `WM_ENTERSIZEMOVE` Windows' modal window-move
loop pumps messages synchronously — so Tk's entire idle queue and `after()`
timers drain inside that loop instead of after it, and the desktop hangs for
as long as the drag lasts.

Dear PyGui (Dear ImGui + DirectX 11) renders the whole UI as GPU geometry
inside a single window: zero child HWNDs, so there's nothing for the modal
loop to stall on. `titantune_dpg.py` is now the only build worth running; the
Tk build is kept only as a parity reference.

## Layout

Three tabs, plus a menu bar:

- **Monitor** — read-only telemetry, refreshed from a background poll
  thread at 1 Hz.
- **Control** — the write knobs, plus the V/F curve editor in its lower
  half.
- **Timings** — read-only decode of the memory timing registers, with a
  built-in GPU load to induce a readable P-state. Needs the `nvtune` tool and
  its driver; see "Timings tab" below.
- **Menu bar** — `Device > Device report...` (driver-reported identity and
  ranges for this specific card, copyable as a bug report), `Clocks` (the
  GPU clock lock), and `Help` (keyboard shortcuts, about).

## Monitor tab

Tiles: core clock, XBAR clock, memory clock (converted to true MHz when the
memory type is known), edge temp, hotspot temp, power draw, vcore. Below
that: the full 9-reason clocks-event mask, the NVAPI perf-decrease bits
(including the insufficient-aux-power bit — a canary for the transplant's
power wiring), a GPU/board power split, per-domain utilisation, PCIe
link generation/width with AER error counters, and a state line (energy
counter, fan duty/RPM, applied offsets, voltage boost %, and BOTH lock
mechanisms read straight from the driver — the V/F-locked domains with the
voltage that was requested, and the NVML clock-lock range).

Between the tiles and those panels sits **ALL CLOCK DOMAINS** — every domain
the private getter populates, one row each, with the *programmed* frequency
next to the *measured* one. It is placed directly under the tiles on purpose:
the tiles quote the programmed figure, and this is the panel that says what
the card is actually doing. See below for what the two numbers are and how
far each row's name can be trusted.

## The private GetAllClocks payload

`NvAPI_GPU_GetAllClocks` (`0x1BD69F49`) — community docs call it "probably
deprecated," but it answers on Turing (status 0) and is the only user-mode
path to domains the public clock getter doesn't expose. The struct is 1156
bytes (`version = 1156 | (2<<16)`), 288 dwords.

Those 288 dwords are **two arrays over the same 32 domains, an exact
partition** (verified over a 192-sample sweep):

| array | dwords | per domain | at | contents |
|---|---|---|---|---|
| A | 0–63 | 2 | `2*d` | `{freq_kHz, capability flags}` |
| B | 64–287 | 7 | `64 + 7*d` | `{freq_kHz, srcid, 0, 0, 0, 0, 0}` |

They are **not two views of one number.** A is the target the driver
*programmed*: always exactly on the 15 MHz grid, and bit-identical across
samples for a fixed domain. B is a *measured* counter: it jitters 1–3 Hz and
never lands on the grid. Anything quoting one of them has to say which.
`PRIV_SLOT` — and therefore every tile — is in array-A dword numbers, i.e. the
programmed figure; XBAR is slot 2 (`PRIV_SLOT["xbar"] = 2` in `nvbackend.py`).

### How far apart A and B run

Measured on this card, GPC (domain 0), under ~99% GPU load, sampled ≥ 8 s
after the clock last changed (40 samples per locked case, 20 free-boosting):

| state | A | B | Δ |
| --- | --- | --- | --- |
| free-boosting at 1950 | 1950.0 | 1949.90 | −0.10 MHz |
| locked at 1920 | 1920.0 | 1917.03–1921.37 | within 3 MHz |
| locked at 1350 | 1350.0 | 1364.91–1364.94 | **+14.9, dead steady** |
| XBAR and domains 2/5, all three cases | | | within 0.14 MHz |

**Settled and loaded, they agree to a few MHz** — and where they don't, B is
*higher*, not lower: at the 1350 lock the card really is running one 15 MHz
bin above what array A reports (domain 2's own programmed word reads 1365
there too, and NVML reports the locked 1350).

Two things make Δ wide, and neither is a steady state:

- **A clock change in flight**, for ~1–2 s, either sign, hundreds of MHz up to
  1.7 GHz (+600 measured while locking *down* from 1950; −1700 while locking
  *up* from idle). An earlier figure quoted here — "at a 1920 MHz lock A reads
  1920.0 while B reads 1886.7", 33 MHz apart — came from a sweep that allowed
  0.22 s to settle. It was this transient, not a steady divergence; re-measured
  with the clock given time to arrive, the same lock holds within 3 MHz.
- **An idle card**, where Δ never settles at all. With no work the GPC clock
  gates, and B measures the average of a mostly-off clock: at a 1350 MHz lock
  with the card idle, B wandered 470–573 MHz (Δ ≈ −840) for tens of seconds.

So the reading rule the panel's colours are for: a wide Δ on an idle card, or
in the second after a clock change, is expected and means nothing. **A steady
wide Δ on a busy card is the one that counts** — there the tiles are
optimistic and the card is not running at the frequency they quote.

### How far the names can be trusted

Eleven domains are populated: 0, 1, 2, 3, 4, 5, 6, 20, 21, 22, 31. The panel
grades every name, because a wrong one sends you debugging the wrong domain
with nothing on screen saying you were misled:

- **CONFIRMED** (shown plainly) — 0 = GPC, 1 = XBAR, 4 = MEM, 21 = VIDEO.
- **LIKELY** (shown in amber with a `?`) — behaviour confirmed, *name* only by
  elimination: 2 = a third core-rail domain with its own V/F table (`SYSCLK?`),
  5 = a fourth that ceilings hard at 1350 MHz (`LTCCLK?`), and 31 (below).
- **Unnamed** (shown as `--`, index only) — 3, 6, 20 and 22. Their values are
  confirmed static here (405 / 1080 / 540 / 108 MHz) but no name for them has
  been earned, so they stay numbers. Note 22 is the one domain whose value is
  *not* a multiple of 15 MHz.

**Domain 31 is not a frequency.** Array-A dword 62 holds the PCIe link
generation (1/2/3), tracks the pstate, and ceilings at
`nvmlDeviceGetMaxPcieLinkGeneration`. The panel renders it as `gen N`; divided
by 1000 like every other row it would print as a perfectly believable 0.0 MHz
clock. Its array-B word has not been identified and is shown raw.

The odd dwords of array A are per-domain capability flags, constant across
every sample (`0x01`, `0x09`, `0x11`, `0x19` on this card). The MEM row is the
**raw** NVAPI figure — half the data rate — where the MEM CLOCK tile converts
to the true memory clock, so those two are meant to differ by the GDDR divisor.

## The XBAR clock domain

Measured behaviour on this card: **XBAR tracks core frequency, not the
voltage rail.** With the clock locked at 1800 MHz and vcore swept from
912.5 mV to 1068.75 mV, XBAR never moved off 1725 MHz. The empirical law —
exact on every independently observed point, and within one 15 MHz bin
across a 104-point sweep — is:

```
XBAR = max(540, snap15(0.95 * GPC + 15))
```

"XBAR = GPC − 90" is only locally true near 2 GHz; it is not the underlying
law and drifts off at other clocks.

## Control tab

All writes here are reversible and reset on reboot; they're gated behind the
"Unlock controls" checkbox (on by default — untick it to make the app
read-only).

- **Core clock offset (MHz)** — snapped to the 15 MHz grid before it's sent,
  because it lands in the same per-point VF delta table the curve editor
  uses (see below).
- **Memory offset** — shown in true memory MHz when the memory type is
  known (NVML units ÷ `2 * divisor`), else raw/effective NVML units.
- **Power limit (W)** — clamped to the driver-reported min/max.
- **GPU clock lock** (menu bar: `Clocks`) — min/max MHz, `Lock` /
  `Release` / `Lock max`. See "Lockable clocks are not a ceiling" below;
  `Lock max` pins both ends to the top of the lockable table, which is
  useful for holding one frequency steady but can be a step *down* from
  what the card is boosting to. The lock holds at idle on this card with no
  GPU load needed, which makes it the cheap instrument for characterising a
  clock domain. `Ctrl+H` in the curve editor drives a **different** mechanism
  (the V/F point lock — see "The V/F point lock, and the two lock mechanisms"
  below); the app keeps one record of which of the two is in force, and taking
  either one releases the other first rather than leaving an untracked lock
  behind.

  **What that record does and does not cover.** It covers the locks *this
  run of the app* took. Within a session, every lock action — `Lock`,
  `Lock max`, `Release`, `Ctrl+H`, and the releases inside `Reset all to
  stock` — leaves the on-screen indicator agreeing with the driver, and a
  release that *fails* deliberately keeps the indicator up rather than
  clearing it. An empty indicator means "this app is not holding a lock", not
  "the card is not locked": another tuner may be holding one, and this card was
  in fact found holding somebody else's V/F point lock at 1137.50 mV.

  **Both locks can now be read back**, which was not true before. NVML's own
  `nvmlDeviceGetGpuLockedClocks` is absent on this card, but both mechanisms
  live in the `0xE440B867` table, so the Monitor state line reports each of them
  straight from the driver rather than from the app's record — which is how a
  lock left by a previous run, by a killed instance, or by another tool becomes
  visible. `Release` (or `Reset all to stock`) clears a lock this app is
  holding; a reboot clears any of them. On exit the app *attempts* to release
  the one it is itself holding — see "Hold this point" below for what happens
  when that attempt fails.
- **Fan duty (%)** — manual duty with the hardware-reported minimum enforced
  as a floor (queried live via `nvmlDeviceGetMinMaxFanSpeed`; measured 41%
  on this card, with 30% used only as a fallback if that query fails).
  Below the floor the app refuses and tells you to use `Auto` for the
  zero-RPM idle curve instead.
- **Core voltage boost (%)** — raises the reliability-voltage ceiling toward
  the VBIOS over-voltage cap via `NvAPI_ClientVoltRailsSetControl`
  (`0xB9306D9B`, read-modify-write, 0–100%). At 0% the card won't request
  above its reliability voltage no matter how the curve looks. Turing
  headroom here is small, so this unlocks a ceiling rather than delivering a
  large absolute jump, and the effect only shows under load.
- **Reset all to stock** — two-step (press once to arm, again to confirm);
  zeroes both offsets, restores the default power limit, releases the clock
  lock, returns fans to auto, zeros the voltage boost, and resets the V/F
  curve.

A live readout row shows current core/VRAM clocks next to their P0 target
maximums, so the effect of any knob is visible without switching tabs.

### Lockable clocks are not a ceiling

The device report's "lockable clocks" figure comes from
`nvmlDeviceGetSupportedGraphicsClocks`, and it is easy to misread as the
card's maximum. It is not. It is the enumerated set of values
`nvmlDeviceSetGpuLockedClocks` will accept, and it is reported **per memory
clock** — on this TU102:

| memory clock | lockable graphics clocks |
| --- | --- |
| 405 MHz | 24 values, 300–645 MHz |
| 810 MHz | 121 values, 300–2100 MHz |
| 5001 / 6801 / 7001 MHz | 121 values, 360–2160 MHz |

The device report's headline shows the top-memory-clock row (360–2160), so a
lock the driver would accept at one memory state can be refused at another (it
lists every row underneath).

The V/F curve is a **separate mechanism**. Its clock is
`floor((base + delta) / 15) * 15` and is never checked against this list, so
the curve editor reaches clocks the lock cannot — 2175 MHz observed on this
card against a 2160 MHz lock ceiling. The two disagreeing is expected, not a
bug.

### The V/F point lock, and the two lock mechanisms

The card can be pinned **two completely different ways**. They are not two
views of one thing, and the app tracks which one is in force because releasing
the wrong one returns OK and leaves the card pinned.

| | NVML locked clocks | V/F point lock |
| --- | --- | --- |
| driven from | `Clocks` menu | `Ctrl+H` |
| call | `nvmlDeviceSetGpuLockedClocks` | NvAPI `0x39442CFB` |
| you ask for | a **frequency** range | a **voltage** |
| at idle here | pstate 5, **mem 810** | pstate 0, **mem 7000** |
| readable back | see below | yes, `0xE440B867` |
| survives reboot | no | no |

The V/F point lock is the stronger hold: measured on this card at ~5%
utilisation it keeps **true P0** — pstate 0, memory 7000 — for as long as it is
held, where `SetGpuLockedClocks` pins the graphics clock but lets the card fall
to pstate 5 and memory 810. Both are volatile; a reboot clears either.

**Ids and struct.** The getter is `NvAPI_GPU_...BoostLock` `0xE440B867`, already
wired for telemetry; the setter is `0x39442CFB`. Both take the **same** 780-byte
struct (`_ClockLock` / `_LockEntry` in `nvbackend.py`), version
`0x0002030C` = `sizeof | (2<<16)`, `count = 7`, entries
`{domain, unk1, lockMode, unk2, volt_uV, unk3}`. This was established
clean-room: the layout came from the driver's own GET output, and Afterburner's
binary was never inspected.

**`lockMode 3` means "the highest V/F point at or below the requested
voltage"** — the same `≤ cap` semantics as the de-flatten voltage cap, and
`below_cap()` is the single shared definition of that boundary. Measured:
requesting **900000 µV delivered 893.75 mV**, which is 143 × 6.25, a real point
on the 6.25 mV grid, and core moved 1950 → 1740. It held stable for 8 s, and
writing the original bytes back restored it exactly. `lockMode 0` is "not
locked". Domain **6** is the one that carries the lock on this card.

**The struct tells you what was ASKED, never what is held.** `volt_uV` is
echoed back verbatim: a 900000 µV lock reads straight back as 900000 while the
rail sits at 893.75, and this card was found holding a **1137500 µV** lock on a
curve that stops at 1087500. So the point actually held has to be *derived*,
which `GPU.resolve_vf_point()` does in two stages — resolve down to a point,
then apply the flat rule that makes the arbiter run the lowest voltage carrying
that frequency. All three observed cases reproduce: 900.00 → idx 71 @ 893.75 /
1740, 950.00 → idx 80 @ 950.00 / 1830, 1137.50 → idx 96 @ 1050.00 / 1950.

**Both mechanisms share this one table, and only the mode separates them.**
`nvmlDeviceSetGpuLockedClocks(lo, hi)` writes **two `lockMode 2` entries whose
`volt_uV` field is a frequency in kHz**, not a voltage: domain 0 takes `hi`,
domain 1 takes `lo` (verified with an asymmetric lock — 1350..1800 produced
domain 0 = 1800000 and domain 1 = 1350000). They coexist with the mode-3 entry,
and `nvmlDeviceResetGpuLockedClocks` clears the mode-2 pair while leaving mode 3
alone. Every lookup in `nvbackend.py` therefore matches on **mode**, never on
`lockMode != 0`: reading a mode-2 entry as a voltage yields a confident and
entirely wrong "1350.00 mV", and clearing one from the V/F side would silently
release the other mechanism's lock.

A side effect worth having: this makes the NVML lock **readable back**
(`read_clk_lock()`), which `nvmlDeviceGetGpuLockedClocks` cannot do here — it is
absent from the DLL. A clock lock left behind by an earlier run used to be
invisible to the next one. The Monitor state line now shows both locks, read
from the driver rather than from the app's own record, so a lock set by another
tuner shows up too.

### The validation ladder

This setter was wired only after climbing a ladder, and that ladder — not the
plausibility of the struct — is why it was safe. Any future unverified setter
gets the same treatment:

1. **The id resolves.** `nvapi_QueryInterface` returns a pointer for both the
   getter and the setter. Half a pair is not a write path, and the code guards
   every route on *both* resolving.
2. **An identity write is accepted and changes nothing.** GET, then hand the
   driver back the exact bytes it just produced. It returns `NVAPI_OK` and the
   state is byte-identical afterwards. This proves the id and the 780-byte
   layout on the running machine while moving nothing, and it is kept as
   `vf_lock_self_test()` so it stays runnable — it is in the standalone
   `python nvbackend.py` snapshot.
3. **A single-field read-modify-write moves exactly one thing, and reverses.**
   Change one entry's `volt_uV`, confirm the card moved as predicted, write the
   original bytes back and confirm it returns exactly.

Rung 2 is what makes rung 3 safe, and it is the reason nothing here ever
**constructs** a `_ClockLock` to write. Every write in this section starts from
a buffer the driver produced and edits one field of it; the unknown dwords,
the flags word and the six other domains go back exactly as they came. A struct
assembled from a header would be a guess wearing the same shape.

## The V/F curve editor

The editor operates on the same per-point delta table Afterburner's curve
editor writes (read `0x21537AD4` / `0x23F1B133`, write `0x0733E009`) — drive
clocks from one tool at a time, since applying an Afterburner profile
clobbers curve edits made here.

It's built around three hardware laws that aren't obvious from the driver's
public surface:

1. **The clock quantises, and the base is unreadable.** A VF point evaluates
   as `floor((base + delta) / 15 MHz) * 15 MHz`. `base` is never exposed —
   the frequency the driver reports back is already floored — so it carries
   an unknowable remainder in `[0, 15)` MHz. That means only *whole-bin*
   moves on the *delta* are predictable; computing a delta from an absolute
   target MHz value lands mid-bin and silently floors down, which is exactly
   how a flat segment gets re-created. `Set MHz` and every nudge button only
   ever add or subtract whole 15 MHz bins for this reason.
2. **Deltas must share a phase, or a uniform offset re-creates flats.** A
   uniform move (e.g. the core-offset slider) only stays grid-exact if every
   point's delta shares the same remainder mod 15 MHz — a point on a
   different phase crosses bin boundaries at a different offset and
   silently re-flattens. **Re-phase** pulls every stray point back onto the
   majority phase, rounding down only (a point can lose at most one bin,
   never gain one unasked).
3. **The boost arbiter parks at the lowest voltage of the top flat.** Turing
   runs the *lowest*-voltage point of any flat run at the peak frequency —
   so a flat sitting near the voltage cap stalls the card at that flat's
   bottom, leaving voltage headroom unused. **De-flatten ≤ cap** raises the
   boundary point (the last point at/below the cap, plus one point past it)
   by whole 15 MHz bins until it — and only it — is the unique top, so the
   arbiter has nowhere to park but there.

De-flatten only stages a plan onto the working copy; nothing reaches the GPU
until **Apply to GPU**, which flags any point that lands off-prediction after
the write. The predicted result — resulting top ≤cap, peak, park point, and a
loud warning when the plan *lowers* the peak — is not shown at the click; it
stands in the plan banner above the button for as long as the edits exist
(see "One click, with the warning first"). Points below the
cap are deliberately left untouched — ramping the low-voltage floor (many
points pinned at minimum clock) would make the card demand high clocks at
tiny voltages.

The plot itself: drag a dot to move it (left-click has to land within ~14 px
of the drawn marker — a true radius in screen pixels, not "the nearest point
by voltage"; miss it and nothing moves, and the press keeps the pan it would
have had), and the point moves vertically only, since voltage is fixed by the
table; frequency snaps to 15 MHz bins. Left-drag anywhere else pans the view,
and the corner readout names the *selected* point (index, mV, MHz, delta)
rather than the cursor. The vertical pan range is deliberately fixed at
0–3000 MHz rather than fitted to the curve: a drag can only reach what the
view shows, so a data-derived ceiling would also cap how far a point could be
dragged, and a Titan RTX clears 2300 MHz under LN2. Also: A/D to step the
selection, W/S to nudge ±15 MHz (hold Shift for ×3), `Ctrl+H` to hold the
selected point, or the −75/−15/+15/+75 buttons / `Set MHz`
box for precise moves. Everything happens on a working copy; **Revert
edits** discards uncommitted changes, and re-reading the curve with pending
edits requires confirming twice so a refused write isn't silently lost.
**Reset curve to stock** zeros every point's delta (Turing's factory deltas
are 0, so there's no persisted baseline to poison); a reboot also clears all
deltas.

### One click, with the warning first

`Apply to GPU` and `Reset curve to stock` each take **one** click. They used
to arm on the first press and commit on the second, with the plan stated in
between — but a consequence that only appears once you have already pressed
is a receipt, not a warning. So the plan moved to where it can actually
change a decision: a coloured, bordered banner sitting between the plot and
the buttons, recomputed on every edit, naming the number of edited points,
the resulting top ≤cap, the peak and how many points hold it, where the card
would park, and — in red — whether the plan **lowers** the curve's peak.
It also states, continuously, what `Reset curve to stock` would discard.

What pays for the missing second press is that every write to the 103-row
delta table now takes an undo point first (see "Profiles and undo" for exactly
which writes those are, and which deliberately don't). `Reset all to stock` is
the one exception and still arms on the first press: it drops every knob at
once — both offsets, voltage boost, power limit, fan and all 103 deltas — so
a stray click there costs a whole tune rather than one table.

### Profiles and undo (menu bar: `Profiles`)

`Save profile` snapshots both offsets, the power limit, the voltage boost,
the fan **policy** (not merely its duty — a card idling at 0% on the auto
curve and one pinned to 0% manually read identically, and handing a captured
duty back as a manual duty would be a thermal change, not a restore) and all
103 V/F deltas, as readable JSON in `profiles/` beside the app.

`Load profile` lists them newest first with a one-line summary and the time
they were taken. Restoring is a destructive write like any other: it is
behind "Unlock controls", it takes its own undo point first, and every knob
reports its own success or failure into the log. The delta table is written
**last** and wins, because the core offset and the delta table are the same
103 driver rows. A profile saved on a different card or VBIOS asks for a
second, deliberate confirmation before it is restored — 103 V/F points are
103 frequencies measured on *one* piece of silicon.

`Undo last write` restores the snapshot taken automatically immediately
before the most recent covered write. It is a write itself, so it takes an
undo point too — pressing it twice returns you to where you were. The
automatic snapshots are kept as a short ring and pruned; they are shown
dimmed in the list because they are not tunes anyone chose to keep. They are
ordered by a sub-second timestamp, so two writes landing in the same second —
a double-clicked `Apply` — still undo newest-first.

**Which writes take an undo point, and which don't.** Every write that
touches the 103-row VF delta table does, because its previous contents appear
nowhere on screen and cannot be reconstructed:

- `Apply to GPU`
- `Reset curve to stock`
- `Re-phase` — the one genuinely *lossy* write in the app. Off-phase deltas
  are rounded down onto the common phase and the original remainders are gone;
  `Reset curve to stock` zeroes the table, which is not the same as putting
  them back. Only a snapshot can.
- **the core-offset `Apply`** — it looks like a slider, but the core offset
  lands in that same delta table, so one drag and one click overwrites a
  hand-tuned curve.
- `Reset all to stock`, and restoring any profile.

The single-knob applies — **memory offset, power limit, voltage boost, fan** —
deliberately do *not*. Each moves one number that its own slider still shows,
and filling the ring with them would evict the curve snapshots nothing else
can reconstruct. The **clock lock** doesn't either, and couldn't: a profile
doesn't record it, so an undo point would silently fail to take it back —
`Release` / `Ctrl+H` is its undo.

**A snapshot that came back incomplete is not called an undo point.** The
capture can fail on exactly the field that matters (the V/F table read is the
one part that can refuse), which would leave a snapshot able to restore every
knob *except* the table it was taken to protect. When that happens the log and
the V/F status line say so in red, the profile row is labelled `INCOMPLETE`,
and the write goes ahead anyway — three of the covered writes only move the
card toward stock, so refusing to let you back out because a JSON file
wouldn't open is the wrong failure.

None of this is a "stock" baseline. It never claims to be factory state and
it is only ever written when something asks for it; `Reset all to stock`
remains the only thing that restores the factory curve.

### Hold this point (Ctrl+H)

This is TitanTune's answer to Afterburner's `Ctrl+L` curve lock. `Ctrl+H`
holds the selected V/F point; `Ctrl+H` again releases it. The hold is shown
as a green vertical line at the point's voltage on the plot and as a status
line on the Control tab, above the collapsible knob groups so it stays
visible whatever is collapsed. Both clear on release.

**It is built out of the per-domain V/F point lock** (NvAPI setter
`0x39442CFB` over the getter `0xE440B867`), *not* out of locked clocks. It
used to be the other way round, while that setter was unverified; it has now
been validated end to end on this card and is the better hold. See "V/F point
lock" below for the ids, the semantics and the measurements.

**It is a VOLTAGE request, so there is no snap-to-a-lockable-clock step.**
`Ctrl+H` asks the hardware to lock at the selected point's own voltage, and
the hardware resolves that itself. The old path had to snap the point's
*frequency* down onto the driver's lockable table (which tops out at 2160 MHz
while the curve reaches 2175); none of that applies here, because the lockable
table is not involved at all.

**The point actually held can still be BELOW the one selected, twice over,**
and the app never claims otherwise:

1. the lock resolves the request down to the highest V/F point *at or below*
   the requested voltage;
2. the boost arbiter then runs that point's frequency at the **lowest** voltage
   any point maps it to — the same flat rule that makes `peak_info` report a
   "park" point.

So selecting a point that is the upper half of a flat holds the lower half.
Measured: with idx 71 and 72 both at 1740 MHz, holding idx 72 (900.00 mV) put
the card on idx 71 at 893.75 mV. The status line and the log both name the
point the card is *really* on, and say what was asked for when the two differ
("asked for point 72 @ 900.00 mV, the card holds the point at or below it:
point 71 @ 893.75 mV, 1740 MHz"). When they agree, the line simply names the
point. If no point on the curve sits at or below the request, the app says it
cannot identify the point rather than guessing.

Four further details:

- **The point identity is exact; the MHz is a snapshot.** Point voltages are
  fixed on the 6.25 mV grid and never move, so the resolved index and voltage
  are always right. The *frequency* attached to a point is re-evaluated by the
  driver with temperature — a cool card was measured a whole 15 MHz bin above
  a warm one with all 103 deltas at zero — so the MHz in the banner is as fresh
  as the last `Read curve`. The resolution is deliberately done against the
  curve the plot is showing, so the banner and the picture cannot disagree.
- A point with an **unapplied editor edit** is noted in the log. Voltage is
  fixed by the VF table and the editor cannot move it, so the hold still lands
  on the intended point; what a staged edit changes is the frequency that point
  will deliver once applied, and until then the card runs the curve it has.
- Holding *and releasing* are both behind "Unlock controls", exactly like
  the `Release` button. Making one write path exempt would mean "read-only"
  no longer described the app; re-ticking the checkbox is always available,
  and a reboot clears the lock regardless.
- **Quitting tries to release it, and tells stdout if it couldn't.** Closing
  the window releases whatever lock the app is still holding, picking the call
  that matches the mechanism — `clear_vf_lock()` for a `Ctrl+H` hold,
  `nvmlDeviceResetGpuLockedClocks` for a `Clocks`-menu `Lock`. Releasing the
  wrong one returns OK and leaves the card pinned, which is why the app keeps
  one record saying *which*. A lock is the only write here that outlives the
  process, so leaving it behind would break this app's one standing promise:
  everything it does is reversible. A V/F point lock is the more expensive one
  to leave, because it is the one that holds the card in true P0.

  **When that release fails, the card stays pinned.** The usual cause is
  running without administrator rights, which is also the case where the lock
  should not have been takeable in the first place. The failure is *not* well
  signposted, and cannot be: the release runs after the render loop has
  exited, so no further frame is drawn — the log line it writes is never seen,
  and the only notice is a line printed to **stdout**, which a `--noconsole`
  build (`dist\TitanTune2.exe`) discards. The app's own record is kept honest
  either way — a failed release deliberately does not clear it — but the
  process is on its way out, so nothing is left to show it. If you locked
  clocks in a run that may not have had admin, re-run TitanTune as
  administrator and press `Release`, run `nvidia-smi -rgc`, or reboot; any of
  the three clears it. Launching from a console rather than the desktop
  shortcut is what makes the failure visible at all.

  This is the single write path not behind "Unlock controls",
  because it does not make a change — it takes back a change the app made
  while that gate was open, and gating it would mean unticking the checkbox
  mid-hold and quitting pinned the card with nothing left able to see it.

## Timings tab

Decodes the framebuffer-partition (FBPA) memory timing registers —
`CONFIG0`..`CONFIG5` and `TIMING22`, at `0x9A0000 + 0x290 + n*4` — and shows
what each field means in nanoseconds at the memory clock it was sampled at.

**It is read-only, and not merely by convention.** `nvtune` can write these
registers, and writing one can hang the machine and corrupt VRAM. TitanTune
therefore has no code path that can build a writing command line: `timings.py`
whitelists the read-only subcommands (`list`, `fields`, `dump`, `get`, `save`,
`probe`, `vbios`) and rejects `set`, `restore`, `apply`, `daemon`, `--commit`
and `--force` before a process is created. Nothing about the tab — no flag, no
disabled button, no dead branch — can change a timing.

**It needs two things, and says which one is missing.** `nvtune.exe` (looked
for beside TitanTune, then in `C:\Users\Administrator\Desktop\nvtune`, then on
`PATH`; override with the `TITANTUNE_NVTUNE` environment variable, which is
exclusive — a bad override fails rather than quietly running some other copy),
and its kernel driver service `nvtunedrv`, which maps the BAR0 FBPA aperture
(`sc start nvtunedrv`, elevated). With either absent the tab explains which,
and stays read-only regardless.

### A cycle count is not a time until you know its clock

This is the whole reason the tab exists. A timing register holds a **cycle
count**. The same registers read as nonsense at idle and as textbook GDDR6 at
P0. Measured on this card — same registers, three memory states, GDDR6 true
clock = the reported NVAPI/NVML figure ÷ 4:

| reported | true clock | RC | RFC | RAS | RP | CL | RD_RCD |
|---------:|-----------:|---:|----:|----:|---:|---:|-------:|
|   405    |   101 MHz  |  6 |  13 |   4 |  2 |  9 |      2 |
|   810    |   203 MHz  | 11 |  25 |   7 |  4 |  9 |      4 |
|  7428    |  1857 MHz  | 78 | 210 |  52 | 26 | 24 |     26 |

At 1857 MHz those are RC 42.0 ns, RAS 28.0 ns, RP 14.0 ns, CL 12.9 ns — real
GDDR6 numbers. The same RC of 6 cycles at 101 MHz is 59 ns, and reading the
idle registers *as if* they were the P0 ones is what made this feature's first
two dumps look like garbage.

So every capture **brackets the register read with a memory-clock read**, one
immediately before and one immediately after. If the two disagree the card
reclocked mid-capture, and the tab prints `clock moved — no ns` in every
nanosecond cell rather than a number: measured, a capture that straddled an
810 → 7428 reclock turned RC's 42 ns into 385 ns. The cycle counts are still
shown; only the conversion is withheld.

### RFC and WL do not convert

Both are shown as cycle counts with `encodes differently — not ns`, never as
nanoseconds:

- **RFC** — 210 cycles is 113 ns against a 240–350 ns GDDR6 tRFC. It is a
  multiplier, or its range splits with `TIMING22.RFCSBA`/`RFCSBR`.
- **WL** — 5 cycles is 2.7 ns. GDDR6 write latency is expressed *relative to
  CL*, not as an absolute delay.

Fields nvtune marks `[structural]` (`REFRESH_LO`, `DELAY0`+`_MSB`/`_HI`,
`OFFSET0..2`, `ADR_MIN`) are refused too — they are fragments of a value split
across bit ranges. So is `REFRESH`, which carries only the bits above
`REFRESH_LO`. `TIMING22`'s offset is marked INFERRED by the tool; its two
fields are drawn in amber with that note on hover.

### An idle capture is worthless. A P2 capture is not.

Timings are selected per **clock band**, not per P-state. Measured on this
card: the registers are **bit-identical** under a CUDA load at NVML mem 7228
(pstate 2) and under a 3D load at 7428 (pstate 0) — `CONFIG0` `0x1A68D24E`,
`CONFIG1` `0x45068298`, `CONFIG2` `0x771B0900`, `CONFIG3` `0x2200204C`,
`CONFIG4` `0xC0820025`, `CONFIG5` `0xD7D270F6`, `TIMING22` `0x12000009`, and
every decoded field (RC 78, RFC 210, RAS 52, RP 26, CL 24, WL 5, RD_RCD 26,
WR_RCD 16, CDLR 9, WR 27, FAW 16, RRD 4, REFRESH 4). The 50 MHz of true clock
between P2 and P0 does not cross a VBIOS timing band.

405 and 810 *do* program different, far slacker values. So the axis that
matters is **the band, not the P-state**: a capture at or above the
second-highest enumerated state (6801 here) is worth reading, and an idle one
is not. The tab shouts about idle captures and treats a P2 capture as what it
is — the same data a game would give you.

This identity is established **for reading**. If a write phase ever happens, a
bandwidth benchmark has to run in the state it claims to describe; measuring
throughput at P2 and reporting it as a P0 result would be a different error
that this finding does not excuse.

### Force vs. induce

**Force** would mean commanding the P-state through an API. **Not available on
this card.** `nvmlDeviceSetMemoryLockedClocks` returns `NVML_ERROR_NOT_SUPPORTED`,
and `nvidia-smi -lmc 7001,7001` fails identically ("Setting locked Memory
clocks is not supported for GPU 00000000:01:00.0") — two independent entry
points, one answer. Memory-clock locking does exist in NVML and works on
datacenter parts, so this is most likely a consumer-segment restriction rather
than a Turing architectural limit; that is *not* proven here and is not
claimed. The scope of the measurement is: not supported on **this card through
this driver path**.

**Induce** means creating the conditions under which the driver raises the
state itself, then watching what it decides. That works — and the state stays
the driver's to withdraw at any moment. **That is exactly why every capture is
bracketed** with a clock and P-state read on each side: a forced state could be
read at leisure, an induced one can drop out mid-read. Measured, a capture that
straddled an 810 → 7428 reclock computed RC as 385 ns instead of 42.

Nothing in TitanTune forces a memory P-state, because nothing can.

| lever | reaches |
|---|---|
| `nvmlDeviceSetMemoryLockedClocks` | **unsupported** on this card |
| `nvidia-smi -lmc 7001,7001` | **unsupported** — same answer, independent path |
| graphics clock lock alone | memory only 810 |
| **CUDA memcpy load** (built in) | **pstate 2, mem 7228**, ~450 GB/s traffic |
| 3D / graphics load | pstate 0, mem 7428 |
| `nvidia-smi -cc 1` | lifts the compute P2 cap (restore `-cc 0`, admin) — **not needed to read timings**, and not wired to any button |

The two top states are identified by the same arithmetic: **7228 − 427 = 6801**
and **7428 − 427 = 7001**, the identical memory offset on both. That is what
establishes 6801 as the P2 state and 7001 as P0 — and it is why the P0 test
uses the **P-state**, not the clock. 7228 is *above* the top clock the driver
enumerates (7001) while still being P2; a clock-only test calls that P0 and is
wrong. (The proof-of-concept this was built from made exactly that mistake.)

### Induce P-state (GPU load)

Runs a CUDA device-to-device memcpy load through `nvcuda.dll` — driver API via
ctypes, no toolkit, no compiler, no PTX, because a memcpy needs no kernel —
waits for the memory clock to settle, **captures while the load is still
running**, then stops it. Buffers are sized from free VRAM, the duration is
bounded, and the allocations and context are released in a `finally:` chain: a
leaked CUDA context would hold the card in a raised P-state after the load
ended, the same class of invisible leftover as a clock lock that outlives the
app.

This is a complete substitute for a game or benchmark when reading timings.

If the card is **already** at P0 the load is skipped and the capture is taken
directly — measured, opening a CUDA context on a P0 card pulls it *down* to P2,
so inducing there would cost you the state you already had.

**Auto-capture at P0** (armed by default) watches the memory clock on the
existing telemetry poll and captures the first time the card reaches P0 on its
own, and again on each re-entry after it drops out — one capture per entry.
Start a game, come back, and a genuine P0 sample is waiting. It is edge
triggered with the exit threshold one enumerated state below the entry one, so
a clock wobbling near the boundary cannot re-fire it every tick.

Running a GPU load is an ordinary workload. It writes no register.

### Capture, and the comparison that proves the decode

**Capture** files a snapshot under the memory clock it was taken at (a capture
that straddled a reclock is not filed — it has no single clock to be filed
under). Every capture is labelled with its memory clock *and* its P-state.
With two or more states captured, the comparison table shows each field's
cycle count at every state, the ratio between them, and — in the column
heading — what the **clock** did over the same states. Those two numbers
agreeing is the proof that the decode is real: RFC went 25 → 210 between the
810 and 7428 states while the clock went ×9.17.

Idle captures are kept and are useful *here*, even though they are useless as a
statement about performance: the cross-state ratio is exactly what verified the
decode. The comparison marks which captures are performance-relevant so the two
roles cannot be confused.

To get a second state: let the card idle until the memory clock drops (~810
reported) and press Capture, then press **Induce P-state** for a top-band
sample.

Verdicts are deliberately not pass/fail. `tracks` means the count moved by the
clock ratio; `flat` means it did not move at all, which is normal for mode and
bus-turnaround fields; `partial` means it moved by something else. Two things
make `partial` unremarkable: cycle counts are integers, so a 2-cycle baseline
rounds hard, and the VBIOS programs each p-state separately and *relaxes*
timings at low clocks (RC really is 42 ns at P0 and 59 ns at idle). The
comparison is judged in cycles, with a rounding allowance of ±1 cycle at each
end, rather than as a flat percentage on the ratio.

Per-partition rows appear **only if a partition disagrees** with the broadcast
aperture. All six are identical on this card, so the normal case is one line
saying so rather than six copies of the same table.

The field list, bit ranges and limits are parsed from `nvtune fields` at
runtime rather than hardcoded, so the decode cannot drift from the installed
tool. Raw `nvtune save` JSON is written to a per-process temp directory, never
into the repo, and is kept as the evidence behind each decode.

## Device report (menu bar: `Device`)

Only what the running program can read back from the driver, and nothing
that belongs in this file: device name, driver and VBIOS version, memory
type with its true-clock divisor, the core/mem offset ranges, the power
range, the supported clock range, and the backend status line. It is a
snapshot of what the driver said at startup, opened as a tool window from
`Device > Device report...`. **Copy device report** puts the whole block on
the clipboard, formatted for pasting into a bug report.

It also carries the one caution that matters at the moment of use rather
than at reading time: the offset sliders and the V/F curve are the same
delta table, and Afterburner writes it too, so drive clocks from one tool
at a time.

The hardware explanations (quantisation, phase, the two-knob voltage
mechanism, reversibility, footguns) live in this README only. They used to
be duplicated into the app as well, and the two copies drifted.

## Safety

**Reversible, no admin needed to read:** all Monitor-tab telemetry, and the
Timings tab (which cannot write at all — see "Timings tab"; its `nvtunedrv`
driver does need admin to *start*, but not to read through once running). The
Timings tab's GPU load is an ordinary CUDA workload: it makes the card busy for
a few seconds, writes no register, and releases its context in a `finally:`.

**Reversible, needs admin to write:** clock offsets (core/mem), power limit,
the NVML GPU clock lock, the V/F point lock, fan duty, voltage boost %, and V/F
curve edits. Every one of these resets on reboot, and `Reset all to stock`
walks them back without one. Writes are gated behind "Unlock controls", with
two exceptions, both of which only ever move the card toward stock: `Reset all
to stock`, and the release of this app's own lock when the window closes (see
"Hold this point").

**Deliberately not wired to a button** — documented here with the
commands to run them by hand, never fired blind by this app:

- Forcing P-state P0 (NvAPI `SetForcePstate`, `0x025BFB10`) — pins max
  clocks with no clean auto-release short of a driver reload.
- CUDA P2-cap removal (`nvidia-smi -cc 1`, restore with `-cc 0`). Not needed
  for the Timings tab — P2 and P0 program identical timing registers here.
- Driver-model TCC (`nvidia-smi -dm 1`, restore with `-dm 0`) — **drops
  display output** on this card; never run blind.
- Writing a memory timing register (`nvtune set`/`apply`/`restore`/`daemon`,
  or any `--commit`) — it can hang the machine and corrupt VRAM. The Timings
  tab reads these registers and is structurally incapable of writing one.
The per-domain V/F point lock (NvAPI `0x39442CFB`) **used to be on this
list** and has come off it. It is no longer unverified: it was validated end to
end on this card by the ladder in "The validation ladder" above — id resolves,
identity write accepted and byte-identical afterwards, then a single-field
read-modify-write that moved the card as predicted and reversed exactly. It is
reversible and volatile, and `Ctrl+H` now drives it. Everything else on the
list above stays off, for the reasons given.

This is research software written against one specific frankencard
(Titan RTX die, Turing TU102, on an ASUS RTX 2080 Ti Strix PCB, driver
591.44 at time of writing). Struct layouts, NVAPI ids, and the empirical
laws above were all verified against that hardware; nothing here should be
assumed to generalize to a different card without re-verifying.

## Build

```
pip install dearpygui
python -m PyInstaller --onefile --noconsole --name TitanTune2 --collect-all dearpygui titantune_dpg.py
```

Output lands in `dist\TitanTune2.exe`.
