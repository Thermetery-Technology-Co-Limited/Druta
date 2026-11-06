# 431.60 power-policy layout: static evidence and caller distinction

The supplied 431.60 driver contains the older `0x208026xx` power interface.
Its packet sizes, row offsets and caller conversion differ from both the
581.42 Turing layout and Druta's previously understood older transport.
The [machine-readable evidence](turing-43160-static-layout-20261028.json)
records exact binary identities, instruction bytes, RVAs and command-table
entries. This report establishes static layout evidence and distinguishes
the public NVAPI wrapper from the raw RM caller observed in a separate live
read-only probe. An additional section relates static setter behavior to
the root agent's separately reported live result. This static report does
not independently qualify SET acceptance, restoration or physical power.

## Sources and method

The owner-supplied compressed files were expanded and inspected as files.
The static investigation did not load an inspected driver image, open a GPU
handle, install a driver, change GPU state, or use a BAR0 scanner. The
expanded display-driver SYS had a valid Windows Authenticode signature.

| Binary | File version | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `nvapi64.dll` | `26.21.14.3160` | 5,087,208 | `2c4650297cedce0c572165c78c87f8bec40df898fe4371579c95f13380c09348` |
| `nvlddmkm.sys` | `26.21.14.3160` | 21,858,904 | `51a94a3a369c1c3b409f9b6c4d24dfa3f7613d3098b465d36e24c9f0018aba96` |

PE parsing used `pefile 2024.8.26`; x64 instruction decoding used Capstone
`5.0.7`. Publication verification re-read both binaries, checked their
SHA-256 hashes, re-decoded all 33 recorded instruction ranges and verified
the nine kernel command-table entries. RVAs are offsets relative to the
recorded image base, not live process or kernel addresses. Kernel table
function pointers have the SYS image base, `0x10000`, subtracted.

No driver binary, installer, raw GPU payload, memory dump, scanner output or
private undo record is published here.

## Observed geometry

Offsets in the final column are relative to the parameter buffer; field
offsets following a row description are relative to that row.

| Operation | Wire command | Parameter bytes | Layout |
| --- | --- | ---: | --- |
| Power INFO transport | `20802612` | 2456 | Candidate total packet size 2524; capture the complete unmodified live envelope |
| Client INFO | `20802630` | 644 | Mask at 0; rows `4 + 20*i`; role byte +2, units byte +3, min/rated/max u32 +4/+8/+12 |
| Client CONTROL GET | `20802632` | 388 | Mask at 0; rows `4 + 12*i`; request u32 +4, caller byte +8 |
| Client CONTROL SET | `20802633` | 388 | Same parameter geometry as client GET; command existence alone does not qualify writes |
| Native INFO | `20802618` | 5008 | Mask at 4; rows `52 + 116*i`; units byte +6, min/rated/max u32 +8/+12/+16 |
| Native STATUS GET | `20802619` | 14148 | Mask at 0; rows `12 + 372*i`; validate selected type and effective fields against live definitions |
| Native CONTROL GET | `2080261A` | 2108 | Mask at 0; rows `4 + 52*i`; request u32 +4 |
| Native CONTROL SET | `2080261B` | 2108 | Same parameter size as native GET; no write qualification in this report |
| Role INFO | `2080261C` | 120 | Selector byte at 0; native 116-byte definition at +4; selector 1 appears in the kernel's own caller |

The client row count is 32. A larger unused tail in a native buffer does not
extend its 32-bit occupancy mask. This geometry must remain separate from
the previously understood 3548/3480-byte transport, whose native INFO is
5520 bytes and native CONTROL is 2124 bytes.

Client INFO's conversion loop at NVAPI RVA `0x19C13E` establishes the mask,
then reads the role and limits and advances by 20 bytes. Native INFO's
consumer uses a 116-byte stride and source offset `0x9CC` in a composite
buffer: `0x9CC = 2456 + 52`. The existing dispatcher size observations are
independently consistent with the kernel command table.

The role getter is directly established by the additional kernel evidence.
At SYS RVA `0x748F0F`, its caller allocates 120 bytes. RVA `0x748F2E` supplies
that size, RVA `0x748F36` selects command `2080261C`, and RVA `0x748F47` writes
semantic selector 1. The caller checks units at packet +10, minimum at +12
and maximum at +20, matching a native INFO row at +4. The table entry at
RVA `0x8059D8` independently lists command `261C`, size 120 and handler RVA
`0x534A70`; that handler fills the native definition starting at +4.
The earlier [NVAPI-only comparison](turing-driver-abi-review-20261028.md)
did not find a user-mode `261C` literal. That absence did not establish that
the kernel getter was missing.

