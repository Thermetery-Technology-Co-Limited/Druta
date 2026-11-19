# Druta 1.5.1

Druta 1.5.1 is the next normal release after 1.4.1. It incorporates the
runtime voltage-rail and opt-in I2C discovery work previously published in
the 1.5.0a prerelease, and adds bounded recovery for transient clock-control
reads.

## Clock capability recovery

- If an initial clock capability read is incomplete because a clock domain,
  layout validation, or current request read is temporarily unavailable, Druta
  keeps the controls that were already positively validated and retries the
  missing capability read-only after 1 second, then 3 seconds later.
- A recovered domain is inserted into the existing UI without rebuilding it or
  discarding staged values. Recovery waits for an active edit to finish so it
  does not interrupt that edit.
- Native clock writes remain subject to the existing strict packet, selected
  field, exact request-readback, and restoration checks. The retry path does
  not write controls.
- When the retry budget is exhausted, use **Device > Refresh capabilities** to
  try again.

## Inherited from 1.5.0a

Runtime voltage-rail discovery uses NVAPI architecture detection when NVML is
unavailable and validates the current adapter's reported layouts instead of
using device, VBIOS, or driver allowlists. I2C controller discovery remains
read-only, explicitly opted in, and shows lazy scan progress; it does not run
at launch or merely because a GPU is selected.

## Validation and scope

The latest hardware-free regression suite passed **1,010 tests**. Six live
Dear PyGui cases on a local TITAN RTX with driver 580.97 used read-only fault
injection into clock GET responses, then exercised the real driver GET and
the live UI. They covered partial and empty responses, staged curve edits,
retry-budget exhaustion with **Refresh capabilities**, and current-request failures.
All recovered missing XBAR and SYS rows without writes, I2C scans, loss of
staged settings, or a UI rebuild. This is injected-failure evidence, not a
claim that a natural driver failure was reproduced. The detailed record is
[`experiments/clock-capability-retry-20260916.json`](https://github.com/Thermetery-Technology-Co-Limited/Druta/blob/1.5.1/experiments/clock-capability-retry-20260916.json).

Existing local TITAN RTX clock sanity evidence on drivers 566.36 and 580.97
measured XBAR `1440 -> 1470 -> 1440` and SYS `1500 -> 1530 -> 1500`, including
restoration. Inherited voltage-rail checks on the local TITAN Xp covered
drivers 472.12, 566.36, 580.97, and 582.66 and were scoped to that board.
Neither set of observations establishes behavior on other adapters; GTX 1060
has no direct validation evidence for this release.

## Package

`Druta-1.5.1-win64.zip` contains the `Druta` directory, including `Druta.exe`
and its adjacent `_internal` directory. Python is not required to run the
package. The archive includes matching source and a SHA-256 source manifest.
