# V3 power-policy rows on the RTX 3090 Ti Kingpin (GA102), 617.42

Raw results: [JSON](ampere-ga102-kingpin-v30-policy-rows-61742-20261105.json).

Scope: one board on one driver at idle. Request, readback and restoration; no
physical measurement.

## Rows

The policy packets have the modern geometry (INFO 20512, CONTROL 13876, STATUS
397048 bytes). Every occupied INFO row records BIOS table version `0x30`, where
the arbitration, integral-bound and relationship decoders required `0x40`.

Read with the version check bypassed, all 14 rows were self-consistent:

- Every host and PMU arbitration vector used a known method, and its output
  equalled the one computed from its inputs. Each host vector held RM client 254
  carrying exactly the CONTROL request.
- Each mode-2 integral row (policies 2 and 5–8; 43 past and 7 next samples) had
  effective = floor((request × upper + 2048) / 4096). For example, policy 5 is
  378000 mW × 4301 → 396918 mW. Policy 2 at upper 4424 is the #59 relation.
- The ten type-0 relationships, all weight 1.0, target occupied policies with
  known units. The type-0 source (policy 2) reports no enabled range (flags 0,
  range `0xFFFF`).

## Writes

Through `PowerIntegralController` after accepting v30 rows:

| Field | Request | Readback | Effective | Predicted |
| --- | ---: | ---: | ---: | ---: |
| Upper | 4300 | 4300 | 396826 | 396826 |
| Upper | 4301 | 4301 | 396918 | 396918 |
| Lower | 3890 | 3890 | 396918 | 396918 |
| Lower | 3891 | 3891 | 396918 | 396918 |

The first read after each write already held the predicted value. Both bounds
ended at their original values.

Sample counts and mode stay read-only (#57). Relationship weights stay read-only
on this board, because weight editing needs a v40 BIOS power table.
