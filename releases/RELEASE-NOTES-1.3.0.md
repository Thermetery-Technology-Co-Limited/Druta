# Druta 1.3.0

Druta 1.3.0 brings confirmed TITAN RTX and TITAN Xp voltage controls, support
for older NVIDIA driver interfaces, and profiles that restore voltage settings
along with the curve and clocks. Profiles can now load at Windows sign-in,
with automatic loading skipped after an abnormal shutdown.

## Profiles and Windows startup

- **Save and restore the voltage settings in a tune.** Profiles now include
  confirmed NVVDD/MSVDD limit fields, the NVVDD offset, the identified I2C
  regulator's offset, per-domain clock requests and Additional Memory Clock
  Offset. The profile list names these settings, and loading reports each
  control's result.
- **Load at startup**, available beside named profiles in the Load Profile
  window, keeps a fixed copy and launches Druta at Windows sign-in. Configure
  it as administrator; no password is stored. Selecting it again updates the
  startup copy. **Disable startup loading** turns it off.
- **Abnormal shutdown recovery.** Automatic loading requires a clean Windows
  shutdown/boot and a clean Druta session record. A crash, forced termination,
  power loss, missing record or unknown shutdown status skips loading for the
  entire boot. Reopening Druta cannot retry it automatically; manual loading
  remains available. Normal Windows shutdown while Druta is open is recognized.
- Startup loading checks the GPU, VBIOS and driver again and requires a complete
  undo capture. Only one Druta window owns startup loading, and each boot gets
  at most one automatic attempt.
- I2C restoration checks the regulator and its local profile, runs fresh
  verification under load in each session, then uses the existing dry run and
  checked setter. Voltage failures stop before applying dependent clocks.
- Saved settings that require XOC carry that requirement into the profile.
  Loading restores the required XOC/rail modes, including for above-normal
  values left on the card after XOC was unticked.

## TITAN voltage controls

- Confirmed **NVVDD reliability, alternate-reliability, overvoltage and minimum
  voltage controls** on the measured TITAN RTX/TITAN Xp board, VBIOS and driver
  combinations, alongside the existing Blackwell controls.
- Both TITANs exceeded their default 1093.75 mV cap in repeated tests on drivers
  580.97 and 472.12: a 1125 mV ceiling and a requesting V/F curve produced
  **1112.5 mV driver-reported live voltage**.
- Request ranges extend to **1200 mV normally / 1500 mV in XOC**, with NVVDD
  offset ceilings of **+200 mV / +500 mV**, respectively. These are software
  request ranges; they do not establish a physical voltage maximum or guarantee
  delivery of the requested voltage.
- Pascal's delayed minimum-voltage update on 472.12 is handled with a verified
  identical re-send. Other fields and drivers do not inherit that workaround.
- **MSVDD remains unavailable on these TITANs.** Unconfirmed voltage-offset
  fields and unknown board/driver write paths are not enabled by this release.

## Curves, clocks and fan controls

- **Switching GPUs no longer loses the V/F curve.** Turing's 128-point and
  Pascal's 80-point tables are handled separately; incomplete reads are rejected.
  If the new GPU cannot initialize, the current curve and staged edits remain.
- **De-flatten and Max it no longer use the stock clock-list maximum as an
  overclock ceiling.** The regular ramp can stage clocks above a stock 1911 MHz
  plateau. Driver and hardware constraints still apply.
- **Additional Memory Clock Offset** replaces the "MEM Request Offset" label
  and is included in profiles. Its effect remains generation-dependent:
  Pascal was measured beyond its ordinary memory-offset ceiling; that result
  is not generalized to Turing or Blackwell.
- Remove the lengthy explanatory text beneath Additional Memory Clock Offset
  to keep the clock controls compact. Control availability and write guards
  are unchanged.
- Legacy fan control restores requested duty and Auto policy. A **0 RPM reading
  does not hide a confirmed fan-control interface**, including on a water-cooled
  TITAN RTX.
- Profile restoration refreshes the sliders from actual readback, including
  voltage and additional memory requests. The profile window fits the viewport
  so the startup controls remain visible on smaller windows.

## Older NVIDIA drivers

- NVML discovery supports trusted Windows System32 and legacy NVIDIA NVSMI
  installation locations, addressing `nvml.dll not loadable` reports with
  Standard driver packages such as **472.12**.
- When modern NVML clock-offset interfaces are absent, Druta uses the supported
  **NVAPI Pstates20** path and preserves the memory slider's true-MHz units.
- Legacy NVAPI fan controls, performance-limit reason queries and the confirmed
  Turing frequency-lock readback path remain available where their modern
  counterparts are missing.
- Voltage-control structure versions and transports follow the validated
  GPU/VBIOS/driver combination. A successful DLL load alone does not enable
  unconfirmed controls.

## Download and upgrading

Download **Druta-1.3.0-win64.zip**, extract it, and keep the whole `Druta` folder
together. Run `Druta.exe`; the adjacent `_internal` and `i2c` folders are part of
the application. The package includes matching source, build scripts, public
measurement records and regression tests under `source`, with SHA-256 hashes
in `source/SOURCE-MANIFEST.json`. No Python installation is needed to run the EXE.

Older tune profiles still load their original fields. They cannot restore rail
settings they never captured, and the profile list says so. Save a fresh profile
for the complete 1.3.0 snapshot and before selecting startup loading. Private
control profiles require matching GPU, VBIOS and driver identity; save a new tune
after changing that configuration. Clock/V/F holds remain separately managed.

Startup loading is **off until explicitly selected**. Moving a portable
installation requires selecting the startup profile again from the new location.
Boot/resume variants without matching clean-shutdown evidence skip automatic
loading. This download is the modern Windows x64 build; the Windows 7 and Vista
ports remain separate previews in #13 and #15.

## Validation

- **188 automated tests passed**, including profile restoration, legacy-driver
  guards, curve handling and startup recovery.
- TITAN RTX/TITAN Xp functional measurements cover the documented driver paths;
  profile capture and UI checks also ran on both cards with 472.12.
- Startup task XML was accepted by Windows Task Scheduler in validation-only
  mode. Shutdown/cancellation messages were exercised against a process-owned
  test window. A real sign-in with automatic live voltage application has not
  been exercised; no such result is claimed here.

See the [driver compatibility matrix](https://github.com/Thermetery-Technology-Co-Limited/Druta/blob/codex/release-1.3.0/DRIVER-COMPATIBILITY.md)
and the [profile/startup change](https://github.com/Thermetery-Technology-Co-Limited/Druta/pull/17)
for validation scope. Voltage, I2C and timing controls retain the risks and
limitations described in the README.

**Full changelog:** https://github.com/Thermetery-Technology-Co-Limited/Druta/compare/1.2.0...1.3.0
