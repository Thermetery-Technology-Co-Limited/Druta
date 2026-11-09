# Druta native core (work in progress)

`native/` holds the C++17 core that is replacing the Python backend one
feature at a time, and the tools built from it. Nothing here is shipped yet.

- `include/drutacore/drutacore.h`: the C ABI. Python (`ctypes`) and the
  future native front end both call this; nothing else crosses the boundary.
- `src/core/`: the pure core (packet layouts, validation, identities). It has
  no Windows calls, so it builds and runs its tests on Linux.
- `src/backend/`: the driver-facing port of `nvbackend.py`: NVML and NVAPI
  binding, enumeration and pairing, telemetry, static capabilities and the
  writers, one feature per file.
- `src/win/`: run-time binding of Windows DLL exports. The core imports only
  kernel32 and the C runtime; every other export is a capability looked up at
  run time, so one binary runs on Windows XP through 11.
- `src/probe/`: `druta-probe.exe`, a read-only capability report for one
  Windows installation (see the file header). Run it on a legacy machine and
  send back `druta-probe-report.txt`.

## Build

```
native/build.sh                      # linux, linux-asan, mingw-x86_64, mingw-i686
native/build.sh mingw-i686           # one configuration
native/fuzz/run.sh                   # libFuzzer over the rail packet parser and the PERF decoder (clang)
```

Linux needs cmake, ninja, g++ or clang++, and `mingw-w64` for the Windows
targets; Wine is optional and only smoke-runs the Windows binaries. On a
Windows host, `cmake -S native -B native/build/msvc` works with MSVC; the
`v141_xp` toolset is required for a binary that runs on XP.

The Windows binaries link the C runtime statically and import only
`KERNEL32.dll` and `msvcrt.dll` (PE subsystem version 5.1 = XP). Whether the
MinGW-w64 runtime itself still starts on XP has not been verified on an XP
installation; the probe is the first thing to run there.

## What is ported

Each slice lands with its C ABI, the `druta.native` binding and a parity
test that feeds the same fake driver callbacks to `nvbackend.GPU` and to the
core, then compares results, driver call sequences and messages.

