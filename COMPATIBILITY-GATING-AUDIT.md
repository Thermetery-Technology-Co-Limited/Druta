# Compatibility gating audit

Audit date: 2026-09-09. Scope: the active UI, NVAPI/NVML backend, profile
capture/replay, timings, CUDA helper, I2C discovery and shipped controller
recipes. Standalone research scripts and the historical Tk UI were checked
separately for reachability. This is a source audit with GTX 770 validation,
not a claim to have tested every GPU/driver combination.

The recurring defect is treating observations from the developer's board as
authorization for another user's controls. A failed local experiment does not
prove that every card with that product ID is incapable. A successful local
experiment does not establish universal voltage bases or factory defaults.

## Included fixes

- **P0 + max fan:** Kepler and Maxwell eligibility now uses architecture,
  callable ForcePstate and healthy, correctly paired GPU handles. PCI IDs,
  subsystem IDs, driver versions and VBIOS versions do not authorize it.
  Historical profiles provide measurement text only. The UI gives this action
  precedence over Max it. Physical P0 and top memory must be observed before
  setting fan duty to 100%; failed cleanup retains ownership for Release/exit.
- **GTX 745 V/F blacklist:** remove the permanent PCI 1382 rejection from
  `vf_curve_applicable()`. The card reaches the existing getter/layout checks
  like other Maxwell cards. Missing/malformed tables remain unavailable; this
  does not claim that GTX 745 now has a working V/F table.
- **NVML frequency-lock UI:** replace the GTX 745 special case with Maxwell
  generation classification. This is distinct from ForcePstate: NVIDIA
  documents GPU frequency locks for Volta-or-newer supported devices.
  [NVML reference](https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceCommands.html).
- **Undo prerequisites:** unsupported current-limit generations do not count
  as failed current-limit snapshots. Applicable read failures still make full
  snapshots incomplete. P0 + fan uses a restorable fan-only snapshot, without
  requiring an unrelated V/F table; undo restores that fan state only.
- **Read at P0:** recognize/revalidate legacy holds without transferring
  Maxwell to V/F. The timing worker already avoided CUDA when it observed the
  performance band; the gap was acquiring and recognizing the legacy hold.

Relevant implementation: `nvbackend.py` (`legacy_p0_supported`,
`hold_legacy_p0`, `vf_curve_applicable`), `druta.py` (`build_control`,
`sync_lock_ui`, `hold_for_read`), and `profiles.py` (snapshot scope).

## Compatibility fixes from the follow-up audit

These eight findings are implemented in the working change. Their eligibility,
readback and recovery contracts are covered by hardware-free regression tests;
new cross-board physical behavior is not implied. Final integration validation
is recorded below separately from earlier hardware measurements.

### 1. Per-device voltage references and exact Initial restoration

Removed generation-wide voltage bases, boost headroom and power-on deltas from
control conversion and restoration. Each understood rail captures its first
stable paired control/absolute-status samples on the current adapter. The
reported boost contribution is removed from reliability when deriving its
reference. Later writes and capability refreshes do not redefine that reference.

Absolute status is quantized driver data. UI limit values are therefore labelled
estimates from the first stable read, not calibrated physical VOUT. The exact
signed microvolt deltas are retained independently. **Initial** restores those
first-read deltas, including any tuning that already existed when Druta started;
it is not presented as factory Stock. Tuning profiles now preserve raw deltas
for exact replay rather than depending on the displayed estimate.

Implementation: `read_volt_rail_limits`, `_volt_rail_profile`,
`set_volt_rail_limits_raw`, `reset_volt_rail_limits`, and profile capture/replay.
A supported architecture still identifies understood register semantics. It no
longer supplies another board's voltage values.

### 2. Independent current policies within the actual buffer capacity

`_current_limit_capacity()` derives policy capacity from the validated info,
status and control buffers, bounded by their 32-bit occupancy mask. The former
18-policy ceiling is gone. Additional unrelated policies no longer discard
understood current controls.

