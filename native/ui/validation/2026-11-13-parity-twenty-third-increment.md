# Twenty-third parity increment: relationship Read and Preview

Both skins now expose explicit power relationship inspection and selected-weight
Preview. This ports the released Python 1.9.1 `fa6ee83` contracts in
`power_relationships.py`, `power_weight_runtime.py`, `vbios_fields.py` and the
weight-specific parts of `registry_overrides.py`. It does not activate an
override: durable receipts, registry writes, driver refresh and removal remain
open in the [backlog](../../../backlog.md).

Read captures current ROM and configured INFO independently through paired
native handles. Matching observations have bounded read-only recovery. Missing
image evidence leaves independent INFO inspection available. Failed sampling
retains last-good values as stale, including Preview/revalidation failures;
invalid requests after successful sampling preserve fresh evidence.

The pure parser derives relationship offsets and target policy metadata from
the current image. Selected runtime type, target, channel, units, bounds and
exact unsigned Q4.12 values must agree. Unknown peers remain inspectable and
do not suppress an independently understood selected row. Configured source
flags, linked references and ranges are displayed without claiming physical
execution or measured power effects.

Preview resolves the selected Windows PCI instance and current display driver
key, reads exact types/bytes for `romOverride00..FF`, `vbios` and
`EnableGpuFirmware`, and projects new dense records without replacing existing
records or changing the firmware selector. Its private state binds the complete
image, INFO, target and captured registry values. Revalidation rejects changed
evidence; mutable display fields cannot alter the plan. Construction, ordinary
polls, Read and Preview perform no registry writes or driver refresh.

## Validation

- Integrated Release build passed. The full 141-test run passed 140 tests and
  exposed a test-harness mistake: ImGui activation was queued after `NewFrame`.
  Moving it before the frame made the failed view test pass. After the final
  capture/message changes, all seven new relationship tests passed again.
  Every one of the 141 tests has a passing result; no unrelated implementation
  changed after the full run.
- Tests cover both understood table headers, alternate adapter identities,
  sparse/partial capabilities, source references, exact Q4.12 bounds, selected
  spans, existing record preservation, stale evidence, sampling failures,
  production capture, actual CoreClient commands and both-skin ImGui actions.
- The [read-only 3070 Ti probe](2026-11-13-parity-twenty-third-weight-read.json)
  captured matching ROM reads and closed cleanly. This adapter returned INFO
  command `0x2080A618`, size **8632**, base **0x58**, stride **0xE4**. The released
  weight decoder requires **20512 / 0xCC / 0x104**, so INFO remained unavailable
  and no live Preview was attempted. The probe's nonzero exit records that
  unavailable capability; it is not evidence of a weight write or activation.
- All **23 live renderer phases pass** in the [renderer report](2026-11-13-parity-twenty-third-smoke.json). The new panel was visually inspected. Pause and default minimization produced zero frames/reads; Always refresh while minimized produced 112 frames and two reads. Four Direct3D resets are renderer resets, not driver restarts. The harness deadline now accounts for its added phase.
- The GUI import audit passed within its stated static-import scope; it does
  not establish Vista/7 execution. No tuning/registry writes, workloads or GPU
  driver restarts were requested during this increment's live checks.

| Tested artifact | SHA-256 |
| --- | --- |
| `druta-ui.exe` | `5F32262DC2C1A80D860FDEBF0D86146B35291F4CFA03D9781146375690F6241F` |
| `drutacore.dll` | `9C1033839A2F89A2A39BEA90CE6CFB8BC08F09A6310D6C45D6E7DEDF5CCD8A05` |
| `druta-ui-power-weights-read-check.exe` | `86DCEBD379E82C4D7D783ECFF6B5A16BEF26146945DFC7505478BFAFB1A449C0` |

Local logs are under `native/build/parity-twenty-third-*`. The scope remains
released parity and the user's explicit layout requests. Broader layout
research, theoretical improvements and closed GitHub issues remain outside
this session.

The backlog now has **6 active parity entries, 30 completed and 8 deferred**. The first three relationship stages are complete; durable activation/recovery and final review reconciliation remain open.
