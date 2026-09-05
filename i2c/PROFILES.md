# Writing an I2C rail profile

Druta can drive a board's voltage regulator directly over the GPU's I2C bus.
That path has **no firmware underneath it** - the GPU does not see the change,
cannot refuse it, and will not compensate for it. A profile is therefore not a
convenience file. It is the thing standing between a slider and a dead card, and
it is read as **untrusted input** no matter where it came from.

This document is the format. The worked example beside it,
[`rtx2080ti-strix-mp2888a.toml`](rtx2080ti-strix-mp2888a.toml), is the one
board where every value below was measured rather than assumed.

---

## The three rules that shape everything else

**1. A profile is matched by MEASUREMENT, not by name.** PCI IDs say which
profiles are *candidates*. An identity read on the actual bus decides which one
is *used*. A profile with no working identity check can be loaded, but Druta
marks it unconfirmed and keeps it read-only.

Boards are the unit, not GPUs. The same GPU ships on many boards with different
regulators, and the same regulator appears under many GPUs. `RTX 3080` is not an
answer to "what is at address 0x20".

**2. Writes are whitelisted per register, never per device.** A profile lists
the registers it may write. Everything else is refused, including registers the
profile never mentions. There is no "write anything" mode and no way to ask for
one.

**3. A profile can never assert that it works.** It declares what a working
board *should* do; Druta runs the staircase on the actual hardware and only then
enables Apply. A profile that shipped with a wrong register still cannot drive a
rail, because the rail will not move and the verifier will say so.

---

## Why TOML

Comments. A register number with no note on where it came from is the problem
this format exists to prevent, and JSON cannot hold that note next to the value
it describes. `tomllib` is in the Python standard library, so this costs no
dependency, and Druta only ever **reads** these files - nothing in the app writes
one, so a profile is always exactly what a human put there.

---

## File layout

### `[profile]` - what this is and where it came from

```toml
[profile]
format = 1                      # this spec's version. Required.
name = "RTX 2080 Ti Strix - NVVDD (MP2888A)"
regulator = "MPS MP2888A"
rail = "NVVDD"                  # NVVDD | FBVDD | MSVDD | PEXVDD ...
author = "Thermetery"
```

### `[provenance]` - required, and it is not paperwork

```toml
[provenance]
datasheet = "MP2888A Rev. 1.1, 12/25/2018"
registers = "datasheet pp.34-37 (command table), p.43 (bit fields)"
address   = "measured: 0xBE reads 0xA0; datasheet Table 6 p.30 gives 20h for ADDR=0V"
behaviour = "measured on the authoring board under load, 2026-09-04"
```

Every register fact must be traceable to a **public** source or to a measurement
you took. Datasheets, manufacturer application notes, FOSS project source (state
the licence), your own bench results. **Do not source register maps from leaked
proprietary driver code.** A profile whose `provenance` is missing or says
"from a forum post" is still loadable, but say so honestly - somebody downstream
is deciding whether to trust it with their hardware.

### `[match]` - candidates only, never authority

```toml
[match]
pci_device  = ["0x1E02", "0x1E04", "0x1E07"]   # optional
pci_subsys  = ["0x12A310DE"]                   # optional
```

Omit both and the profile is offered for any card, which is fine for a widely
used regulator. Matching narrows the candidate list; `[identity]` decides.

### `[bus]` - where to talk

```toml
[bus]
port    = 1        # NVAPI port id
addr7   = 0x20     # 7-bit. Druta shifts left by one for the wire.
```

If a part's address is strap-selected and you are unsure, list `addr7_probe =
[0x20, 0x21, 0x22]`; Druta tries each and keeps the one whose identity passes.
It never keeps two.

### `[identity]` - the gate

```toml
[[identity]]
reg = 0xBE
bytes = 1
equals = 0xA0
note = "MFR_PMBUS_ADDR. Bit7=1, 3 MSB=010, 4 LSB=0000 -> 0x20; the part states
        its own address, so this both identifies it and confirms the strap."
```

Multiple `[[identity]]` blocks all have to pass. If a part has no ID register
(many uPI and Chil parts do not), use a **fingerprint**: several registers whose
combined values are distinctive.

```toml
[[identity]]
reg = 0x12
bytes = 1
equals = 0xBC
fingerprint = true      # marks this as a weak check, not a real ID register
```

A profile whose identity is fingerprint-only is flagged in the UI and its
staircase must pass before Apply unlocks - same as any other, but the label
tells the user what they are relying on.

### `[telemetry]` - read-only, and the honest readout

```toml
[[telemetry]]
key = "vout_mv"
reg = 0x8B
bytes = 2
encoding = "uint"
scale = 1.0
note = "DIRECT millivolts on this part, not LINEAR11 - matched the GPU's own
        reading exactly at 681 mV. Do not assume one encoding across commands."

