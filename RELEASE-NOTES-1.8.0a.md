# Druta 1.8.0a

**Superseded by [1.8.0b](RELEASE-NOTES-1.8.0b.md).** The 1.8.0a package is unchanged.

**Pre-release.** Druta 1.8.0a opens tuning controls on Ada (RTX 40 series)
and adds voltage offsets for the uPI uP9512R I2C controller. Everything
Ada-specific here was measured on one card: a Manli RTX 4080 SUPER (AD103) on
drivers 617.42 and 581.42. It is released for testing on other cards.

## Ada controls

Druta used to stop several Ada controls at the generation gate. It now opens
them on Ada once the card in front of it passes the same packet, field and
capability checks as other generations. There is no device-id or
driver-version allowlist.

- **Clock-domain sliders:** Crossbar, SYS and Video, whose names match GPU-Z
  on the tested card, plus Additional Memory Clock and **LTC?** (the LTC name
  is inferred). On that card, other domains stored requests with no observed
  effect, so they get no slider.
- **NVVDD offset and rail limits:** reliability, alt-reliability, overvoltage
  and the minimum voltage. The MSVDD (rail 1) getters rejected requests on the
  tested card.
- **Core current:** power policy 13. Driver 617.42 uses a larger
  current-policy packet (a 20512-byte info GET). Druta picks it because the
  card accepts that GET, not from the driver version. It is tried after the
  older layouts on any generation with current-policy support.
- **Power policies and Max all** are now open on Ada. The owner set the policy
  sliders by hand on the tested card. There is no recorded log of that, and no
  recorded Max all run.
- **V/F hold headroom** is now on by default on Ada too, since Ada now has
  rail-limit control. The clock loss it corrects was measured on a TITAN RTX
  and an RTX 3070 Ti, not on Ada. On the tested card, the 25 mV raise (925 mV
  ceilings for a 900 mV hold) was applied and restored on both drivers.
- **Memory timings:** nvtune reads Ada timing registers, but the one write
  tried (RFC 460 to 461) failed readback on both drivers. Timing writes are
  not claimed to work on Ada.

Core and memory offsets, power limit, fans, voltage boost, V/F edits and holds
and the NVML clock lock also passed readback and restoration on the tested
card.

## uP9512R I2C voltage offsets

The uP9512R holds five positive offsets, one per load-current state, plus an
enable bit. The offsets are volatile, so a power cycle clears them. Druta now
drives them through a built-in adapter.

**Verify and Apply make real VRM writes.** I2C voltage control stays off until
you tick **I2C rail**.

- **Offset (all load states)** sets one offset for all five states, in 10 mV
  steps: 0-50 mV normally, 0-150 mV with XOC. A readback line shows the five
  programmed values and whether they are enabled.
- On Ada, each write is a register/value request sent through `I2CReadEx`,
  and every control byte is then read back. Both tested drivers rejected the
  conventional `I2CWriteEx`. Other generations keep `I2CWriteEx`. A live
  architecture query picks the route, not a board or driver list.
- Druta never writes the SMBus lock register (0x39). A locked controller is
  read-only.
- **Verify** is required once per session before Apply. It holds P0 and steps
  the offset up by +10/+20/+30/+40/+50 mV, inside 50 mV and a 1200 mV
  feedback (FB) ceiling normally, or 150 mV and 2000 mV with XOC. It stops at
  the first FB rise above noise, puts back the exact entry bytes, and then
  needs FB to come back down. Core and memory clocks are recorded but do not
  decide the result, and it runs no CUDA load.
- On the tested card, Verify passed at +40 mV on both drivers: FB 1130 ->
  1150 -> 1130 mV on 617.42 and 1120 -> 1150 -> 1110 mV on 581.42, at low load
  with a 1100 mV hold. FB is the controller's own uncalibrated 10 mV ADC. This
  establishes no gain and no safe voltage for any board.
- **Apply, Reset and profile replay were not run from the UI on hardware.**
  They share the transaction code that Verify's writes ran through, and have
  mocked coverage.
- **Reset** zeroes and disables all five offsets. That can only lower the
  request, so Reset does not need a prior Verify.
- Profiles and undo points save all five offsets and the enable bit. A
  locked controller is not captured. A saved state that already matches the
  controller writes nothing and needs no Verify.

## Changes on every generation

- **Full scan** now also probes the uP9512R identity on every address of
  ports 0-7: 896 more single-byte reads, about a third more probes, and no
  writes. A board that answers as both a uP9512R and another controller now
  lists both and needs you to pick one. Connect / Scan with a remembered route,
  or with one named controller selected, is unaffected.

## Fixes from the PR #33 review

These came after the hardware runs above and have hardware-free tests only.

- A session that started at the normal +50 mV maximum could not be reset:
  Verify had no room for a rung, so it wrote nothing and Reset stayed locked.
  Reset no longer needs Verify.
- A locked controller was saved into every undo point, and Undo then refused
  the whole snapshot. It is no longer captured, and an I2C entry that already
  matches no longer blocks the rest of an undo or profile.
- Reset all took a second undo point after the GPU reset, so Undo did not
  return the tune Reset all discarded. It now takes only its own, unless its
  own came out incomplete.
- One failed read at the start of Verify's restoration left the trial offset
  applied, and one failed identity read refused Apply or Reset and cleared
  Verify. Every read after identification, including the identity re-checks,
  is now retried up to three times, and restoration up to three times. The
  independent readback, not a write call's status, decides whether a restore
  worked. An unreadable controller is reported apart from an identity change.
- The "Offset restore failed" status now clears after a confirmed Reset,
  Apply or Verify. The session is still marked unclean.
- uP9512R Verify no longer needs nvcuda.dll.

## Validation and scope

The hardware-free regression suite passed **1,746 tests**.

Hardware validation, every write restored, on one Manli RTX 4080 SUPER
(10de:2702, subsystem 41401458, VBIOS 95.03.44.40.09), drivers 617.42 and
581.42:

- core and memory offsets, power limit, fans, voltage boost, V/F point and
  hold, NVML clock lock
- the clock-domain requests, NVVDD offset and all four NVVDD limit fields,
  and policy 13 at 400 / 399 / 400 A
- profile replay with the 25 mV hold headroom
- uP9512R Verify after Max it

During testing, two driver switches to 581.42 were each followed by an
unexpected PC shutdown. The cause was not established; see
`ADA-VALIDATION.md`.

Not run on hardware: the review fixes above, uP9512R Apply, Reset and profile
replay from the UI, and Max all on Ada.

These results describe that card, those drivers and those operating points
only. They do not establish limits, offsets, clock steps or physical
behaviour for another board, including another card of the same generation.

Evidence: `ADA-VALIDATION.md`, `i2c/UP9512R.md`,
`experiments/ada-ad103-validation.json` and
`experiments/ada-up9512r-product-2026-10-09.json`.

## Package

`Druta-1.8.0a-win64.zip` contains:

- the onedir `Druta` application
- its beside-EXE I2C profile folder
- a matching working-tree source snapshot under `source/`, including the
  evidence files these notes cite

`source/SOURCE-MANIFEST.json` records SHA-256 hashes for that source snapshot
and the packaged executable. Keep the extracted directory together; Python is
not required to run Druta.
