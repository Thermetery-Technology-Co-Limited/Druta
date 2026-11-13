# Released Python 1.9.1 parity reconciliation

Read-only source reconciliation, 2026-11-13. Reference: Python release `fa6ee83`.
Compared the [Classic review](2026-11-12-verbose-parity-review.md),
[Focused inventory](2026-11-12-focused-port-inventory.md),
[completion ledger](PARITY-COMPLETION.md) and historical
[profile scope audit](2026-11-13-profile-scope-gap-map.md) with the current native source.
Python line references below are pinned to `fa6ee83`; native references describe
this integration working tree. No build, GPU operation or OS configuration change
was performed for this reconciliation.

## Original review mapping

| Original requirement | Integrated implementation / recorded checkpoint |
|---|---|
| Classic six-column geometry, typography, wrapping, formats, units, real tabs and copyable log | [First checkpoint](2026-11-12-parity-first-increment.md); `classic_layout.h`, Classic branches in `ui_view.cpp`, font setup in `main.cpp`. |
| Floating/dock-back pages and armed Reset | [Second checkpoint](2026-11-12-parity-second-increment.md); shared session/drafts, one rendering owner, timeout/adapter cancellation. |
| Monitor tiles, reason lamps, sensors, utilization, PCIe, histories, clock-domain table and clock evidence | [First](2026-11-12-parity-first-increment.md) and [second](2026-11-12-parity-second-increment.md) checkpoints; `monitor_view.cpp`, `clock_monitor_service.cpp` and native domain classifier. |
| Device report/copy/save, explicit-target second window, capability refresh, Help/licences/About | [Third checkpoint](2026-11-12-parity-third-increment.md); `app_actions.*`, `app_actions_view.cpp`. |
| Exact-target panic/recovery workflow | [Eighth checkpoint](2026-11-13-parity-eighth-increment.md); `device_restart.*`, `device_recovery.*`. Implementation is distinct from physical restart validation. |
| Ctrl+H hold/release, Classic keyboard scope, guides/nudges, planner consequences and acknowledged hard de-flatten | [First](2026-11-12-parity-first-increment.md) and [second](2026-11-12-parity-second-increment.md) checkpoints; `ui_view.cpp`, `curve_planner.cpp`. |
| Manual curve, rail/policy and fan profiles; selected contents, typed imports, catalog/combine and grouped partial-failure Undo | [Second](2026-11-12-parity-second-increment.md), [third](2026-11-12-parity-third-increment.md), [sixth](2026-11-13-parity-sixth-increment.md), [ninth](2026-11-13-parity-ninth-increment.md) and [tenth](2026-11-13-parity-tenth-increment.md) checkpoints. The remaining aggregate capture and disk-history workflow is identified below; import/composition is not absent. |
| General min/max/lock-max and legacy P0/max-fan workflow | [Fourth checkpoint](2026-11-12-parity-fourth-increment.md); `frequency_lock.*`, `frequency_lock_view.*` and CoreClient handover. |
| PERF diagnostics and arbitration | [Fourth](2026-11-12-parity-fourth-increment.md) and [fifth](2026-11-13-parity-fifth-increment.md) checkpoints; independent read-only services/views. |
| Feedback/integral ratios, admission/VF flags, six-word requests, device delays and manual/catalog profiles | [Fifth](2026-11-13-parity-fifth-increment.md), [seventh](2026-11-13-parity-seventh-increment.md), [ninth](2026-11-13-parity-ninth-increment.md), [tenth](2026-11-13-parity-tenth-increment.md) and [eleventh](2026-11-13-parity-eleventh-increment.md) checkpoints. |
| REL/ALT/OV/VMIN production evidence and selected operations | [Twentieth checkpoint](2026-11-13-parity-twentieth-increment.md); current-ROM graph, raw retained requests and independent policy evidence. Additional layouts remain deferred. |
| Installed signed-offset recognition, selected Apply/readback/initial/Undo/recovery and native/Python profiles | [Twenty-first](2026-11-13-parity-twenty-first-increment.md) and [twenty-second](2026-11-13-parity-twenty-second-increment.md) checkpoints; distinct installed-head provider/service, not absolute-constant substitution. |
| GR/individual/clock gating, idle, adaptive, DIFR, PCIe and qualified profile handovers | [Eighth](2026-11-13-parity-eighth-increment.md), [twelfth](2026-11-13-parity-twelfth-increment.md) and [thirteenth](2026-11-13-parity-thirteenth-increment.md) checkpoints. Source-unqualified modes remain named limitations. |
| Timing staging/batches, initial restore, save/load/exchange, diagnostics, comparisons and capture | [Fourth](2026-11-12-parity-fourth-increment.md), [sixth](2026-11-13-parity-sixth-increment.md), [seventh](2026-11-13-parity-seventh-increment.md), [ninth](2026-11-13-parity-ninth-increment.md), [fourteenth](2026-11-13-parity-fourteenth-increment.md) and [nineteenth](2026-11-13-parity-nineteenth-increment.md) checkpoints. Helper registration/setup remains below. |
| Classic I2C control geometry/order/help, NCT Zero all, remembered read-only discovery and cancellation | [Second](2026-11-12-parity-second-increment.md) and [sixth](2026-11-13-parity-sixth-increment.md) checkpoints. |
| NCT/uP/NCP/coarse-controller imports, timing+memory and qualified iCX composition | [Fourteenth](2026-11-13-parity-fourteenth-increment.md), [fifteenth](2026-11-13-parity-fifteenth-increment.md), [sixteenth](2026-11-13-parity-sixteenth-increment.md) and [seventeenth](2026-11-13-parity-seventeenth-increment.md) checkpoints. |
| Policy annotations/connector presets, reviewed maxima and full-profile labels | [Seventh](2026-11-13-parity-seventh-increment.md), [eighth](2026-11-13-parity-eighth-increment.md) and [eighteenth](2026-11-13-parity-eighteenth-increment.md) checkpoints. |
| Persistent relationship weights | [Twenty-third checkpoint](2026-11-13-parity-twenty-third-increment.md) records Read/Preview. Increment 24 source now includes `power_weight_receipt.*`, `power_weight_transaction.*`, `power_weight_registry_write.*` and production host integration for activation/removal. The subsequent [twenty-fourth checkpoint](2026-11-13-parity-twenty-fourth-increment.md) records a passing integrated build, 147/147 tests and 23 renderer phases. Physical weight activation remains unqualified on this card. |

