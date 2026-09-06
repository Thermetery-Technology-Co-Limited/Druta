# Adding a board to the I2C rail list

Druta talks to a board's voltage regulator through a **profile**: a TOML file
describing where the part is, how to identify it, what can be read from it and
- optionally - the one register that may be written. Profiles are data. The
guards are code, and a profile cannot weaken them.

This is the path we wish had existed when the first board was done by hand.

`PROFILES.md` is the field-by-field reference. This file is the workflow.

---

## Before you start

You need three things, and the third is the one people skip:

1. **The card**, in a machine where you can run Druta as administrator.
2. **A load.** Never characterise a regulator at idle. A multiphase controller
   sheds phases under light load and behaves like a different part - readings
   taken there have sent us down a blind alley more than once. Druta's own
   `Max it` or any sustained 3D load will do.
3. **The regulator's datasheet.** Not a datasheet for "the same family". Parts
   within one family differ in register width, encoding and offset range, and
   every one of those differences has already caused a real bug here. If you
   cannot get the datasheet, you can still contribute - see *telemetry-only*
   below - but you cannot contribute a write.

---

## Step 1 - survey the bus

```
python tools/i2c_discover.py
```

Read-only. It issues no I2C writes and has no code path that could. It walks
the ports, reports which addresses answer, and dumps the standard PMBus
identification and telemetry registers for each responder as a Markdown block.

**Finding nothing is a normal result.** On many boards the regulator's bus is
not connected to anything the GPU can reach. That is a property of the board,
not something a profile can work around, and the honest thing is to say so.

**An address that answers is not a regulator.** Fans, thermal sensors and
EEPROMs live on these buses too. And a card can carry several regulators, so
"something answered" is a long way from "this is the core rail".

## Step 2 - identify the part

This is your step, and it needs the datasheet. Match the bytes from Step 1
against candidate parts. Useful anchors:

- `MFR_ID` / `MFR_MODEL` (`0x99` / `0x9A`) where implemented - many parts
  don't, and return a bus error rather than a value. That is information, not
  failure.
- A register that states something *specific*, ideally the part's own address.
- `READ_VOUT` (`0x8B`) decoding to a number that tracks the GPU's own reported
  core voltage. This is the strongest single check available to you, because it
  ties the device to the rail rather than just to the bus.

**Do not identify a part by address alone.** On the reference board, fitting
the modification links made a *different* device start answering at the same
address. Address is where you look; identity is what you check.

## Step 3 - write a telemetry-only profile first

Copy `TEMPLATE.toml`, fill in `[profile]`, `[provenance]`, `[match]`, `[bus]`,
`[[identity]]` and `[[telemetry]]`, and **leave the `[[write]]` section
commented out**.

A telemetry-only profile is a complete, valid, mergeable contribution. It
cannot write - not by policy but by construction, because it names no register
to write - which makes it the safest thing to accept for a board nobody here
owns. It gets you a live rail readout in the UI, and it is the foundation any
later write work stands on.

Check it:

```
python -m unittest test_profiles
```

That runs without a GPU, so a reviewer gets the same answer you do.

## Step 4 - prove the telemetry

Under load, compare your decoded `vout_mv` against the GPU's own core voltage
reading at the same moment. They should agree closely. If they don't, one of
these is true and you need to find out which:

- the encoding is wrong (`uint` vs `linear11` vs a scale factor),
- the part you found is a different rail,
- it is not a regulator at all.

**Do not assume one encoding across commands.** On the reference part
`READ_VOUT` is direct millivolts while `READ_IOUT` on the same device is
LINEAR11.

## Step 5 - the write, if you're going there

Only with the datasheet in hand. Fill in `[[write]]`, and note two things that
have each caused a bug here:

- **`bytes` and `bits` are different numbers.** `bytes` is the width of the
  transaction on the wire; `bits` is the field inside it. On the reference part
  the offset is an 8-bit field inside a 2-byte transaction, and treating it as
  a 16-bit value made every *negative* offset silently fail.
- **`raw_min`/`raw_max` are the PART's documented range, not the field's.**
  They are often narrower. Past the top of the documented range the value can
  wrap through the sign bit and move the rail the *wrong way*.

Start `envelope_max_mv` low. Down is the safe direction. You can raise it once
you have measured the response.

Then run the staircase: request the smallest step, watch the *rail*, and grow
the step only until the board is observed to move. If nothing has moved by the
largest rung, the write is not working - report that, don't widen the rung.

---

## What your PR should contain

- the profile itself, in `i2c/`
- the **Step 1 output**, pasted - reviewers want the bytes you saw
- **how you identified the part**, with datasheet page numbers
- **your telemetry comparison** from Step 4: decoded value against the GPU's
  own, under load
- if you added a write: the **staircase result**, including the rung at which
  the rail first moved
- whether the board is **stock or modified**. If links had to be fitted, say
  so plainly - a profile for a modified board must never look like a stock one.

`python -m unittest test_profiles` must pass.

## What gets sent back

- **A guessed identity.** Worse than no profile, because it will be trusted.
- **Numbers copied from another board.** Including ours. The reference profile
  carries a measured deadband that is specific to that part and unexplained;
  copying it into another profile states something you did not observe.
- **Anything measured at idle**, or a profile with no evidence it was ever run
  under load.
- **A profile that tries to whitelist a denied register.** `STORE_*`,
  `RESTORE_*` and MPS `MFR_USER_PWD` commit to non-volatile memory or lock the
  part out permanently. The loader refuses this and so will we.
- **Provenance left blank.** Every line there answers "how do you know?". A
  reviewer who cannot retrace a number cannot approve it.

## What a profile still cannot do

It cannot make an unreachable regulator reachable, and it cannot make the GPU
aware of a voltage set behind its back. An I2C rail write does not pass through
the driver or the firmware, so nothing on that path can catch a mistake. That
is why this list is built out of measured, cited, board-specific facts rather
than plausible ones.
