# BIOS Override relationship weights on extended RM26

Druta's native **Advanced > Registry / BIOS overrides > Power relationship
weights** panel was checked on the RTX 3070 Ti and GTX 1050 Ti with NVIDIA
driver 472.12. Both cards reported INFO command `0x20802618` with 5,520 bytes
and policy records at base `0x3C` with stride `0x84`. The relationship mask
is at `0x10BC`. The relationship records start at `0x10C0` with a
`0x24` stride. An occupied type-0 row supplies a target index at byte 1
and a little-endian Q4.12 weight at byte 4.

| Adapter | VBIOS version | Power table version/header/row | ROM relationship row | Matching weights |
| --- | --- | --- | ---: | ---: |
| RTX 3070 Ti (GA104) | 94.04.5a.00.56 | `0x30/0x2C/0x47` | 14 bytes | 15 |
| GTX 1050 Ti (GP107) | 86.07.39.00.e9 | `0x30/0x25/0x2E` | 10 bytes | 3 |

Every editable relationship matched the running driver's target ordinal,
policy class, channel, units, three limits and original weight. The parser
preserves the other ROM and INFO bytes. The source-policy relationship fields
in this packet remain uninterpreted.

On Windows 10 build 19045, the native workflow previewed relationship 1,
saved the recovery receipt, staged one registry record and refreshed the
selected GPU. Both cards reported the change from raw 4096 to raw 4095.
Fresh-process checks matched the complete projected running image and INFO.
An independent read confirmed the other GPU's image, INFO and managed
registry state matched its baseline during each trial.

Removal cleared the owned registry record and refreshed the selected GPU.
Both original images and INFO packets matched their baselines byte for byte,
the weight returned to 4096, and neither trial left an active recovery pointer.
The trials verified the configured weight at idle. Physical power and current
enforcement under load were outside the test.
