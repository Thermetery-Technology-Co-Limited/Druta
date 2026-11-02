# Druta 1.9.06

Supersedes [1.9.05](RELEASE-NOTES-1.9.05.md) and carries its Turing clock and
power-interface work forward unchanged. This remains a prerelease on the
registry-controls development line.

## The XOC power slider now extends on Pascal

With **XOC** ticked, the power-limit slider stayed at the card's ordinary
maximum on Pascal instead of opening the extended request range. Two
independent gates were responsible, and both are fixed.

### A captured power GET is a transport, not a record layout

Druta borrows the paired RM transport from an unmodified power getter and
reuses its handles for its own read-only commands. It also keys a table of
*current-policy record layouts* by that getter's packet and parameter sizes.
Those were the same lookup, so a driver whose current-policy record layout is
unknown could not reach live power either, even though the client-power group
negotiates its own packet sizes by read-only INFO acceptance and never
consults that table.

The two are now separate questions. The transport is validated as an envelope
— a 17-word header of a known command family whose packet length is its
parameter length plus the 68-byte prologue — and the current-policy record
layout is selected after that. A parameter size no entry describes removes the
current (A) sliders and nothing else. That removal is reported as an
unsupported interface, not a failed read, so profiles stay complete and
"Reset all to stock" does not fail.

The local Gigabyte GTX 1060 6GB on driver 576.80 presents an A612 transport of
11740/11672 bytes, which none of the four recorded layouts describes.

### Pascal may fall back to the smaller client group

The fallback from the modern client-power group to the smaller one was limited
to the generations validated when it was written, Turing and Ampere. Pascal is
added by the qualification below. Generation still decides whether these policy
semantics are understood at all; *which* group a card speaks is decided by
read-only INFO acceptance and exact readback, never by a device, board or
driver version.

## Core current limits on Pascal

The same blocked transport removed the current (A) sliders. Restoring the
transport was not enough on its own: the current-policy *record* layout for
this driver genuinely was not one Druta knew.

It is now measured, not inherited. This driver's A612 transport shares the
8620-byte info block and the 4432-byte control block with the 11748/11680
entry, which makes copying that entry's status block tempting and wrong:

| A619 STATUS parameter bytes | Result on GP106 / 576.80 |
| ---: | --- |
| **171048** | **accepted** |
| 172080, as used by the 11748/11680 entry | refused, RM `0x1F` |

171048 was the only size accepted across a 140000..210000 sweep at 4-byte
steps. Its record geometry was then solved from live data: of every
`(base, stride)` pair with base 0..2048 and stride up to 12000, exactly one
makes all ten occupied policies' status type bytes agree with their info type
bytes and keeps every effective value inside that policy's own bounds. The new
entry therefore differs from the Turing one in all three status fields —
size 171048, header `0x68`, stride `0x1434` — while sharing info and control.
Two blocks matching is not evidence that the third does.

Core current policy 13 now reads on this adapter at 1/86.8/95 A, type `0x0B`
channel 11, matching the descriptor the build already expected for Pascal. A
reversible trial set 86.8 → 86 A with the effective limit verified, refused
95.001 A against the reported 95 A maximum without changing state, and
restored 86.8 A with its effective limit verified.

Because reading the policy table needs this layout, the **native effective
power limit** also became available on this adapter: it reports 185.625 W
against a configured 180 W, where 1.9.05 could only report it as unavailable.

## Pascal qualification

On the local Gigabyte GTX 1060 6GB (GP106, driver 576.80, VBIOS
`86.06.4b.00.32`), role 1 uniquely bound client row 0 to native policy 2, with
90/180/200 W minimum/rated/maximum in milliwatts and a captured 180 W request.
It speaks the same 1160-byte client group and 124-byte native controls already
qualified on Turing 581.42, reached through the same acceptance probe.

Three reversible idle requests, derived from the adapter's reported bounds
rather than fixed points, passed exact request readback and full client/native
restoration:

| Board request | XOC required | Exact readback | Restoration |
| ---: | --- | --- | --- |
| 190 W, inside the ordinary range | No | Verified | Verified |
| 201 W, 1 W above the reported maximum | Yes | Verified | Verified |
| 250 W, 50 W above the reported maximum | Yes | Verified | Verified |

With XOC ticked the slider range became 0.001..1999 W. With XOC unticked the
bound correctly carried over the live extended request, so a value already set
could stay or move back toward the ordinary envelope.

These results establish software request acceptance, exact readback and
restoration on one adapter and one driver at idle. They do not establish
physical enforcement under load, behavior on other Pascal boards or drivers,
or any increase in actual power draw; nothing was measured with a power meter.
The inherited 1999 W XOC range remains a software request envelope, not a
measured Pascal physical ceiling.

Evidence: [report](experiments/pascal-gp106-live-power-20261102.md),
[sanitized data](experiments/pascal-gp106-live-power-20261102.json) and
[validation script](experiments/validate_pascal_live_power.py).

## Unchanged

Everything else in 1.9.05 is carried forward as released: the clock-offset
capability fallback, power-slider readback and native-limit reporting, and the
retained Turing power qualification. The four pre-existing current-policy
record layouts are untouched — the Pascal work adds a fifth key rather than
changing any of them — so the current (A) sliders behave exactly as before on
every driver that already had them. The unresolved blank-viewport presentation
issue recorded in 1.9.05 after a live driver swap is unchanged.

`Druta-1.9.06-win64.zip` includes the application, adjacent I2C profiles,
licenses, matching source, tests and public validation material. Extract the
complete folder and run `Druta.exe`.

**This build moves to CPython 3.14.4.** 1.9.05 and earlier bundled CPython
3.13.8, which is not installed on the build machine. The pinned third-party
dependencies are unchanged — Dear PyGui 2.3.1 and PyInstaller 6.22.0 — and
the hardware-free suite and the Pascal hardware qualification above both ran
on 3.14.4. Rebuild on a 3.13.8 host if the interpreter change is unwanted;
nothing in this release requires 3.14.

Validation: the complete hardware-free suite passed **6688 tests**, the 6672 of
1.9.05 plus sixteen covering the transport/record-layout split, the Pascal
client group, and the new status geometry — including one test per status
field asserting that carrying the Turing value over breaks the read, so the
two entries cannot silently converge. The new and changed files pass Ruff; the
changed modules carry the same pre-existing whole-module findings as 1.9.05,
unchanged.
