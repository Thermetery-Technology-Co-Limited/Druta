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

## Remaining findings, not fixed by the P0 change

### 1. Voltage conversion and Stock inherit local measurements

`GPU._VOLT_RAIL_GENERATION_PROFILES` contains absolute reliability,
alt-reliability, overvoltage/vmin bases, headroom and power-on deltas selected
by architecture/getter version. `_volt_rail_profile()` supplies these to
display, absolute-to-delta conversion and reset/Stock behavior.

Examples: Turing reliability 1068.75 mV / alt-reliability 1093.75 mV; Blackwell
reliability 1040 mV and MSVDD power-on reliability delta -50000 microvolts.
Structural ABI validation does not prove another board has those defaults.
This can affect write interpretation, beyond hiding a slider.

**Priority follow-up:** establish native per-device bases/defaults or distinguish
captured initial state from factory Stock. Do not describe architecture
constants as confirmed current-card defaults. No different-board voltage error
was measured during this audit; this is a code-supported risk. Simply removing
the surrounding gates would not solve it.

### 2. Current limits require the local complete policy set

`_current_limit_state()` requires every policy in
`CURRENT_LIMIT_GENERATION_POLICIES` and rejects bits outside
`_CURRENT_LIMIT_ALL_MASK = (1 << 18) - 1`.

- Valid Blackwell core policy 13 is discarded if policy 14 is absent.
- A valid known current is discarded if an unrelated policy uses bit 18+.
- An invalid expected policy discards otherwise valid rows too.

The mask is 32 bits; all three known packet sizes/strides accommodate at least
32 records. Eighteen reflects sampled occupancy, not buffer capacity.

**Follow-up:** validate each present understood policy independently, derive
bounds from the verified ABI, preserve unrelated bytes/write masks and report
unavailable policies separately. Keep type/unit/range/header/readback checks.
These rejection paths follow from code; no partial-policy board was measured.

### 3. Voltage-limit visibility requires an exact rail set

`volt_rail_limits_supported()` requires both getters to expose exactly `{0}`
on Pascal/Turing or `{0,1}` on Blackwell. Write/reset paths repeat it. Valid
Blackwell NVVDD without exposed MSVDD disables the whole group.

**Follow-up:** per-rail capability after resolving conversion/default provenance
above. Keep agreement about the selected rail's identity and record structure.
A missing second rail should not invalidate the first.

### 4. Memory-offset precision depends on one exact RTX 5080

`memory_offset_step_units()` returns two units only for
`2C02 / 89DE1043 / 98.03.3b.c0.6f / 580.97`; others get one. Setter, slider
snapping and profile preflight consume this. It does not hide the slider, but
changes accepted precision and can cause readback failures elsewhere.

**Follow-up:** determine granularity from transport/runtime evidence and report
quantization. Do not replace the board-specific rule with an equally unproven
claim about every Blackwell GPU.

### 5. Private clock controls depend on optional telemetry pairing

Pascal/Turing `clkdom_controls_for_ui()` uses `clkdom_pairing()` keys. A valid
control can disappear if the private getter lacks the expected domain 0/15.
Additional memory offset already has an exception because Pascal R470 can
lack the pairing while the control works. Blackwell uses accepted masks.

**Follow-up:** separate understood writable indices from optional live labels.
An unavailable counter should not erase a valid control. Unknown register
semantics still require investigation before writes.

### 6. Transient discovery failures can become permanent absence

`clkdom_layout()` caches failed reads/version checks as False;
`clkdom_domains()` caches empty accepted-domain lists;
`_read_volt_rail_blocks()` caches the initially observed rail set, even empty.
Controls can stay missing until the GPU object is rebuilt.

**Follow-up:** explicit capability refresh; distinguish unsupported ABI from
transport failure or temporary unavailability. Keep retries bounded.

### 7. I2C discovery still has local routing/fingerprint constraints

The MP29816 recipe requires Astral RTX 5080 device/subsystem IDs, port 2/address
0x30, observed manufacturer/model/revision fingerprints, PAGE 0 and one scaling
configuration. Another MP29816 board can be excluded before identity reads.
PAGE/scaling protect decoding; PCI/revision prefilters also limit discovery
to local observations.

NCP4206 has no GPU PCI/VBIOS allowlist, but is Kepler-only, probes address 0x20
on ports 0..7 and accepts documented-default or observed-OEM identity tuples.
Other routing/identity variants are not discovered.

MP2888A already ignores the original recipe's PCI match metadata at runtime,
scans ports 0..7 and unicast addresses, and validates a repeatable candidate
layout. Its old TOML match fields preserve saved-profile identity rather than
gating product IDs. User-programmable vendor/product IDs are diagnostic.

**Follow-up:** controller-specific identity/decoding and location-bound candidate
selection independent of GPU model. PAGE 0 and model identity alone do not
identify which physical rail a new PCB routes there. Preserve explicit
selection for ambiguity and per-session response checks. This change does not
generalize an I2C recipe.

### 8. One inert MSVDD offset experiment became a global omission

The UI omits MSVDD clock-domain offset after a stored-but-inert observation
on RTX 5080/580.97. This is a universal omission, not an identity allowlist;
separate MSVDD rail ceilings remain available through their own backend.

**Follow-up:** keep the negative experiment scoped to its tested mechanism.
A separately understood layout could justify an experimental control, but
this audit does not establish a working alternative.

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
coverage, not a fresh Maxwell hardware claim. The remaining findings above
were identified by source inspection and should not be mistaken for fixes.
