# Daily tuning, shunt correction and risk colors — 2026-11-12

Focused Daily now contains Crossbar, additional memory, SYS, Video, LTC,
NVVDD and MSVDD offsets. The controls retain their existing adapter bindings,
drafts, XOC gates and selected-field write paths. Focused Advanced and Rails
no longer duplicate these rows; Classic retains its released grouping.

Shunt mod is beside Board power in both layouts. The implementation follows
`src/druta/shuntmod.py`: original/effective resistance per rail, uniform
correction or an explicitly labeled capacity-weighted mixed-rail estimate,
and an independent parallel-resistor helper. The connector/resistance defaults
are editable templates. Explicit Save uses the Python-compatible config path
and preserves unknown fields and rail entries. Unknown rails block correction
but do not prevent saving valid edits to understood rails. Unreadable files
and invalid known resistances cannot be silently overwritten. Loading and
saving this display preference does not replay GPU tuning.

Board readings and power history can display corrected watts while retaining
raw readings. Board-power drafts and Apply stay in raw driver watts. The
headless interaction test verifies that a 2x display correction still sends
a staged 175 W request as 175 W. Classic cached board readings retain their
stale indication. The shunt popup keeps Save and Close visible while its body
scrolls, including at 1024x768 with 150% font scale.

The session score is XOC +1, armed writable Rail limits +2, and armed writable
I2C +3. The palette is green 0, yellow 1, amber 2, red 3 and crimson 4+.
Known zero with editing locked is neutral. Active I2C ownership or pending
restoration remains scored even when editing is locked or a read fails.
Unknown state remains distinct from green. The score describes established
control modes, not a measurement of hardware safety, and requires no new bus
polling. Both layouts display the color name and numeric score.

The Release build, bounded PE/import audit and all 29 CTest suites passed:
903 general UI checks, 307 Advanced-view checks, 30 risk-policy checks and
65 shunt-view checks, plus shunt arithmetic/configuration round trips and the
existing backend/controller suites. Coverage includes all eight mode
combinations, actual background tint rendering, locked/unknown/recovery
states, draft retention, explicit Save, invalid values and metadata retention.

All 12 read-only live renderer phases passed on the owner's RTX 3070 Ti.
Daily, Classic and the shunt popup captures were reviewed. Pause and normal
minimization each recorded zero frames and zero telemetry reads. Always
refresh continued both while minimized. The harness requires zero completed
tuning actions throughout; no GPU write or global shunt-config Save was
performed in this arc. Existing configured power/current remained
1917.26 W / 471.399 A in the reviewed Classic capture.

The independent desktop helper failed both captures with
`window capture timed out: timed out waiting on channel`; no physical input
was issued. The renderer's own captures and headless interaction tests were
used instead. This is not physical mouse/keyboard delivery, physical shunt
calibration or Vista/7 execution qualification.

See [machine-readable results](2026-11-12-daily-shunt-risk.json) for final
binary hashes and the live renderer report. Earlier controller write and
restoration qualification remains in the separate
[I2C checkpoint](2026-11-12-i2c-parity.md).
