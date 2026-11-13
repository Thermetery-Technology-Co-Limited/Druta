# Remaining disk-history parity

Reference: released Python 1.9.1 `fa6ee83`, `druta.py` and `profiles.py`.
Function names below identify the pinned source; current working-file line
numbers must not be used as release citations. This is implementation guidance,
not completion evidence. Ordinary requests remain session-only.

| Released path | Snapshot and failure behavior | Native integration point |
| --- | --- | --- |
| `vf_apply`, Re-phase, `vf_reset`, core offset | Full tune before the curve write. No plan means no write/snapshot. Failed or incomplete snapshot is reported as unprotected; the write can proceed. | CoreClient curve and core-offset actions |
| `reset_all`, one-click max | One full snapshot after refusal/confirmation gates and before the first write. The internal V/F step suppresses another snapshot. Failure warns. | Grouped tuning actions |
| `write_tuning_action`: memory, clock domain, voltage offset, boost | Exact selected semantic scope. Same raw request skips snapshot and write. Capture/save failure blocks. | Existing qualified request captures and setters |
| Fan policy | Per-fan requested policies. No-op skips capture/write. Failure blocks, except explicit Auto can proceed with a warning. | Fan snapshot and restoration path |
| Board/policy power | Coupled board/current/policy/pin snapshot. Equal nominal power is not assumed to be a no-op because pins can change. Failure blocks except explicit Stock. | Coupled power capture and grouped Undo |
| P0 lock plus max fan | Fan snapshot failure blocks; the clock request retains its independent Release lifecycle. | Frequency action handover |
| I2C writes, Zero all, Auto/reset | Full snapshot; autosave failure warns. Mandatory fresh controller preparation/verification can still refuse the write. | I2cSession typed per-family paths |
| Timing write | Mandatory timing backup first; backup failure blocks. Full autosave failure warns. Empty staged selection exits first. | Timing batch plan/Undo |
| Full profile Load | Whole preflight first, then full reverse snapshot. Autosave failure warns. | Catalog full-tune transaction |
| Scoped/combined profile Load | Capture the corresponding selected semantic scope(s). Failed/incomplete reverse snapshot blocks before the first write. | Catalog selected plan and existing typed captures |
| `undo_last_write` | Choose newest autosave and use normal explicit Load, which creates the reverse point. A second history Undo can reverse the first. | New explicit history action, separate from process-local Undo |

`profiles.list_profiles` orders finite `saved_ts` descending with file-time
fallback. Autosaves retain 20 entries. Native library storage and browsing are
implemented in increment 25; the automatic producers and newest-history action
still need integration. Native retention must only prune files owned by that
library, preserving Python/external and named files.

Never serialize opaque in-memory Undo receipts. Use existing semantic profile
documents and repeat current-target qualification on explicit restore. No
automatic startup, reconnect or driver-reset replay is authorized.

Acceptance must exercise real command paths: no-op suppression, power pin
exceptions, grouped actions producing one snapshot, required capture failure
blocking before any setter, recovery exceptions, warning-only incomplete full
captures, newest ordering, reverse capture, and no automatic application.
