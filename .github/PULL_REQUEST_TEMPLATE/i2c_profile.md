<!-- For I2C evidence, discovery adapters or register recipes.
Read i2c/CONTRIBUTING.md and i2c/PROFILES.md. Existing MP2888A/NCP4206 boards
usually need evidence, not another board-ID entry or duplicate TOML. -->

## Contribution

- [ ] Existing controller: new board/location evidence
- [ ] New read-only recipe or discovery adapter
- [ ] New or changed writable recipe/adapter
- Changed files / capability:

## Hardware and scope

- Card and PCI slot:
- Driver / VBIOS:
- PCI device / subsystem IDs (evidence, not automatic scanner gates):
- Controller marking / rail and how the rail was established:
- Stock or modified; exact SMBus links or other modifications:
- Live-tested boards versus mocked/untested configurations:

## Read-only discovery

- Ports / addresses surveyed:
- Candidates shown by Druta; selection if multiple responded:
- Identity or compatibility-fingerprint registers, raw bytes and expected fields:
- Datasheet URL, exact part/revision/pages; other public code sources and licence:
- Unrelated responders rejected; live or mocked evidence:

<details><summary>Survey output: python -m druta.tools.i2c_discover --slot YOUR_SLOT</summary>

```text
paste here
```

</details>

<!-- Address acknowledgement and MP 0xBE alone are not unique model identities.
An empty scan is a useful negative result. Discovery performs no writes. -->

## Telemetry

| Operating point / load | Raw register bytes | Decoded rail voltage | GPU VID / independent measurement |
|---|---|---|---|
| | | | |

- Per-command widths, encodings and scale/configuration:
- Explanation of expected sensed-voltage versus VID differences:

## Write and restoration evidence (omit for read-only work)

- Documented command/field, transaction width, supported raw range:
- Generic signed offset or adapter sequence; preservation of neighboring fields:
- Original control bytes / entry offset or mode:
- Full Verify log: baseline/noise, every requested step, response and threshold:
- Restoration result and exact readback:
- Bounded Apply and Stock/Auto results after Verify:
- Failure/recovery tests (wrong readback, partial sequence, load failure):
- Final hardware state:

<!-- Verify itself writes. Do not infer a calibrated gain/deadband from its
first detecting step. A failed or inconclusive result stays in the report;
do not enlarge the trial ladder solely to obtain a pass. -->

## Software checks

- [ ] Recipe changes pass `python -m unittest tests.test_profiles` (or not applicable)
- [ ] Adapter/discovery/UI changes pass `python -m unittest discover -q` (or not applicable)
- [ ] Discovery tests issue no writes; ordinary tests do not access real GPUs
- [ ] Ambiguity, wrong-controller rejection and connection-bound Verify tested
- [ ] Restore failure cannot authorize Apply; untouched MP candidates cannot reset
- [ ] Saved-profile identity compatibility or migration documented
- [ ] Provenance is complete; no unexplained copied measurements or TODOs
- [ ] Hardware and mocked results are clearly distinguished
