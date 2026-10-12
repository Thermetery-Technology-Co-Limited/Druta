# Druta 1.8.0

Supersedes [1.8.0b](RELEASE-NOTES-1.8.0b.md). The unpublished 1.8.0c notes are
folded in here. 1.8.0b remains the less brittle uP9512R Verify build. Ada
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

NVVDD and FBVDD requests use the GPU's NVAPI I2C interface. The controller
checks the current adapter's Ampere architecture, the live E251 controller
identity, the understood packet layout, and the selected operation's readback.
The tested device/subsystem IDs and firmware revision are evidence about the
test rig, not a compatibility allowlist.

The corrected protocol comes from black-box API captures of RTX Classified
Controller 3.1.9.0 on 2026-10-12. Before an acknowledged controller operation,
Druta checks B2, sends the captured enable packet if needed, and verifies that
the gate is enabled. NVVDD uses WriteEx register `10`; FBVDD uses register `18`.
Their four-byte payloads contain a little-endian 16-bit count in 5 mV units.
Register `19` returns the controller's requested state. There is no C8 voltage
command, packed ReadEx write, or separate voltage trailer in this protocol.

The gate comparison used an independent 64-bit client with identical 950 mV
requests. Under B2 `FC`, NVVDD measured 0.942 V. Under `FE`, the write returned
NVAPI success but the controller remained on Auto and the meter stayed at
0.911 V. Re-enabling alone did not apply that request; resending it under `FC`
worked. Firmware identification and state reads remained available under `FE`.

The panel distinguishes controller request readback from physical voltage.
Apply succeeds only after bounded reads confirm the selected request; an
immediate stale read or one transient read failure does not cause a second
voltage write. Driver success without matching readback is reported as an
unverified operation. Physical voltage still requires an external measurement.

**Auto applies to NVVDD only.** FBVDD remains independently adjustable while
NVVDD is on Auto. The Auto action sends `10 / 03 00 00 00`; it does not request
a manual 750 mV voltage or change FBVDD. FBVDD has no inferred Auto or stock
command. Its verified field is the returned count; the neighboring state byte
has unknown semantics and is not treated as a mode switch.

The existing ranges remain NVVDD 750-1150 mV (up to 1600 mV with XOC) and
FBVDD 1250-1450 mV, in 5 mV steps. Counts above 255 use the high byte; the old
one-byte rollover refusal was based on the discarded capture interpretation.
These ranges are input policy, not measured safe operating ranges.

Five additional controls now use separately captured setters and readback:

| Control | Choices | Write register | State register |
| --- | --- | --- | --- |
| NVVDD switching frequency | 300-1000 kHz, 100 kHz steps | `13` | `1C` |
| FBVDD switching frequency | 300-1000 kHz, 100 kHz steps | `1F` | `1C` |
| NVVDD OCP | Enable / Disable | `12` | `1B` |
| FBVDD OCP | Enable / Disable | `1E` | `1B` |
| NVVDD loadline | Level0-Level15 | `11` | `1A` |

Each two-byte setter carries `01` followed by the selected code: frequency in
100 kHz units, OCP `00`/`01`, or the loadline level. Paired state blocks preserve
the other rail's setting. The second loadline state byte remains uninterpreted
and is preserved. Unknown selected encodings refuse the operation; an unknown
sibling does not discard an independently understood control. Malformed packet
geometry or framing fails verification. Missing unrelated readback is reported
without hiding other available controls.

Row Apply requires the I2C-rail gate, Unlock, an identified controller, and an
intentional slider or setting edit. All Apply actions and NVVDD Auto retain the
graphics clock clamp. Reading identity or state does not enable B2 or move a
rail. Exiting the application releases the clock clamp but does not restore iCX
requests; use NVVDD Auto explicitly and confirm the other settings separately.

