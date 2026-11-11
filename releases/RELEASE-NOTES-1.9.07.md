# Druta 1.9.07

Supersedes [1.9.06](RELEASE-NOTES-1.9.06.md) and carries its Pascal power and
current work forward. This remains a prerelease on the registry-controls
development line.

## A refused role GET no longer removes live power and the current sliders

On the same Gigabyte GTX 1060 6GB qualified for 1.9.06, driver **572.83**
refused `0x2080A61C` — the role INFO GET — with RM `0x1F`, and both the
extended power ceiling and the current sliders disappeared again.

The role parameter is a 4-byte semantic selector followed by exactly one
native policy INFO record, so its accepted size *is* that record's stride.
1.9.06 carried that stride as a per-table constant (228 for the small client
group), and had no size negotiation on the role GET at all, so a driver whose
policy record is 216 bytes failed at the first role request.

1.9.07 changes two things:

- The role GET now negotiates its size the same way the client and native INFO
  GETs already do: a refusal **for its size** tries the other understood
  small-group record size, and a transient error still selects nothing.
- The native policy record stride is **derived** from the accepted role size
  rather than carried as a constant. The derived value is not trusted on its
  own — it still has to bind the role definition to exactly one occupied
  native policy before anything uses it, which is what would catch a wrong
  stride. It is recorded on the binding only when it differs from that
  layout's usual record size, so bindings captured on every previously
  supported ABI stay byte-identical.

The relationship holds across every ABI measured so far, which is why it is
derived rather than tabulated:

| Role INFO size | Native INFO record stride |
| ---: | ---: |
| 264 | 260 |
| 232 | 228 |
| 220 | 216 |
| 136 | 132 |
| 120 | 116 |

## Three blocks shared, two not

572.83 presents an A612 transport of **3676 / 3608** bytes. Against the
11740 / 11672 transport added in 1.9.06 on the same board:

| | 576.80 | 572.83 |
| --- | ---: | ---: |
| Client INFO / CONTROL / SET | 1160 / 392 / 392 | identical |
| Native CONTROL | 4432, `0x14` / `0x7C` | identical |
| Native STATUS | 171048, `0x68` / `0x1434` | identical |
| **Role INFO** | 232 | **220** |
| **Native INFO** | 8620, record 228 | **8236, record 216** |

A sixth current-policy record layout is added for the new transport. Its info
base and its status and control geometry were each re-solved against live
descriptors rather than copied from the neighbouring entry, even where the
answer came back the same — the 1.9.06 notes explain why that distinction
matters.

## A known RM26 ABI is no longer gated to Turing

Driver **472.12** on the same board presented the `20802612` RM26 transport at
3548/3480 bytes — an interface Druta already described in full. Every block
matched the existing table on the first try, and sweeps confirmed each is the
only accepted size. The sole blocker was a generation gate reading
"This client-power RM26 ABI requires Turing", an incremental record of what
had been qualified rather than a property of the interface.

It becomes `RM26_ARCHITECTURES`, now `(4, 6)`. It is kept **separate** from
`LEGACY_GROUP_ARCHITECTURES`, which is `(4, 6, 7)`: merging them would claim
Ampere on the RM26 family, which nothing has tested. A generation qualified
against one packet family is not thereby qualified against the other, and the
test suite asserts the two lists stay distinct.

## The same packet geometry can mean two different layouts

Driver **516.94** presents an A612 transport at **3548/3480 bytes** — the
exact packet geometry 472.12 presents under `20802612` — but answers the
**A6xx** command ids while laying its policies out the compact way. The
records differ too: role INFO 132 against 136, native INFO 5392 with a
128-byte record against 5520 with a 132-byte one, and native STATUS 70684
(`0x64`/`0x858`) against 69148 (`0x64`/`0x828`). The 69148 size is refused
outright on 516.94.

`_CURRENT_LIMIT_LAYOUTS` is therefore keyed on the transport's **command as
well as its sizes**; keying on size alone cannot represent these two. The
client group's command family and its record layout are now tracked
separately for the same reason, since a driver can answer one family while
using the other's record shape.

