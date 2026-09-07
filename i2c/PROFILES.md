# I2C recipes and controller adapters

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow. This
reference describes the current interfaces in [railctl.py](../railctl.py),
[mp2888.py](../mp2888.py), and [ncp4206.py](../ncp4206.py).

An I2C recipe is TOML register data. A controller adapter supplies behavior that
cannot be represented by the generic signed-offset writer. A saved **tuning
profile** is a separate JSON snapshot of requested settings and the controller
connection/recipe fingerprint; it is not a new register recipe.

## Discovery and selection

| Path | Discovery | Write behavior |
|---|---|---|
| NCP4206 | Kepler ports 0–7, address `0x20`; documented or observed controller identity plus VOUT_MODE; no PCI/subsystem filter | Absolute VID and ordered command/mode changes in the Python adapter; Auto restores GPU VID control |
| MP2888A | Ports 0–7, addresses `0x08`–`0x77`, `0x20` first; repeated register/telemetry fingerprint; no PCI/subsystem filter | Adapter binds the TOML offset recipe to the discovered location and rechecks its fingerprint |
| Other TOML recipes | Optional PCI constraints, one configured port and configured address(es), then all identity checks | One signed `offset_mv` field through the generic guarded writer |

`railctl.discover()` returns all matching candidates. `railctl.find()` returns a
rail only when exactly one matches. Druta displays the candidates' locations
and available scan-time telemetry. Multiple candidates require a selection; a
saved tuning profile may select exactly one candidate matching its complete
recorded identity. Neither an address acknowledgement nor an address register
is a unique model ID or proof of which rail the controller drives.

The MP scanner is dispatched for recipes whose `profile.regulator`, compared
case-insensitively, is `MPS MP2888A`. This is recipe routing, not evidence that a
device is an MP2888A. Do not give another part that name to bypass its discovery
checks. NCP4206 uses its built-in adapter rather than a TOML offset recipe.

The MP fingerprint checks `0xBE & 0x7f` against the responding address, documented
field/reserved-bit patterns, supported offset range, and repeated voltage,
current and temperature readings. User-programmable ID bytes are diagnostics,
not mandatory default values. It is labelled a candidate; Verify establishes a write response, not a unique
model identity. See the [compatibility matrix](../DRIVER-COMPATIBILITY.md)
for the distinction between live board evidence and mocked coverage.

## Verify, Apply and recovery

Discovery performs reads only. **Verify performs real bounded writes** under
load, measures response against baseline variation, and restores the entry
setting. Fingerprint checks must establish compatibility before that first
trial. Verify cannot make an incorrectly specified register safe to probe.

The generic verifier compares sensed rail voltage with GPU VID when available,
stops at a detected response or failure, and checks restoration of the exact
original offset field. Refused restoration, exceptions or incorrect readback
force failure. A detecting step is not a calibrated gain or exact deadband.
NCP4206 has its own voltage-target ladder and command/mode restoration.

Apply requires a valid verification bound to the current GPU, controller object,
port/address and recipe. Card/controller changes, rescans and observed connection
loss invalidate that result. Stock/reset cannot make a first write to an
untouched MP candidate. Recovery remains available on the same connection after
a verification write/restoration attempt; load setup failure alone grants no
writes. Stock sets the offset to zero, whereas Verify restores its entry offset,
which may be nonzero. NCP4206 Auto returns voltage control to GPU VID.

No `[[write]]` means a read-only recipe. Missing `[[identity]]` is rejected by
the parser; an identity mismatch is not selected as a candidate. These are
separate conditions. The unit tests validate software contracts and recipe
structure, not hardware compatibility.

## Recipe files and compatibility

The app loads `i2c/*.toml`, excluding `TEMPLATE*` and `_`-prefixed authoring files.
For a packaged build, files beside the EXE take precedence over same-named
bundled fallbacks. Copy [TEMPLATE.toml](TEMPLATE.toml) only for a genuinely new
recipe; another board with an already supported controller usually needs an
evidence report rather than a duplicate file.

[rtx2080ti-mp2888a.toml](rtx2080ti-mp2888a.toml) retains its historical parsed
recipe data for saved-profile compatibility. The MP adapter overrides its old
current encoding and fixed-location assumptions. At the original connection its
saved recipe identity is preserved; relocated candidates bind their actual bus
and discovery recipe into the fingerprint. Correct explanatory comments without
silently changing parsed data. Behavioral recipe changes need compatibility
tests or a deliberate migration; old tuning profiles may otherwise be refused.

## TOML fields

### `[profile]`

```toml
[profile]
format = 1
name = "Example board - NVVDD (Example controller)"
regulator = "Vendor PARTNUM"
rail = "NVVDD"
author = "Contributor handle"
```

Use the actual part and rail. Do not infer rail identity solely from voltage or
bus position. The controller display name and saved recipe identity may differ
for discovered adapters.

### `[provenance]`

```toml
[provenance]
datasheet = "Exact part, public document URL, revision and date"
registers = "Command-table pages and field-definition pages"
address = "Port/address, raw identification reads and interpretation"
behaviour = "Measured conditions, load, results and date"
hardware = "Stock board or exact modifications"
```

Cite manufacturer documentation, public application notes or licensed open-source
code, and distinguish those sources from your own measurements. Do not use leaked
proprietary register maps. Contributor tests require filled provenance and no
TODO markers; parsing alone does not certify the sources or measurements.

### `[match]`

```toml
[match]
pci_device = ["0x1E02"]
pci_subsys = ["0x12A310DE"]
```

