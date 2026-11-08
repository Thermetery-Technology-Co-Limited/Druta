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
native/fuzz/run.sh                   # libFuzzer over the rail packet parser (clang)
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

Test seams: `dc_set_export_resolver` serves driver exports (libraries
`nvapi`, `nvml` and `gdi32`) from the test process instead of the DLLs, and
`dc_set_sleep_hook` replaces the bounded waits inside readback loops. Both are
set through `druta.native`.

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

Not yet ported: power limit (its NVML half is simple, its live-power half
needs the transport), the rest of the RM escape transport and the controls
built on it (increments 2 to 7 of `DESIGN-TRANSPORT.md`: the hook, the
power and legacy captures, `RmTransport`; the Turing legacy clock-lock
fallback of `read_clk_lock*` and `refresh_capabilities`'s live-power and
rail resets wait for it), the GPU restart, and the front end.

Text formatting follows Python's `:g` / `:+.2f` through the C runtime's
`printf`; the rounding of an exact half by an old msvcrt has not been checked
against Python's round-half-even on a legacy Windows installation.
