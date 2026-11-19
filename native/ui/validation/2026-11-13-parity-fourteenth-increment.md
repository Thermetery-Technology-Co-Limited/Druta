# Fourteenth parity increment: NCT profile import and timing ordering

Reference: released Python `fa6ee83`, especially `src/druta/profiles.py`
(`rail_identity`, `capture_rails`, `preflight`, and restore) and
`src/druta/controllers/nct3933.py`; native implementation in
`python_i2c_profile.cpp`, `i2c_session.cpp`, and the controller driver. The
[completion ledger](PARITY-COMPLETION.md) remains active. Earlier notes retain
their historical evidence.

The selected full-tune NCT3933 current-DAC import uses typed exact output bytes.
The released recipe identity is checked using its canonical SHA-256, route and
controller identity; current configuration must match the saved configuration
and is comparison-only. This preserves distinct `0x00` and `0x80` encodings.
Fresh selection, gate and identity checks precede the selected output writes;
issued-only receipts retain partial restoration and session ownership. NCT
remains a separate scope until composition with other tuning writers is
qualified. A saved profile does not enable the session gate or authorize an
automatic startup/post-reset apply.

Timing plus ordinary or additional memory-offset catalog composition now uses
whole preflight followed by timing-first Apply and memory-first Undo. Initial
memory requests stay frozen through preparation and final dispatch; external
changes cannot replace the original Undo baseline. Final read-only timing
verification checks the same fields, helper and geometry in the newly qualified
operating band. Timing SET and restore retain the existing helper, target and
band checks. Partial memory recovery completes before timing restoration; failed
or conflicting requests retain their receipts, including completed-child checks
on retry.

## Validation

Release build passed. **100/100 CTest suites passed in 33.09 seconds**, including
five new suites for the NCT parser, exact driver, session and CoreClient paths,
and timing/memory catalog transactions. The latter uses the native helper
process transport with a test-owned file and fake GPU API, not a real device.

Coverage includes alternate controller routes and paired identities, exact zero
encodings, configuration changes, bounded transient reads, journal rejection
before dispatch, uncertain partial writes, foreign-state conflicts, retired
receipts and restored manual edits. Timing tests cover both memory APIs, both
file orders, a combined native memory document, no-op selections, foreign
interference, original-band rejection, the second-read baseline race, failed
Undo, preserved unknown bits and helper geometry changes.

The [22-phase live renderer check](2026-11-13-parity-fourteenth-smoke.json)
passed on Windows 10 x64 and the owner's RTX 3070 Ti, PCI slot `0000:01:00.0`,
UUID `GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. Focused catalog and Classic
timing captures were visually inspected. Explicit Pause and default minimization
produced zero frames and telemetry reads; Always refresh continued both while
minimized. The four reported device resets are Direct3D renderer resets.
Injected fixtures and live reads do not establish physical response or broader
adapter qualification. No NCT controller write, timing SET, PnP restart or
registry mutation was issued on hardware.

SHA-256 of the tested Release artifacts:

- `druta-ui.exe`: `9E8CE24678E5564E997F802F63B9D3E61B617AB8C10FA0E0267E4FD695017F84`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The core DLL is unchanged from the preceding checkpoint.

## Remaining work

Full-tune uP/NCP and coarse I2C import and power annotation execution remain
open; iCX combined composition remains separate. The optional CUDA timing
fallback remains unported ([CUDA map](2026-11-13-cuda-capture-port-map.md)).
Current-ROM role/graph and scaffold evidence providers remain open
([provider map](2026-11-13-native-evidence-provider-map.md)), as does the narrow
persistent relationship-weight exception
([port map](2026-11-13-relationship-weight-port-map.md)). Physical effects,
controller and adapter coverage, actual device-reset execution and Vista/7
execution retain their existing qualification boundaries. Overall parity is
not complete.
