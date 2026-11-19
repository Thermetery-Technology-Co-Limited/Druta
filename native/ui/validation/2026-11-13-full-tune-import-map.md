# Released Python full-tune import map

Source: pinned `fa6ee83`, `src/druta/profiles.py` (`capture`,
`capture_rails`, `_validate_saved_fields`, `preflight`, `_restore_validated`),
`tests/test_profile_preflight.py`, `tests/test_profile_memory_scale.py`,
`tests/test_profiles.py`; native `profile_catalog.cpp`, `curve_profile.cpp`,
`fan_profile.cpp`, `python_scoped_profile.cpp`, and their dedicated tests.
This is an implementation map, not a claim that these controls are qualified
on any particular device.

The full snapshot is **unscoped** (`schema: 2`, no `scope`), unlike the
already recognized schema-2 scoped children. `saved_at`, `saved_ts`, `device`
(name, vbios, driver, uuid, slot, mem_type_id), `incomplete`, and optional
`current_limits_note`, `power_policy_names`, `xoc` describe capture and
context. Nonempty `incomplete` must be surfaced; `xoc` is not permission to
unlock native controls. Missing/null fields are not zero. The released capture
can omit policy/pin/name keys and can contain partial capabilities.

| Full snapshot fields | Serialized meaning and native import work |
| --- | --- |
| `core_off_mhz` | Scalar core offset in MHz; overlaps the core curve target and a domain-0 control. Preflight before any write. |
| `mem_off_units`, `mem_off_true_mhz`, `mem_off_scale`, `device.mem_type_id` | Driver units and true MHz with capture scale. Python checks agreement and uses saved units on the same memory; otherwise true MHz. Native must use current qualified scale/grid, exact representability and adapter/memory evidence, not assume scale 2 or 16. |
| `power_limit_mw`, `current_limits_ma` | Configured board request, plus policy-index-to-mA map. A bare policy index/value does not establish type/channel/unit/bounds for a native selected write. Full power and pins are coupled to board changes; fresh current identity and selected readback/Undo are required. |
| `power_policies`, `power_policy_pins` | String policy index -> `{value,channel,type}`; pins may be absent, and old integer-only pin entries lack binding. Not the scoped `power_action` rows with bounds/unit/board markers. Derive only from a fresh qualified matching policy, preserve pin presence and unrelated pins; don't silently drop ambiguous or unsupported entries. |
| `fan_control_state`, `fan_manual`, `fan_pct` | Preferred structured `source` plus per-fan IDs, manual/policy and level; existing fan profile validation and same-source ID/position conversion can qualify it. Measured duty alone cannot distinguish Auto from Manual; absent structured state needs explicit mode and a proven compatible native fallback, never infer manual from duty. |
| `vf_applicable`, `vf_deltas` | Map canonical decimal string point index to integer kHz delta, bounded by Python's current maximum; no saved point voltages, divisor, GPU/other partition, or complete layout. Current GPU-index validation is necessary but cannot prove historical geometry. Native curve profile requires saved geometry/voltage; do not manufacture it. A separate explicitly reviewed current-layout-bound adapter or refusal is needed. |
| `rail_limits_uv`, `rail_limits_mv` | Raw signed integer microvolt request fields (`reliability`, `alt_reliability`, `overvoltage`, `vmin`) under rail `0`/`1`; raw wins whenever the key exists. The mV map is estimated absolute values, not a raw request; never silently translate it without fresh verified base. Existing Python rail subset and canonical `rail:<index>:<field>` overlap checks apply. |
| `nvvdd_offset_mv`, `msvdd_offsets_mv`, `clock_domain_offsets_mhz` | Selected scalar/decimal-domain requests; validate finite exact representation, current layout, bounds and overlap with core/domain aliases. Nonzero MSVDD required Python XOC opt-in; saved flag does not authorize native session gates. |
| `i2c` | Optional controller identity plus legacy offset, absolute `control`, current-DAC format/`current_dac_control`, or multi-state offset format/`control`. Python preflights identity, recipe, presence, ownership and dry run, rejects session-only manual/iCX replay, and orders controller work before clocks. Native iCX remains standalone; an unrecognized/non-null I2C component cannot be discarded in a purported full import. It needs a typed controller-specific execution/receipt contract or explicit refusal. |

Python validates deterministic fields and current GPU V/F indices before any
restore; its individual setters still perform live checks. It prefers full fan
state, restores board before selected policies/pins, and compares selected
policy channel/type. See pinned `tests/test_profile_preflight.py` for invalid
late fields stopping writes and non-GPU curve indices; `test_profile_memory_scale.py`
covers historical/current memory scale. Native catalog flattens only known
scoped components, rejects unknown scopes, checks whole-selection overlap and
uses typed issued receipts and final readback. Existing native curve tests cover
identity/layout/divisor/voltage; fan tests cover ID reorder/source conversion;
scoped tests cover coupling and pins. None establishes the missing historical
V/F geometry or a generic full-snapshot I2C replay contract.

A bounded importer should validate the **entire source before selection**,
expose every present actionable field or reject it with a precise reason, retain
unknown metadata without treating it as tuning, and assemble only qualified
existing semantic components. It must check cross-component canonical overlaps
and whole preflight before any writes, retain issued-only grouped recovery and
verify selected no-ops. Do not implicitly apply on load or restart. Where a
source lacks evidence required by the native writer, report the limitation
rather than invent geometry, policy bindings, ownership or physical defaults.
