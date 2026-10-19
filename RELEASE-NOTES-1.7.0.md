# Druta 1.7.0

Druta 1.7.0 adds two things above all: rail-limit, clock-domain and
core-current controls on Ampere, and voltage-limit headroom that keeps a held
V/F point off the voltage ceiling, so it runs the clock it shows. It also
shows every power policy as a slider, and shows the measured clock under each
clock tile on the Monitor tab.

## Ampere controls

Before this release, Ampere stayed fail-closed: Druta never read its live
control packets, so its rail limits and private clock sliders never appeared.
Druta now validates those packets on the card in front of it. Generation is
the basis for support; the card's own getter layout must still match. There
is no device-id or driver-version allowlist.

- **NVVDD rail limits** use the recognized 1104-byte `0x2080B213` /
  `0x2080F214` packet. The MSVDD rail limits stay hidden where the card
  rejects their mask.
- **Clock-domain sliders** read the Turing control-block geometry
  (`0x124 + n * 0x304`, frequency at `+0x10C`). On the measured card the
  sliders are Crossbar, SYS, Video, Additional memory and **LTC?** (the Turing
  LTC role, marked as inferred). On that card LTC? moved only downward, for
  requests of -45 MHz or more; a request to raise it, or one of -15 MHz, was
  stored with no effect.
- **Core current** is power policy 13. It is used only when the card's own
  info block reports that policy's expected record type. The info, status
  and control buffer sizes are chosen by which info GET the adapter accepts,
  not by driver version.
- **GDDR6X memory slider: new unit.** On any card that reports NVAPI memory
  type 15 (GDDR6X), the memory slider now reads the true megahertz GPU-Z
  shows, one eighth of the "MHz eff" number 1.6.0 showed for the same offset.
  Retype offsets you set by hand. Profiles, undo points and sign-in profiles
  from 1.6.0 replay the same driver units as before.

Measured on one RTX 3070 Ti (GA104, driver 595.97, VBIOS 94.04.5a.00.56). Every
write was checked by readback and restored. Another Ampere card has to return
the same getter layout before these controls appear on it. Evidence:
`experiments/ampere-ga104-3070ti-20260928.md`.

## Voltage-limit headroom for V/F holds (#29)

A V/F point held (Ctrl+H, Max it) on the effective voltage ceiling runs below
the clock it shows. The ceiling is the lowest of reliability plus its boost
contribution, alt-reliability and overvoltage. GPU-Z and Druta's header both
show the programmed clock, so the loss is invisible there. Two cards were
measured on the ceiling:

- One TITAN RTX lost 15-28 MHz, depending on the held point.
- One RTX 3070 Ti lost one 15 MHz step, steadily, at 99 % load.

25 mV of headroom removed the loss on both.

- While a V/F hold is active, Druta raises the ceiling terms that sit below
  hold + margin to exactly hold + margin. It restores them before the lock is
  released.
- The setting is **Keep headroom above a held point (strongly recommended)** in
  the Clocks menu. It is **on by default**, with a **25 mV** margin.
  - 25 mV is a fixed safe margin for every card. It is not tuned per card or
    generation, and you can change it (6.25-100 mV).
  - This is the one rail-limit write that does not need the **Rail limits**
    box. Untick Keep headroom to stop Druta from writing rail limits during
    holds. With **Unlock controls** unticked, Druta makes no new raise, but
    still takes one off.
  - **Restore raised limits now** takes the raise off by hand.
- The held point's voltage stays the same. The rail itself may run up to the
  margin higher: up to 18.75 mV on the TITAN RTX, the full 25 mV on the RTX
  3070 Ti.
  - A point held above the current ceiling gets no raise, only a warning.
- Only the three ceiling terms are written: upward only, to exactly hold +
  margin, inside the 1200 mV bound (1500 mV with XOC). Every write is checked
  by readback, and two holds never stack their raises.
- **Live check:** while a raise is active, the live rail is checked on every
  poll. If it reads above the raised ceiling by more than this card's own
  allowance, the raise comes off. The allowance is the card's own live-reading
  offset plus half its voltage step (6.25 mV is assumed when its V/F curve
  cannot be read).
  - If another tool replaces the V/F lock, the raise also comes off.
- **Crash tracking:** a marker file is written before the limits move (not on
  a card that reports no UUID). If Druta dies during a raise, the next session
  reports it and offers a GPU device restart (PnP), which is not proven on
  every GPU. It never writes old values back by itself.
- **Clock check:** the log reports a GPU that runs below the clock it shows,
  and its recovery. It follows the Monitor tab's judgement, described below.

Evidence: `experiments/hold-headroom-titan-rtx-61088-20260929.md`,
`experiments/measured-clock-sources-titan-rtx-61088-20260929.md` and
`experiments/hold-headroom-rtx-3070ti-59597.md`.

