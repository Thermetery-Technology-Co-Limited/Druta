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

Test seams: `dc_set_export_resolver` serves driver exports from the test
process instead of the DLLs, and `dc_set_sleep_hook` replaces the bounded
waits inside readback loops. Both are set through `druta.native`.

Not yet ported: clock offsets and P-state writes, power limit, VF and clock
locks, voltage boost, clock-domain naming, the RM escape transport and the
controls built on it (see `DESIGN-TRANSPORT.md` once it exists), the
GPU restart, and the front end.
