# Contributing I2C controller support

Druta discovers a controller on the selected GPU's I2C buses, checks its
register layout, and uses **Verify** to test a bounded voltage response before
Apply. Discovery is read-only; Verify itself makes hardware writes and restores
the entry setting. A responding address or a successful register readback does
not establish controller compatibility or prove that voltage changed.

This is the contribution workflow. [PROFILES.md](PROFILES.md) describes the
TOML format and adapter interfaces. Use the [I2C PR template](../.github/PULL_REQUEST_TEMPLATE/i2c_profile.md)
for evidence reports as well as code or recipe changes.

## Choose the contribution type

| What you found | What to contribute |
|---|---|
| NCP4206 on another Kepler board | Discovery and Verify evidence; no new PCI/subsystem whitelist entry or duplicate TOML. |
| MP2888A on another board or I2C location | Discovery and Verify evidence using the existing adapter and recipe. Report any fingerprint mismatch before changing the scanner. |
| An unfamiliar controller with documented telemetry | A telemetry-only recipe, or a read-only discovery adapter when fixed-location TOML is insufficient. |
| A new writable register layout or ordered control sequence | A cited recipe or controller adapter, mocked transport tests, and measured write/restore evidence. |

NCP4206 discovery scans Kepler ports 0–7 at address `0x20`. MP2888A discovery
scans ports 0–7 and addresses `0x08`–`0x77`, starting at `0x20`. Neither uses
DevID/subsystem filtering. Other TOML recipes still support optional PCI filters
and configured bus locations. Record board IDs as evidence even when they are
not discovery gates; controller support does not prove every board's wiring.

## 1. Record the board and discover candidates

Record card name, PCI slot, driver, VBIOS, board IDs, regulator marking, rail,
and whether the board is stock or modified. Describe fitted SMBus links if any.
Select the intended GPU in Druta and open **I2C regulator**. **Rescan I2C** lists
candidates by port/address with scan-time telemetry and clears verification.
If several respond, choose a controller explicitly. Do not assume two matching
ports are aliases or label every regulator NVVDD.

For raw bus evidence, run the separate read-only survey:

```powershell
python -m druta.tools.i2c_discover --slot 0000:01:00.0
```

Replace the slot with your GPU's slot. Use `--ports 0,1,2` to narrow the survey
or `--full` for its wider address survey. This tool's responder list is broader
than Druta's controller fingerprint checks. A bus error is a failed read; it
alone does not prove a command is unimplemented. Discovery can be reported at
idle; voltage response characterization needs a settled load.

Finding nothing is a useful result. Many boards do not connect the regulator's
SMBus to the GPU. A recipe cannot make an electrically disconnected part
reachable. Do not create a new profile solely because an existing scan was empty.

## 2. Establish controller and rail evidence

Use the exact part's public datasheet, manufacturer documentation, or cited
open-source driver code. Include revision, page/command references and raw bytes.
Do not import register maps from leaked proprietary driver code.

- Prefer documented model/manufacturer IDs where implemented; respect each
  command's transaction width. User-programmable IDs are supporting evidence,
  not universal default values to require.
- Address configuration is a consistency check, not a model ID. MP2888A's
  `0xBE` lower seven bits must match its responding address; `0xA0` is not a
  universal identity value. The adapter also checks register fields and repeated
  telemetry. See [mp2888.py](../mp2888.py).
- Record the voltage encoding and compare synchronized controller telemetry with
  GPU VID or an independent rail measurement at several operating points. VID
  is a requested voltage and can differ from the sensed rail because of loadline
  or existing offsets. A plausible voltage alone cannot identify the rail.
- Supply negative cases: unrelated responders must be rejected. If another board
  is unavailable, exercise them with a mocked transport and label that evidence
  as simulated.

Do not assume one encoding across commands. MP2888A voltage is direct mV;
current uses the low 12 bits times 0.25 A or 0.5 A according to register `0x44`
bit 3. It is **not LINEAR11**. The adapter corrects the historical encoding in
the shipped recipe; do not copy that legacy field into a new recipe.

## 3. Add only the missing recipe or adapter

For a new fixed-location controller, copy [TEMPLATE.toml](TEMPLATE.toml), rename
it, fill the identification/telemetry/provenance fields, and omit `[[write]]`
until write behavior is established. `TEMPLATE*` and `_`-prefixed files are
excluded from normal loading. A completed read-only recipe is a valid
contribution; omitted writes are intentional, not unfinished capability.

Generic recipes support one signed `offset_mv` field. Specify transaction width,
field width, scale, documented raw range and measured software limits. The
setter does not preserve unrelated writable fields in that transaction. If a
part needs read-modify-write of other fields, page selection, absolute VID, or
ordered commands, implement an adapter with explicit capture/restore behavior
instead of adding arbitrary `[[write]]` entries. NCP4206 is the existing example.

For MP2888A, extend the existing adapter only when evidence supports a missing
layout case. Do not bypass its fingerprint with a second fixed-address profile
or add a board-ID exception. Multiple recipes at the same location can create
ambiguity. Changes to recipe data also change saved-profile compatibility;
preserve or deliberately migrate identity, and test the migration.

## 4. Prove the write and the restoration

Keep clock/voltage settings controlled and record the entry register value.
Use **Unlock controls**, enable **I2C rail**, then press **Verify**. Druta induces
a load for verification. **Max it is a tuning action, not a load generator**;
it is not a prerequisite for I2C discovery or verification.

Verify uses bounded trial steps, measures response against baseline variation,
and restores the entry offset/control state. Submit the complete log, including:

- baseline, noise/threshold, requested steps and measured response;
- any refused step, missing reading, load error or failed fingerprint;
- original control bytes and the restoration readback, including a failure if any.

No response at the allowed steps is a failed or inconclusive result, not a
reason to widen the ladder. A detecting step proves a response under those
conditions; it does not calibrate 1:1 gain or establish an exact deadband.
Restoration failure invalidates Verify and leaves Apply unavailable. Failed load
setup before any MP write attempt does not authorize Stock/reset writes.

After a successful Verify, test Apply and Stock/Auto with a bounded request and
record both readbacks. Exercise negative offsets only where documented and
appropriate, and record instability or refusals rather than assuming downward
voltage changes are harmless. Restore the entry state when finished. For adapters,
also test recovery from partial sequences and read/write failures with mocks.

Changing GPUs/controllers, rescanning, or observed connection loss invalidates
the pass. A saved tuning profile records the connection and recipe fingerprint;
it can select exactly one matching candidate, but needs a fresh Verify when
that connection has not been verified in the current session. Verification is
not persisted as a permanent board approval.

## 5. Submit the evidence and run checks

Include the survey, discovery result, sources, stock/modified status, operating
conditions, and write/restore log if writes were tested. Distinguish live hardware
results from mocked tests and untested boards. An existing supported controller
usually needs an evidence/matrix update, not another recipe.

For a recipe contribution, run:

```powershell
python -m unittest tests.test_profiles
```

For discovery, adapter, Verify or UI changes, add focused mocked regressions and
run the full hardware-free suite:

```powershell
python -m unittest discover -q
```

These tests check software contracts; they do not certify a controller on real
hardware. Keep discovery tests write-free. Cover unrelated responders, multiple
candidates, location binding, failed restoration, and prevention of stale Verify
results. Do not put live GPU writes into the ordinary unit-test suite.

Leave capability unavailable when its write semantics or restoration are
unconfirmed. A read-only result or a documented negative result is still useful.
