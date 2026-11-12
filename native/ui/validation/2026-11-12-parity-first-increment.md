# Classic / Focused parity implementation — 2026-11-12

This increment implements the first presentation and interaction findings from
the revision-pinned [Classic review](2026-11-12-verbose-parity-review.md) and
[Focused inventory](2026-11-12-focused-port-inventory.md). Two Sol workers owned
Classic presentation and Monitor bindings; a Terra worker handled Ctrl+H and
an independent telemetry-contract review. The root reviewed, integrated and
validated the combined changes. It is not full Python frontend parity.

## Implemented

- Classic uses the released six fixed knob widths (210/200/66/92/78/74), shared
  by ordinary and advanced rows. The two columns scroll horizontally when
  necessary. Labels wrap, sliders omit duplicate numbers, configured readbacks
  show units, and the integrated V/F editor remains below the knob region.
- Separate Segoe body/semibold headings and Consolas readings/log restore the
  typography, with fallback fonts. Control/Advanced/Monitor/Timings use actual
  tabs. The read-only log supports selection and Copy log.
- Both presentations expose seven telemetry tiles, throttle/performance reason
  statuses, power and utilization, PCIe counters, state and fans. Focused hides
  secondary detail by default; Classic expands it. History remains optional.
- Present zero, absent and stale are distinct. Integer PCIe counters retain
  full precision; unknown reason bits remain visible. Aggregate PCIe totals
  require all three understood current AER contributors, and historical
  baseline coverage is explicitly unverified. Raw private-clock arrays are
  not decoded or described as measured clocks by this change.
- TDP utilization uses raw board draw / reported enforced limit. Power-policy
  target percentage is separate. Shunt-corrected watts retain raw watts; no
  hardware request unit or background polling cadence changes.
- Ctrl+H initiates the retained selected-point hold across pages, including a
  closed/collapsed editor, and releases only the session's owned hold/recovery.
  The editor button shares command construction. Busy/text-entry/dirty/stale/
  disconnected guards prevent initiation; duplicate commands are suppressed.
  Existing fresh backend preflight and ownership checks remain authoritative.

## Validation

Windows 10 x64, owner's RTX 3070 Ti. Release build and bounded PE/import audit
passed. All **32 CTest suites passed**, including 993 general UI checks, the
dedicated Classic geometry suite, 43 Monitor-model checks, 8 Monitor-view
checks, 307 Advanced-view checks and 143 session checks.

The geometry fixture covers normal/domain/rail alignment, long labels,
horizontal overflow, stable content height, integrated curve placement,
partial capabilities and fallback fonts at 100% and 150% DPI. Shortcut tests
exercise actual ImGui key dispatch with the popup focused, other pages and a
collapsed editor, plus dirty/stale/text-entry/recovery cases without a GPU.
Monitor tests cover independent masks, large integer counters, unknown types,
partial aggregate coverage, real zeroes, invalid values and composed TDP
staleness. Initial integration failures were resolved by updating assertions
for native tabs/nested scrolling and modeling fresh curve revisions correctly
after simulated stale/disconnected states; production freshness gates remain.

Six final read-only native renderer captures exited with code 0 in
`native/build/ui-integrated/parity-final/`: Classic Daily at 1800x1200 and
1024x768, Classic Daily at 150% DPI, Focused Monitor at both sizes, and Classic
Monitor at 1800x1200. Visual review confirmed fixed geometry, intended overflow,
readable panels and the corrected TDP/policy distinction. No tuning command,
controller write, driver restart or profile application was issued for this
increment. Shortcut dispatch was tested headlessly, not with physical inputs.

Final application SHA-256:
`CEB442E9CE60A95A6A509254AC0B33E5D039E36E959D492247DCA7DD6AEE7C02`.
Unchanged core DLL SHA-256:
`687DFF65557C785388D01887FF36FCC20E6C43FB2FF3D0E1D426D03F25012F64`.

## Remaining

The reviews still track the device/panic menu, page tear-out, broader qualified
Advanced services, profile browser and additional scopes, richer timing
workflows, curve consequences/hard de-flatten, I2C presentation/discovery
polish, and qualified clock-domain/measured-clock evidence. A separate existing
session limitation remains: a valid partial fan collection does not preserve
each previously valid missing duty/RPM field as stale; Monitor faithfully shows
the supplied per-fan availability. This increment does not change that backend
merge behavior or establish Vista/7 execution compatibility.
