# Classic / Verbose parity review — 2026-11-12

Reference: released Python frontend at `fa6ee83`, principally `src/druta/druta.py` and `src/druta/registry_panel.py`. Native baseline: `d5034e82169a673d64198e6d9a54680877ad67bd`. Python line references below are **at fa6ee83**, not current working-tree Python (which has additional backend/refresh changes). Native references are **at d5034e8**, not the root agent's concurrent working-tree edits. References can be reproduced using `git show fa6ee83:path` or `git show d5034e8:path` with one-based line numbering. Scope is static source review plus the existing `native/build/ui-integrated/daily-shunt-final/08-classic.png` capture; no application launch, build, or GPU operation was performed.

Classic currently reproduces the broad Control page grouping, but it is not yet a wholesale 1.9.1 frontend port. The largest visual difference is geometry; the largest functionality differences are Advanced, Monitor, recovery, and profile/timing workflows. “Missing backend” below means missing from the native frontend's live data/command/execution contract, not proof that no reusable native decoder or lower-level export exists anywhere in the repository.

## Approved differences: preserve these

- Risk points are XOC +1, Rail limits +2, I2C +3; bands green 0 / yellow 1 / amber 2 / red 3 / crimson 4+. Unlocked at score zero is green. Do not replace these with old Python theme/risk policy. Native `advanced_view.cpp:125` (`session_risk`) and `:136` (`draw_mode_bar`) implement this policy.
- Integrated V/F editor, large draggable points, WASD and Shift controls are required. Native `ui_view.cpp:661–704`, `:704–801`, `:1278` already provide them; do not regress to a separate editor only. Native local stage/apply separation and explicit manual profile load are compatible with the standing instructions.
- Shunt correction has been ported and is intentionally beside power: native `ui_view.cpp:584`, `shunt_view.cpp`, `shunt_model.cpp`. It is not a missing feature merely because its old Device-menu entry is absent.
- The shunt helper's new “Use for all rails” and “Copy value” convenience actions are approved concurrent work owned by the Terra helper. They are not remaining handoff gaps in this review.
- Additional-memory domain 2 is being moved from Focused daily to Advanced; NVVDD/MSVDD offsets from Focused daily to Rails by the root agent. This is coordinated concurrent work, not a Classic regression. Classic must retain released Control grouping.
- Do not port automatic startup or post-reset tuning application. Saved settings require explicit application in the current session. Registry/VBIOS power relationship-weight overrides are the standing narrow persistence exception.

## Prioritized mismatch inventory

### P1 — Control geometry is still an approximation (missing UI)

Evidence: Python `druta.py:1959–1973` defines six **fixed** knob columns `(210, 200, 66, 92, 78, 74)` plus 34 px padding; `:3410–3438` uses two fixed-width outer cells. Native `ui_view.cpp:1213–1220` uses flexible label/slider weights 1.3:1.2, narrower 60 px action columns, and `:1246` stretches both outer columns equally. Advanced row helpers independently repeat flexible geometry (`advanced_view.cpp:22–35`). In the supplied 1800×1200 capture, “Additional Memory Clock Off…” clips while its adjacent slider still consumes its share; readback and inputs vary in density. Native sliders also print a number on the bar (`ui_view.cpp:608–609`, `advanced_view.cpp:63`), unlike Python's deliberately blank slider format in `slider_row` (`druta.py:3764–3778`), so each request appears twice.

Target: derive Classic geometry from one fixed six-column specification, including private-domain/rail/current rows; preserve a clear horizontal overflow policy when the window cannot fit both columns. Suppress the redundant slider number and wrap the intentionally long labels as Python does. Keep Focused responsive geometry separate.

Acceptance: at identical client dimensions and DPI, align every Apply/Stock/Auto column across each Classic column; all long labels remain readable; no header overlaps another column; slider/input/readback widths match the reference specification. Capture at reference desktop size and 1024×768, with long domain labels and partial capabilities.

### P1 — Header lacks the recovery action and menu structure (missing UI + native workflow)

Evidence: Python `druta.py:14776–14814` (`build_shared_header`) reserves a 360 px upper-right column for the PnP Panic Button; `:11592–11655` supplies Device report/copy, capability refresh, restart, second-window, and nvtune setup actions. Native `ui_view.cpp:1154–1194` replaces that area with a second row of Save/Load/refresh/presentation controls and has no panic action. `backend.h:87–92` has no device-restart command; `main.cpp` handles profile/helper dialogs but no corresponding PnP recovery flow. Capability refresh exists as Advanced > Diagnostics > Refresh details (`ui_view.cpp:1311–1314`), so it is misplaced rather than absent.

