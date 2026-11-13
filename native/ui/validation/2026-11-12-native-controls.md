# Native controls validation — 2026-11-12

Status: implemented and live checked on Windows 10 x64, RTX 3070 Ti at PCI 0000:01:00.0, NVIDIA driver 595.97, VBIOS 94.04.5a.00.56. These identities describe the test, not compatibility gates. Earlier physical measurements remain in the existing JSON records and do not establish behavior on other adapters.

## Mock coverage

The native UI test suites exercise selected-device identity, capability and partial-read handling, exact request readback, scoped Undo and failure paths. They include ordinary and advanced offsets, power policies, rail-limit fields, V/F curve drafts and planners, session ordering and pause behavior, and profile parsing. The I²C suites use mock controller transport and clock operations to cover explicit selection, gating, verification, selected requests, restoration, ownership and failure recovery. Timing suites use mock helper output and commands for field parsing, preview, selected-field updates and restoration; they do not exercise a real memory controller.

All 18 integrated CTest suites passed after a Release build and the bounded Windows 6.0 PE/import audit. Checks include 565 client, 141 session, 755 UI, 211 advanced-view, 147 I2C-session, 31 hold, 83 timing-service/transport and 14 timing-view cases. Tests cover alternate identities, partial capabilities, transient reads, failed restoration, uncertain helper-process exit, propagated curve frequencies, and exact request readback. Paused board-power actions refresh core-current and policy fields; required hold maintenance runs without ordinary telemetry polling.

## Helper contract

Source inspection of the local `nvtune-main/tool/src/cli.cpp` found that `dump -d SLOT --raw` reads the selected device's broadcast aperture. The local source's `set -d SLOT FIELD=VALUE` is a direct write: it creates a backup on the first write, plans the update and checks readback; it advertises neither `--dry-run` nor `--commit`. The native service constructs a selected-field preview before calling such a helper. This source inspection does not prove that a selected executable matches that source, that the driver is available, or that a write has a particular physical effect.

## Live results

Record the adapter, driver and helper identity, exact requested and read-back values, restoration and observed limitations for each live check. Do not infer factory defaults from captured initial requests, or infer physical voltage from controller request values. Physical I²C voltage needs independent measurement. Timings require an available qualified helper and driver, and their behavior across performance-state changes and the associated hold lifecycle remains to be established for the current adapter.

- Starting requests: memory +32 MHz true, board 1999 W, fan 100%, boost 100%, core offset 0. The actual core-current request was 483.014 A; the previous UI had retained 270 A because it rejected a derived request outside the normal manual range.
- Board 1999 → 500 W changed core current 483.014 → 270 A. Related policies changed too (for example policy 5: 172.673 → 78 W; policy 6: 372.235 → 175 W). Undo restored all captured requests. Unpinned relationships remain driver-derived; no proportionality constant was inferred from this board.
- Native Max it used the adapter's normal 500 W maximum, fan/boost 100%, ramp floor 1000 mV and cap 1093.75 mV. The domain-6 hold and headroom were confirmed; explicit maintenance passed. Undo removed headroom before releasing the hold and restored the captured scalar/curve requests.
- Reset reached 310 W, core current 243 A, memory offset 0, boost 0, fan Auto and zero curve deltas. Initial Undo exposed the invalid frequency-minus-delta mapping assumption. The original 127 known delta requests were restored through the existing native Python facade with matching index/voltage/layout and exact readback. After the geometry correction, the native Reset/Undo retest passed: all 127 voltage/delta entries and all 19 policy requests matched the pre-reset baseline, with no remaining Undo or hold.
- Explicit I2C scan selected the sole candidate at port 1 / address 0x20. Read controller followed by XOC off/on and Rail limits on/off all succeeded. Explicit Verify armed the gate, performed its A–B–A response test and restored the captured controller state before releasing the clock clamp. Final controller VOUT was 676 mV. No new multimeter observation is claimed; see the earlier physical measurement record.
- The installed nvtune v1.0.2-alpha helper captured the selected card's timing fields and previewed RC 96 → 255 cycles. This was a preview only; no timing register write was issued. Timing write/readback/Undo paths have mock coverage but still require live qualification.

The final UI capture shows all four NVVDD sliders and the fresh 483.014 A request in Classic. XOC/Rail modes remain session flags and are off in a new application session. Captured absolute rail references are estimates. Nominal I2C presets are command values within the current controller range, not calibrated voltages. Other controller methods and actual Vista/7 execution remain unqualified.

Existing scoped evidence: [live checks](2026-11-12-live.json), [control checks](2026-11-12-controls.json), and [classic I²C checks](2026-11-12-classic-i2c.json).

## Final build and window check

The Release build passed all 18 CTest suites again after the final source cleanup.
All 11 read-only live window phases passed: normal visible, Always refresh visible
and minimized, explicit Pause, hotkey restoration, normal minimized/restored,
Classic, Advanced, Monitor and resize. Explicit Pause and normal minimization
each recorded zero frames and telemetry reads; Always refresh continued both
while minimized. Four D3D9 device resets completed. The check used ImGui input
injection and window-message hotkey delivery, not physical keyboard/mouse input.
No owned tuning mode was active during this window check; paused maintenance has
separate mock coverage and a live explicit maintenance check during Max it.

[Build identities and window results](2026-11-12-native-controls.json) record
SHA-256 hashes and the final test scope. The Reset/Undo retest and window check
preceded the final whitespace/test-INI cleanup and CLI help-text addition. The
Max it and I2C checks preceded the curve-geometry correction; the controller
write/restoration protocol did not change afterward. These distinctions avoid
claiming that every hardware path was rerun against the final binary.

## Follow-up: unlocked startup and integrated Classic curve

Controls now start unlocked, while an explicit user lock survives layout and
adapter changes in the current application. Classic / Verbose embeds the live
V/F editor beneath the tuning controls, with the same draggable dots, WASD +
Shift, staged shape tools and explicit Apply. Opening the optional separate
window moves the editor there; closing it returns to the integrated editor.
Only the focused editor consumes curve keys, and drafts survive this move.

Release build and import audit passed. The UI suite passed 797 checks, including
inline mouse dragging, keyboard edits, focus isolation, popup transitions,
unlocked startup without writes and explicit lock preservation. The curve and
import-policy suites also passed. A read-only 2200×1800 live capture on the same
RTX 3070 Ti was visually checked: unlock was selected, all 127 curve points were
present inline, and the four NVVDD sliders remained visible. No tuning write was
issued by this capture.

Follow-up UI EXE SHA-256:
`9A8530B778A7492B45BEB5C3E45D5F70E54A1B5EE4E826F5FE10ED9B231851A0`.
Earlier build hashes and hardware tests above remain historical evidence.
