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

Three tabs:

- **Monitor** — read-only telemetry, refreshed from a background poll
  thread at 1 Hz.
- **Control** — the write knobs, plus the V/F curve editor in its lower
  half.
- **Device** — driver-reported identity and ranges for this specific card,
  copyable as a bug report.

## Monitor tab

Tiles: core clock, XBAR clock, memory clock (converted to true MHz when the
memory type is known), edge temp, hotspot temp, power draw, vcore. Below
that: the full 9-reason clocks-event mask, the NVAPI perf-decrease bits
(including the insufficient-aux-power bit — a canary for the transplant's
power wiring), a GPU/board power split, per-domain utilisation, PCIe
link generation/width with AER error counters, and a state line (energy
counter, fan duty/RPM, applied offsets, voltage boost %, VF-locked domains).

## The XBAR clock domain

XBAR is read through the private `NvAPI_GPU_GetAllClocks` (`0x1BD69F49`) —
community docs call it "probably deprecated," but it answers on Turing
(status 0) and is the only user-mode path to domains the public clock getter
doesn't expose. Layout, verified on this card: a 1156-byte struct
(`version = 1156 | (2<<16)`), 288 dwords, stride 2 (`slot = 2 * domain`,
value in kHz); XBAR is slot 2 (`PRIV_SLOT["xbar"] = 2` in `nvbackend.py`).

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
- **GPU clock lock** — min/max MHz, `Lock` / `Release` / `Lock max`.
  See "Lockable clocks are not a ceiling" below; `Lock max` pins both ends
  to the top of the lockable table, which is useful for holding one
  frequency steady but can be a step *down* from what the card is boosting
  to. The lock holds at idle on this card with no GPU load needed, which
  makes it the cheap instrument for characterising a clock domain.
  `Ctrl+H` in the curve editor drives this *same* driver-side lock (see
  "Hold this point" below), so the app keeps one record of what is locked
  and why: taking a hold, or pressing `Lock` / `Lock max` / `Release`,
  always leaves the on-screen state agreeing with the driver.
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

The Device tab's "lockable clocks" figure comes from
`nvmlDeviceGetSupportedGraphicsClocks`, and it is easy to misread as the
card's maximum. It is not. It is the enumerated set of values
`nvmlDeviceSetGpuLockedClocks` will accept, and it is reported **per memory
clock** — on this TU102:

| memory clock | lockable graphics clocks |
| --- | --- |
| 405 MHz | 24 values, 300–645 MHz |
| 810 MHz | 121 values, 300–2100 MHz |
| 5001 / 6801 / 7001 MHz | 121 values, 360–2160 MHz |

The Device tab headline shows the top-memory-clock row (360–2160), so a lock
the driver would accept at one memory state can be refused at another.

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
until **Apply to GPU**, which previews the predicted old→new ceiling and
flags any point that lands off-prediction after the write. Points below the
cap are deliberately left untouched — ramping the low-voltage floor (many
points pinned at minimum clock) would make the card demand high clocks at
tiny voltages.

The plot itself: drag any dot (left-click grabs the nearest point by
voltage and moves it vertically — voltage is fixed by the table, so only
frequency moves, snapped to 15 MHz bins), A/D to step the selection, W/S to
nudge ±15 MHz (hold Shift for ×3), `Ctrl+H` to hold the selected point, or
the −75/−15/+15/+75 buttons / `Set MHz`
box for precise moves. Everything happens on a working copy; **Revert
edits** discards uncommitted changes, and re-reading the curve with pending
edits requires confirming twice so a refused write isn't silently lost.
**Reset curve to stock** zeros every point's delta (Turing's factory deltas
are 0, so there's no persisted baseline to poison); a reboot also clears all
deltas.

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

Two further details:

- The frequency used is the point's **hardware** frequency, not its staged
  editor value. The arbiter reads the curve that is in the card, so a point
  with an unwritten edit would otherwise be held at a frequency that curve
  does not carry at that voltage. The log notes this when it applies.
- Holding *and releasing* are both behind "Unlock controls", exactly like
  the `Release` button. Making one write path exempt would mean "read-only"
  no longer described the app; re-ticking the checkbox is always available,
  and a reboot clears the lock regardless.

## Device tab

Only what the running program can read back from the driver, and nothing
that belongs in this file: device name, driver and VBIOS version, memory
type with its true-clock divisor, the core/mem offset ranges, the power
range, the supported clock range, and the backend status line. **Copy
device report** puts the whole block on the clipboard, formatted for
pasting into a bug report.

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
Writes are gated behind "Unlock controls"; `Reset all to stock` is the one
exception, since it only ever moves the card toward stock.

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
