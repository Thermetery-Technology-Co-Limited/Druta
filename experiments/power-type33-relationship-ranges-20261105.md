# Type-33 configured relationship ranges

The saved 617.42 and 621.01 Windows host code treats the two INFO fields of a type-33 power policy as the first and last indices of an inclusive relationship range on a guarded traversal path. The host code checks the class, reads the two u16 fields, looks up each relationship and its target policy, and increments through the upper endpoint. It skips a reversed range and reports missing lookups. The interpretation comes from those source-bound host paths, not from matching the captured numbers. It supersedes the earlier conclusion that these specific type-33 fields had unknown meaning.

The pinned host images and trace hashes are recorded in the receipt. In the
617 host, constructor `8DB830` binds predicate `8DB4B0` and INFO callback
`8DB6A0`; `8DB752..58` copies object `+0x378` to INFO row `+0x34`. The
type-33 branch of traversal `8E0D10` reads the lower and upper u16 values at
object `+0x378/+0x37A`, resolves relationship group `+0x2700`, then resolves
the relationship's target byte through policy group `+0x1CC0`. Its increment
and inclusive comparison are at `8E0E62..6D`. The equivalent 621 bindings are
constructor `9104E0`, predicate `910160`, INFO callback `910350`, and traversal
`9159F0` with inclusive comparison at `915B42..4D`. These addresses locate
evidence in those images; they are not runtime offsets or compatibility gates.

One saved 617.42 INFO capture returned the following configured graph. Each relationship and target was independently present in the returned masks and understood records:

| Source policy | Relationship range | Relationship → target policy |
| --- | --- | --- |
| 8 | 7..8 | 7 → 7; 8 → 2 |
| 15 | 6..7 | 6 → 16; 7 → 7 |

The same INFO capture separately reports policy 2 → relationship 9 → policy 9 and policy 7 → relationship 9 → policy 9. Thus the understood configured links include policy 8 → relationship 8 → policy 2 → relationship 9 → policy 9, and policy 8 → relationship 7 → policy 7 → relationship 9 → policy 9. These are configured links, not proof that a particular runtime traversal ran or a vote propagated.

The capture contains one INFO GET with a zeroed 20512-byte request payload, successful transport and RM status, and completed response. An offline audit decoded the raw packet, checked the occupied records and compared the result with both the saved decoder output and displayed lines. The [receipt](power-type33-relationship-ranges-20261105.json) records the joins and hashes for the capture, audit, decoder, renderer and supporting host traces.

This interpretation is scoped to the understood INFO layout and traced host paths; it is not a device or driver allowlist. It does not establish a type-33 enabled flag, identify PMU clients or vote producers, derive an arithmetic formula, or measure a physical effect. The earlier public-header search's negative finding remains limited to those public sources; the new binding comes from the saved host traversal evidence.
