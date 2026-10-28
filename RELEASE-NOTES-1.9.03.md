# Druta 1.9.03

Extends the live power-limit override introduced in 1.9.0b to Turing adapters
that expose the validated smaller client-power and native-policy interfaces.
The existing XOC Power limit control, numeric entry and profiles use the
qualified live request path without a driver restart.

- Selects the 8620-byte native INFO / 4432-byte CONTROL layout with 124-byte
  native rows after explicit read-only size rejection. The Ampere layout and
  modern interface retain their existing behavior.
- Preserves paired GPU identity, role binding, packet geometry, units, opaque
  bytes, exact request readback and compensating restoration. Transient errors
  receive bounded retries without guessing another layout.
- Retains readable Turing core current when an extended board request causes
  the driver to derive a value above its advertised maximum, requiring matching
  native snapshots and equal requested/effective current. New independent
  current requests remain within the adapter's advertised range. Session pins
  are re-applied after coupled board-limit changes.
- Aligns application and package version metadata and includes the Turing
  experiment, sanitized measurements and regression coverage in the package.

On one Gigabyte RTX 2080 SUPER AORUS Xtreme, driver **581.42**, idle tests
accepted 349, 351 and 400 W requests against the reported 350 W ordinary
maximum. At 400 W the driver derived a 378 A core-current limit against a
360 A advertised maximum. The production setter also accepted 351 W,
re-applied a 350 A pin on a repeated board request, and refused a new
360.001 A current request. Each trial restored the full captured 350 W initial
client/native state. A driver upgrade was not needed on this configuration.
See the [mapping and validation record](experiments/turing-tu104-live-power-20261028.md).

An [offline review of the supplied 431.60 and 621.01 binaries](experiments/turing-driver-abi-review-20261028.md)
records their differing command/packet geometries. It is not a runtime driver
qualification. The 431.60 wrapper's smaller `26xx` packets require separate
mapping work; unknown geometry is refused before a live power SET. No display
driver was swapped or restarted, and the memory-modified card's existing P0
hold was preserved throughout.

These are idle software observations on one card, not measurements of physical
power/current enforcement under load or universal Turing defaults. No registry
override, restart, voltage change or workload was used. Four-digit Turing
requests were not hardware tested. Smaller-layout arbitration diagnostics and
original-BIOS-range coloring remain unavailable. The inherited XOC envelope is
1999 W; downstream constraints can differ from the accepted request.

This release is a prerelease on the ongoing registry-controls development line.
The [1.9.0b notes](RELEASE-NOTES-1.9.0b.md) describe the broader feature set and
its remaining qualification limits. It does not complete the unqualified
persistent power/voltage registry paths.

`Druta-1.9.03-win64.zip` includes the application, adjacent I2C profiles,
licenses, matching source, tests, this release note and public validation
material. The source manifest and accompanying ZIP SHA-256 record the exact
distributed bytes. Extract the complete folder and run `Druta.exe`.

Validation: the full hardware-free suite passed **6377 tests** on CPython
3.13.8. Focused protocol, watchdog and current-control checks passed 77 tests;
changed protocol/recovery modules passed Ruff. The local mapping GETs also ran
successfully under the owner's CPython 3.14.10 installation. The Windows
application is built with CPython 3.13.8 and the pinned requirements.
