# Druta 1.10.0

Druta 1.10.0 is the first stable release of the native C++ application. It supersedes the 1.9.4 prerelease. Extract the native x64 ZIP and run `druta-ui.exe`. The guest-check ZIP contains a separate validation utility for Windows XP Professional x64, Vista x64, and Windows 7 x64.

## BIOS Override and clock controls

- Edit BIOS power relationship weights on qualified Turing, Ampere, and Pascal adapters with matching V30 firmware and a supported driver INFO layout. The editor handles the 5,008-byte Turing layout and the 5,520-byte layout. Apply and Restore verify the full driver-loaded image and INFO block.
- On Turing drivers that accept the compact clock-control packet, Druta exposes XBAR, SYS, VIDEO, and LTC offsets. The clock adjustment uses the requested offset field and verifies driver readback.
- On Pascal drivers with the older packet, Druta restores Additional memory offset and NVVDD offset controls. Its built-in Pascal timing reader can use the qualified older transport for read-only timing capture.

## Board power and Classic layout

- Turning XOC off restores the adapter's normal editable board-power range. Druta reads the configured request through the interface that owns it and shows the enforced limits separately. The driver derives the current limit when it is unpinned.
- On qualified hardware the XOC control exposes software requests up to 1,999 W. Firmware and board constraints determine the effective limit.
- Classic wraps long Rails summary readouts.

## Hardware validation

The combined source was tested on a TITAN RTX with NVIDIA driver 431.36. BIOS relationship weight Apply and Restore completed without a Windows reboot. XBAR, SYS, VIDEO, and LTC each shifted their programmed clock by the requested amount at P0 and returned to baseline.

On a GTX 1050 Ti, the 431.36 build verified Additional memory offset and NVVDD request readback and restoration. The built-in Pascal timing reader also returned consistent P0 samples on 431.36 and 512.15.

[Pascal validation](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/97) · [Turing validation](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/98)