## Measured clock on the Monitor tab

- The core, XBAR and memory tiles still show the programmed clock as their big
  number. Under each one, a new line shows the measured clock and its gap, for
  example `measured 2087  Δ -27.9`.
- The measured figure is the second private clock array (B) of the same row.
  Each card has to earn the word "measured" from its own readings, with no
  list of cards:
  - B must turn back on its own twice (up after down, or down after up) while
    the programmed clock holds still.
  - A B equal to the programmed clock shows `measured: same as A`. Once B has
    followed it exactly through a change, it shows `measured: none (B = A)`.
  - A B that has not shown those turns is `unproven`.
- **Only the core line is coloured:** plain within one clock bin, amber at one
  bin or more, red at three or more, in either direction.
  - A reading is only judged at load, with a steady programmed clock.
  - The colour comes from the median of up to 15 readings, in this card's own
    clock bins.
  - A colour once reached is held until the gap clearly clears, so it does not
    flicker on an edge. The tooltip says when a colour is held.
  - XBAR and memory are shown, but not coloured.
- If the private read fails, each tile keeps its last reading as `last …` for
  up to 3 s, then shows `measured: n/a`.
- The clock tiles' subtitles no longer clip.

## Power policies

- The Control tab has a new **Power policies** section, collapsed by default.
  It lists every policy in the driver's power-policy table as a slider,
  bounded by the card's own minimum and maximum. The board limit and the
  named current policies keep their existing sliders, and appear here as
  read-only mirrors.
- On both measured cards (TITAN RTX, RTX 3070 Ti), every board-limit write
  made the driver recompute several policies from the board limit,
  overwriting values set by hand. **Values set by hand are now kept:** they
  are applied again after every board-limit write Druta makes, and named in
  the log. They are marked `*` while the card still holds them; another
  tool's board-limit write can recompute them until Druta's next write.
  - **Stock** hands a policy back to the driver.
  - **Max all** raises the board limit and every other writable policy to its
    maximum.
  - **Reset controls** returns everything to default.
- **Channel notes:** you can label each channel with its connector (PCIE 8pin,
  PCIE slot, EPS 8pin, 12VHPWR/12V-2x6, or free text).
  - Current rows on a noted cable are coloured by the current per 12 V wire.
    The PCIe slot is judged as a whole.
  - Notes are saved per card and in profiles.
- **Profiles** save every writable policy and the values set by hand. On
  restore, each saved value is matched against the live table (channel,
  record type, range), not against the driver version. A value that no longer
  fits is reported on its own, and the rest of the profile is still restored.
  A profile from before 1.7.0 leaves this session's values set by hand alone.
- The one warning: "Beware of your PSU's rating as you change power limit."

## Validation and scope

The hardware-free regression suite passed **1,538 tests** on the release code.
Hardware validation, every write restored:

- **TITAN RTX (TU102), driver 610.88**, on each change's own branch:
  - holds on the ceiling, the headroom raise, a re-plan, restore and release
  - the Monitor tab's measured line under GPUPI load
  - the power-policy table, values set by hand, Stock, Max all, and a profile
    restore (runs recorded in PR #31)
- **TITAN RTX, on the release code itself**
  (`experiments/release-1.7.0-smoke-titan-rtx-61088.md`):
  - a hold with the headroom raise, restore and release under GPUPI load,
    with the measured line drawn
  - a hold with a power policy set by hand while the board limit changed: the
    driver recomputed it, Druta put it back, and the raise was untouched
  - the application's full loop on both tabs
- **RTX 3070 Ti (GA104), driver 595.97**, on the builds of those changes
  before they were combined:
  - the Ampere rail, clock-domain and core-current controls
  - the power-policy write and recalculation test (recorded in PR #31)
  - the headroom test: 15.0 MHz below the shown clock, 0.2 MHz with headroom,
    15.0 MHz again once restored

Headroom is on by default on every generation with rail-limit
control: Pascal, Turing, Ampere and Blackwell. It was not measured on Pascal or
Blackwell, and the Power policies section and Max all were not run on
Blackwell.

Those figures describe those two cards, drivers and operating points only.
They do not establish limits, offsets, clock steps or physical behaviour for
another board, including another card of the same generation.

## Package

`Druta-1.7.0-win64.zip` contains:

- the onedir `Druta` application
- its beside-EXE I2C profile folder
- a matching working-tree source snapshot under `source/`, including the
  evidence files these notes cite (the power-policy runs are recorded in PR
  #31, not in an evidence file)

`source/SOURCE-MANIFEST.json` records SHA-256 hashes for that source snapshot
and the packaged executable. Keep the extracted directory together; Python is
not required to run Druta.
