# Druta 1.9.1

Stable release. Supersedes 1.8.1 and every 1.9 prerelease (1.9.0a through
1.9.08).

## New since 1.8.1

- **Registry and BIOS overrides:** preview and apply selected VBIOS-field and
  driver-policy changes through a verified GPU restart, with durable Undo.
  Includes power ceilings, offset ranges, fan and thermal bounds, Speedo/IDDQ
  inputs, low-power (GRPG, MS, media, DIFR) entry disables and
  relationship weights.
- **Live session controls:** voltage-request gate, V/F policy checks, voltage
  policy offsets, rail requests, REL bounds and constants, switch delays,
  power-saving holds (ELPG, ELCG, BLCG, SLCG, individual inhibits, idle
  thresholds, adaptive policy), DIFR and PCIe speed requests. These last for
  the driver session only.
- **Power and current:** extended live power requests (XOC, up to 1999 W) on
  Turing, Ampere and Pascal; core current limits on Pascal; power-policy
  feedback bounds. A lowered current limit can be set back to its
  driver default without XOC.
- **Profiles and Undo** for voltage, low-power, V/F policy and ordinary tuning
  controls, plus combined profiles.
- **Ampere (GA102) support:** LPWR tables with the shorter row sizes, legacy
  ROM ownership and non-v70 PERF headers, and v30 power-policy rows for
  arbitration, feedback bounds and relationships.
- **Interface:** an Advanced tab holds power policies, registry overrides,
  live controls and diagnostics, each section stating its scope; nested
  sections are indented and styled by level; the top strip is one row.

Per-release detail is in the 1.9.0a–1.9.08 notes. Each control's tested scope
and limits are in the [manual](../MANUAL.md) and
[technical documentation](../TECHNICALDOCUMENTATION.md). Support is decided from
the current adapter's capabilities; results from tested boards are not
universal, and a verified readback is not a physical measurement.

## Validation

The hardware-free suite passes. Changed-state trials ran on an RTX 5080,
RTX 3090 Ti Kingpin, RTX 3070 Ti, RTX 2080 SUPER, TITAN RTX and GTX 1060; see
the hardware validation documents for each board's scope.

`Druta-1.9.1-win64.zip` contains the application, adjacent I2C profiles,
licenses, release notes, tests and matching source. Extract the whole folder
and run `Druta.exe`.
