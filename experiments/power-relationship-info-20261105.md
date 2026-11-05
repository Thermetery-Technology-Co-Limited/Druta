# Configured power-policy relationships

Druta can now display configured relationship targets, type-0 Q4.12 weights,
target minimum/nominal/maximum values, and two understood kinds of source
reference. The reader uses the existing native INFO GET (0x2080A618); it does
not query CONTROL/STATUS, set a weight, or calculate replacement limits.

The root live read on the RTX 5080 / 617.42 returned 14 occupied relationship
records, all type 0 with raw weight 4096 (1.0). Policy 2's configured additional
range references relationship 9, and policy 7's type-32 selector also references
relationship 9. That relationship targets policy 9, whose reported limits are
1/45000/45000 mA. These are configured references, not proof that either source
produced a particular firmware vote or that 45 A is a measured current.

The type-32 selector is a **relationship index**, not a target-policy index.
The two indices happen to be 9 in this capture. Tests use different source,
relationship and target indices to verify the join. Source mappings for other
policy types remain explicitly uninterpreted; this is not a complete graph.

## Source binding

The saved 621.01 host nvlddmkm.sys has SHA-256
`3F8D33495CABEB2FA11EBDCF0B12705E9CCFB4E53005324BCD86E9DEA8BA2AB4`.
The following RVAs identify evidence in that image only. Product code uses
reported packet geometry and types, never binary offsets or driver allowlists.

- INFO policy base/stride 0xCC/0x104: group dispatch 4ECAF0..4ECB34.
- Low 32-bit relationship mask at 0x41CC, base/stride 0x41D0/0x24:
  group dispatch 4ECB6F..4ECC90. This reader covers those 32 slots only.
- Relationship type 0: C57490 installs INFO callback C579B0. Its base chain
  CB5940 -> CF9EE0 copies target policy to row+1. C57A2B..C57A32 copies the
  unitless Q4.12 weight to row+6. The earlier interpolation investigation
  documents the weight's arithmetic and its remaining runtime-input boundary.
- Policy type 0: 90CBF0 installs INFO callback 90C400. At 90C4F1..90C50A,
  it passes row+0xB8 to C399A0. C39A2F..C39A65 copies interface+8/+0x10 to
  row+0xB9/+0xC0, and interface+0x0C..0x0F to the inclusive little-endian
  u16 range at row+0xBC/+0xBE. The host loop at C38F1B..C38F33 enters when
  either flag is nonzero. Druta recognizes boolean flag values and validates
  the referenced occupied records; other values remain uninterpreted.
- Policy type 32: 90FED0 installs INFO callback 90FD40 and runtime callback
  90FA70. INFO copies object+0x378 to row+0x34 at 90FDF2..90FDF8. Runtime
  90FA96..90FACF uses its low u16 to look up the relationship group, proving
  the selector's namespace. The high half and other bytes remain opaque.

Root reviewed the serializer/consumer chains and independently extracted the
type-32 INFO copy. The matching 617.42 bytes are live observations, not an
independent trace of that driver's serializer. No firmware analysis occurred.

## Validation and limits

The [compact receipt](power-relationship-info-20261105.json) records the native
GET, raw row fragments, independently checked fields, source hashes and decoded
display. Private raw evidence and host extracts are under
`registry-overrides/power-relationship-info-source-20261105`.

This view reads current configured metadata. It does not identify PMU client
numbers, establish which callback executed, expose a weight setter, prove
physical enforcement, or generalize this board's limits. Unknown types and
missing references remain visible. A failed refresh labels previous results
historical; changing adapters discards them. No GPU setting, registry value,
workload, driver restart or Windows reboot was part of this read-only trial.
