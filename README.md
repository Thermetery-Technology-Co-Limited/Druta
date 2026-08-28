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

Two tabs, plus a menu bar:

- **Monitor** — read-only telemetry, refreshed from a background poll
  thread at 1 Hz.
- **Control** — the write knobs, plus the V/F curve editor in its lower
  half.
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
counter, fan duty/RPM, applied offsets, voltage boost %, VF-locked domains).

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
  clock domain. `Ctrl+H` in the curve editor drives this *same* driver-side
  lock (see "Hold this point" below), so the app keeps one record of what is
  locked and why.

  **What that record does and does not cover.** It covers the locks *this
  run of the app* took. Within a session, every lock action — `Lock`,
  `Lock max`, `Release`, `Ctrl+H`, and the release inside `Reset all to
  stock` — leaves the on-screen indicator agreeing with the driver, and a
  release that *fails* deliberately keeps the indicator up rather than
  clearing it. What the app cannot do is *discover* a lock:
  `nvmlDeviceGetGpuLockedClocks` is not available on this card, so there is
  no way to read the driver's lock state back, and a lock left by a previous
  run, by a killed instance, or by another tool is invisible here. An empty
  indicator therefore means "this app is not holding a lock", not "the card
  is not locked". `Release` (or `Reset all to stock`) clears such a lock
  anyway, even with nothing on screen naming it, and so does a reboot. On
  exit the app *attempts* to release a lock it is itself holding — see
  "Hold this point" below for what happens when that attempt fails.
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

**It is built out of locked clocks, not the hard VF lock.** The card is
pinned with `nvmlDeviceSetGpuLockedClocks(f, f)` at the selected point's
frequency, and the boost arbiter then supplies that point's voltage — the
same observable result as a curve lock. The alternative, NvAPI `0x39442CFB`
(per-domain hard VF lock), is deliberately *not* used: its write struct is
unverified against this card and it is rail-adjacent, so this app only ever
reads lock state through it (see "Deliberately not wired to a button"
below). `SetGpuLockedClocks` is documented, reversible, releasable in one
call, and proven to hold at idle here with no GPU load needed.

**A point's frequency is often not lockable, so the hold snaps DOWN.** The
lockable set and the V/F curve are unrelated mechanisms (see "Lockable
clocks are not a ceiling"): this card's table tops out at 2160 MHz while
the curve reaches 2175. The hold therefore takes the highest lockable value
*at or below* the point's frequency — never above, the same standing rule
as every other snap in this app, so a hold can lose a bin but can never
gain clock nobody asked for. It says so plainly in the log when it happens
("point 96 is 2175 MHz; held at 2160 MHz, the highest lockable value") and
repeats it in the status line. If no lockable value sits at or below the
point, the hold is refused rather than approximated upward.

Three further details:

- The frequency used is the point's **hardware** frequency, not its staged
  editor value. The arbiter reads the curve that is in the card, so a point
  with an unwritten edit would otherwise be held at a frequency that curve
  does not carry at that voltage. The log notes this when it applies.
- Holding *and releasing* are both behind "Unlock controls", exactly like
  the `Release` button. Making one write path exempt would mean "read-only"
  no longer described the app; re-ticking the checkbox is always available,
  and a reboot clears the lock regardless.
- **Quitting tries to release it, and tells stdout if it couldn't.** Closing
  the window calls `nvmlDeviceResetGpuLockedClocks` for whatever lock the app
  is still holding — a `Ctrl+H` hold and a `Clocks`-menu `Lock` alike. The
  lock is the only write here that outlives the process *and* the only one no
  later session could find again (nothing can read it back — see "GPU clock
  lock" above), so leaving it behind would break this app's one standing
  promise: everything it does is reversible.

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

**Reversible, no admin needed to read:** all Monitor-tab telemetry.

**Reversible, needs admin to write:** clock offsets (core/mem), power limit,
GPU clock lock, fan duty, voltage boost %, and V/F curve edits. Every one of
these resets on reboot, and `Reset all to stock` walks them back without one.
Writes are gated behind "Unlock controls", with two exceptions, both of
which only ever move the card toward stock: `Reset all to stock`, and the
release of this app's own clock lock when the window closes (see "Hold this
point").

**Deliberately not wired to a button** — documented here with the
commands to run them by hand, never fired blind by this app:

- Forcing P-state P0 (NvAPI `SetForcePstate`, `0x025BFB10`) — pins max
  clocks with no clean auto-release short of a driver reload.
- CUDA P2-cap removal (`nvidia-smi -cc 1`, restore with `-cc 0`).
- Driver-model TCC (`nvidia-smi -dm 1`, restore with `-dm 0`) — **drops
  display output** on this card; never run blind.
- The hard per-domain VF lock (NvAPI `0x39442CFB`) — the write struct is
  unverified against this hardware, so the app only reads lock state (shown
  on Monitor via `BoostLock`/`0xE440B867`) and never writes it. `Ctrl+H`
  ("Hold this point", above) gets the same observable result out of
  `nvmlDeviceSetGpuLockedClocks` instead.

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
