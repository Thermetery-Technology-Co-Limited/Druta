# Druta 1.8.0c

**Not published. Superseded by [1.8.0](RELEASE-NOTES-1.8.0.md).** The text
below is the note prepared for 1.8.0c. 1.8.0b remains
the less brittle uP9512R Verify build. 1.8.0c adds the commits below. Ada
controls are unchanged.

## Graphics V/F shape

Writing a core or memory clock offset keeps the graphics V/F shape. This
driver flattens the shared delta table on both calls. Druta snapshots the
graphics deltas and restores them after the offset write. Sending an offset
that is already stored does not call the driver again, so it cannot flatten
the table. Reset still clears the offsets without that restore.

## Pascal frequency sliders

On Pascal, frequency sliders whose measured clocks do not follow are hidden.
That covers the XBAR, SYS, and VIDEO controls. Additional Memory Clock stays.
The hide does not apply to Maxwell, Kepler, or later generations.

## EVGA RTX 3090 Ti Kingpin iCX

User-mode NVVDD and FBVDD requests for the EVGA RTX 3090 Ti Kingpin
(device `0x2203`, subsystem `0x49983842`), through the GPU's own NVAPI I2C
master. There is no private driver.

The commands were captured from RTX Classified Controller 3.1.9.0. A Druta
process has not sent them, so this does not claim the control works on a
card. The NVIDIA driver does not read the setpoint back, and GPU-Z will not
show it. A write persists until a power cycle.

NVVDD is 750 to 1150 mV. FBVDD is 1250 to 1450 mV. Both step by 5 mV. The
sliders start at 950 and 1300 mV, which were the last labels from the capture
session, not a reading. Apply is refused until that slider is moved. Apply
also requires the I2C-rail checkbox, Unlock, and Check iCX identity. Identity
re-reads the iCX firmware and does not require Unlock.

NVVDD Stock sends the captured Auto packet `09 00 96` and then the separate
`0E 01` trailer, once each. A manual Apply does not send that trailer. There
is no FBVDD stock command. Frequency, OCP, loadline, the init write, and the
save write are not sent. The voltage is not stored in a profile and is not
replayed at startup.

## AORUS GTX 1060

The read-only I2C survey of the AORUS GTX 1060 is recorded. No new control
is added for that card.

## Validation and scope

The hardware-free regression suite passed **1,774 tests**. The new cases
cover the iCX payloads and gates, the V/F restore, and the Pascal reset path.
The two discovery progress totals now count the Kingpin probe.

Not run from Druta on hardware: an iCX NVVDD or FBVDD write, NVVDD Stock, and
profile startup with a Kingpin installed. Do not treat the captured framing
as a confirmed Druta write.

These notes do not establish a safe voltage for any board.

## Package

`Druta-1.8.0c-win64.zip` contains:

- the onedir `Druta` application
- its beside-EXE I2C profile folder
- a matching working-tree source snapshot under `source/`, including these
  notes

`source/SOURCE-MANIFEST.json` records SHA-256 hashes for that source snapshot
and the packaged executable. Keep the extracted directory together; Python is
not required to run Druta.
