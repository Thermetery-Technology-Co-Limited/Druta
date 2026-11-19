# Druta 1.9.08

Prerelease. Merges the registry and live-tuning line (PR #39) with the
1.9.03–1.9.07 power work (PRs #40 and #42).

## New since 1.9.07

- Manual profiles and Undo for voltage requests and controls, NVVDD offsets,
  low-power requests, V/F policies and ordinary tuning controls, plus combined
  profiles.
- Power-policy feedback adjustment: lower and upper bounds for understood
  integral-control policies.
- Live voltage-rail requests with explicit replay, independent VFE offsets,
  PERF-limit voltage requests and fixed REL bounds.
- Individual engine power-gating inhibits, PCIe link speed and additional
  profiler controls.
- Board power requests save an Undo point before writing. Readback
  verification is unchanged; a request whose Undo point cannot be saved is
  not sent.

Each control's tested scope and limits are in the [manual](../MANUAL.md) and
[technical documentation](../TECHNICALDOCUMENTATION.md). Support depends on the
current adapter and driver; results from the tested boards are not universal.

## Validation

The hardware-free suite passes 8623 tests. This merge adds no new hardware
qualification; earlier results stand for the boards they were measured on.

`Druta-1.9.08-win64.zip` contains the application, adjacent I2C profiles,
licenses, release notes, tests and matching source. Extract the whole folder
and run `Druta.exe`.
