# Blackwell: short A618 packets with V40 policy definitions

Follow-up to the PR #100 RTX 5080 matrix at `2298568a04cfc2500ec88548688fba7231de5e62`.
Host: RTX 5080 / GB203, Windows 11 Enterprise 10.0.22000. Driver numbers below
identify observations; product selection uses architecture, accepted packet
geometry, unique role/native binding, and exact field/readback checks.

## Why two controls were missing

On 595.97, the initial modern client INFO GET (`0x2080A630`, 2340 bytes)
returned RM `0x1f`. The controller did not try the smaller client group on
Blackwell. A guarded read-only capture then accepted this existing group:

| Interface | Command | Bytes |
| --- | --- | ---: |
| Client INFO | `0x2080A630` | 1160 |
| Role INFO | `0x2080A61C` | 232 |
| Native INFO | `0x2080A618` | 8632 |
| Client CONTROL GET | `0x2080A632` | 392 |
| Native CONTROL GET | `0x2080A61A` | 6356 |

The captured discovery was `0x2080A612`, packet/parameters 54420/54352.
Client 0 bound uniquely to occupied native policy 8. The 228-byte role
definition was V40 and matched its native INFO record exactly; limits and
units agreed with client INFO. Client and native CONTROL both reported
360000 mW. Guards and transport envelopes remained intact on all five GETs.

The C++ controller now qualifies this legacy32/V40 combination on Blackwell.
Its separately unqualified compact-record fallback remains unavailable there.
Existing V40 complete-state verification preserves every peer and opaque byte;
the V30 allowance for driver-recalculated peers is not extended to V40.

Relationship-weight planning had independently classified the short A618
geometry as V30-only. The captured 8632-byte INFO has the same occupied
36-byte relationship records and selected +6 Q4.12 weight words as the longer
modern INFO, at array base `0x1CDC` instead of `0x41D0`. The running ROM is
identical across the five driver captures. Only this exact 8632/88/228 A618
geometry gains V40 planning support. The 7724/8620 shapes remain unqualified
for V40 writes. Selected row versions and metadata must match the firmware;
unqualified policy source fields remain opaque.

## Live request verification on 595.97

- XOC exposed 1 mW..1999000 mW. A **1999 W request echoed 1999 W** through
  the production backend. Undo restored 360 W; disabling XOC restored the
  ordinary 250..450 W range. The GPU remained P8, with 0% reported GPU
  utilization after SET and about 12 W reported power. No workload was started.
- Relationship 0 raw weight **4096 -> 4097** was prepared through the normal
  application path and activated by its GPU-driver-refresh helper. Independent
  raw INFO and full-ROM captures verified the result. Remove refreshed the
  driver and restored both complete original byte sequences exactly. All
  decoded rows and exposed policy/reference data were checked as well.

Original ROM SHA-256:
`80d7d77fd21d19af6a3008e4c7386a06c26b6d79c17df34c7d5c17d691bad202`

Activated ROM SHA-256:
`5fac3a2ef1281d79587471871525f1d7d785c529edd3fe6505d0fa2fbbb71e83`

These are software requests and complete-image checks, not measurements of
physical power enforcement or performance. No device, board, VBIOS, or driver
version compatibility list was introduced.

The same idle 1999 W request/Undo also passed on the original 621.01 driver
after restoring it. Every recorded sample on both runs remained P8 with at
most 2% reported GPU utilization. Final state: 621.01, 360 W, XOC off, exact
original ROM bytes. Windows boot time was unchanged; no Windows reboot or
NVIDIA App installation was performed.

## Regression checks and retained evidence

- 315 mocked native protocol tests passed, including 11 new legacy32/V40 cases:
  alternate identities and row indices, 1999 W round trips, malformed or
  mismatched definitions, refusal/transient handling, ignored writes, and strict
  peer preservation.
- The relationship suite passed 459 checks, including first/last relationship
  words, different identities, V40 headers, wrong versions/metadata, and changes
  to unknown fields or appended masks. The normal CTest target also passed.
- Offline replay of the actual capture qualified all 14 configured editable
  relationships with exact projected INFO/ROM verification.

Local raw evidence is retained in `Druta-pr100-evidence-20261127/` under
`fix595-xoc/`, `fix595-weights/`, and `fix595-idle/`. This includes the rejected
original read, guarded accepted GETs, before/active/restored INFO and ROM,
production helper receipts, request telemetry, build logs, and artifact hashes.
The original matrix remains an immutable record of the unmodified PR head;
this follow-up describes the compatibility fix.
