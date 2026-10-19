# Druta 1.8.1

Supersedes [1.8.0](RELEASE-NOTES-1.8.0.md). Fixes MP2888A control on the tested
ASUS Strix RTX 3070 Ti while preserving the recorded working offset method
for the TITAN RTX die fitted to an ASUS RTX 2080 Ti Strix PCB.

## Two MP2888A control methods

The I2C regulator panel offers two recipes for the same physical controller:

- **MP2888A (ASUS Turing)** retains the original register-23h GPU VID offset
  method, loaded Verify, Stock, limits, and saved-profile identity. It remains
  the default after discovery. Existing offset profiles use this method.
- **MP2888A (ASUS Ampere)** selects explicit manual PMBus control using the
  nominal register-21h voltage command and E1h mode bit. Commands use 6.25 mV
  steps; existing coarse/fine trims and unrelated controller fields are retained.

The labels name selectable methods. Discovery and capability validation do
not restrict either method to a list of board IDs. Selecting a method sends
no voltage write. Each method requires its own successful Verify.

The Ampere value is a **nominal command**, not a calibrated physical core
voltage. Controller-reported VOUT is shown separately. On the tested Strix,
the old offset register accepted writes without a measurable physical response
under GPU PWMVID control; explicit PMBus commands produced a reversible change.

## Manual control, restoration, and profiles

Before manual Verify or Apply, Druta acquires its own minimum graphics-clock
clamp and checks the actual clock. An unreadable lock or a lock owned by another
application refuses activation. Verify checks command response and reversal
while the graphics clock remains clamped; it does not establish stability at
an overclocked operating point.

Auto returns voltage authority to GPU PWMVID. Release, exit, and controller or
method changes restore captured controller state before releasing the owned
clock clamp. Failed restoration retains the clamp and pending recovery.
Controller protection, calibration, EEPROM, phase count, and switching frequency
are preserved.

Manual PMBus requests are session-only and are omitted from saved profiles.
Return to Auto before loading a profile. Legacy offsets keep their original
profile format and verified replay path; when the manual method is selected
in Auto, profile capture still retains an existing legacy offset.

## Validation and scope

The regression suite passed **2,076 tests**, including the original offset
transactions and profile identity, separate method verification, partial
capabilities, transient reads, ownership conflicts, and restoration failures.
The Turing method was checked against its recorded working transactions and
regression tests; a fresh physical TITAN RTX test was not available.

On this ASUS Strix RTX 3070 Ti, with an observed 405 MHz graphics clamp, the
production PMBus path and an independent meter gave:

| Stage | Nominal command | Controller-reported VOUT | Meter |
| --- | --- | --- | --- |
| A1 | 1100 mV | 1066 mV | 1.049 V |
| B | 1125 mV | 1091 mV | 1.074 V |
| A2 | 1100 mV | 1066 mV | 1.049 V |

This establishes a physical 25 mV change and return at that operating point.
It does not establish a universal voltage conversion or calibration. Nominal
requests in the working-path, production, and meter tests did not exceed
1125 mV.

The production controller/UI lifecycle harness also passed Verify at nominal
900/925/900 mV, with controller-reported 868/891/869 mV. Apply at
1100/1125/1100 mV reported 1066/1091/1066 mV. Recovery restored exact captured
controller state before releasing the owned clock clamp, and the final state
had no NVML or V/F lock. These harness checks exercised production methods
with rendering and persistence replaced by journal hooks; desktop screenshot
capture was unavailable, so they are not a packaged visual UI walkthrough.

See the [investigation and evidence](experiments/strix-3070ti-mp2888-20261015.md),
[production journal](experiments/validate_strix_mp2888_production.json), and
[meter journal](experiments/meter_strix_mp2888_production.json).

## Package

`Druta-1.8.1-win64.zip` contains the complete `Druta` application directory,
beside-EXE I2C profiles, and matching source under `source/`, including these
notes, the regression suite, and the cited evidence.
`source/SOURCE-MANIFEST.json` records SHA-256 hashes of the executable and
source files. A ZIP checksum sidecar is attached to the release.

Extract the complete directory and run `Druta.exe` from it. Keep its adjacent
files together; Python is not required to run the application.
