# Druta 1.6.0

Druta 1.6.0 adds NCT3933U current-DAC control, user-selected board voltage
offset presentation, manual scoped I2C discovery, portable timing profiles,
and an immediate Windows PnP recovery action.

## NCT3933U current-DAC controls

- Druta can discover the Nuvoton NCT3933U by its documented identity bytes and
  exposes its three outputs as signed native current commands. **Raw outputs
  (µA)** remains the default: OUT1, OUT2, and OUT3 retain their raw bytes and
  report source/sink current without inventing a rail name or a live voltage.
- **Read settings** refreshes the command and configuration registers on
  demand. Apply and Zero capture the complete current-DAC control state before
  writing, use exact register readback, and profiles/undo retain that raw state.
  The DAC has no live VOUT measurement; register readback is not presented as
  physical voltage verification.
- A user can select **GPU / memory / PEX-PLL (mV)** only for a locally chosen
  adapter/controller route. The selection is stored by GPU UUID, port, and
  address when the UUID is available; it is not a shipped board allowlist.
  The measured ASUS CG611P / Strix-Poseidon route maps OUT3 to GPU and OUT1 to
  memory at 10 mV per normal negative 10 µA command, and OUT2 to PEX/PLL at
  66 mV. Doubled mode gives 20/20/132 mV. Positive current lowers those
  measured rails. Other boards stay in raw µA mode until their owner verifies
  and selects a route.

## Manual, scoped I2C discovery

- Checking **I2C rail** now only reveals controls. It never scans or reconnects
  at launch, on card switch, or when the checkbox changes.
- The **IC to scan** chooser defaults to **Unknown -- Full Scan** and lists the
  supported controller families. **Connect / Scan** first read-verifies a
  compatible saved route, then falls back to a scan limited to the selected
  controller. **Full scan** explicitly ignores both the saved route and scope.
- A saved route is a convenience, never authorization for a write. A stale,
  unreadable, or corrupt cache record is reported and falls back to the chosen
  read-only scan. Completion is bound to the still-selected GPU and opt-in
  state, so cancelled or stale work cannot publish or save a route.
- The risk banner and control tint follow the selected writable controller.
  A transient I2C read failure no longer silently drops I2C risk points or
  leaves the tab amber. I2C plus Rail limits shows **CRIMSON (risk 3)**;
  live identity checks still run before writes.

## Timing profiles

- The Timings tab can save a decoded, top-memory-band broadcast capture with
  its staged field edits, and load Druta timing profiles or compatible
  `fields` profiles. Loading only replaces staged editor values; it never
  writes the GPU. Existing preview, fresh-band, and apply checks still govern
  a later **Apply to memory controller** action.
- Raw `nvtune save -o` register backups remain nvtune restore files and are
  refused as broadcast timing profiles. Upstream nvtune profile export support
  is merged as [PR #3](https://github.com/sebastianmarrufo/nvtune/pull/3) and
  is included in [nvtune v1.0.2-alpha](https://github.com/sebastianmarrufo/nvtune/releases/tag/v1.0.2-alpha).
  nvtune remains a separate tool: Druta does
  not bundle, download, or update it.

## Panic Button and layout

- The red **Panic Button (PnP Reset, Deeper than Shift+Ctrl+B)** is persistent
  in the upper-right shared header, while each tab's content scrolls below it.
  **Device > Restart GPU device (PnP)...** starts the same action.
- The action immediately closes Druta, waits for that process to exit, restarts
  the exact selected NVIDIA display device through Windows PnP, and launches a
  fresh Druta window. It does not ask for a confirmation, reapply a tuning
  profile, or reboot Windows. Windows reboot requirements and failures are
  recorded for the relaunched window. Administrator rights and Windows 10
  version 2004 or later are required.

## Validation and scope

The hardware-free regression suite passed **1,148 tests**. A real Dear PyGui
render check confirmed the crimson banner and control tint with a simulated
failed controller read, without I2C reads or writes. Read-only validation
on one local card completed a full I2C scan in **14.25 s**, an NCT3933U scoped
scan in **0.26 s**, and a cached reconnect in **9 ms** at the backend (about
**0.03 s** through the GUI). Scan validation issued **zero writes**. Those
timings and the CG611P current-to-voltage measurements describe that local
adapter/controller route only; they do not establish wiring, gain, voltage, or
performance behavior for another board.

## Package

`Druta-1.6.0-win64.zip` contains the onedir `Druta` application, its beside-EXE
I2C profile folder, and a matching working-tree source snapshot under `source/`.
`source/SOURCE-MANIFEST.json` records SHA-256 hashes for that source snapshot
and the packaged executable. Keep the extracted directory together; Python is
not required to run Druta.
