# Driver 566.36 rail schema evidence

This note records the evidence used to admit the 976-byte rail packet as a
known wire protocol. It is not a driver-version gate. Production recognizes
the complete packet shape at runtime and refuses packets outside the reviewed
schemas.

## Binary identity and entry points

The reviewed image was the 64-bit `nvapi64.dll` from display driver 566.36
(`32.0.15.6636`):

```text
SHA-256 C6448E310AA79CDFFD2A98D5DDC10D15A4F3271994EB66425002F537A90F830E
```

All addresses below are image-relative virtual addresses (RVAs), so they do
not depend on the module's load address. NVAPI query id `0xA3070DB0`
(`ClientVoltRailsGetControl`) resolves to RVA `0x22C740`, and query id
`0xB9306D9B` (`ClientVoltRailsSetControl`, the voltage-boost setter used for an
identity call) resolves to RVA `0x2277D0`. The adjacent rail-control routine at
RVA `0x22DD60` was also followed through the same translation layer. These
entry points converge on the native client-control dispatcher at RVA
`0x79870`; the packet facts below come from that data flow rather than from a
packet-length calculation.

## Native packet construction

The dispatcher emits GET command `0x2080B213` at RVAs
`0x79A2D`–`0x79A3B` and SET command `0x2080F214` at RVAs
`0x79A7B`–`0x79A89`. In both paths it supplies a parameter size of `0x38C`
(908) bytes. With the 68-byte escape envelope this produces the observed
976-byte packet.

The record builder addresses the first record at native structure offset
`0x44` and advances by `0x1C` (seven dwords) per rail. The public-to-native
type converter spans RVAs `0x1B26E0`–`0x1B2715`; for the control record used by
the public setter it maps public type 0 to native type 2. The reverse converter
at RVAs `0x1B2680`–`0x1B26CA` maps native type 2 back to public type 0. The
getter then copies the six dwords following the type without rearranging them.
This establishes a full seven-dword record rather than a shortened record
inferred from the remaining packet length.

At the wire-packet level the reviewed layout is:

| Field | Dword | Evidence |
|---|---:|---|
| command | 14 | dispatcher GET/SET constants above |
| parameter size | 15 | `0x38C` |
| RM status | 16 | native response status |
| GET valid mask | 17 | completed GET output; observed as `0xFF` |
| requested rail mask | 18 | native request/set input |
| voltage boost | 19 | identity setter input |
| first record | 20 | native type 2 followed by six payload dwords |
| record stride | 7 | `0x1C` bytes |

The read-only [getter response canary](getter-response-canary.json) supplies a
second, independent mapping check. After a real B213 GET completed, only the
process-local response record words `+1` through `+6` were replaced with
`1001, 2002, 3003, 4004, 5005, 6006`. The public getter returned those six
values in exactly the same order after converting native type 2 to public type
0. No SET was issued, and a fresh untouched getter confirmed that hardware
controls had not changed. The first four payload words are therefore the four
exposed reliability, alternate-reliability, overvoltage, and minimum-voltage
fields; words `+5` and `+6` remain opaque and must be preserved.

## GET-before-SET preservation

Production now forwards the matched native GET unchanged and requires both a
successful NTSTATUS and RM status before considering a SET. It also requires
the returned pointer, size, recognized layout, and requested mask to match the
request.

The SET source packet combines the original GET input header through the first
record boundary (packet dword 20, byte `0x50`) with the completed GET response
record area from that boundary onward. `prepare_write` then changes only the
known SET-owned fields. This preserves the current full record, including
opaque words `+5` and `+6`, while excluding response-only header output.
Specifically, completed GET dword 17 is a valid mask (`0xFF` in the captured
response), whereas the statically reviewed native SET constructor and the
identity SET trace leave dword 17 zero. The production merge therefore retains
the original input zero at dword 17.

The bounded hardware result is recorded in
[runtime-rails-56636.json](../runtime-rails-56636.json). On the tested TITAN Xp
and driver 566.36, each of the four exposed fields was changed separately by a
small amount, read back, and restored exactly; the final raw rail records and
voltage boost also matched their initial values.

## Scope

The wider runtime recognizer contains independently reviewed 716/648,
976/908, and 1104/1036 packet schemas. They are protocol facts, not a database
keyed by driver, GPU identity, VBIOS, or public getter version. This analysis
does not authorize extrapolating another packet size, record stride, record
type, command, or field meaning. It also does not establish physical regulator
output, load stability, a safe voltage range, or behavior on another board.
Unknown schemas remain read-only and are never written.
