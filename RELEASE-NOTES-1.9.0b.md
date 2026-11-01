# Druta 1.9.0b prerelease

This prerelease continues the NVIDIA registry and live-control work introduced
in [1.9.0a](RELEASE-NOTES-1.9.0a.md). The corresponding pull request remains a
draft while the remaining hardware and packaged-UI validation is completed.

## Changes since 1.9.0a

- **1999 W XOC request ceiling:** qualified live power requests now extend to
  1,999 W through the existing Power control, numeric input and profiles. Turning
  XOC off retains the observed request and permits movement toward the ordinary
  range. Larger requests can wrap native effective limits; see the measurements
  below before treating a larger request as more effective headroom.
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

An extended eight-point idle sweep also accepted 1,000 through 1,999 W, including
a thirty-second 1,999 W hold. The native board status exactly followed a 33/32
adjustment wrapped modulo 2^20 milliwatts: it reported 7.424 W at a 1,024 W
request and 1,012.893 W at 1,999 W. NVML retained the complete requests, while
linked policies kept scaling; core current reached 894.474 A at 1,999 W. The
conversion site and physical enforcement are unproven. Every trial restored
the complete captured baseline. The measured table and sanitized summary are
included under `source/experiments/` in the Windows package.

Support depends on the current adapter and driver interfaces. The tested card's
values are not universal. Ada's full live-write path has not been validated by
these tests. Original-BIOS-range coloring and detailed arbitration may be
unavailable with the smaller layout. Other downstream limits can still apply.

The broader controls and their remaining limitations are described in the
[1.9.0a notes](RELEASE-NOTES-1.9.0a.md), [manual](MANUAL.md), and
[technical documentation](TECHNICALDOCUMENTATION.md). Physical enforcement,
remaining power-policy and low-power semantics, legacy six-word voltage-request replay,
cross-generation validation, and the packaged UI walkthrough remain open. A
successful request or configuration readback is not a physical measurement.

## Package

`Druta-1.9.0b-win64.zip` contains the Windows application, adjacent I2C profiles,
licenses, release notes, regression tests, and matching source under `source/`.
The source manifest records SHA-256 hashes; a ZIP checksum accompanies the
release. Extract the complete directory and run `Druta.exe`, keeping its
adjacent files together. Python and the NVIDIA App are not required.

## Post-release branch continuation

Independent signed REL/ALT/OV/VMIN offsets now use added VFE constant heads,
with CLI and Controls-panel capture and explicit replay. A captured RTX 5080
passed independent ±5 mV changes on all eight roles and a mixed profile replay
after a GPU driver restart. This qualifies reported policy changes at that
operating point; physical voltage, broader ranges and other configurations
remain unmeasured. This is separate from the unresolved legacy six-word request
replay. See the manual for activation and profile commands.

Development after this prerelease adds a qualified path for disabling all
enabled GR power-gating entries, including unused entries. It verifies main
controller support and mask, independent controller feature and type 7
configuration readbacks, and a finite set of known peer outcomes; unexpected
peer changes fail verification and invoke restoration. Final raw bit 4 BIOS
contributor removal and broader hardware qualification remain open. These
configuration checks do not measure physical power-gating residency.
