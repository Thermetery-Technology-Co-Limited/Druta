# Kepler and Maxwell current-limit investigation

**ROM follow-up:** The supplied GTX 770 ROM confirms that all eight limits
below are in mW and reveals physical and modeled channel relationships.
See [the ROM findings](experiments/kepler-rom-analysis/FINDINGS.md) for the
66 / 87 / 162 W input-channel mapping, derived 196 / 90 W policies, ninth
ROM-only entry, and the user's 5 mOhm-on-5 mOhm shunt modification. The
capture-only uncertainties below describe the earlier investigation.

Read-only measurements on 2026-09-08 with NVIDIA driver **472.12**:

| Card | VBIOS | Legacy policy mask | Modern current-policy info GET | Druta current sliders |
|---|---|---|---|---|
| GTX 745 (Maxwell), `0000:01:00.0` | `82.07.32.00.6a` | `0x0`: no active records | RM `0x56` (`NV_ERR_NOT_SUPPORTED`) | None |
| GTX 770 (Kepler), `0000:02:00.0` | `80.04.c3.00.01` | `0xff`: policies 0–7 | RM `0x56` (`NV_ERR_NOT_SUPPORTED`) | None |

The ordinary NVAPI power queries use older RM commands on these cards:
`0x20802618` (info, 5,520 parameter bytes), `0x20802619` (dynamic,
69,148 bytes), and `0x2080261a` (control, 2,124 bytes). These differ from
the layouts validated for Druta's Pascal, Turing, and Blackwell current
controls. Reusing their offsets or policy numbers would not be justified.

The GTX 745 info query succeeds with no active policy records. Its legacy
dynamic query returns RM `0x40` (`NV_ERR_INVALID_STATE`), even though the
outer NVAPI query returns success. The GTX 770 exposes eight legacy records,
with no policy 13 or 14 and no validated current-unit descriptor. Their
numeric values are not evidence of amp limits. Further decoding below links
policy 7 to the ordinary power target. The ROM follow-up establishes all
eight entries as mW and resolves their channel formulas; physical connector
assignments remain inferred.

To distinguish missing driver support from Druta's architecture filter, a
read-only modern `0x2080a618` query was also issued for each card using its
freshly captured client/subdevice and escape transport. Both returned
NTSTATUS zero but RM `NV_ERR_NOT_SUPPORTED`. No setter was called and no GPU
configuration was changed. Production `GPU.get_current_limits()` returned
an empty list for both cards, as expected.

**Conclusion:** Druta's validated current-limit controls do not apply to
these two cards on this driver. This does not establish that every Kepler
or Maxwell board lacks firmware or regulator current limits, nor that a
different board or driver could never expose another usable interface.

## GTX 770 legacy records

Info records start at byte `0x3c` with `0x84` stride. The packed descriptor
appears to encode type in the low byte and channel in the next byte; these
field names remain a provisional interpretation of the legacy layout.

| Policy | Descriptor | Apparent type / channel | Minimum raw | Default raw | Maximum raw |
|---|---|---|---:|---:|---:|
| 0 | `0x0601` | 1 / 6 | 25000 | 196000 | 196000 |
| 1 | `0x0402` | 2 / 4 | 10000 | 90000 | 90000 |
| 2 | `0x0601` | 1 / 6 | 25000 | 196000 | 196000 |
| 3 | `0x0402` | 2 / 4 | 10000 | 90000 | 90000 |
| 4 | `0x0003` | 3 / 0 | 30000 | 66000 | 100000 |
| 5 | `0x0103` | 3 / 1 | 30000 | 87000 | 100000 |
| 6 | `0x0203` | 3 / 2 | 30000 | 162000 | 180000 |
| 7 | `0x0300` | 0 / 3 | 120000 | 228000 | 243000 |

Policy 7 is selected by the ordinary NVAPI power-status query (mask `0x80`)
and its control readback contains `228000`. Its minimum/default and
maximum/default ratios, rounded to integer pcm, are **52632 / 100000 /
106579**, exactly matching NVAPI PowerPolInfo. This strongly identifies
policy 7 as the ordinary board-power target, consistent with **120 / 228 /
243 W** when the raw unit is mW. No write was needed for this correlation.

The ROM follow-up confirms that the other records also use mW. Physical
connector assignments remain inferred. Policies 0/2 and 1/3 share descriptors and
bounds, differing in another flag field; they should not be assumed to be
four separate rails. Policies 4–6 appear to monitor three distinct channels.
These observations do not establish whether any additional policy accepts
a write or changes actual throttling behavior.

Evidence:

- [Public legacy policy records](experiments/current-limits-kepler-maxwell-20260908.json)
- [Public modern info GET results](experiments/current-limits-kepler-maxwell-modern-get-20260908.json)
- [Read-only legacy capture script](experiments/probe_kepler_maxwell_current.py)

Public artifacts omit device UUIDs, process transport handles and bulk
unused packet data; the legacy artifact retains each active policy record.
Original capture SHA-256 hashes identify the source of each summary.

The capture script deliberately refuses to overwrite its existing evidence
file. Choose a new output filename before collecting another run.
