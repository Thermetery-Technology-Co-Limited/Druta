# Classic / Verbose against Python 1.9.1

Parity check on 2026-11-15 at 539bb9d: native Classic / Verbose beside the released Python 1.9.1
frontend, both reading one RTX 3090 Ti at a 1528x1261 client area. 143 differences were found. None needs
anything Direct3D 9 lacks: both frontends draw with Dear ImGui. The evidence covers this one card.

| Cause | High impact | Medium | Low |
|---|---|---|---|
| Port choice (ordinary ImGui code written differently) | 19 | 48 | 39 |
| Not ported | 7 | 14 | 1 |
| Recorded as deliberate in the validation docs | 2 | 4 | 4 |
| Defect | 1 | 2 | 1 |
| Build dependency (font rasteriser) | 0 | 0 | 1 |

## Fix order

Live readbacks first, then high, medium and low impact.

| Step | Scope | Findings | Status |
|---|---|---|---|
| 1 | Live readbacks: domain clocks, true memory MHz, power, current, fan, delta sign | 9 | done |
| 2 | High: density and header, rail summary, V/F editor, Monitor cards and bars, Timings, Advanced sections | 26 | done, except the DIFR read (see Open) |
| 3 | Medium: palette and menus, Control rows, V/F details, Monitor panels, Timings layout, Advanced panels | 58 | done, except the lines under Open |
| 4 | Low: wording, widths, tooltips, legends, labels | 40 | done, except the rows under Kept and Open |
| 5 | Build dependency: FreeType text rasterisation | 1 | done (FreeType 2.15.0; 1.9.1's Dear PyGui ships 2.10.1) |

Classic text now measures as Python's on this card: the 26 px title 202 px wide in both, the mono read-out
477 px, body labels to the pixel. Focused keeps stb_truetype.

A fresh audit after step 4 raised 61 more points (wording, pixel offsets, log lines, tests). They are
fixed except where Kept or Open says otherwise. It also found two reads that had reached Focused (a
service-manager query and a clock-domain call per poll); both are Classic-only again, with tests.

Verification is read-only on the card: screenshots beside Python's, and the headless suites. No write
path was exercised on hardware; new write paths (linked rail Apply, "Restore raised limits now") are
covered by fake-backend tests only.

## Kept as they are

Nine of the ten rows recorded as deliberate stay: the risk colour tint (four rows, one per page), the
shunt-mod button beside the power limit, I2C "Advanced routing", Monitor History plots, Timings "Restore
captured initial" and the absent force checkbox. The tenth, the Registry / BIOS placeholder, keeps its
deferral and gets Python's label and a scope note.

Also kept, by decision during the fixes:

- Window title, Focused as the default presentation, the native DPI scaling policy.
- The observed operating-point marker and the padded plot fit in the V/F editor.
- Monitor fits the window, so THROTTLE and POWER are shorter than Python's, and the tile row ends flush
  with the panels.
- "Max all" goes through the reviewed preview. Knob labels keep the ` *` and ` (last read)` marks.
- Native-only groups stay reachable in Classic: "More V/F tools", "Undo, recovery and profiles",
  "Single-field preview and apply", "Cancel reset" (Reset button's right-click menu).
- Ticking the hold-headroom setting writes nothing; it applies at the next hold. Python re-plans at once.
- The hard-mod acknowledgement clears when a hard plan is staged; Python clears it on read or revert.

## Open

- DIFR: the configured-state read, and the driver-enabled values on an adapter without request support,
  need reads the core does not have. Deferred with the Registry / BIOS work.
- "Optional readbacks unavailable" and "Fixed bounds unavailable" lines need peer reads and a
  qualification the native services lack.
- Python logs a warning when a card runs below the clock it shows (`check_clock_gap`). Native has no
  such check; it was not among the 143.
- A feedback row that STATUS reports as not understood is not marked unavailable: STATUS is read only in
  Classic's explicit Read and does not gate Apply.