## Public wrapper `F8` and raw RM `FE`

NVAPI's public client GET and SET wrappers call a conversion helper at RVA
`0x196660`. The helper compares its caller argument with `0xF8` at RVA
`0x1966B5`, and writes the `0xF8` selector byte at RVA `0x196702`. This
establishes what that public wrapper sends. It does not establish every
caller the raw RM interface accepts.

Separately, the root agent reported these read-only `20802632` results while
431.60 was active on the owner's Gigabyte RTX 2080 SUPER AORUS Xtreme / TU104
with the 16 GB memory modification:

| Raw caller selector | Reported result |
| --- | --- |
| `F8` through `FD` | `FFFFFFFF`: no existing request returned |
| `FE` | Existing `250000` mW request |
| `FF` | Rejected with RM `56` |

The static investigation did not perform those live reads. This separately
attributed observation resolves the distinction: the wrapper's `F8`
conversion does not require replacing or seeding the existing raw `FE`
request. The selected caller must come from validated live state. A missing
request must not be treated as permission to invent one, and this GET result
does not by itself prove write acceptance, restoration or physical power
enforcement.

## Request acceptance and the unresolved effective cap

Additional offline disassembly separates request storage from the effective
status result. The root agent's [live comparison](turing-driver-live-comparison-20261028.md)
reported the following native effective status samples after accepted raw
`FE` requests on the same modified board under 431.60:

| Configured request | Native effective status samples, mW |
| ---: | --- |
| 349000 mW | `349000, 359906, 359906, 359906, 359906` |
| 351000 mW | `350000, 360938, 360938, 360938, 360938` |
| 400000 mW | `350000, 360938, 360938, 360938, 360938` |

Core-current status remained 324000 mA throughout. The settled board value
plateaued at 360.938 W for both above-maximum requests; the initial sample
was 350 W. This is consistent with a 350 W request clamp plus a status
uplift, rather than a strict 350 W effective status at every sample. This
static review did not perform those writes or measure physical power; the
linked live report owns write and restoration qualification.

| SYS RVA | Static finding |
| --- | --- |
| `0x58920C` | Client SET dispatches through PMGR method `+0x1918`; initialization at `0x5892D4` installs target `0x5888EC` |
| `0x588E61` | The client row setter admits caller bytes `F5` through `FE`, inclusive; this is broader than the public wrapper's `F8` conversion |
| `0x588EE1` | It forwards row request u32 +4 and caller byte +8 to the bound native policy method `+0x210`, with fixed request type 3 |
| `0x53437D` | The decoded native policy constructor installs the base vote setter `0x533990` into method `+0x210` |
| `0x533990` | That base setter resolves the request-type vote group and updates a caller vote through `0x533A50`; it does not supply a decoded override flag |
| `0x533B30` | Request types 0, 1, 2 and 3 resolve to separate vote groups; type 3 resolves to native object `+0x190` |
| `0x536AAB` | The native CONTROL SET row path compares its requested value against aggregated type-0 and type-2 bounds before forwarding an `FE`, type-3 vote |
| `0x5369D5` | The native STATUS serializer copies effective output +4 from a separate incoming status word +8; exact request storage alone does not establish this value |

The native `261B` setter's bounds check therefore provides no understood
way around the reported cap. The raw `2633` client route reaches a request
vote without that same admission check, but the decoded path does not
establish a way to supersede the effective limit. This bounded investigation
did not identify the downstream clamp's complete implementation, a known
override flag, or another caller with understood creation, removal and
restoration semantics. Changing caller bytes or policy bounds would require
new evidence and independent restoration qualification. Treat the reported
settled 360.938 W status plateau for both 351 W and 400 W requests as an
unresolved material limitation of the tested 431.60 configuration, not as
a physically qualified override.

## Implementation requirements

Select an understood interface using the captured transport, command
acceptance, geometry and capabilities. The binary versions and identities
above identify evidence; they must not become compatibility allowlists.
Validate PCI/handle pairing, stable role-to-native binding, units and the
selected request before dispatching a SET. Preserve opaque fields, verify
the exact resulting projection and qualify full restoration independently.
Keep modern PERF diagnostics restricted to their understood transport;
recognizing the `2612` power path does not establish modern PERF support.

These observations apply to the identified files and separately reported
local operating point. They do not identify persistent registry or VBIOS
patch mappings, establish a physical power ceiling or provide a load test.
