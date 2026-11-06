# Relationship-weight override: RTX 5080, driver 617.42

On this device, a persistent VBIOS/registry override changed configured relationship 9's Q4.12 weight from 4096 (1.0) to 4080 (0.99609375). A GPU driver refresh loaded the new value. Removing the override and refreshing again restored 4096, the original registry snapshot and the original running image hash. This was one device and one operating point; it does not establish safe bounds or a general dependency formula.

The ROM edit changed only the two low bytes of the relation parameter (`0010` to `f00f`), leaving its high half and all other image bytes unchanged. Across three observations in each phase, the complete INFO packet matched the baseline with only those two selected weight bytes projected for the changed phase. The complete CONTROL packet matched the original in all nine observations. The independently decoded raw STATUS prefixes for policies 2 and 9 matched their recorded arbitration rows. The ordinary board limit was 360000 mW throughout the sampled phases; the trial explicitly requested 360000 mW after each refresh. No workload was run.

| Phase | Weight raw | Policy 2 PMU output, mW | Policy 9 effective and PMU client 2, mA | Policy 9 PMU client 7, mA |
| --- | ---: | ---: | ---: | ---: |
| Before | 4096 | 360000 | 36002 | 45000 |
| Changed | 4080 | 360000 | 36035 | 44824 |
| Restored | 4096 | 360000 | 36002 | 45000 |

Each row summarizes three matching samples of these values. Policy 2's effective limit was 370811 mW, distinct from its PMU arbitration output. Applying the previously documented weighted interpolation with policy 2's PMU output as a **hypothetical** input predicts the client 2 values here. Rounding `45000 × weight / 4096` also fits the observed client 7 values. These numerical fits do not identify either vote's producer or prove that this host calculation runs on the PMU. The mA values are policy readbacks, not physical current measurements.

The [machine-readable receipt](power-weight-override-5080-61742-20261105.json) records all nine observations, packet-prefix hashes, image hashes and source hashes. The offline audit and original local captures are under `registry-overrides/power-weight-root-20261105`; the prior [relationship analysis](power-relationship-formula-5080-20261105.md) describes the host-side arithmetic and its limits. Device metadata is attributed to the earlier [5080 receipt](power-explicit-source-20261105.json).