These optional filters apply to ordinary TOML recipes. Each nonempty filter requires the selected GPU's known ID to occur in its
list before that recipe probes the bus. Omitting
both removes only this prefilter, not the identity checks. Built-in NCP4206 and
MP2888A discovery bypass board-ID matching; the historical MP IDs record the
authoring board, not an eligibility restriction.

### `[bus]`

```toml
[bus]
port = 1
addr7 = 0x20
```

Addresses are seven-bit values. For a generic recipe, replace `addr7` with
`addr7_probe = [0x20, 0x21, 0x22]` to probe several documented addresses on the
configured port. All matching candidates are retained; the first response does
not automatically win. MP discovery supplies the runtime port/address itself.
There is no generic TOML port-list or command-sequencing facility.

### `[[identity]]`

```toml
# Address consistency ONLY; insufficient to identify a controller on its own.
[[identity]]
reg = 0xBE
bytes = 1
mask = 0x7F
value = 0x20
fingerprint = true
note = "MP address field at this location; runtime adapter checks the rest."
```

For another controller, use its documented registers and expected values.
`equals` checks the whole result; `mask` and `value` check selected bits. Every
identity block must pass. Specify an actual expected value/mask, not merely a
readable command. Mark fingerprint checks honestly; do not describe address
configuration or user-programmable bytes as immutable model identification.

### `[[telemetry]]`

```toml
# MP2888A voltage example; not a universal PMBus encoding.
[[telemetry]]
key = "vout_mv"
reg = 0x8B
bytes = 2
encoding = "uint"
scale = 1.0
```

Generic decoding supports `uint`, `int` (two's complement), and `linear11`.
Optional `bits = "hi:lo"` extracts a field; `scale` applies to integer encodings.
`linear11` uses its embedded exponent. There is no generic `vid` decoder.
`vout_mv` is required. Give signed fields explicit bit widths.

MP2888A current is **not LINEAR11**: the adapter checks the fixed high nibble,
uses the low 12 bits and selects 0.25/0.5 A per code from `0x44` bit 3. Its voltage
report is direct mV and temperature uses 0.1 degrees C per code. Static TOML
scaling cannot express a scale selected by another register; use adapter logic.
Sources: [MPS datasheet](https://www.monolithicpower.com/en/documentview/productdocument/index/version/2/document_type/Datasheet/lang/EN/sku/MP2888A)
and [Linux MP2888 driver](https://kernel.googlesource.com/pub/scm/linux/kernel/git/axboe/linux/+/fc2ce3ee106f2d53eb344f5c4963c897bbb21634/drivers/hwmon/pmbus/mp2888.c).

### `[[write]]`

```toml
# MP2888A-specific signed offset recipe.
[[write]]
key = "offset_mv"
reg = 0x23
bytes = 2
bits = "7:0"
encoding = "int"
lsb_mv = 6.25
raw_min = -111
raw_max = 112
note = "Transaction width 2 bytes; only the low 8-bit signed field is writable."
```

Omit this section for read-only contributions. The generic UI/writer operates
one `offset_mv` entry; adding other keys does not implement new controls or
ordered sequences. `bytes` is the transaction width; `bits` is the field width.
The generic setter writes that field and zeros bits outside it. A controller
requiring preservation of neighboring writable fields needs a dedicated adapter.

Document both the supported raw range and the field's representable range.
MP2888A supports -111..112 codes, narrower than signed eight-bit representation;
values 113..127 are already outside the documented range, while 128 wraps to a
negative signed value. Druta refuses requests outside the declared supported
range in every mode. Do not copy MP limits or scale into another part's recipe.

### `[limits]`

```toml
[limits]
envelope_min_mv = -200.0
envelope_max_mv = 100.0
rail_ceiling_mv = 1200.0
sanity_max_rail_mv = 2000.0
plausible_rail_mv = [400.0, 1300.0]
```

These are the historical MP recipe's software bounds, not universal board
ratings. Normal mode enforces its offset envelope and predicted rail ceiling;
XOC removes those software bounds. Supported raw range and the sanity ceiling
remain enforced. Neither permission implies that cooling, silicon or board
components tolerate the request. Establish the bounds from your contribution's
sources and measured operating conditions; lowering voltage can also destabilize
the GPU. Controller-specific limits may additionally constrain the request.

### `[verify]`

```toml
[verify]
rungs_mv = [6.25, 12.50, 25.00, 50.00, 75.00]
min_loaded_vout_mv = 800.0
```

These are bounded trial steps for the generic offset verifier, relative to its
entry offset. The UI induces load and supplies the GPU voltage reference; the
minimum operating voltage is checked before trials. `expect_deadband_mv` is
optional historical metadata, not a gain correction or proof of behavior on
another board. Do not widen a failed ladder merely to obtain a pass.

### `[[never_write]]`

```toml
[[never_write]]
reg = 0x04
why = "MPS password command; not a voltage adjustment."
```

This adds controller-specific hazards to `railctl.NEVER_WRITE`; it cannot remove
a built-in entry. Whitelisting a built-in denied command is rejected. The generic
writer refuses commands outside its write whitelist or in the combined denylist.
These explicit lists do not automatically recognize every undocumented vendor
command: document additional hazards and review adapter write paths separately.

## Checks before submitting

Use the [contribution workflow](CONTRIBUTING.md#5-submit-the-evidence-and-run-checks)
and [PR template](../.github/PULL_REQUEST_TEMPLATE/i2c_profile.md). Include
read-only discovery evidence for every contribution and write/restore evidence
only for capabilities actually exercised. Mock failure paths and unrelated
responders; do not put live hardware writes in unit tests. Distinguish measured
boards, controller-level expectations and untested configurations.