Target: restore Classic header/menu hierarchy, retaining the newly required presentation/refresh controls in an unobtrusive location. Implement a selected-adapter PnP recovery workflow using existing repository recovery abstractions, including visible outcome and fresh adapter re-open, without replaying session tuning. Device report/copy and second-window actions need explicit native plumbing.

Acceptance: panic remains visible while any page scrolls; acts only on the displayed adapter; cancellation writes nothing; failed recovery reports why; successful recovery does not reapply saved tuning. Capability refresh and report/copy are reachable without leaving the current page. Do not test a hardware reset as part of a visual-only increment.

### P1 — Advanced page is mostly unported, not just differently labeled (missing backend + UI)

Evidence: Python `registry_panel.py:105–111` builds registry, live voltage, live power and diagnostics. `:121–156` has read/preview/apply/restart/restore registry overrides and DIFR configuration. `:157–327` has voltage-request gate, VF monotonicity/smoothing, REL bounds, voltage constants and switch delays. `:329–377` has ELPG, individual engine gating, idle thresholds, adaptive policy, engine/block/SLCG gating, DIFR disable and manual PCIe speed requests. `:378+` exposes policy/arbitration/relationship diagnostics. Native `ui_view.cpp:1303–1315` explicitly displays “Native workflow pending” for Registry / BIOS; “Session controls” calls `draw_advanced_controls`, whose actual fields are only offsets, power policies and rail limits (`advanced_view.cpp:165–227`). Native `LiveData` and `Command` (`backend.h:56–118`) contain no counterpart requests/snapshots for these families. Identity alone (`live_identity`) is not these diagnostics.

Target: port one independently qualified family at a time through explicit typed native snapshot/command paths, preserving capability checks, initial state, readback, owned-request lifecycle, bounded retries, scoped Undo and manual save/load. First restore section hierarchy and scope notes so the incomplete surface is accurately visible. Registry relationship-weight work is the authorized persistence exception; do not infer permission to replay opaque configuration or generalize hardware measurements.

Acceptance: for each family, alternate identities, partial capability and transient-read fixtures preserve established controls; selected-field writes/readback/restore are validated; session-only requests never auto-reapply. Disabled unexplained placeholders are not completion. Unresolved firmware research remains parked.

### P1 — Monitor loses substantial existing telemetry (mostly missing UI; evidence model additionally needed)

Evidence: Python `druta.py:658–703` specifies seven tiles in order Core, XBAR, Memory, Edge, Hotspot, Power, Vcore; `:704–794` builds throttle/perf-decrease lamps, power/utilization bars and paired PCIe/State panels; `:827–888` builds all-clock-domain table. Native `ui_view.cpp:1317–1351` shows six tiles (Core, Memory, XBAR, Temperature, Power, Vcore), drops Hotspot, throttle/perf reasons, domain table, bus utilization, PCIe errors and power split, and replaces panels with a few vertically stacked text rows. The underlying `dc_telemetry` already has relevant present bits (`native/include/drutacore/drutacore.h:289–345`: hotspot, split, BUS utilization, event/perf masks, PCIe error, private clocks). Thus these are not missing merely because backend telemetry is unavailable.

Python's measured-clock evidence is a separate behavior: `druta.py:1035–1112`, `realclock.py:171–200` distinguish programmed clocks, measured B, identical/unproven values, load settling and stale reads. Native Monitor renders programmed scalar values and has no equivalent evidence tracker in `LiveData`/`SessionSnapshot`. Do not simply label raw B “measured”.

Target: restore tile order, typography, seven bars, paired panels, reason lamps and domain table using independent present bits; port the clock-evidence state machine separately with accurate uncertain/stale states. Keep native history as an additional collapsible section.

Acceptance: fixtures activate each event/perf lamp, suppress unsupported fields without removing unrelated tiles, show hotspot separately, preserve absent versus zero values, and cover B=A/unproven/stale/load transitions. Matched-DPI screenshots show seven tiles and the released panel arrangement.

### P1 — Profiles exist but full-tune/profile-browser parity does not (missing workflow + backend scope)

Evidence: Python `druta.py:11894–11990`, `:10811+` has Contents selection, full tune and scoped saves, a profile browser, autosave/undo entries, combine and per-profile outcomes. Native `main.cpp:229–250` offers OS Save/Load dialogs; native `core_client.cpp:643–706` supports qualified daily/advanced fields and imported Python `scope=tuning_action`, but explicitly says core curve, individual policies and rail limits are not saved (`:699`). Other Python scopes are refused (`:603–604`). I2C has separate prepare/save/load/stage/apply commands (`backend.h:90`), so it is present through its own qualified workflow, not globally missing.

