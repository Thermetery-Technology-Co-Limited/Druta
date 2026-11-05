# Linked-current and relationship-weight composition on one RTX 5080

A reviewed idle trial on one RTX 5080 with driver 617.42 exercised two nested persistent BIOS registry overrides, then restored each one. The tested adapter reported a type-32 policy 7 linked-current setting and a type-0 relationship 9 weight targeting policy 9. These results describe this adapter and operating point; they are not a compatibility rule or proof of physical enforcement.

| Phase | Relationship 9 weight | Policy 7 linked-current readback | Policy 9 client 7 vote | Policy 9 effective limit |
|---|---:|---:|---:|---:|
| Before | 4096/4096 = 1 | 45,000 mA | 45,000 mA | 36,002 mA |
| Weight applied | 4080/4096 = 0.99609375 | 45,000 mA | 44,824 mA | 36,035 mA |
| Linked current also changed | 4080/4096 = 0.99609375 | 44,992 mA | 44,816 mA | 36,035 mA |
| Linked current restored | 4080/4096 = 0.99609375 | 45,000 mA | 44,824 mA | 36,035 mA |
| Weight restored | 4096/4096 = 1 | 45,000 mA | 45,000 mA | 36,002 mA |

Each row was observed in three samples. Policy 9 reported minimum arbitration: client 2 supplied the lower value matching the effective limit in each phase. The policy 7 linked-current readback is a separate reported base/request value from policy 9's client 7 contribution. The values are consistent with a weighted calculation: 45,000 × 4080/4096 = 44,824.21875 and 44,992 × 4080/4096 = 44,816.25. A host callback contains multiply and Q12 rounding operations, but the saved host trace does not prove the complete dispatch or identify the producer of client 7's vote. These observations do not establish a universal formula, timing, clamping behavior or physical response.

The audit compared all 20,512 INFO bytes and all 13,876 CONTROL bytes in every sample to the baseline, allowing only the selected relationship weight and linked-current field changes. It decoded the saved STATUS prefixes for policies 2, 7 and 9, checked the type-32 STATUS `+0x148` float, and reconstructed both changed image hashes from the original ROM and exact patches. Each nested restore recovered its corresponding complete saved registry state and running-image SHA-256. The child restore returned to the weighted state; the parent restore returned to the original state. Both production transaction receipts reported successful selected driver readback verification.

The baseline sample made no board-limit request. The four later sampling phases each issued a 360 W board-limit request and reported the board limit as 360 W before and after; those phases were therefore not passive reads. No workload or Windows reboot was performed. The weight-only phase reproduced the earlier effective-limit change from 36,002 to 36,035 mA; the separate base-current edit left that lower competing limit unchanged.

The machine-readable [audit results](linked-current-weight-composition-5080-61742-20261105.json) contain the image hashes, phase values and SHA-256 hashes of the 12 saved trial, sample and receipt files. The audit script and raw receipts are in `registry-overrides/linked-current-weight-interaction-20261105/`; the accompanying host review records the indirect dispatch boundaries.
