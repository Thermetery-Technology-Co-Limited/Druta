<!--
For adding or changing an I2C rail profile. Read i2c/CONTRIBUTING.md first.
For anything else, delete this template and describe your change normally.
-->

## Board

- Card:
- PCI device / subsystem id:
- Regulator (as the datasheet spells it):
- Rail:
- **Board is:** stock / modified *(if links were fitted, say exactly which)*

## Profile type

- [ ] Telemetry-only (no `[[write]]`) - read-only, cannot write by construction
- [ ] Includes a write

## Step 1 - bus survey

<details><summary><code>python tools/i2c_discover.py</code> output</summary>

```
paste here
```

</details>

## Step 2 - how the part was identified

<!-- Which register(s), what they returned, and the datasheet page. "It ACKed at
this address" is not an identification: on the reference board, fitting the mod
links made a different device answer at the same address. -->

- Datasheet (part, revision, date):
- Identifying register(s) and page:

## Step 4 - telemetry checked against the GPU

<!-- Under load, not at idle. -->

| | decoded from I2C | GPU's own reading |
| --- | --- | --- |
| core voltage | | |

- Load used:
- Encoding confirmed as:

## Step 5 - staircase (only if this PR adds a write)

- Rung at which the rail first moved:
- Measured response:
- Deadband observed, if any:

<!-- If nothing moved by the largest rung, say so here and leave the write out
of the profile. A write that was never observed to work should not ship. -->

## Checks

- [ ] `python -m unittest test_profiles` passes
- [ ] Every `[provenance]` line is filled in and says how I know
- [ ] No numbers copied from another board or another profile
- [ ] Measurements were taken under load
- [ ] No `TODO` markers left in the file
