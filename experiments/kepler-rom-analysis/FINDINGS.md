# GTX 770 ROM: power-policy and sensor mapping

The user-supplied `GK104.rom` has SHA-256
`7c4f19ed1bf85fa8e69f597460448eab59e9f5b5161e5b5ec37459f1b30d63a1`.
It starts with an NVGI wrapper; the legacy PCI ROM image begins at file
offset `0x600`. Table pointers below are relative to that image unless
explicitly labeled file offsets. The input file was only read.

## What the ROM establishes

BIT P v2 points to the power-sense table at `0x7ed5`, topology at `0x7f18`,
and budget table at `0x7fd2`. The budget table is v0x20 with nine 34-byte
records. Records 0–7 match all eight live RM policies exactly, including
their channel indices and min/default/max values. **The values are mW**;
this removes the units uncertainty in the earlier capture-only report.
The header selects entry 7 as the ordinary power-cap entry. The table
layout and mW interpretation are supported by the
[envytools power-table parser](https://github.com/envytools/envytools/blob/master/nvbios/power.c)
and [nouveau power-budget parser](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/bios/power_budget.c).

The active sense record selects external device 1, type `0x4e`, address
`0x80` in 8-bit notation (`0x40` in 7-bit notation). This identifies an
**INA3221**, using the primary communications port. The ROM specifies
**three 5 mOhm shunts**. Type/address interpretation follows
[NVIDIA's DCB specification](https://nvidia.github.io/open-gpu-doc/DCB/DCB-4.x-Specification.html#i2c-device-table-entry);
the resistor layout follows the
[nouveau sense parser](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/nouveau/nvkm/subdev/bios/iccsense.c).
Two other sense records reference external-device entries marked `0xff`
(skip), so they do not establish two additional active physical sensors.

## Policy mapping

| Policy | Default | Channel | Best-supported interpretation |
|---|---:|---:|---|
| 0 / 2 | 196 W | 6 | Modeled power: about 76% of channels 1 + 2; likely GPU-core power estimate |
| 1 / 3 | 90 W | 4 | Channel 0 minus 5 W, floored at zero; a slot-derived model if channel 0 is the slot |
| 4 | 66 W | 0 | First INA3221 lane; likely PCIe-slot 12 V input |
| 5 | 87 W | 1 | Second INA3221 lane; likely 6-pin input |
| 6 | 162 W | 2 | Third INA3221 lane; likely 8-pin input |
| 7 | 228 W | 3 | Combined board-power target: channels 0 + 1 + 2 + 4 W |
| ROM-only 8 | 170 W | 3 | Another budget for the same combined channel; absent from the live policy mask |

The channel links and numerical formulas have stronger evidence than the
physical connector names. Slot / 6-pin / 8-pin names are inferred from the
66 / 87 / 162 W thresholds, three physical 12 V channels, and the aggregate
topology. PCB continuity or independent connector measurements would be
needed to prove the lane ordering. The 90 W policy is **not established to
be FBVDD/memory power**, although a derived rail estimate is plausible.

The 196 W duplicates have ROM kind/flag bytes `0x02` and `0x22`; the 90 W
duplicates have `0x03` and `0x23`. Their channel and numerical limits are
identical. The differing `0x20` flag does not identify a different sensor;
its exact control semantics remain unknown.

ROM entry 8 has min/default/max **120 / 170 / 225 W**, channel 3 and kind
byte `0x00`, whereas entry 7 has kind `0x01`. It is present in the ROM but
not instantiated as an active policy in the captured `0xff` RM mask.
This does not establish that its 170 W value is currently enforced. A reserved
Quadro/Tesla configuration or an optional unpopulated EPS connector is a
hypothesis; the shared combined-channel reference does not identify a separate
sensor or prove that connector assignment.

## Topology evidence

The ROM's physical channel records select INA3221 device 0 (sense-table
index) and lanes 0, 1, 2. They contain scale `4064/4096` and an additive
`-1200 mW` field. Their domain tags are `0xff`, `0xfc`, and `0xfa`.
These fields also appear in the live RM channel-info records.

The relation subtable has three unity relations referencing channels
0, 1, 2, and a fourth relation referencing channel 5 with Q12 coefficient
`3113/4096 = 0.760009765625`. Composite records select ranges of these
relations. That produces the following formulas in reported mW:

- `C3 = C0 + C1 + C2 + 4000` (tag `0xf5`).
- `C4 = max(0, C0 - 5000)` (tag `0xf9`).
- `C5 = C1 + C2` (tag `0xf6`).
- `C6 = round(C5 * 3113 / 4096)` (tag `0x01`).

A read-only request to the already captured legacy channel-status GET,
`0x20802613`, selected the valid `0x7f` channel mask. All three idle samples
matched these formulas. One example, channels 0–6 in mW:
`[3402, 0, 1017, 8419, 0, 1017, 773]`.
This validates the aggregate relations at idle; it does not prove loaded
response, physical lane names, or behavior below a calibration clamp.
No GPU settings or I2C registers were written, and no load was started.

## Shunt modification

The user reports 5 mOhm stacked on existing 5 mOhm shunts. Each modified
shunt is therefore nominally 2.5 mOhm. The ROM's 5 mOhm calibration implies
roughly half-scale uncorrected sensor power/current on that lane. The BIOS
thresholds themselves retain their programmed values in the driver.

Calibration offsets and derived channels prevent an exact blanket doubling
of every displayed number. For example, a modeled core-power value is not
an independent direct measurement of NVVDD output current. Correct each
modified sensor path before applying the calibrated sums and estimates;
do not treat twice a policy threshold as a verified physical power ceiling.

## Reproduction and evidence

- `decode_rom.py`: offline parser, exact eight-policy comparison, and formula checks.
- `decoded-rom.json`: table offsets, raw records, parsed fields, and checks.
- `read_channels.py`: explicitly restricted GTX 770/R472 read-only channel capture.
- `channel-readbacks.json`: fresh metadata and three raw channel-status readbacks.

The original ROM hash is recorded by the decoder. The hardware-read script
refuses to overwrite existing evidence. Upstream parsers are linked above and are not redistributed here. The ROM
is not included. Hardware captures omit device UUIDs and process transport
handles; the public legacy capture retains only the relevant policy records.

Run the offline comparison without touching hardware:

```powershell
python experiments/kepler-rom-analysis/decode_rom.py path/to/GK104.rom --output new-decoded-rom.json
```
