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

User-mode NVVDD and FBVDD requests for the EVGA RTX 3090 Ti Kingpin
(device `0x2203`, subsystem `0x49983842`), through the GPU's own NVAPI I2C
master. There is no private driver.

The commands were captured from RTX Classified Controller 3.1.9.0. On
2026-10-11 a Druta process sent 950 mV twice, with Classified closed. Both
calls returned NVAPI status 0 and named command `0xC8`, bytes `09 01 BE`.
The port-1 capture of that window did not show those bytes, and it did not
show Classified's address word `0x0000C82D`. After the firmware read, the
write used address word `0x0006402D` and the data dword stayed 0. NVAPI
status 0 is not evidence the MCU accepted the setpoint. There is no voltage
readback. Do not treat the panel's sent line as confirmation.

The NVIDIA driver does not read the setpoint back, and GPU-Z will not show
it. A write that does land persists until a power cycle.

NVVDD is 750 to 1150 mV, or 750 to 1600 mV with XOC. The captured command is
one byte, millivolts divided by 5, so 1275 mV is the highest value that byte
can hold. Apply from 1280 through 1600 raises before any write and sends
nothing. 1200 mV encodes `09 01 F0` and is accepted only with XOC. It was
not sent. 1600 mV was not sent.

FBVDD is 1250 to 1450 mV. Both step by 5 mV. The sliders start at 950 and
1300 mV, which were the last labels from the capture session, not a reading.
Apply is refused until that slider is moved. Apply also requires the
I2C-rail checkbox, Unlock, and Check iCX identity. Identity re-reads the iCX
firmware and does not require Unlock.

NVVDD Stock sends the captured Auto packet `09 00 96` and then the separate
`0E 01` trailer, once each. A manual Apply does not send that trailer. There
is no FBVDD stock command. Frequency, OCP, loadline, the init write, and the
save write are not sent. The voltage is not stored in a profile and is not
replayed at startup. No Druta process has sent Stock.

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

The hardware-free regression suite passed **1,778 tests**. The cases
cover the iCX payloads and gates, the one-byte XOC refusal, the V/F restore,
the Pascal reset path, the pinned pages, and the unchecked-box flash.

Not confirmed on this Kingpin: a Druta iCX write that matches Classified on
the wire, NVVDD Stock, FBVDD, and any NVVDD request above 1150 mV. The 950 mV
attempt above is the Druta write that was listened to.

These notes do not establish a safe voltage for any board.

## Package

`Druta-1.8.0-win64.zip` contains:

- the onedir `Druta` application
- its beside-EXE I2C profile folder
- a matching working-tree source snapshot under `source/`, including these
  notes

`source/SOURCE-MANIFEST.json` records SHA-256 hashes for that source snapshot
and the packaged executable. Keep the extracted directory together; Python is
not required to run Druta.