Target: add a native manual profile browser/contents summary and compose only understood qualified scopes, including explicit omissions before loading. Extend curve/policy/rail scope support in bounded increments with exact identity and readback. Preserve native rejection of unknown/incomplete formats; do not add startup loading.

Acceptance: browser shows included/omitted scopes before applying; alternate-GPU and incomplete/unknown scopes refuse without writes; a combined explicit load captures meaningful Undo and reports partial failures; startup/reset never applies these requests. Do not advertise a full tune until all advertised scopes are actually saved/restorable.

### P2 — V/F workflows differ beyond the approved integrated editor (missing UI/planner + behavior)

Evidence: Python `druta.py:8476–8575`, `:9969+` includes point/index controls, continuously visible plan/consequence information and planner-specific details, and `:8576–8650` has acknowledged Hard de-flatten. Native `ui_view.cpp:886–980` has staging for de-flatten, ramp and re-phase only; `curve_planner.h` contains only those plan kinds. Its plan summary is created after staging (`ui_view.cpp:946–961`) rather than a complete continuously updated pre-write description. Native re-phase is staged; Python describes re-phase as a direct write with autosave (`druta.py:48–53`). Explicit staging is an already approved native design choice: preserve it and document the reference behavior difference; it is not a remaining approval or implementation gap.

Target: restore continuously visible predicted consequences/uncertainty and planner parameter organization. Evaluate Hard de-flatten as a separate qualified feature with its required explicit acknowledgement; do not silently substitute normal de-flatten. Preserve the already approved stage/apply design and label its behavior clearly.

Acceptance: every curve Apply can be reviewed before pressing it; changing drafts updates its plan; no planner writes implicitly; each implemented planner has observable geometry and failure tests. Acknowledgement cannot be replaced by a tooltip.

### P2 — Ctrl+H is only half implemented; keyboard scope differs (missing UI dispatch)

Evidence: Python `druta.py:9524` and `:11832–11872` define window-wide Ctrl+H as selected-point hold/release toggle, with text-entry guards. Native `ui_view.cpp:1500–1504` only dispatches Ctrl+H inside the already-owned/recovery hold branch, so it cannot start a hold. Native “Hold selected point” (`:965–970`) proves the hold backend exists. `curve_keyboard` (`:662–698`) is scoped to the focused editor child, whereas Python handlers operate window-wide when not typing. WASD, Shift and Ctrl+Z/Y are already implemented and must not be reported missing.

Target: share hold-command construction between button and guarded global toggle; agree whether Classic should retain the released window-wide editing scope while Focused remains editor-local. Add visible shortcut help with card-derived bins.

Acceptance: Ctrl+H holds selected captured point when eligible, releases the owned request on second press, never releases another client's lock, and does nothing while typing or against stale drafts; test page-collapsed/other-page states and avoid duplicate dispatch. No GPU write is needed for dispatcher tests.

### P2 — Timing editor is a reduced single-field tool (missing UI + service/model)

Evidence: Python `druta.py:12360–12631` builds setup instructions/test-signing entry, editable new-value table, staged multi-field apply/revert, Restore stock, timing profile save/load, read/hold-P0, reread, auto-capture, decoded ns and compared captures/partition divergence panels (`:12601–12631`). Native `timings_view.cpp:83–119` offers helper locate/read, a four-column cycle table, selected one-field preview/apply and Undo. `timing_model.h:10–16` holds one snapshot/one preview; `backend.h:91` exposes only helper/read/preview/apply/undo. This is a real qualified timing write path, not a missing Timings tab. `timings_service.cpp:141` already pins the transport for capture, so “hold P0/read” must be traced before adding another lock mechanism.

Target: restore setup/help and richer read-only tables first; extend explicit draft sets and timing-profile model separately, reusing selected-field preview and transport identity checks. Keep uncertain/inferred/structural writes refused rather than restoring a broader force button blindly.

Acceptance: correct units and unsupported/unconfirmed states; multiple staged cells survive reread/failure; apply preserves unrelated register bits and validates selected adapter/band; profile mismatch refuses; comparison/divergence uses actual captured data. Any broader force path needs its own qualification, not label parity.

### P2 — Native reset is immediate instead of armed (behavior divergence, existing backend)

Evidence: Python `druta.py:61–65`, `:8185+` explicitly arms Reset all on first press. Native `ui_view.cpp:1486–1487` queues `reset_controls` immediately; `core_client.cpp:790+` performs the reset through the real backend and records scoped recoverable state. This is not an inert button.