Each present generation-understood policy is validated separately. Missing or
invalid policy 14 no longer hides a valid policy 13. Diagnostics distinguish
absent policies from malformed present records; incomplete capture of a present
record remains an undo/profile error. Writes still select one understood policy,
preserve unrelated bytes and validate stored/effective readback. Type, units,
ranges, packet geometry and echoed masks remain required.

### 3. Independent voltage-rail visibility and writes

`volt_rail_limits_supported(rail)` and `volt_rail_limit_fields(rail)` operate on
the selected rail's current-adapter control record and independent rail identity.
The UI renders each supported rail without requiring an exact topology. A
missing MSVDD record no longer removes valid NVVDD controls, and an unrelated
unsupported record does not authorize writes to itself.

The writer/reset paths likewise validate and select only requested, understood
rails. Other records and voltage boost are preserved. Initial references and
restoration remain per rail, rather than being copied from a generation profile.

### 4. Memory precision follows transport and reports readback

Removed the exact RTX 5080 device/subsystem/VBIOS/driver tuple from
`memory_offset_step_units()`. Every adapter is offered the transport's integer
offset-unit precision; the Pstates20 fallback represents one unit as 500 kHz.
Neither transport declares a hardware quantization grid, so the code does not
invent one from a product ID or a single truncating write.

The setter compares each requested value with actual readback and reports
mismatch. Slider snapping and profile preflight use the same transport units.
This makes another board's accepted precision testable without claiming that
all GPUs implement every representable request physically.

### 5. Clock controls no longer require optional telemetry pairing

`clkdom_controls_for_ui()` intersects understood control indices with masks
accepted by the validated runtime layout. It no longer requires private clock
counters at locally observed domain indices before exposing a control.

Telemetry pairing supplies live labels/readouts when available. A missing
counter leaves its live value unavailable instead of erasing a valid control.
Unknown field semantics remain outside the writable control catalogue, even
when a driver accepts the corresponding mask.

### 6. Explicit capability refresh retries cached discovery

**Device > Refresh capabilities** clears positive and negative clock-layout,
accepted-domain, pairing, and voltage-rail discovery caches for a deliberate
read-only retry. Clock failures retain a reason distinguishing a failed read
from an unsupported response. Partial or empty rail discovery can be retried
without rebuilding the GPU object.

Polling continues to use cached outcomes so transient or unsupported transports
do not cause repeated full scans. Refresh preserves settings, owned holds and
first-read restoration references. I2C has its separate explicit Rescan action.

### 7. Controller-based I2C discovery across boards and routes

NCP4206, MP2888A and MP29816 now scan ports 0–7 and unicast addresses
`0x08`–`0x77` independently of GPU generation, PCI IDs and VBIOS. NCP4206/MP2888A
try `0x20` first; MP29816 tries `0x30` first. Discovery only reads registers.

MP29816 requires the source-backed count-prefixed `0xAD` model ID; the Astral's
manufacturer/model strings and revision are diagnostics. It binds the currently
selected PAGE 0 or 1 and runtime scale, checks them around operations, and never
writes PAGE to discover another output. All eight source-documented scales can
provide telemetry. The sourced offset field is available on either page only
in its documented 5 mV mode; other scales are explicitly telemetry-only.

NCP4206 retains the documented or measured controller model IDs and VOUT_MODE
semantics, while accepting unsampled revisions and pinning the observed identity
to the instance. Its IDs are read-only according to the onsemi datasheet; they
are not MP2888-style programmable labels. Unfamiliar onsemi models are reported
without assuming compatible voltage-register semantics. MP2888A continues its
repeatable register-layout fingerprint, with programmable IDs as diagnostics.

All three adapters label the physical rail as unassigned. A controller identity
or PAGE number alone does not establish NVVDD wiring. Historical names/rail
labels remain only in saved-profile identity fields so existing hashes retain
compatibility. Ambiguous candidates require explicit selection, and Apply still
requires per-session response verification. Each empty bus costs 896 initial
identity reads per enabled scanner, only during discovery/rescan.

