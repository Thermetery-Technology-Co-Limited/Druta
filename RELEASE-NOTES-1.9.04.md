# Druta 1.9.04

Supersedes [1.9.03](RELEASE-NOTES-1.9.03.md) with fixes found by installing
431.60 and 621.01 on the local Gigabyte RTX 2080 SUPER AORUS Xtreme / TU104.
The XOC Power limit slider now accepts and restores configured requests across
these interfaces as well as the previously tested 581.42 layout. On 431.60,
native effective status settles at the same 360.938 W for 351 W and 400 W
requests; downstream override remains unqualified. On 621.01, native status reports software headroom
above 350 W. Physical enforcement on these two drivers remains unmeasured.

- Adds the short RM `26xx` power interface: captured 2524/2456-byte transport,
  644-byte client INFO, 388-byte client CONTROL, 120-byte role INFO, and
  5008/2108/14148-byte native INFO/CONTROL/STATUS. The native CONTROL mask is
  its first word. The older 3548/3480-byte interface remains separate.
- Validates v30 firmware policy definitions carried inside modern RM packets.
  On 621.01, treating the larger packet capacity as a v40-only firmware format
  had incorrectly refused the Turing role binding.
- Handles coupled peer requests, exact recovery, session-pin replay and
  driver-derived current visibility using the selected native geometry and
  validated firmware definition. Independent current requests retain the
  current adapter's reported bounds.
- Preserves full PCI/handle pairing, stable role binding, units, unknown
  bytes, exact request readback and compensating restoration. Runtime
  capability and packet checks select support; no driver/device allowlist or
  persistent registry/VBIOS mapping was introduced. Modern PERF diagnostics
  retain independent capability checks.

Both newly installed drivers accepted idle **349, 351 and 400 W** configured
requests against the board's reported 350 W ordinary maximum. Paired native
STATUS captures on 431.60 first reported effective board limits of
**349/350/350 W**, then settled within about 0.3 seconds at
**359.906/360.938/360.938 W**, with four subsequent samples per request agreeing
and core current remaining at **324 A** throughout. The plateau is consistent
with a 350 W clamp plus a driver status uplift; it is not a strict 350 W
reported effective cap and does not resolve physical behavior. Its qualified
scope is configured-request acceptance/readback/restoration and native
current-policy controls; downstream power-limit override remains unqualified.
On 621.01, the same requests produced native effective limits of
**359.906/361.969/412.5 W** and core current of **359.640/360.360/378 A**.
All five samples per request agreed on 621.01. These are software status values.
The control view now independently shows the NVML enforced limit alongside the
configured request. The production setter on both drivers
also accepted 351 W, re-applied a separate 350 A current pin after a repeated
board request, and refused an independent 360.001 A request. Every trial restored
the complete captured **250 W** client/native baseline and retained the
1062.5 mV V/F hold with P0 verified. Driver replacement used no Windows reboot.
These results apply to this shunt-modified board with its imperfect 16 GB
VRAM modification. See the
[live driver comparison](experiments/turing-driver-live-comparison-20261028.md),
[431.60 report](experiments/turing-43160-live-power-20261028.json),
[621.01 report](experiments/turing-62101-live-power-20261028.json), and
[431.60 static evidence](experiments/turing-43160-static-layout-20261028.md).

The earlier [offline ABI review](experiments/turing-driver-abi-review-20261028.md)
and 1.9.03 notes describe the investigation before these installations. Their
then-pending live comparisons are completed by the evidence linked above.

The owner separately reported a [FurMark A/B response](experiments/turing-owner-furmark-ab-20261028.md)
during the preceding 581.42 session: approximately 140% TDP with frequent power
throttling before extension, then more than 150% TDP and a reported PSU OPP
trip. This supports an operative limit increase on that board. The estimated
900 W+ transient is not a calibrated measurement, and shunt-modified TDP
readings do not establish actual GPU watts. Our 431.60/621.01 qualification
used idle request/readback/recovery checks; physical enforcement under load
on those drivers remains unmeasured. Four-digit Turing requests and a
universal physical ceiling remain unqualified. The inherited 1999 W XOC
envelope is a software request range.

This remains a prerelease on the registry-controls development line. See the
[1.9.0b notes](RELEASE-NOTES-1.9.0b.md) for the broader feature set and pending
qualification of separate persistent controls.

`Druta-1.9.04-win64.zip` includes the application, adjacent I2C profiles,
licenses, matching source, tests and public validation material. The source
manifest and accompanying ZIP SHA-256 identify the distributed bytes. Extract
the complete folder and run `Druta.exe`. The build uses CPython 3.13.8 and the
unchanged pinned dependencies.

Validation: the complete hardware-free suite passed **6530 tests** in 142.99 seconds.