**iCX presets use a separate, explicit JSON workflow.** Save current iCX preset
captures complete controller readback, excluding unapplied row edits. Load iCX
preset validates the file and stages a full preview. Apply iCX preset performs
fresh checks, sends only changed settings through the normal verified setters,
and verifies complete final readback. Preset previews and row edits are kept
separate. Restore captured connection settings stages the first complete read
from the current connection; it does not claim factory defaults. Changing GPU
or controller clears the captured and pending presets.

Preset application stops at the first failed or unverified operation. Earlier
changes remain applied, and the failed operation may also have applied. There
is no automatic retry or rollback. Presets are never automatically replayed at
startup. Saving writes a JSON file, not controller EEPROM. The black-box
Classified Save action likewise produced a profile file without an observed
I2C write; no controller EEPROM-save command is established or implemented.

## Window

The unlock, XOC, I2C rail, rail limits, and reset strip stays pinned with
the Control, Monitor, and Timings selector. Scrolling a page does not move
either of them.

Dragging a tab label off the selector floats that page inside the same Druta
window. Dock back, or closing the float, returns the page. This does not
open a second taskbar window.

Trying an I2C or rail-limit control while its checkbox is clear flashes that
checkbox and the text box red and does not write.

## AORUS GTX 1060

The read-only I2C survey of the AORUS GTX 1060 is recorded. No new control
is added for that card.

## Validation and scope

The protocol captures and controlled gate comparison establish behavior on
one EVGA RTX 3090 Ti Kingpin and firmware. The final experiment restored
NVVDD Auto at 0.911 V, FBVDD at 1.349 V, B2 enabled, and the complete initial
voltage-state response. A later user observation confirmed an FBVDD 1.380 V
request changes the meter while NVVDD remains on Auto; no exact meter value
was recorded for that observation. The user subsequently confirmed that a
1295 mV NVVDD request from Druta produced 1.285 V on the meter, then promptly
returned NVVDD to Auto. That measurement applies to the tested card and
operating point; it is not a calibration constant. Cold-start defaults and
FBVDD's disabled-gate behavior were not tested.

The ancillary capture established the frequency, OCP, and loadline request
formats and matching controller readback on that same Kingpin. It did not
measure physical PWM frequency, test OCP trip thresholds, or measure the
loadline's electrical response. The observed ranges and levels do not establish
safe settings for another board. Classified's captured Default action returned
a changed NVVDD frequency from 400 to 300 kHz through its ordinary setter; Druta
instead offers restoration of the current connection's captured settings.

The completed integration passed **1,950 regression tests**. The packaged EXE
was also exercised through Save current, Load preview, individual row Apply,
Apply iCX preset, and Restore captured connection settings. Independent reads
confirmed that previews sent no writes and that application changed only the
selected settings.

A live harness using the production controller and preset code completed all
13 stages with **20 ancillary writes and zero voltage writes**. It exercised
both frequency setters, both OCP setters, loadline, a combined preset, captured
restoration, and an already-matching preset that issued no writes. All 612 I2C
calls were paired with successful returns. The complete initial state was
restored byte-for-byte: NVVDD Auto, FBVDD 1350 mV requested, both frequencies
300 kHz, both OCP settings enabled, and loadline Level0. The graphics-clock
clamp was released afterward. These are controller-readback checks, not
physical PWM, OCP-trip, or loadline measurements.

Ruff retains 456 existing findings with no additions. Pyright has 1,203
findings versus 1,191 in the saved baseline; the 12 additions repeat the
existing DearPyGui context-manager stub issue. No new substantive type
findings remain; the repository's static checks are not globally clean.

## Package

`Druta-1.8.0-win64.zip` contains:

- the onedir `Druta` application
- its beside-EXE I2C profile folder
- a matching working-tree source snapshot under `source/`, including these
  notes

`source/SOURCE-MANIFEST.json` records SHA-256 hashes for that source snapshot
and the packaged executable. Keep the extracted directory together; Python is
not required to run Druta.
