# Druta native core (work in progress)

`native/` holds the C++17 core that is replacing the Python backend one
feature at a time, and the tools built from it. Nothing here is shipped yet.

- `include/drutacore/drutacore.h`: the C ABI. Python (`ctypes`) and the
  future native front end both call this; nothing else crosses the boundary.
- `src/core/`: the pure core (packet layouts, validation, identities). It has
  no Windows calls, so it builds and runs its tests on Linux.
- `src/win/`: run-time binding of Windows DLL exports. The core imports only
  kernel32 and the C runtime; every other export is a capability looked up at
  run time, so one binary runs on Windows XP through 11.
- `src/probe/`: `druta-probe.exe`, a read-only capability report for one
  Windows installation (see the file header). Run it on a legacy machine and
  send back `druta-probe-report.txt`.

## Build

```
native/build.sh                      # linux-asan, mingw-x86_64, mingw-i686
native/build.sh mingw-i686           # one configuration
```

Linux needs cmake, ninja, g++ or clang++, and `mingw-w64` for the Windows
targets; Wine is optional and only smoke-runs the Windows binaries. On a
Windows host, `cmake -S native -B native/build/msvc` works with MSVC; the
`v141_xp` toolset is required for a binary that runs on XP.

The Windows binaries link the C runtime statically and import only
`KERNEL32.dll` and `msvcrt.dll` (PE subsystem version 5.1 = XP). Whether the
MinGW-w64 runtime itself still starts on XP has not been verified on an XP
installation; the probe is the first thing to run there.
