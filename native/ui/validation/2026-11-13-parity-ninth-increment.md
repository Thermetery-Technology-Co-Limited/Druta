# Ninth parity increment: selected profiles and timing exchange

Reference: Python release `fa6ee83`; native parent `5db0786` and the ninth
integration working tree. The [full completion ledger](PARITY-COMPLETION.md)
remains active. The earlier [profile scope map](2026-11-13-profile-scope-gap-map.md)
records the pre-increment baseline; it is not a current completion report.

## Source scope in this increment

- Voltage admission and the selected V/F monotonicity/smoothing flags have
  native typed manual Save/Load-to-draft paths and adapters for the released
  Python `voltage_controls` gate and `clock_vf` documents. Admission and V/F
  flags remain separate transactions. A load stages only; current target,
  selected state and a fresh expected value are checked before explicit Apply.
- Power feedback ratio profiles capture selected policy fields as raw U4.12
  requests, with policy/type/channel/unit identity. Native and released Python
  `power_integral` documents stage typed requests. The selected-range preview
  orders lower/upper transitions against current bounds; issued-only Undo and
  recovery are retained after a partial operation. These values are ratios,
  not measured power or current limits.
- Native and released Python per-fan profiles preserve each fan's requested
  manual/automatic policy and level. Save reads current configured requests;
  staging checks target, source, fan count, identifiers, current manual bounds
  and restorable automatic defaults. Same-source identities can be reordered;
  understood source conversion uses the existing positional restore contract.
  Automatic target fluctuations are not treated as manual requests. Catalog
  preflight, grouped Apply/Undo and final selected readback use the existing fan
  writer, with conflicts retaining Undo.
- Released Python timing documents, `nvtune-profile-1` and flat fields
  exchanges normalize to selected typed fields only against a fresh paired
  adapter, qualified operating band and current helper metadata. Helper-reported
  aliases are case-sensitive and checked for ambiguity. Raw register snapshots,
  framebuffer partition scopes, mixed unrelated tuning fields, structural,
  inferred, overlapping and out-of-range fields are refused. Legacy timing is
  treated atomically in the catalog until fresh helper normalization; timing
  and memory-offset changes still require separate preflights/applications.
- The catalog recognizes native/Python per-fan profiles and legacy timing
  exchanges. A fan profile occupies `fan:policy`, so it cannot overlap a Daily
  fan control. This does not add catalog execution for the standalone voltage
  policy or power-integral profiles.

All ordinary tuning remains session-only. Profiles are explicitly loaded and
applied in the current session; no profile or tuning request is automatically
reapplied at startup or after a driver reset. Preserved raw bytes are used only
for qualification and comparison, never opaque replay.

## Review and validation

- Release build passed with the project's warning-as-error settings.
- All **87/87 CTest suites passed** (13.82 seconds). New tests exercise actual
  CoreClient profile dispatch and fresh Save, immutable selected metadata,
  coupled bound ordering, stale/no-op preservation, partial recovery, mixed
  fan modes, reordered IDs, false-positive/partial restores, identity changes,
  legacy timing normalization and helper aliases. Explicit recovery remains
  available after ordinary telemetry disconnects while generation, busy,
  identity and lifecycle gates remain in force. Ordinary Apply stays blocked.
- The final **20-phase live renderer run passed** on Windows 10 x64, RTX 3070 Ti,
  PCI slot `0000:01:00.0`, UUID
  `GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. The [saved report](2026-11-13-parity-ninth-smoke.json)
  records zero frames and zero telemetry reads for explicit Pause and default
  minimization, and continued rendering/telemetry with Always refresh enabled.
  Reported device resets are Direct3D renderer resets, not driver/PnP resets.
- Focused policy profiles and Classic feedback profiles were visually inspected
  in the final executable. Menu navigation scrolls to the requested panel.
  The live render captures show uninspected policy state; opening a panel does
  not silently acquire diagnostic tables. Populated editing and failure paths
  are covered by the injected service/client and headless ImGui tests.
- No new hardware tuning SET, controller write, PnP restart, registry mutation
  or profile application was issued in this checkpoint. The smoke harness
  requires zero completed tuning commands. Previous read-only and physical
  qualification retains its original adapter/operating-point scope.

Validation commands:

```text
cmake --build native/build/ui-integrated --config Release --parallel 4
ctest --test-dir native/build/ui-integrated -C Release --output-on-failure
druta-ui.exe --live-smoke-test <output-directory> --gpu 0000:01:00.0 --expect-uuid GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1 --width 1800 --height 1200
```

Final executable: `native/build/ui-integrated/ui/Release/druta-ui.exe`.
SHA-256: `1A87EB546CEF23449F93DCD91B2C446A207B16134C0DE786895454C35014B81B`.
Core DLL: `native/build/ui-integrated/ui/Release/drutacore.dll`.
SHA-256: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`;
unchanged from the previous checkpoint.

## Remaining work and qualification

The [provider map](2026-11-13-native-evidence-provider-map.md) remains open for
current-ROM semantic graph/role evidence, installed-scaffold receipts and
retained-request/policy providers needed by semantic REL/constants and scaffold
offsets. The [relationship-weight map](2026-11-13-relationship-weight-port-map.md)
remains open for the narrow persistent registry/VBIOS exception, including its
preview, journal, exact projection, explicit refresh, verification and removal.

The optional CUDA timing-capture fallback remains unported; its
[port map](2026-11-13-cuda-capture-port-map.md) retains workload, cancellation
and P2 qualification boundaries. The native temporary-hold path is separate.

The earlier [scope audit](2026-11-13-profile-scope-gap-map.md) identifies the
remaining Python low-power, PCIe speed, voltage-request and switch-delay imports,
the unscoped full-tune importer, and catalog/composition adapters for standalone
voltage, feedback, low-power and clock/PCIe families. Gate-dependent ordering,
whole-selection preflight, preservation of unknown fields and typed recovery
must be established for each composition. iCX remains standalone; timing and
memory-offset changes remain separate transactions. These restrictions and
missing adapters are not counted as completed parity.

Actual device-reset execution, device-specific physical tuning response,
controller/adapter coverage and Vista/7 execution remain separately unqualified.
No developer board or driver version is a universal capability allowlist. This
increment does not complete the overall parity objective.
