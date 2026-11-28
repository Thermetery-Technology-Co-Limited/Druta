# Capability and packet layout inventory, 2026-11-27

The current compatibility work uses an observed interface signature and an
adapter capability check, followed by exact request readback. A driver version
identifies an evidence row here; it is not a product gate. This inventory
separates direct observations from layouts encoded in source and from work
still requiring a live adapter.

## TITAN RTX, Windows 10 x64 build 19044

The following native power INFO command, accepted packet size, and policy
geometry came from `rerun-20261127/<driver>/bios-apply-receipt.json` in the
local matrix workspace. Each receipt belongs to a one-weight BIOS relationship
Apply followed by exact full-image Remove; `live.txt` for each bind reported
one TITAN RTX at the expected slot and UUID. The receipt's INFO size alone is
not a complete interface signature.

| Driver | Native INFO command | INFO bytes | Policy base / stride |
| --- | --- | ---: | ---: |
| 417.35 | `0x20802618` | 5008 | 52 / 116 |
| 431.36 | `0x20802618` | 5008 | 52 / 116 |
| 472.12 | `0x20802618` | 5520 | 60 / 132 |
| 512.15 | `0x20802618` | 5392 | 60 / 128 |
| 560.94 | `0x2080A618` | 7724 | 88 / 200 |
| 581.80 | `0x2080A618` | 8620 | 88 / 228 |
| 595.71 | `0x2080A618` | 8632 | 88 / 228 |
| 595.97 | `0x2080A618` | 8632 | 88 / 228 |
| 610.88 | `0x2080A618` | 20000 | 204 / 252 |
| 621.24 | `0x2080A618` | 20512 | 204 / 260 |

These are eight distinct observed INFO shapes across ten drivers. Every
`live.txt` reported writable core, memory and power requests, 13 power
policies, four writable rail controls, a 128-point V/F curve with divisor 1,
and a V/F hold. This is capability *presence*; the separate matrix records
which reversible writes and readbacks passed. `locks.clock.present=0` is the
absence of an active lock in the stock snapshot, not evidence that a frequency
lock is unsupported.

The implementation maps a fuller transport key of discovery command, packet
bytes and parameter bytes to candidate native INFO/STATUS/CONTROL sizes in
[`power_layout_catalog_generated.h`](../native/src/backend/power_layout_catalog_generated.h).
Client power probes accept INFO, role, native INFO and CONTROL sizes before
binding an occupied policy; its size groups are in
[`client_power.cpp`](../native/src/backend/client_power.cpp). Those source
tables describe *candidate* packets. The table above is the direct TITAN
observation; it does not claim that every candidate in the catalog was
independently exercised in each row. The regression matrix records Apply and
Undo outcomes for power, current, clocks, rails, V/F and Max It.

## Reuse across architecture, and why a size is insufficient

Earlier live receipts in
[`native-driver-gpu-issues-20261126.md`](native-driver-gpu-issues-20261126.md)
record 560.94's 7724-byte native INFO, 149928-byte STATUS and 4172-byte
CONTROL accepted on both GTX 1050 Ti (Pascal) and RTX 3070 Ti (Ampere), with
board power and Max It request restoration. Those are independent cross-card
observations of the same packet shape, not proof that every Pascal or Ampere
board has the same capabilities.

The GTX 1060 on 472.12 used discovery `0x20802612`, 3548/3480-byte
packet/parameters, and native INFO 5520 with 132-byte records. The *same*
3548/3480 packet sizes on 516.94 used discovery `0x2080A612`, native INFO
5392 with 128-byte records and a different STATUS size. Its 572.83 bind used
`0x2080A612`, 3676/3608 and native INFO 8236 with 216-byte records. These
measurements are in the three
[`pascal-gp106` reports](pascal-gp106-driver47212-20261102.md).
Command family and geometry must therefore be checked together, followed by
the role/occupied-policy match; neither driver version nor INFO size alone
selects semantics.

The 560.94 V/F first-edit refusal was a single no-write guard event followed
by a successful loaded retest. The focused follow-up reproduced the old
hold/edit order, ran three Max It/Undo cycles and sampled V/F mapping and
signed requests after each. It did not reproduce the refusal. The original
generic message did not identify the changed field, so its trigger remains
unknown. The current prewrite diagnostic identifies the disagreeing field
without relaxing any request or layout guard. See the regression report and
local `rerun-20261127/vf-first-560-investigation/` receipts.

## Remaining coverage

The ten TITAN rows above are one adapter and one Windows build. Earlier
Pascal/Ampere rows were captured on another host and earlier candidate builds;
they establish packet evidence, not a current-branch regression pass. Legacy
Windows validation and its interactions with these packet shapes are a
separate coverage dimension. On the other machine, first record the exact
adapter identity, OS, accepted discovery and native/client packet sizes,
reported capabilities and selected policy; then run bounded Apply/readback/
Undo for each available feature. A new observed signature warrants a fixture
and a live qualification, not a driver-version exception.