Target: restore first-press arm/consequence display and second-press execution for Classic, with arm cancellation on adapter changes/time expiry; keep existing qualified reset and scoped Undo.

Acceptance: first press causes zero backend writes, second matching press dispatches once; timeout/adapter switch cancels; partial capture/readback failures remain explicit. No new hardware reset needed to verify the UI state machine.

### P3 — Typography, tabs, log and floating pages remain visually different (missing UI)

Evidence: Python `druta.py:620–638` loads Segoe UI 16, Segoe Semibold 26/21 and Consolas 14. Native `main.cpp:395–411` loads only Segoe UI 18 and uses rescaling (`ui_view.cpp:1157`, `:1165`, `:1444`), so semibold title/selector and mono live/log typography are lost. Native `classic_tabs` (`:1196–1210`) draws buttons, whereas Python uses actual tabs and draggable floating/dock-back pages (`druta.py:649–656`, tear-page code around `:1550+`). Only V/F has a separate native window. Native log (`ui_view.cpp:1353–1359`) is text in a child rather than selectable/copyable monospaced multiline text (`druta.py:3595–3600`); its height is similar (150).

Target: load distinct existing Windows font faces for Classic, use real tabs with matching spacing/active colors and implement same-window page float/dock-back; provide copyable log/report surfaces. Preserve approved risk theme over the old neutral palette.

Acceptance: title/selector weight and mono alignment match at equal DPI; tab order Control/Advanced/Monitor/Timings matches; floating a page keeps one live session and docks without losing edits; logs are copyable. Include font-fallback coverage.

### P3 — Power-policy annotations and bulk workflow are reduced (missing UI/model)

Evidence: Python `druta.py:2619–2626`, `:2668–2738` includes per-policy connector/custom annotations and Max all; native `advanced_view.cpp:276–286` (`draw_power_policy_controls`) simply iterates label/value/Apply/Default. `backend.h:31` stores label/binding/core-current classification but no editable connector note. Current-policy backend writes and core-current identification already exist; do not report them as absent.

Target: port editable user annotations and clear bulk preview using actual policy IDs/bindings, not guessed labels or developer-board topology.

Acceptance: alternate policy ordering/IDs and unnamed/partial policy tables retain correct request binding; annotations do not change policy identity; bulk action shows coupled effects and has meaningful Undo/failure reporting.

## I2C findings and qualification boundary

Native I2C is substantially functional: family scan/full scan/manual routing and candidate selection (`i2c_view.cpp:233–275`), recipe/read/verify (`:279–303`), field slider/input/readback/reset (`:101–186`), NCT3933 raw/mapped selection (`:70–100`), iCX profile commands, owned release/recovery and verification paths are present. It must not be summarized as a pending backend based on an old fallback string in `advanced_view.cpp`.

The visible parity gap is its separate five-column stretch table with generic family workflow (`i2c_view.cpp:101–107`) rather than each released controller's six-column controls and explanatory section placement. Classic's I2C section defaults closed (`ui_view.cpp:1277`), while Python defaults open when discovered candidates exist (`druta.py:3588–3590`). Restore per-family row/order/help geometry where useful, but preserve native stale-field caching (`i2c_view.cpp:38–45`), selected-route revision binding and guarded apply conditions (`:122–125`). Candidate/read/verify/apply are distinct capabilities; no missing or disabled write should be “fixed” by bypassing controller qualification. Existing `validation/2026-11-12-i2c-parity.md` and qualification tests should govern write scope, not the smoke capture's one adapter.

Bounded acceptance: render fixture screenshots for every supported family, telemetry-only/partial fields, owned/recovery states and transient reads; compare labels/units/order/help to the corresponding released controller implementation. Only a separately authorized qualification task may exercise hardware writes.

## Suggested bounded implementation order

1. Classic geometry/fonts/tab/header placement and copyable log, plus Monitor rendering of telemetry already carried by snapshots. Static/fixture render verification only.
2. Ctrl+H shared dispatcher, armed Reset, visible curve consequences, capability/report navigation. Mock command and failure-state coverage.
3. Manual profile browser/scope additions and read-only Advanced/timing diagnostics, one understood scope at a time.
4. Independently qualified Advanced/timing/backend increments. Park unresolved controls; never turn visual parity into opaque-state replay.

The smoke capture confirms clipping/density and current green unlocked styling on that captured adapter/configuration only. It does not establish universal controls, voltage bases, rail topology, safe ranges, or successful hardware behavior. No implementation changes were made by this review.
