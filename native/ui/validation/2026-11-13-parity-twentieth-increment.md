# Twentieth parity increment: production voltage evidence

The native voltage-limits service now has a production evidence provider. It
uses a paired-target current-ROM getter, a pure ROM ownership/voltage graph
parser, independent rail policies, complete retained CONTROL/clock requests
and a guarded voltage-boost getter. This closes the missing production wiring
for the understood layouts; it does not qualify every firmware layout.

Opening a session constructs the provider without reading the ROM or writing
hardware. Explicit voltage-definition Read acquires evidence. Selected Apply,
exact readback, captured-initial/ROM Restore, Undo and manual profiles/catalog
use the existing selected-request service. No startup or driver-reset replay,
scaffold installation, registry mutation or driver refresh was added.

The image reader validates the 64-bit V2 descriptor, pointer, bounded length,
guards and paired PCI identity before exposing bytes. The provider requires
matching image reads and retains the SHA-256. Unsupported exports, transient
failures, malformed packets and identity changes have distinct outcomes.

ROM inspection preserves legacy/EFI/object ownership, BIT relocation and
checksum, aliases, complete forward/reverse equation dependencies, polynomial
references and clock roots. Qualification also matches the complete active
loaded inventory and immutable graph against the ROM. A changed dependency or
new active consumer cannot leave a supposedly independent constant writable.
Stored ROM values remain distinct from current loaded values and captured
initial values. Unknown bytes are comparison evidence, never restoration data.

Rail policy and raw CONTROL captures retain independent per-rail availability.
Missing unrelated rails do not remove supported controls; REL requires policy
evidence for every affected root. Read failures preserve established readings
as stale. Unsupported/malformed semantic evidence leaves raw loaded definitions
inspectable. Only transient failures receive recovery retries. Image and rail
reads own bounded matching-read recovery; the service supplies one complete
observation retry, without an extra provider-level retry loop.

The retained peer reader preserves Pstates20, clock-control and BoostTable bytes,
and keeps the evaluated V/F curve separate from stable request comparison.
It uses understood version fallback and current architecture/layout evidence,
not device or driver allowlists. The 40-byte voltage-boost getter preserves all
caller-owned bytes except its one output byte; an absent/explicitly unsupported
boost getter does not erase independent OV/VMIN capability.

## Scope still open

The source-backed runtime mappings are ROM type 1 to runtime type 3 and ROM
type 7 to runtime type 6. Other active equation mappings are explicitly
unqualified. The official NVIDIA `ctrl2080vfe.h` and `ctrl2080perf.h` inspected
in the bounded source lookup did not supply their missing enum/field geometry.
Do not infer translations from numeric patterns or one device's dump.

Installed-scaffold recognition and signed offset-head requests remain separate
work. This provider does not treat an absolute constant as an installed offset
or use historical receipt packets as authorization to replay state.

## RTX 3070 Ti read-only evidence

The [integrated probe](2026-11-13-parity-twentieth-voltage-read.json) ran on
`0000:01:00.0`, UUID `GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`.
It captured 203 loaded equations, retained the six NVVDD request fields and
read two independently qualified device-delay requests at 128 us. It issued
no tuning writes and closed cleanly.

The current ROM has a rail-selector header `20 05 11 01`, outside the traced
`30 05 41` layout. The [independent Python parser check](2026-11-13-parity-twentieth-rom-read.json)
rejected the same layout. Two current-ROM reads matched exactly: 565248 bytes,
SHA-256 `feffb49533d53e51112cfe2a1ff4b9a53113c9d9a28d022fe49bdf8a6c421541`.
Therefore no semantic REL/ALT/OV/VMIN row was enabled on this adapter. This is
an explicit unqualified layout, not a device/driver compatibility exclusion.
The new peer-capture and semantic write paths are covered by injected tests;
this run stopped before those provider stages and does not physically qualify
them on this board.

## Validation

- The integrated Release build passes; all **122 CTest suites pass in 43.61
  seconds**. Six focused voltage/parser/ABI suites also pass.
- Resolver-injected ABI tests exercise alternate PCI identities, descriptor and
  buffer overruns, returned length/version/mask failures, explicit unsupported
  boost results, Pstates version fallback, unknown-byte drift and independent
  optional exports. Clock captures cover Ampere and Blackwell layouts.
- Pure parser tests cover legacy/EFI/object ownership, relocation, checksum,
  truncation, aliases, cycles, dangling/high-index references, shared REL roots
  and unknown clock consumers. Provider tests use actual ROM/packet adapters,
  not a prequalified semantic callback, through CoreClient Read, selected OV
  Apply and Undo. Actual-provider REL/ALT/OV/VMIN Apply/Undo and a four-role
  manual profile round trip preserve the complete original CONTROL state.
  Tests cover current values differing from ROM, absent boost,
  partial rails, transient recovery, malformed/invalid single-attempt failure,
  extra loaded consumers and opaque peer drift. No injected test writes a GPU.
- All **22 live renderer phases pass** in the
  [renderer report](2026-11-13-parity-twentieth-smoke.json). Explicit Pause and
  default minimization produce zero frames and telemetry reads; Always refresh
  while minimized produces 113 frames and two reads. Classic Control and the
  voltage-definition/device-delay page were visually inspected. The four
  resets are Direct3D renderer resets, not GPU-driver restarts.
- GUI and console-worker import-policy checks pass. They do not establish
  Vista/7 execution. No controller, clock, timing, voltage or registry write,
  workload launch or PnP restart was requested during live validation.

SHA-256 of the tested Release artifacts:

| Artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `3BD10E1060E39AFAE99EC243206AF344D06C4CE13A1860030B74F2498BF57893` |
| `drutacore.dll` | `9C1033839A2F89A2A39BEA90CE6CFB8BC08F09A6310D6C45D6E7DEDF5CCD8A05` |
| `druta-ui-voltage-read-check.exe` | `1B19C9935302E57570AD84FD13565CA487E1F4898637ECBEEF2D53F43A000DD9` |

The [backlog](../../../backlog.md) keeps the remaining scope open; overall
frontend parity is not complete.
