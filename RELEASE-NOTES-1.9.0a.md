# Druta 1.9.0a prerelease

This work in progress prerelease adds registry-backed NVIDIA controls and live
driver controls to [1.8.1](RELEASE-NOTES-1.8.1.md). The corresponding pull request
remains a draft. The features below are available for review and testing; the
pending tasks are explicitly excluded from completed support.

## New controls

- **Registry overrides:** preview selected VBIOS-field and driver-policy changes,
  activate them by restarting the selected GPU, and restore captured settings
  using durable Undo receipts. These overrides change the driver's loaded
  configuration; they do not flash the physical VBIOS. Registry overrides persist
  across driver and Windows restarts until restored.
- **Power limits:** raise supported BIOS ceilings and inspect finer power/current
  policies, linked-current requests and arbitration votes. The existing Power
  slider keeps the original BIOS range in its normal color and marks the
  extended range amber. Its numeric text turns amber outside that original
  range. XOC exposes a qualified live client-power path without a driver restart.
- **Clock controls:** supported offset ranges, factory offsets, rated base/boost
  metadata, all-P-state editability, and live VF monotonicity/smoothing policies.
  Editing rated metadata does not force an operating frequency.
- **Voltage controls:** supported minimum/ceiling and REL-policy overrides,
  voltage-request admission/startup controls, and live PWM voltage-device delay
  requests. Delay values describe configuration in microseconds, not measured
  physical switching latency.
- **Fan and thermal controls:** supported duty/RPM bounds, PWM frequency, stop
  settings, curve coordinates and reported thermal thresholds.
- **Model and performance controls:** supported Speedo/IDDQ substitutions,
  performance cache and host-update policy overrides, plus read-only diagnostics.
- **DIFR configuration:** disable captured enabled entries in a separate registry
  transaction, verify loaded P-state-to-LPWR routing and configured DFPR/MS DIFR
  support/state masks, and restore the original entry through Undo. A supported
  zero state mask and an unavailable mask are shown distinctly.

Capabilities are checked on the current adapter. The tested board's device ID,
driver version, firmware, limits and defaults are not compatibility allowlists
or universal values. See [the manual](MANUAL.md) for individual operations and
[technical documentation](TECHNICALDOCUMENTATION.md) for their contracts.

## Validation and limits

The full regression suite passes **6,130 tests**. Production Apply/Undo and
independent driver readbacks were exercised on the current RTX 5080, including
selected registry controls, live client-power requests through 999 W, live VF
policies, voltage-device delay requests and DIFR disables. The final original
image, registry settings and captured configuration were restored. Activation
used GPU-only restarts where required; Windows was not rebooted.

The live client-power path now also accepts an understood smaller layout. On
one shunt-modified RTX 3070 Ti / 595.97, a 381 W request read back against a
380 W reported maximum; a pinned peer was re-applied, and the full original
control state was restored. This was an idle test, not a physical-power test.
Ada has an independent smaller-INFO observation; its complete live write path
has not been tested here. Original-range coloring and arbitration-vote details
can be unavailable for the smaller layout without disabling the live request.

A successful request or configuration readback is not a physical power,
voltage, timing or residency measurement. In particular, the tested 999 W live
request did not remove downstream firmware power votes; a 451 W request still
encountered a 450 W downstream vote with the original BIOS configuration.
There is no claim of a universal BIOS power-limit bypass.

The Windows bundle is built from matching source and checked through its frozen
driver getters. Production UI callbacks were exercised against real driver
GETs with text storage substituted for rendering. Desktop capture was
unavailable in the validation environment, so the new panels have not received
a visual walkthrough of the packaged application.

## Pending tasks excluded from completed support

- **Physical enforcement and behavior:** measure finer power/current enforcement,
  DIFR execution/residency/power savings, voltage settling/timing, and behavior
  under alternate fan initialization. Configuration verification alone does not
  complete these tasks.
- **Remaining semantics:** decode and validate the remaining PERF_CF/LPWR fields,
  voltage thresholds/timers, IDDQ physical units and firmware evaluators, and
  unresolved power-policy channel roles. The raw PERF v70 LPWR-selector producer
  remains untraced; DIFR contracts use actual loaded selectors instead.
- **Arbitrary voltage-offset replay and recovery:** some live setters on the
  tested adapter return success while leaving requests unchanged. Exact readback
  rejects those changes. Full replay/Undo of arbitrary custom offsets remains
  incomplete; recovery is reported when the captured state cannot be reproduced.
- **Older hardware and operating systems:** the new controls have not received
  fresh physical validation across older generations or the separate legacy
  Windows branches. Legacy coefficient/offset paths requiring an exposed VDT
  remain unvalidated on this adapter. Existing support is not evidence that every
  new override works on every generation.
- **Unqualified overrides:** unavailable getters, unrecognized table layouts and
  unsupported routing remain unavailable. Arbitrary enabling of originally
  disabled DIFR entries and a working `EnableCoreVoltage` override are not
  completed features.
- **Release readiness:** finish the packaged UI walkthrough and remaining
  hardware validation, review the draft PR, and resolve the outstanding work
  before declaring a stable 1.9.0 release.

## Package

`Druta-1.9.0a-win64.zip` includes the complete application folder, adjacent I2C
profiles, licenses, these notes, regression tests, and matching source under
`source/`. `source/SOURCE-MANIFEST.json` records executable and source SHA-256
hashes. A ZIP checksum sidecar accompanies the release.

Extract the complete directory and run `Druta.exe`; keep the adjacent files
together. Python and the NVIDIA App are not required. New raw driver/firmware
dumps, vendor installers, private kernel-driver material and unreviewed
machine-specific research journals are excluded. Previously published board
measurements remain in the source package.
