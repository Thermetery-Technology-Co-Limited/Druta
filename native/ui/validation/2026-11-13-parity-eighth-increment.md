# Eighth parity increment: power saving, recovery and annotations

Reference: Python release `fa6ee83`; native parent `5f6fb49`.
The [full completion ledger](PARITY-COMPLETION.md) remains active.

## Implemented scope

- Advanced and the Clocks menu expose independent power-saving and clock-gating
  families in both skins. Reads are explicit. Ordinary telemetry and rendering
  do not acquire those tables or create owned requests.
- GR/individual gating and DIFR preserve counted/profiler ownership. Release and
  close release only this application's known requests; uncertain counts require
  explicit recovery. Idle thresholds use current raw/scaled evidence. Adaptive
  intent remains distinct from aggregate state, and Allow does not restore idle
  thresholds. Manual profiles stage requests; each power-saving Apply has its own
  Undo rather than implying a whole-profile transaction.
- Engine, block and second-level clock gating use source-qualified predicates and
  register layouts. Independent configuration evidence does not prove physical
  gating. Clock/PCIe profiles preflight the complete selection, preserve unknown
  fields and retain issued/uncertain recovery across partial failure. A pending
  PCIe receipt survives reads and unrelated operations; close does not restore an
  ordinary PCIe target automatically.
- Current-target binding, partial capabilities and transient reads are handled
  independently. Initial unsupported readings are not labelled stale. Invalid,
  malformed and identity failures do not receive transient retries. Auxiliary
  ownership and in-flight operations prevent adapter switching; Pause introduces
  no background polling for these services.
- Policy annotations add source connector presets, same-channel labels and
  explicit Python name-store import. Unknown/unmatched metadata survives. Current
  colors use observed current and a labelled even-split-per-wire assumption;
  requested current limits are not presented as measured wire current.
- Panic / restart GPU resolves exactly one selected NVIDIA PCI display instance.
  The helper waits for the parent process to exit before a bounded PnP restart,
  writes a durable result and relaunches with only target/core/receipt arguments.
  No profile or tuning request is replayed. Discovery/preparation runs off the
  renderer and the selected session is revalidated before handoff. Timeout,
  reboot-required and uncertain outcomes remain explicit; no system reboot is
  issued. The restart-specific shutdown drains admitted worker work and skips
  restoration/destruction of uncertain backend requests until process teardown.
  Ordinary close retains its owned-request cleanup and refusal behavior.

All ordinary tuning remains session-only. Saved profiles require explicit loading
and Apply in the current session. Missing UUIDs do not by themselves disable paired
runtime controls; saved profile bindings still require a UUID.

## Review and validation

Two Sol implementations and bounded Terra annotation/recovery/test work were
integrated by the root agent. Cross-review corrected initial stale classification,
source role names, adaptive binding, counted cleanup, partial Undo, PCIe recovery
receipts and invalid-error retry. Root review covered shared dispatch, worker
shutdown, header placement, exact target handoff and read-only qualification.

- Release build and all **84 CTest suites passed** (13.94 seconds). Tests cover service protocols,
  partial/transient capabilities, explicit profile staging, actual CoreClient
  integration with fake transports, ownership/switching, paused operation, PnP
  target ambiguity, recovery receipts and restart shutdown without restoration.
- All **18 live renderer phases passed** on the selected RTX 3070 Ti. Explicit
  Pause and default minimization each produced zero frames and zero telemetry
  reads. Always refresh continued rendering and telemetry while minimized. Both
  new panel captures were visually inspected. They show the initial uninspected
  state; populated interaction coverage comes from the fixture tests. The report's
  four device resets are Direct3D renderer resets, not GPU-driver resets.
- Explicit read-only qualification found GR/MS gating and source-qualified
  ELCG/BLCG/SLCG inspection on this adapter. Individual optional engines and DIFR
  were readable without qualifying writes. Idle/adaptive dispatch and PCIe target
  layouts remain outside traced support here. SetupAPI resolved one matching
  present, started display device. No request was owned, no possible change was
  reported and close succeeded.
- No new tuning SET, controller write, PnP restart or persistent registry change
  was issued on hardware in this checkpoint. Protocol tests and readable support
  do not prove physical gating, PCIe response, other boards or Vista/7 execution.

Current adapter: RTX 3070 Ti, PCI `0000:01:00.0`, UUID
`GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`, Windows 10 x64.

[Renderer report](2026-11-13-parity-eighth-smoke.json) and
[read-only qualification report](2026-11-13-parity-eighth-saving-read.json) are
retained with this checkpoint. Local build/test logs and renderer BMPs are under
`native/build/ui-integrated/`, using the `parity-eighth-` prefix.

SHA-256:

- `druta-ui.exe`: `6F26656825AD42E7D7549F1C8634D554ADBFB178E4A5523688E546AD74CFE58F`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The DLL is unchanged from the seventh checkpoint. The final shutdown regression
also blocks a post-action getter, requests restart, and proves that later getters,
reads and cleanup are not admitted after the blocked call drains.

## Remaining work

The current-ROM graph/role, installed-scaffold receipt and retained-request/policy
providers for semantic REL/constants and scaffold offsets remain open. The
[provider map](2026-11-13-native-evidence-provider-map.md) traces those requirements.
The [relationship-weight map](2026-11-13-relationship-weight-port-map.md) preserves
the separate persistent exception's preview, journal, exact registry projection,
explicit refresh, image/INFO verification and three-way removal requirements.

The [profile scope audit](2026-11-13-profile-scope-gap-map.md) separates missing
gate/flag, power-integral and per-fan manual profiles from standalone native
profiles awaiting Python-import/catalog composition. Full-tune and timing exchange
imports remain open too. The [CUDA timing-capture map](2026-11-13-cuda-capture-port-map.md)
records the missing fallback, cancellation and P2 qualification boundaries.
This checkpoint does not complete the parity goal or convert unimplemented scopes
into approved omissions.