## Three concrete remaining workflows

### 1. Disk-backed pre-write snapshots and saved-profile/autosave browsing

Released `druta.py:10608–10655` automatically captures an Undo snapshot before
covered writes. The explicit list at `:10614–10623` includes V/F Apply, curve
Reset, Re-phase, core-offset Apply, Reset all and profile Load; ordinary tuning
uses scoped snapshots and clock locks retain Release. Fan capture failure has
its own refusal behavior (`:10635–10644`), while the documented V/F path reports
an incomplete/failed snapshot without falsely calling it a usable Undo point.

Released `profiles.py:47–50,1437–1505` uses schema 2/full or existing typed scoped
JSON, `autosave-<action-slug>-YYYYMMDD-HHMMSS.mmm.json`, keeps 20 snapshots and
orders profiles by `saved_ts` with mtime fallback. `paths.py::profile_dir` selects
source/bundle `profiles/` or installed `%LOCALAPPDATA%/Thermetery/Druta/profiles`.
`druta.py:10811–10887` lists files newest first with saved time, contents and
manual/autosave distinction; `:11504–11529` explicitly loads the newest autosave
for Undo and captures the reverse point.

Native `core_client.cpp:450` keeps a process-local 32-entry Undo vector.
`profile_catalog_view.cpp:38–44` offers chosen files and individual scope capture;
there is no equivalent automatic disk snapshot producer, directory/history
refresh or newest-autosave Undo route. Existing typed capture/catalog loaders
are reusable, but process-local ownership receipts must not become disk replay
payloads. Loading an Undo file remains explicit and freshly qualified.

Disk snapshots do not apply anything on startup or after reset. This workflow
fits the authorized manual saved-profile/Undo scope; startup replay does not.

### 2. Aggregate Full tune capture/save

Released `druta.py:10705–10714` offers a `Full tune` contents choice;
`:10689–10695` invokes `profiles.capture(gpu, rail)` and saves that snapshot.
The historical full-tune/import gap is therefore not solely a parser concern.

Native `core_client.cpp:1236–1290` saves Daily/Advanced controls. The catalog's
capture choices (`profile_catalog_view.cpp:42–44`) are individual scopes; it
can compose previously captured files but has no equivalent aggregate current
Full tune capture action. Full-tune **import**, selected expansion, metadata and
qualified composition have been implemented in checkpoints 13–18. Reuse those
typed scopes for capture, with honest unavailable/omitted contents, rather than
reintroducing opaque state or startup application.

### 3. Remembered nvtune location, Forget and test-signing setup

Released `timings.py:134–149,240–274` remembers `nvtune_exe` in
`%LOCALAPPDATA%/Thermetery/Druta/nvtune.json` (home fallback), reads UTF-8 with
optional BOM and atomically updates/clears registration. Released
`druta.py:13552–13590` implements Locate, registration, immediate recapture and
Forget with fallback to derived locations.

Native `main.cpp`'s Locate handler submits a selected helper to CoreClient;
`core_client.cpp:2082–2088` requires a nonempty path and stores it in the live
service. `app_actions_view.cpp:54–59` offers navigation and Locate. There is no
corresponding saved-location registration/Forget workflow; native helper
selection is session-local apart from explicit environment/launch options.

Released `druta.py:11647–11656,12095–12142,13026–13105` also provides the test-signing
entry, copyable setup/undo commands, current Secure Boot/admin/BitLocker context,
click-time checks and command-by-command outcome/reboot handling. Exact released
command lists are at `:14511–14519`. Native timing/helper views contain neither
that setup dialog nor its explicit action. The existing helper picker and
runtime diagnostics do not complete this supporting workflow. This records the
released behavior; it does not claim the source's device-specific recipe is a
universal setup requirement or authorize any OS change during this audit.

## Approved differences and deferred scope

Preserve weighted risk colors, Focused routing/fluid layout, Classic integrated
versus Focused popup V/F editing, explicit staged curve/profile Apply, captured
initial state distinct from factory defaults, and session-only ordinary tuning.
Do not port automatic startup or post-reset tuning replay.

Additional ROM/runtime layouts, unknown-prior adaptive restoration, simultaneous
shared GR/count ownership, BLCG ENABLE, lower-P2 band equivalence and broader iCX
clamp/timing/controller combinations remain deferred research/extensions, not
missing released behavior to invent. Expanded physical-effect, PnP and legacy
Windows qualification campaigns are also separate from this bounded source port.
General VBIOS/registry editing and scaffold installation remain outside the
narrow authorized persistent relationship-weight exception.

No physical voltage, controller, timing, weight activation/removal or restart
qualification is established by this reconciliation. The original inventories
and profile audit are historical source maps, not current lists of absent
services. Overall parity remains open for the three workflows above and
their final integrated acceptance.
