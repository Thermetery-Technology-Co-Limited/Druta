# Druta 1.10.1a

This native x64 prerelease is pending hardware verification on Turing with older
400-series NVIDIA drivers and on higher-end Pascal cards beyond the GTX 1050 Ti.

## Changes since 1.10.0

- The Classic profile bar adds five assignable quick slots and direct Apply and
  Save actions. Save remembers the capture type and destination across launches.
  Profiles can also be configured to apply at Windows sign-in.
- The Classic V/F editor shows pending writes once you make an edit. Selecting
  another GPU during a V/F hold opens a second window and leaves the hold in the
  original one.
- Driver fixes cover Pascal and Ampere clock and voltage requests, extended
  ClientPower, and BIOS power relationship Apply and Restore across the tested
  driver layouts.

## Hardware verification

The driver matrix in [PR #99](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/99)
covers 14 GPU and driver combinations on the GTX 1050 Ti and RTX 3070 Ti. It
recorded 56 tuning round trips, 14 BIOS Apply and Restore cycles, and two
extended ClientPower round trips. See the [issue ledger](../experiments/native-driver-gpu-issues-20261126.md)
for the driver versions, results, and 431.36 recovery and retest.

The older-driver Turing run and the higher-end Pascal run are required before
publishing. The TITAN RTX 431.36 results in the [1.10.0 notes](RELEASE-NOTES-1.10.0.md)
apply to that release.

## Package checks

The draft build passed all 171 native tests on Windows. The four packaged
binaries passed the XP x64, Vista x64, and Windows 7 x64 import audits.
The extracted package passed all 23 GUI smoke phases with fixture data on a
Windows 10 host. The guest check also verified all four binary hashes and
the native library ABI.

## Running the build

Extract **Druta-native-legacy-x64-1.10.1a.zip** and run `druta-ui.exe`. The
separate **Druta-legacy-x64-1.10.1a-guest-check.zip** contains the Windows guest
validation utility. **SHA256SUMS.txt** contains the checksums for both ZIPs.