Sources and decoding details: [I2C adapter reference](i2c/PROFILES.md) and
[MP29816 source/measurement scope](i2c/MP29816-ASTRAL.md).

### 8. MSVDD is an explicit stored-request experiment

The UI now offers **MSVDD requested offset** when the understood clock-control
layout exposes a readable field on an accepted control. An explicit XOC opt-in
permits the experiment. This removes the global omission caused by one stored-
but-inert RTX 5080/580.97 observation, without turning that observation into a
claim that physical MSVDD voltage will move on other boards.

The writer changes only the selected signed microvolt field, checks a stable
getter block before dispatch and verifies the stored request afterward. The
result says that physical voltage response is unverified. **Zero** can clear
stored requests after leaving XOC; profile capture/replay and reset track the
relevant control domains. Optional driver rail telemetry is informational and
is not proof of a direct I2C VOUT change.

## Restrictions with different purposes

- `_pair()` compares PCI identities to ensure NVAPI and NVML target the same
  card. It is not a list of developer-owned hardware.
- Packet geometry, echoed versions, valid masks within buffer capacity,
  types/units and readback establish an understood private ABI. An accepted
  all-zero response alone does not establish write semantics.
- Private profile replay compares the user's saved GPU/VBIOS/driver to the
  current target, protecting against stale private state rather than hiding
  controls on hardware the developer has not owned.
- GP102/TU102 timing-band equivalence is measured locally; other chips retain
  timing controls at their highest enumerated band. Broader P2/P0 equivalence
  requires timing-register evidence.
- `tools/probe_volt_rails*.py` exact TITAN/driver checks bound invasive research
  scripts, which are not part of application control discovery.
- Ordinary fan, board power, core/memory offsets and voltage boost use API and
  readback capability rather than developer board/VBIOS lists.
- `src/druta/app.py` is the historical Tk UI; current entry points use
  `src/druta/druta.py`.

## Contract for subsequent changes

Keep API/layout understanding, current-card capability, and hardware-validation
history separate. The first two determine whether a control can be offered;
the third supplies evidence and limits, not universal authorization. Runtime
validation may still be required before writes. Do not infer another card's
factory values from the developer's board. Transient failures need a reason
and retry path; an unrelated missing rail/counter should not erase controls.


## Validation of the included changes

The actual Dear PyGui widget tree was built against the GTX 770 / 472.12.
Its registered P0/max-fan callback produced observed P0 at core 535 MHz,
memory 3505 MHz and requested fan duty 100% manual. Undo restored automatic
fan policy while preserving P0 ownership and tuning-mode checkboxes. The
registered Release P0 callback then released ownership and cleared the UI
hold. A later read confirmed automatic fan duty had returned to 26%.
Evidence: `experiments/kepler-p0-fan-ui-gtx770-47212-20260909.json`.

Kepler/Maxwell eligibility and UI routing, repeated-hold ownership, failed
cleanup, missing/malformed fan snapshots, scope-confused payloads, fan-only
undo, and timing-read hold behavior are covered by automated tests. No Maxwell
GPU is currently installed; its new broad eligibility has mock/API contract
coverage, not a fresh Maxwell hardware claim. The eight follow-up fixes have
regression coverage for alternate identities/topologies, malformed responses,
refresh/retry, raw restoration and experimental-request behavior.

The exact staged source passed **898 tests** after export to an independent
folder. The working tree additionally contains 12 excluded launch-notice tests.
The follow-up actual-widget check on GTX 770 / 472.12 rediscovered NCP4206 at
port 2 / 0x20 and rebuilt capabilities while retaining the P0/max-fan controls,
same GPU generation and I2C connection. Fan policy stayed at 26% Auto; no P0
request was owned and no tuning writes occurred. Initial UI/discovery took
about 3 seconds. Evidence:
`experiments/gating-refresh-gtx770-47212-20260909.json`.
This check does not validate modern voltage/current writes or physical MSVDD
offset response. The downloadable diagnostic build carries matching source
and SHA-256 manifest; pending launch-notice work is excluded from the commit.
