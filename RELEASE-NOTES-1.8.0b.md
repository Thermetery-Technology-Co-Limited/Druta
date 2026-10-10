# Druta 1.8.0b

**Pre-release.** Supersedes [1.8.0a](RELEASE-NOTES-1.8.0a.md). 1.8.0b is that
build plus a less brittle uP9512R Verify reversal check, from issue 34. Ada
controls, the uP9512R adapter, and the scope limits in the 1.8.0a notes are
unchanged.

## uP9512R Verify

Verify still holds P0 and steps the offset by +10/+20/+30/+40/+50 mV, inside
50 mV and a 1200 mV feedback ceiling normally, or 150 mV and 2000 mV with XOC.
It stops at the first feedback rise of at least one 10 mV ADC count that is
larger than the observed peak-to-peak variation, then puts back the exact
entry bytes. Core and memory clocks are recorded and do not decide the result.
There is no CUDA load.

The restored median has to sit inside the baseline variation band and at least
one 10 mV count below the elevated median. It no longer also has to beat the
trial window's peak-to-peak spread. Issue 34's 617.42 run moved feedback
1100 -> 1140 mV against 30 mV of variation, came back inside the baseline band,
and was still inconclusive under that extra comparison. A quieter retry in the
same session passed at 1110 -> 1140 mV against 20 mV of variation. That
inconclusive run is not reclassified. A rise that does not clear the spread
still cannot confirm a write, and a restore window that never falls by one
count stays inconclusive.

Verify now logs the restored reading, the reversal, the baseline, and the
acceptance band.

On 2026-10-10 the same Manli RTX 4080 SUPER on driver 617.42, at low load,
confirmed the write path twice and restored entry bytes `00 00 00 20`. The
SMBus lock stayed open and no V/F lock was left held.

- Live voltage, idle feedback 930 mV. The +10 mV rung read 920 mV. The +20 mV
  rung read 940 mV, equal to the 10 mV spread, so it did not qualify. +30 mV
  did: 930 -> 950 -> 930 mV.
- A temporary 1100 mV V/F hold qualified at +10 mV with zero observed spread:
  1080 -> 1090 -> 1080 mV, reversal 10 mV.

These windows were quieter than issue 34, so they would also have passed the
1.8.0a rule. Feedback is the controller's own uncalibrated 10 mV ADC. This
establishes no gain and no safe voltage for any board.

## Validation and scope

The hardware-free regression suite passed **1,748 tests**, two more than
1.8.0a. The new cases cover a one-count reversal inside the baseline band and
a noisy restore window that never falls.

Not newly run on hardware: uP9512R Apply, Reset and profile replay from the
UI, and Max all on Ada. The 1.8.0a evidence still covers everything else in
those notes.

These results describe that card, driver 617.42, and those operating points
only. They do not establish limits, offsets, clock steps or physical
behaviour for another board, including another card of the same generation.

## Package

`Druta-1.8.0b-win64.zip` contains:

- the onedir `Druta` application
- its beside-EXE I2C profile folder
- a matching working-tree source snapshot under `source/`, including the
  evidence files these notes cite

`source/SOURCE-MANIFEST.json` records SHA-256 hashes for that source snapshot
and the packaged executable. Keep the extracted directory together; Python is
not required to run Druta.