| Slice | Python original | ABI | Parity test |
| --- | --- | --- | --- |
| PCI slot helpers | `nvbackend.parse_slot` family | `dc_parse_slot`, `dc_format_slot`, `dc_same_slot` | `tests/test_native_core.py` |
| Rail packet parser and writer | `rail_transport.py` | `dc_rail_*` | `tests/test_native_core.py`, `native/fuzz/` |
| Identity, enumeration, pairing | `NvAPI`, `Nvml`, `GPU.__init__` | `dc_enumerate_gpus`, `dc_gpu_open`, `dc_gpu_identity_get`, `dc_gpu_status_line` | `tests/test_native_identity.py` |
| Telemetry tick and static capabilities | `GPU.read`, `GPU._read_static` | `dc_gpu_read_telemetry`, `dc_gpu_capabilities_get`, `dc_gpu_architecture` | `tests/test_native_telemetry.py` |
| Fan control | `GPU.read_fan_control_state` .. `restore_fan_control_state` | `dc_gpu_fan_*` | `tests/test_native_fan.py` |
| Voltage boost | `GPU.read_voltage_boost`, `set_voltage_boost` | `dc_gpu_voltage_boost_*` | `tests/test_native_vflock.py` |
| V/F point lock and frequency-lock readers | `GPU.set_vf_lock`, `recover_vf_lock`, `clear_vf_lock`, `vf_lock_self_test`, `read_vf_lock*`, `read_clk_lock*` | `dc_gpu_vf_lock_*`, `dc_gpu_clk_lock_*` | `tests/test_native_vflock.py` |
| V/F curve | `GPU.vfp_layout`, `read_vf_curve`, `read_vfp_other_rows`, `apply_vf_deltas`, `reset_vf_curve`, `_vf_delta_snapshot` | `dc_gpu_vf_layout`, `dc_gpu_vf_curve_*`, `dc_gpu_vf_other_rows`, `dc_gpu_vf_deltas_apply`, `dc_gpu_vf_delta_snapshot` | `tests/test_native_vfcurve.py` |
| Clock offsets, frequency lock, clock grid | `GPU.set_clock_offset`, `_set_pstate20_offset`, `_keep_vf_shape`, `clock_step_khz`, `lockable_clocks_by_mem`, `lock_gpu_clocks`, `reset_gpu_clocks` | `dc_gpu_clock_offset_set`, `dc_gpu_clocks_*`, `dc_gpu_clock_step_khz`, `dc_gpu_lockable_clocks`, `dc_gpu_mem_offset_scale` | `tests/test_native_clocks.py` |
| Pure V/F planners | `GPU.peak_info`, `resolve_vf_point`, `compute_deflatten`, `evaluate_curve_law`, `predict_curve`, `cascade_meta`, `compute_hard_deflatten`, `compute_ramp`, `compute_rephase` | `dc_vfp_*` (`drutacore/vfplan.h`, bound by `druta.native_vfplan`) | `tests/test_native_vfplan.py` |
| Private clock-domain rows and names | `GPU.read_clock_domains`, `classify_domain_names` | `dc_gpu_clock_domains`, `dc_clock_domains_classify` (`drutacore/clkdom.h`) | `tests/test_native_clkdom_names.py` |
| Clock-domain control block and rail offsets | `GPU.clkdom_*`, `read_clk_domain_offsets`, `set_clk_domain_offset`, `rail_offset_capability`, `read_rail_offset_mv`, `set_rail_offset_mv`, `reset_msvdd_offset_mv`, `retry_clock_capabilities`, `refresh_capabilities` | `dc_gpu_clkdom_*`, `dc_gpu_rail_offset_*`, `dc_gpu_msvdd_offset_reset`, `dc_gpu_retry_clock_capabilities`, `dc_gpu_refresh_capabilities` | `tests/test_native_clkdom.py` |
| RM escape transport, layer 0: gdi32 capability and raw escape | `GPU._legacy_clk_gdi`, `_legacy_clk_escape` | `dc_escape_capability_get`, `dc_rm_escape_raw` (bound as `native.escape_capability` and `Gpu._legacy_clk_escape`) | `tests/test_native_transport.py` |
| Current-limit tables | `GPU.CURRENT_LIMIT_GENERATION_POLICIES`, `_CURRENT_LIMIT_LAYOUTS`, `_CURRENT_LIMIT_LAYOUT_FALLBACKS` | `dc_power_layouts_lookup`, `dc_generation_policies` (`drutacore/power_layouts.h`, bound by `druta.native_power_layouts`) | `tests/test_native_power_layouts.py` |
| RM escape transport, layers 1 and 2: the hook captures and the transport cache | `GPU._capture_current_limit_transport`, `_capture_legacy_clk_transport`, `_rm_power_transport`, `_current_limit_transport_layouts`, `_current_limit_profile_supported` | `dc_rm_capture`, `dc_rm_transport_*`, `dc_rm_observed_get`, `dc_rm_layout_choice_*`, `dc_rm_power_transport`, `dc_rm_power_layouts`, `dc_hook_*` | `tests/test_native_rm_transport.py` |
| RM escape transport, layer 3: the Turing legacy clock-lock records | `GPU._legacy_clk_limit_records`, `_read_legacy_clk_lock` (the fallback inside `read_clk_lock*`) | `dc_legacy_clk_records`, and `dc_gpu_clk_lock_*` now reach the fallback | `tests/test_native_legacy_clk.py` |
| RM escape transport, layer 4: the rail-record splice | `GPU._write_rail_records_locked` (after its Python preconditions) | `dc_rail_write_locked` (bound as `Gpu._rail_write_locked`) | `tests/test_native_rail_splice.py` |
| Current-limit RM operations | `GPU._current_limit_abi`, `_current_limit_probe_info`, `_current_limit_rm`, `_current_limit_capacity`, `_power_policy_table` and the `_last_policy_table` / `_power_policy_mask` attributes | `dc_cl_*` (bound as the same `Gpu` methods) | `tests/test_native_current_limits.py` |
| Current-limit rows, getter and verified setter | `GPU._current_limit_state`, `get_current_limits`, `current_limit_diagnostics`, `set_current_limit_ma`, `_set_current_limit_ma` and the `_current_limit_unavailable` / `_current_limit_error` / `_current_limit_read_error` / `_current_limit_last_rows` / `_current_limit_established` / `_power_policy_pins` attributes | `dc_cl_state`, `dc_cl_get_current_limits`, `dc_cl_set_current_limit_ma` and their attribute getters | `tests/test_native_current_limit_state.py` |
| Paired target binding and its prerequisites | `PerfDiagnosticsReader._binding`, `ClientPowerController._binding`; `GPU.arch_name`, `vf_curve_applicable`, the capability keys of `GPU.static`, `NvAPI._i` presence | `dc_gpu_target_bind`, `dc_gpu_target_reset`, `dc_nvapi_interface_present` (bound as `Gpu._target_binding`, `_client_power_binding`, `_target_binding_reset`, `nvapi_interface_present`) | `tests/test_native_target_binding.py`, `tests/test_native_telemetry.py` |
| Power-arbitration decoder | `power_arbitration.decode`, `_geometry`, `_arbitration`, `ROW_FORMATS` | `dc_power_arbitration_*` (`drutacore/power_arbitration.h`, bound by `druta.native_power_arbitration`) | `tests/test_native_power_arbitration.py` |
| Pure rail-limit and hold-headroom helpers | `GPU._valid_volt_rail_state`, `volt_boost_headroom_mv`, `abs_limit_mv`, `rail_ceiling_mv`, `rail_floor_mv`, `hold_headroom_architecture`, `hold_headroom_ceiling_mv`, `hold_headroom_targets`, `_headroom_enforced_mv`, `hold_headroom_bound_mv`, `hold_headroom_allowance_mv` and their constants | `dc_rl_*` (`drutacore/rail_limits.h`, bound by `druta.native_rail_limits`) | `tests/test_native_rail_limits.py` |
| Client-power tables and geometry helpers | `client_power_runtime` constants, `_small_native_geometry`, `_definition_stride`, `_native_geometry`, `_coupled_requests`, `_project`, `_matches` | `dc_cp_*` (`drutacore/client_power.h`, bound by `druta.native_client_power`) | `tests/test_native_client_power.py` |
| Client-power transport and guarded exchange | `ClientPowerController._transport`, `_exchange`, `_reset_transport` and its `__init__` state; `clock_bios_runtime._guarded` / `_guards` | `dc_cpc_transport`, `dc_cpc_exchange`, `dc_cpc_reset_transport`, `dc_cpc_reset`, `dc_cpc_state_get` (`drutacore/client_power_controller.h`, bound as `Gpu._cp_*`) | `tests/test_native_client_power_controller.py` |
| Client-power metadata read | `ClientPowerController._client_group`, `_metadata_once`, `_stable_locked`, `read_metadata` | `dc_cpc_read_metadata` (bound as `Gpu._cp_read_metadata`) | `tests/test_native_client_power_metadata.py` |
| Client-power full read | `ClientPowerController._once`, `_arbitration`, `_read_locked`, `read` (the STATUS packet decoded by the native power-arbitration decoder) | `dc_cpc_read` (bound as `Gpu._cp_read`) | `tests/test_native_client_power_read.py` |
| PERF diagnostics layouts, transport, GET, decoder and read loop | `perf_diagnostics.LAYOUTS`, `_GET_COMMANDS`, `decode_group`, `PerfDiagnosticsReader._transport`, `_get`, `read` (one group per call; `last_valid` / `last_errors` stay in the binding's `Reader`); `hashlib.sha256` | `dc_perf_layouts`, `dc_perf_transport`, `dc_perf_bind`, `dc_perf_get`, `dc_perf_decode_group`, `dc_perf_read_group`, `dc_perf_reset`, `dc_sha256_hex` (`drutacore/perf_diagnostics.h`, bound by `druta.native_perf_diagnostics`) | `tests/test_native_perf_diagnostics.py`, `native/fuzz/` |
| Voltage-rail getters | `GPU._read_volt_rail_blocks`, `_read_volt_rail_deltas`, `read_volt_rail_state`, `read_rail_live_mv` and the four `_volt_rail_*` read caches | `dc_rails_*` (`drutacore/rails.h`, bound by `druta.native_rails`) | `tests/test_native_rails.py` |
| Legacy P0 hold and verification holds | `GPU.legacy_p0_supported`, `legacy_p0_owned`, `hold_legacy_p0`, `release_legacy_p0`, the request half of `verification_legacy_p0`, the `_vf_lock_recovery` record and the lock-table entry read (`_vf_lock_read_raw` / `_vf_lock_entries`) that `verification_p0` needs, `read_vcore_mv`; the two context managers are composed in the binding | `dc_legacy_p0_*` (`drutacore/legacy_p0.h`), `dc_gpu_vf_lock_recovery_get` / `dc_gpu_vf_lock_recovery_set`, `dc_gpu_vf_lock_entry_get`, `dc_gpu_read_vcore_mv` (bound by `druta.native_legacy_p0` and `Gpu.read_vcore_mv`) | `tests/test_native_legacy_p0.py` |
| Clock-BIOS read path | `clock_bios_runtime._control_once`, `_control_shape`, `_info_once`, `_decode`, `read` | `dc_clock_bios_control`, `dc_clock_bios_control_shape`, `dc_clock_bios_info`, `dc_clock_bios_decode`, `dc_clock_bios_read`, `dc_clock_bios_reset` (`drutacore/clock_bios.h`, bound by `druta.native_clock_bios`) | `tests/test_native_clock_bios.py` |

Test seams: `dc_set_export_resolver` serves driver exports (libraries
`nvapi`, `nvml` and `gdi32`) from the test process instead of the DLLs,
`dc_set_sleep_hook` replaces the bounded waits inside readback loops, and
`dc_set_clock_hook` replaces the monotonic clock behind retry cadences (the
rail getters' two-second backoff), as Python tests patch `time.monotonic`.
All three are set through `druta.native`.

The planners refuse two things Python would compute: a NaN voltage that
Python would have to sort (its order there is an artefact of the sort
algorithm) and integers beyond 2^53. `clkdom_mapping_probe` and
`clkdom_debug_report` stay in Python: they are diagnostics that write to the
card and sample over time, which is experiment territory.

The transport's layer 0 reports gdi32's `D3DKMT*` exports as a capability
record (every export is resolved at run time; `complete` means the
EnumAdapters2 path of main is available, an older gdi32 with only
`D3DKMTEnumAdapters` is reported but not yet used) and issues one raw escape
on the adapter whose PCI address matches the paired slot, closing every
enumerated handle. Its errors carry a kind (`transient`, `invalid`,
`unsupported`) and whether the condition is fixed for the process.

The hook (layer 1) rewrites the first bytes of `D3DKMTEscape` with a jump
for the duration of one getter call, under a process-wide mutex, exactly as
the Python closures do: restore first, inspect on the installing thread
only, forward, re-arm until the power GET is found. While a test resolver
is installed the patch is a flag pair rather than a write (a served fake
escape is a trampoline), and the thunk body is reached through the
`dc_hook_test_entry` seam. The cache (layer 2) validates a transport when it
is used, never when it is set, so fixtures can store one directly.

The current-limit RM operations keep every refusal of the Python original
in its order and text: a SET must select exactly one policy (a generation
policy, or with `any_live_policy` one the last validated table read listed),
a GET refused for its size is a fixed `CurrentLimitUnsupported`, and a failed
escape drops the cached transport and layout choice. The layout choice is
kept as an index into the candidate list rather than a copy of the layout.
The rows, the getter's error bookkeeping and the setter's write, read-back
and restoration are the Python ones line for line. One dependency is not
ported: `read_live_power_request` (the client-power runtime), which
`_current_limit_state` consults to accept a derived clamp on a Turing or
Ampere FE board. The core takes that check as a callback; the binding
supplies it from `Gpu.read_live_power_request`, which is a stub until the
client-power runtime is ported, so natively a derived clamp stays an invalid
record for now rather than being guessed.

The paired target binding is the first step of every PERF, client-power
and clock-BIOS consumer. The core keeps one sticky identity per handle (the
NvAPI object, its handle, the PCI address, devid, subsys and architecture),
reset by `dc_gpu_target_reset` as a fresh Python reader would be, and reports
the Python error kinds `identity`, `transient` and `unsupported` through
`dc_error` (which gained `DC_ERR_IDENTITY` and `DC_ERR_MALFORMED`). One
refusal is ported verbatim and flagged for the user rather than relaxed: a PCI
domain other than 0 is refused although pairing compares bus and device only.
The arbitration decoder is pure: the binding keeps Python's dynamic checks
(a layout, size, command or packet word that is not an exact unsigned 32-bit
int) and maps each to the sentinel the core refuses with Python's reason, so
every reason string comes from the core.
The rail-limit helpers carry each dict value with its Python kind (missing,
None, int, float, an int beyond 2^53, or another object with its truth and
`float()`), so int arithmetic stays int, `min`/`max` keep Python's order
and every place the Python raises returns a status the binding turns back
into the same exception type by replaying the failing operation. Two things
are refused that Python computes: an int sum beyond 2^53 (`OverflowError`)
and arithmetic on numeric objects that are not `int` or `float`
(`TypeError`). `hold_headroom_grain_mv` stays in Python: it depends on
Python's decimal `round`; only its bounds are ported as constants.
The client-power helpers are the pure half of `ClientPowerController`: the
size groups and command tables, the geometry a captured binding implies,
the projection of a request into the CONTROL payloads and the exact-readback
test that tolerates recalculated peer votes on v30 tables. The core
reproduces CPython's own messages for `pack_into`, `unpack_from` and byte
indexing, so a malformed payload raises the same exception natively.
The client-power controller and the PERF reader each keep their own sticky
target binding on the handle, as each Python object keeps its own reader.
Their exchanges run inside a guarded allocation (64 sentinel bytes on each
side of the packet) and report an escape that could not be issued the way
Python does: `_legacy_clk_escape` returns `None` there, so both sides raise
the transient "NT None" / "NTSTATUS None" failure rather than a transport
exception. The PERF transport stays A612-only on purpose: an RM26 transport
is a validated power transport the PERF GETs are not understood to share.
`dc_perf_get` takes the raw header words and escape fields rather than a
`dc_rm_transport`, because Python's `_get` copies whatever header it is given
(a long header spills into the payload), and the binding binds the reader
before re-raising a marshalling error, as Python binds before it builds the
packet. `dc_perf_read_group` runs one group of `read` and takes the handle
lock per attempt, as Python takes `gpu._lock` inside its attempts loop, so a
caller must not hold the lock around it; the decoded snapshot is a fixed-size
struct (512 entries of the largest rows, about 870 KB) the caller allocates,
and the binding adds `captured_at`, the registry name and the per-group
`last_valid` / `last_errors` bookkeeping that Python keeps on the reader. The
digests come from an embedded SHA-256 (`core/sha256`), checked against
`hashlib`.
The client-power metadata read probes the A612 groups by acceptance alone
(a definite size refusal moves to the next group, a transient error selects
nothing and puts the previous size table back) and reports two differing
fresh captures with the `unstable` error kind (`DC_ERR_UNSTABLE`).
The clock-BIOS reader's raw user-clock CONTROL GET runs the same guarded
allocation around the exact 24996-byte NvAPI record and distinguishes the
two Python exception types by return status (`DC_E_FAILED` with a kind for
`PerfDiagnosticError`, `DC_E_UNRECOGNIZED` for `ClockBiosError`). Its INFO
GET goes through the paired A612 escape transport of the PERF reader (the
clock-BIOS reader owns a `PerfDiagnostics` as each Python `read()` constructs
its own `PerfDiagnosticsReader`), and `dc_clock_bios_read` runs the whole
two-attempt, two-sample `read()` with a fresh reader per call, taking the
handle lock per attempt as Python does and dropping the transport cache
after the first attempt under that lock. `_shape`'s checks are not repeated
natively: every one re-validates values the core produced itself.
The rail getters read each rail with its own singleton mask through
`VoltRailsCtlGet` (v2, then v1 only after an incompatible-version status) and
`VoltRailsAbs`, keep the Python caches (the rails each getter has read, the
retry time of a rail that was not read, the last attempt's diagnostics, the
version each getter answered at) and never decide presence from a record's
contents; their diagnostics' `error` is always `None` and `status` always an
int. Python reaches its `error` branch when ctypes converts a Windows
structured exception raised inside the getter into `OSError`; the core, like
every other NVAPI call it makes, has no such guard, so a faulting driver
export is not survived on any native path.

The legacy P0 hold decides support at run time (NVAPI live, `ForcePstate`
present, the NVAPI/NVML pairing intact, a Kepler or Maxwell architecture) and
confirms a hold only from telemetry; `GPU.LEGACY_P0_PROFILES` and
`legacy_p0_profile`, a table of developer boards, are deliberately not ported
(AGENTS.md forbids such tables). `LegacyP0::hold` takes the handle lock around
the request, each telemetry sample and the release, and drops it across the
100 ms waits, so a concurrent telemetry tick is not blocked for the two seconds
the verification may take; Python's `hold_legacy_p0` takes no lock. Ownership
is this session's own request, kept on the handle and never persisted. Python's
`except Exception` branches ("P0 request failed: ...", "P0 verification failed:
...", "P0 release failed: ...") have no native counterpart: the telemetry read
does not throw and a faulting export is not guarded.

Not yet ported: power limit (its NVML half is simple, its live-power half
needs the transport), the rest of the RM escape transport and the controls
built on it (increments 5 to 7 of `DESIGN-TRANSPORT.md`: the fallback
adapter openers for older gdi32 builds; the full policy table of
`_power_policy_rows` and the pin re-application after board-limit writes;
the client-power runtime beyond its full read (the live request wiring and the request SET); and the Python consumers of the transport such as
`_write_rail_records` with its profile checks and `_verify_rail_records`;
`refresh_capabilities`'s live-power and rail resets wait for them), the GPU
restart, and the front end.

Text formatting follows Python's `:g` / `:+.2f` through the C runtime's
`printf`; the rounding of an exact half by an old msvcrt has not been checked
against Python's round-half-even on a legacy Windows installation.
