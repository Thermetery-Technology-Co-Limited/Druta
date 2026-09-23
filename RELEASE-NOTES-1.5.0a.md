# Druta 1.5.0a

Druta 1.5.0a is a prerelease that makes voltage-rail capability detection
runtime-specific and moves regulator discovery behind an explicit I2C opt-in.

## Runtime voltage-rail detection

- GPU architecture can be read through NVAPI when NVML is unavailable. This is
  an architecture check, not a device-ID, VBIOS, or driver-version allowlist.
- The rail getter negotiates its understood public layouts and retries a
  transient getter failure once. Successful rail masks remain known for later
  retry, but a write still requires fresh controls from the current read.
- Native write packets are recognized by observed shape, not a driver lookup.
  The reviewed schemas are 716-byte packets with 648-byte parameters,
  976/908-byte packets, and 1104/1036-byte packets. A packet must match its
  size, command, parameter size, selected mask, and record bounds exactly.
  Unrecognized packets fail closed.
- Before every native SET, Druta obtains a fresh native GET and preserves all
  native fields it does not edit. It retains the original input header for SET;
  the GET response valid mask is output data and is not a SET input. The four
  exposed rail fields are changed only in selected records.
- Rail diagnostics show the current status and per-rail reason. **Copy info**
  contains bounded diagnostic data, including a prior native write error where
  applicable; it does not include raw packet buffers.

The verification establishes stored rail-control fields, driver-reported
telemetry, exact readback, and restoration. It does not establish physical
voltage at a regulator output, load stability, a safe operating range, or a
physical maximum.

## Verified matrix

On the local TITAN Xp, drivers 472.12, 566.36, 580.97, and 582.66 each passed
four independent field writes: reliability, alternate reliability,
overvoltage, and minimum voltage. Each check included exact driver readback,
restoration, voltage-boost preservation, and NVAPI-only architecture detection.
The evidence is scoped to that adapter and those driver sessions. GTX 1060 was
not directly tested.

## I2C discovery

The I2C controller scan is read-only and starts only after **I2C rail** is
checked. It does not run at launch, on GPU switch, or while the checkbox is
unchecked. The UI displays completed and total probe units from the scanner and
labels the active controller/address phase. Unchecking, switching GPUs, or
closing cancels further probes at the next bounded step and ignores any stale
result. A cancelling worker must finish before another scan can start.

Discovery is separate from voltage control. A detected controller still needs
the existing identity, verification, readback, and restoration gates before a
write. I2C-bearing profiles require a completed manual scan before they can be
loaded; they do not trigger scanning themselves.

## Validation

The Windows regression suite passed **992 tests**. Live UI verification through
Druta's render loop confirmed zero I2C scans at startup, retained opt-in state,
visible progress from actual probe counts, cancellation, and a complete
2,688-probe scan. The local TITAN Xp returned no compatible I2C controller.
The rendered UI was inspected through Dear PyGui's framebuffer output; the
packaged EXE's launch was checked separately. The four driver round trips
required no reboot, and the machine was returned to driver 580.97.

## Package

The prerelease archive is `Druta-1.5.0a-win64.zip`. Keep the extracted `Druta`
directory together and run `Druta.exe`; Python is not required for the packaged
application. The archive includes matching working-tree source and its SHA-256
source manifest.
