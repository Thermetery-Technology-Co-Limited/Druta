# Fifteenth parity increment: exact uP and NCP profile imports

Reference: released Python `fa6ee83`, `src/druta/profiles.py` and
`src/druta/controllers/{up9512r,ncp4206}.py`. The
[completion ledger](PARITY-COMPLETION.md) remains active.

Selected full-tune imports now preserve uP9512R's five independent offsets and
enable flag, and NCP4206's enabled flag and retained VID command. A disabled
NCP command is not translated into the scalar Auto action. The released recipe
identity, canonical SHA-256 and route are validated before current-controller
qualification. Display names remain informational. Unknown non-null I2C
formats and malformed compound payloads fail whole-source validation even when
the controller target is omitted from the selection.

The existing staged catalog calls typed driver plans through the selected
session. Plans and receipts bind to the session, GPU pairing, route, recipe,
selection revision and driver lifetime. Changed requests retain the existing
opt-in, physical-response verification and controller-specific checks. The
released uP matching exception performs no write and acquires no ownership,
including when the lock or FB ADC cannot authorize a changed request. Neither
family acquires a new continuous clock clamp; response verification retains its
existing temporary held-P0 workflow. Profiles never enable gates implicitly.

One host journal precedes the first possible controller SET. Shared receipts
record subsequent possible effects before dispatch, preserving recovery after
false or thrown write results. Restoration checks known selected states before
writing and rejects foreign replacements. uP restores only issued fields,
guards unknown bits and refuses to enable externally replaced unissued
offsets. NCP preserves fresh unrelated configuration bits and restores the
exact paired mode/retained-command state. Both recognize already-restored
original state without another SET. Later scalar mutations retire old
receipts. No profile replays at startup or following a driver reset.

## Validation

Release build passed. All **104 CTest suites pass**: the full run passed 103
and identified one new test-fixture error, which reset a command to 80 while
expecting 1. After explicitly setting the intended disabled command byte, the
rebuilt failed suite passed on rerun. Production parser behavior was unchanged
by that correction. Four added suites cover both drivers, session integration
and CoreClient catalog routing; existing parser/full-source suites were extended.

Coverage includes alternate identities/routes, independent nonuniform offsets,
disabled retained bytes, runtime gates and mode changes, locked matching no-ops,
bounded read failures, journal rejection, uncertain writes at each dispatch
boundary, preserved unknown fields, partial Undo retries, already-original
read-only restoration, foreign-state refusal and retired ownership. The two
Sol workers cross-reviewed the drivers; root reviewed and integrated their
changes and the Terra parser/session/catalog tests.

The [22-phase live renderer check](2026-11-13-parity-fifteenth-smoke.json)
passed on Windows 10 x64 and the owner's RTX 3070 Ti, slot `0000:01:00.0`,
UUID `GPU-8f27b43c-149d-36d3-73cd-b448b4fdbab1`. Focused and Classic catalog
captures were visually inspected. Explicit Pause and default minimization
produced zero frames and telemetry reads; Always refresh continued both while
minimized. The four device resets in the report are Direct3D renderer resets.
This run issued no tuning SET, controller write, PnP restart or registry
mutation. Injected controller tests do not establish physical uP/NCP response.

SHA-256 of the tested Release artifacts:

- `druta-ui.exe`: `168B27A7C2F49F27BC3609C0E13CEB7E48D511332F425145F61C94EBB86CCA5B`
- `drutacore.dll`: `2AD92F8A20957A9D476431FF89C552F8B9A10D95AA3DCC8E072D9D08E065A715`

The core DLL is unchanged from the preceding increment.

## Remaining work

Full-tune coarse MP2888/MP29816 offsets and power annotation execution remain
open. Controller imports and iCX remain standalone until mixed catalog
ownership is implemented and qualified. Optional CUDA capture, current-ROM
graph/scaffold providers and the persistent relationship-weight exception
retain the scope recorded in the ledger and their port maps. Physical effects,
other adapters/controllers and Vista/7 execution remain separately unqualified.
This increment does not complete the overall parity goal.
