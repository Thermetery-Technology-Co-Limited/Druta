# Sixteenth parity increment: coarse controller imports and composition

Reference: released Python `fa6ee83`, including `src/druta/profiles.py`,
`src/druta/controllers/mp2888.py`, `src/druta/mp29816.py` and their pinned
TOML recipes. The [completion ledger](PARITY-COMPLETION.md) remains active;
earlier checkpoint notes retain their historical evidence.

Selected full-tune MP2888 and MP29816 coarse offset requests use typed imports.
The parser checks the released canonical recipe hash, route and saved recipe
identity, including the released alternate-route and PAGE transformations.
This hash identifies the saved recipe; historical board and match metadata do
not authorize or exclude hardware. Unknown custom recipes remain unsupported
when their source is unavailable. Requests retain their exact 6.25 mV or 5 mV
encoding grid; current adapter, selected controller, route, PAGE/scale, gate,
verification and session ownership are checked by the existing runtime path.
No PAGE or unknown control fields are replayed.

Qualified typed legacy controller profiles can compose with ordinary catalog
targets. Whole-selection preflight precedes writes. Voltage requests and the
controller are ordered before dependent clock requests; grouped Undo restores
the controller before clocks. Frozen memory baselines prevent silently rebasing
on a changed request, while selected final readback includes no-op fields.
Issued-only controller receipts preserve partial recovery and prior ownership;
foreign changes or unreadable recovery state stop the remaining restoration.
Ordinary manual actions remain subject to their normal ownership guards.
Explicit Apply remains session-only, with no automatic startup or driver-reset
replay. iCX combined composition remains separate.

## Validation

The Release build and all **109 CTest suites pass**, with the final complete
run taking 33.52 seconds. Five new suites cover both coarse drivers, session
routing, controller dependency partitioning and actual CoreClient composition.
Existing parser/full-source and catalog tests were extended. Coverage includes
alternate routes/PAGE, exact grids, preserved unknown bytes, fresh bounds,
transient reads, failed/uncertain SET, journal failure, foreign replacements,
retired ownership, already-original Undo, dependency order in either file order,
mid-transaction memory changes and changed completed restoration receipts.

The initial run passed 108 suites and exposed Windows stack exhaustion
(`C00000FD`) in the broad CoreClient regression. Recursive dispatch kept large
local Undo snapshots on the stack. Heap-owned temporaries reduce its compiled
frame from 110,496 to 30,656 bytes; the test and executable stack reserve remain
unchanged. The full suite was rebuilt and rerun after this production fix.
Sol workers cross-reviewed the drivers, composition and snapshot lifetimes;
root reviewed their changes and Terra's parser/session/catalog tests.

The [22-phase live renderer report](2026-11-13-parity-sixteenth-smoke.json)
passes on Windows 10 x64 and the owner's RTX 3070 Ti at `0000:01:00.0`, UUID
`GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. Focused and Classic catalog captures
were visually inspected. Pause and default minimization produced zero frames
and telemetry reads; Always refresh while minimized produced 114 frames and
two reads. The four device resets are Direct3D renderer resets. The example
full tune remained unselected. No tuning SET, controller write, PnP restart or
registry mutation was issued by this check. Mock transports do not establish
physical controller response or qualification on other devices.

SHA-256 of the tested Release artifacts:

- `druta-ui.exe`: `FA11F9C7EEA7589EC8583B40B8E6F99064E79B25E0069AA295DC5381D2A4F445`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The core DLL is unchanged from the preceding increment.

## Remaining work

Full-tune power annotation execution and combined iCX composition remain open.
The [annotation map](2026-11-13-full-tune-annotations-port-map.md) records the
UI-owned metadata transaction; the [iCX map](2026-11-13-icx-composition-port-map.md)
also records partial-Undo retry and retired-lifecycle defects to fix first.
The optional CUDA timing fallback remains unported
([CUDA map](2026-11-13-cuda-capture-port-map.md)). Current-ROM role/graph and
scaffold evidence providers remain open
([provider map](2026-11-13-native-evidence-provider-map.md)), as does the narrow
persistent relationship-weight exception
([port map](2026-11-13-relationship-weight-port-map.md)). Physical effects,
controller and adapter coverage, actual device-reset execution and Vista/7
execution retain their existing qualification boundaries. Overall parity is
not complete.
