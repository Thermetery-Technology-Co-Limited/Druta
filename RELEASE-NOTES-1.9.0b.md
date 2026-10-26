# Druta 1.9.0b prerelease

This prerelease continues the NVIDIA registry and live-control work introduced
in [1.9.0a](RELEASE-NOTES-1.9.0a.md). The corresponding pull request remains a
draft while the remaining hardware and packaged-UI validation is completed.

## Changes since 1.9.0a

- **Ampere live power requests:** on adapters exposing the understood smaller
  client-power layout, the existing XOC Power limit control can request values
  outside the driver's ordinary range without a driver restart. Adapter pairing,
  packet geometry, role binding, readback and recovery are checked. Coupled
  policies set by the user are re-applied after a board-limit change.
- **Core current visibility:** when a qualified Ampere board request causes the
  driver to derive a core-current value above that policy's advertised maximum,
  Druta retains the current control after verifying matching driver snapshots.
  New independent current requests remain within the policy's advertised range.
- **GR power gating:** supported registry transactions verify loaded P-state
  eligibility and preserve peer configuration and recovery information. See the
  manual for capability checks and the remaining qualification limits.

## Validation and limits

On one shunt-modified ASUS Strix RTX 3070 Ti, driver 595.97, idle tests accepted
381 W and 999 W board requests against the reported 380 W ordinary maximum. At
999 W the driver reported a 508.755 A derived core-current limit; an independent
508.855 A current request was rejected. At 381 W, independent 242 A and 241 A
requests were verified and the latter survived a repeated board request. Each
test restored the complete captured client and native control state. Reported
power is uncalibrated on this card; these tests do not establish physical power
or current-limit enforcement under load.

Support depends on the current adapter and driver interfaces. The tested card's
values are not universal. Ada's full live-write path has not been validated by
these tests. Original-BIOS-range coloring and detailed arbitration may be
unavailable with the smaller layout. Other downstream limits can still apply.

The broader controls and their remaining limitations are described in the
[1.9.0a notes](RELEASE-NOTES-1.9.0a.md), [manual](MANUAL.md), and
[technical documentation](TECHNICALDOCUMENTATION.md). Physical enforcement,
remaining power-policy and low-power semantics, arbitrary voltage-offset replay,
cross-generation validation, and the packaged UI walkthrough remain open. A
successful request or configuration readback is not a physical measurement.

## Package

`Druta-1.9.0b-win64.zip` contains the Windows application, adjacent I2C profiles,
licenses, release notes, regression tests, and matching source under `source/`.
The source manifest records SHA-256 hashes; a ZIP checksum accompanies the
release. Extract the complete directory and run `Druta.exe`, keeping its
adjacent files together. Python and the NVIDIA App are not required.