**Correction carried from 1.9.07's own notes:** the 472.12 work recorded
RM `0x3A` as the RM26 families' size-refusal code, contrasted with `0x1F` on
the A6xx families. That was wrong. 516.94 returns `0x3A` on the **A612**
family, so the code is a property of the driver, not the family. Both are now
treated as the definite "wrong size" answer that selects the next candidate —
which is exactly what 516.94's detection needed, since the modern client INFO
was refused with `0x3A` and 1.9.06's fallback only moved on for `0x1F`.

## Max All asks for the XOC envelope

With **XOC** ticked, "Max All" now requests the extended envelope for the
board row instead of the card's nominal maximum, mirroring what the named
current rows beside it already did. It only ever raises: if the live request
interface is unavailable, or the envelope is no wider than the nominal top,
the nominal maximum stands.

Accepting a request is not the same as the board enforcing it. On the
measured GP106 the effective board limit settles at the firmware maximum
regardless of the request, and under load the binding constraint was core
current rather than board power — see the
[472.12 under-load measurement](../experiments/pascal-gp106-driver47212-20261102.md).

## Qualification

Reversible idle trials on the GP106 under 572.83, each restored and verified
with a hidden 30-second restoration watchdog armed:

| Trial | Result |
| --- | --- |
| Power 190 W (inside the 90/180/200 W range) | exact readback, restored |
| Power 201 W (above the reported maximum, XOC) | exact readback, restored |
| Power 250 W (above the reported maximum, XOC) | exact readback, restored |
| Core current 95 → 94.2 A | accepted, effective limit verified |
| Core current 95.001 A (above the reported maximum) | refused, no state change |
| Core current restored to 95 A | effective limit verified |

With XOC the slider range becomes 0.001..1999 W. Core current policy 13 reads
1/86.8/95 A, and the native effective power limit reads 200000 mW against a
configured 200 W. Sweeps accepted exactly one size per block on this adapter;
every other size tried returned RM `0x1F`.

The same trials passed on **472.12** against its captured 180 W baseline:
190/201/250 W and a core current move of 86.8 → 86 A, each with exact
readback and verified restoration, and 95.001 A refused against the reported
maximum. Its native effective power limit reads 185625 mW.

These are idle software observations on one adapter across three drivers.
They do not establish physical enforcement under load, behavior on other
Pascal boards or other drivers, or any change in actual power draw — nothing
was measured with a power meter. The 1999 W XOC range remains a software
request envelope.

Evidence: [572.83](../experiments/pascal-gp106-driver57283-20261102.md) and
[472.12](../experiments/pascal-gp106-driver47212-20261102.md) reports with their
sanitized data, alongside the
[576.80 qualification](../experiments/pascal-gp106-live-power-20261102.md).

## Unchanged

The five earlier current-policy record layouts are untouched; this adds a
sixth key, and the RM26 layouts are reached without changing any of their
sizes. No role, client or native size is selected by a driver version, board
identity or device ID — each is chosen by read-only acceptance and validated
by exact binding and readback. Generation still decides only whether a
packet family's policy semantics are understood at all. The unresolved blank-viewport
presentation issue recorded in 1.9.05 is unchanged.

`Druta-1.9.07-win64.zip` includes the application, adjacent I2C profiles,
licenses, matching source, tests and public validation material. Extract the
complete folder and run `Druta.exe`. As in 1.9.06, the build uses CPython
3.14.4 with the existing pinned dependencies; 1.9.05 and earlier used 3.13.8.

Validation: the complete hardware-free suite passed **6708 tests**, the 6688
of 1.9.06 plus twenty covering the 216-byte record, the derived stride, the
transient-versus-size distinction on the role GET, every qualified generation
on both RM26 geometries, an assertion that the two generation lists stay
separate, that the two 3548/3480 layouts are distinct entries, and Max All's
behaviour with XOC on, off, and with the envelope unreadable.

Qualified on four drivers on one GP106 — 576.80, 572.83, 472.12 and 516.94 —
each with reversible idle power and core-current trials, exact readback and
verified restoration.
