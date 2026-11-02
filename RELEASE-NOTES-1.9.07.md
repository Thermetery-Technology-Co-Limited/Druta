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

These are idle software observations on one adapter and two drivers. They do
not establish physical enforcement under load, behavior on other Pascal boards
or other drivers, or any change in actual power draw — nothing was measured
with a power meter. The 1999 W XOC range remains a software request envelope.

Evidence: [572.83 report](experiments/pascal-gp106-driver57283-20261102.md)
and [sanitized data](experiments/pascal-gp106-driver57283-20261102.json),
alongside the [576.80 qualification](experiments/pascal-gp106-live-power-20261102.md).

## Unchanged

The five earlier current-policy record layouts are untouched; this adds a
sixth key. No role, client or native size is selected by a driver version,
board identity or device ID — each is chosen by read-only acceptance and
validated by exact binding and readback. The unresolved blank-viewport
presentation issue recorded in 1.9.05 is unchanged.

`Druta-1.9.07-win64.zip` includes the application, adjacent I2C profiles,
licenses, matching source, tests and public validation material. Extract the
complete folder and run `Druta.exe`. As in 1.9.06, the build uses CPython
3.14.4 with the existing pinned dependencies; 1.9.05 and earlier used 3.13.8.

Validation: the complete hardware-free suite passed **6695 tests**, the 6688
of 1.9.06 plus seven covering the 216-byte record, the derived stride, and
the transient-versus-size distinction on the role GET.