[[telemetry]]
key = "iout_a"
reg = 0x8C
bytes = 2
encoding = "linear11"
```

`encoding` is one of `uint`, `int` (two's complement), `linear11`, `vid`.
`vout_mv` is **required** - it is what the verifier watches and what the UI shows
instead of the number on the slider.

### `[[write]]` - the whitelist, and the part to get right

```toml
[[write]]
key = "offset_mv"
reg = 0x23
bytes = 2          # TRANSACTION width on the wire
bits = "7:0"       # FIELD width inside it
encoding = "int"
lsb_mv = 6.25
raw_min = -111
raw_max = 112
note = "VOUT_OFFSET. bytes=2 and bits=7:0 are DIFFERENT NUMBERS and both matter."
```

> **`bytes` and `bits` are not the same thing, and conflating them is the single
> most likely way to write a broken profile.** On the MP2888A the command table
> gives `23h` a two-byte transaction, while p.43 shows bits 15:8 are reserved -
> "writes are ignored and always read as 0" - and only bits 7:0 hold the value.
> Druta originally masked this to 16 bits, so every negative offset went out as
> `0xFFxx`, the reserved half was dropped, and read-back refused the write.
> Undervolting was silently impossible until the datasheet was read properly.
> Give the transaction width in `bytes` and the field in `bits`, always.

`raw_min`/`raw_max` are the **representable** range in raw codes, and they are
not a safety opinion - they are what the field can hold. Past the MP2888A's +112
the low byte wraps through its sign bit, so a request for +800 mV becomes raw 128
-> `0x80` -> -128 -> **-800 mV delivered**. Druta refuses outside this range in
every mode, XOC included, because a clamp here would silently deliver a voltage
nobody asked for.

### `[limits]` - three tiers, and only the middle one is removable

```toml
[limits]
envelope_min_mv = -200.0    # default policy. XOC removes this.
envelope_max_mv =  100.0
rail_ceiling_mv = 1200.0    # default policy. XOC removes this.
sanity_max_rail_mv = 2000.0 # typo catcher. NOTHING removes this.
plausible_rail_mv = [400.0, 1300.0]
```

The tiers exist because they answer different questions:

| tier | question | XOC |
|---|---|---|
| representability (`raw_min/max`) | can the register even hold this? | **never removed** |
| envelope / ceiling | is this sensible for this cooling? | removed |
| sanity | is this a typo? | **never removed** |

The sanity ceiling exists because a vendor ring-0 tool in this class accepted
`14000` on a CPU rail and put it straight through. 14000 is 1400 with a slipped
digit, and no sub-zero run on any part needs 14 V.

### `[verify]` - the staircase this board should show

```toml
[verify]
rungs_mv = [6.25, 12.50, 25.00, 50.00, 75.00]
min_loaded_vout_mv = 800.0
expect_deadband_mv = 31.0    # optional, informational
```

`min_loaded_vout_mv` is the floor below which Druta refuses to render a verdict.
**Never characterise a regulator at idle.** Multiphase controllers shed phases
and change loadline under PSI/auto-phase, so an idle card is a different
regulator from the one that carries an overclock - the measurement will be both
noisy and *unrepresentative*, which is worse than noisy.

### `[[never_write]]` - board-specific hazards

```toml
[[never_write]]
reg = 0x04
why = "MFR_USER_PWD. One-shot: locks out PMBus writes until a power cycle and
       can be committed to EEPROM."
```

> Every repeating section here is an array-of-tables - `[[identity]]`,
> `[[telemetry]]`, `[[write]]`, `[[never_write]]` - and that uniformity is
> deliberate. A bare `key = [...]` written after a `[table]` header silently
> becomes part of *that table* rather than a top-level key, which is how the
> first draft of the example file put its `never_write` list inside `[verify]`
> and quietly disarmed it. Use `[[double brackets]]` and the mistake is
> unavailable.

This **adds to** Druta's built-in denylist, and cannot subtract from it. Any
register that commits to non-volatile storage is refused regardless of what a
profile says - `STORE_DEFAULT_ALL (0x11)`, `STORE_USER_ALL (0x15)`,
`RESTORE_*`, and known vendor password/EEPROM commands. A profile is untrusted
input; it must not be able to hand itself permission to brick a card.

---

## Checklist before you share one

- [ ] Identity read passes on your board, and **fails** on a board it should not
      match (test it if you have a second card).
- [ ] `bytes` is the transaction width; `bits` is the field. You checked both.
- [ ] `raw_min`/`raw_max` come from the datasheet's stated offset range, not from
      the field width. They are usually narrower.
- [ ] Verify passes under load, and the ladder is in your notes.
- [ ] Negative offsets read back correctly - that is where width bugs hide.
- [ ] `provenance` names a real source for every register.
- [ ] You have power-cycled and confirmed the rail returned to stock.

## What a profile still cannot do

Change the guard structure. The identity check, the read-back verification, the
dry run, the staircase, the sanity ceiling and the NVRAM denylist are Druta's,
not the profile's. A profile chooses *which* registers and *what* bounds; it
does not choose whether it is checked.
