#!/usr/bin/env bash
# Build the native core for every configuration a Linux host can produce:
#   linux         the Linux library the Python test suite loads through ctypes
#   linux-asan    the same with AddressSanitizer/UBSan for the C++ self-test
#   mingw-x86_64  drutacore.dll + druta-probe.exe for 64-bit Windows, targeting XP x64 SP2 .. 11
#   mingw-i686    the same for 32-bit Windows, targeting XP SP3 .. 11 (and WOW64 on 64-bit)
# After each MinGW build, tools/check_imports.py reads every Windows binary
# (the PE headers and import directory, without external tools) and fails the
# build on any DLL or function not in tools/xp-imports.txt for that machine
# (kernel32.dll and msvcrt.dll alike), on header versions above XP's (5.1 for
# i686, 5.2 for x86_64), and on anything it cannot read. CMake runs the same
# audit as a build step. Passing is a static check of the import tables and
# headers only: start-up on a real XP installation has not been verified; run
# druta-probe.exe there. When Wine is present, the self-test and the
# run-time import harness (druta-runtime-imports-test.exe, which drives the
# shims' fallbacks with the Vista exports hidden) run under it, bounded.
# Usage: native/build.sh [config ...]   (default: all four)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
configs=("$@")
[ ${#configs[@]} -eq 0 ] && configs=(linux linux-asan mingw-x86_64 mingw-i686)
generator=()
command -v ninja >/dev/null 2>&1 && generator=(-G Ninja)
for config in "${configs[@]}"; do
  build="$here/build/$config"
  case "$config" in
    linux-asan) extra=(-DDRUTA_SANITIZE=ON) ;;
    linux) extra=() ;;
    mingw-x86_64|mingw-i686) extra=(-DCMAKE_TOOLCHAIN_FILE="$here/cmake/$config.cmake") ;;
    *) echo "unknown configuration: $config" >&2; exit 2 ;;
  esac
  cmake -S "$here" -B "$build" "${generator[@]}" "${extra[@]}" >/dev/null
  cmake --build "$build"
  if [ "$config" = linux-asan ] || [ "$config" = linux ]; then
    (cd "$build" && ctest --output-on-failure)
    continue
  fi
  python3 "$here/tools/check_imports.py" --quiet "$build"
  if command -v wine >/dev/null 2>&1; then
    # Wine has no NVIDIA driver: this only proves the binaries start and report.
    WINEDEBUG=-all timeout 300 wine "$build/drutacore-selftest.exe"
    # The shims of src/win/runtime_imports.c in their real, fallback, partial
    # and no-ntdll modes; each mode is a child process with its own 60 s bound.
    WINEDEBUG=-all timeout 300 wine "$build/druta-runtime-imports-test.exe"
  fi
done
