#!/usr/bin/env bash
# Build the native core for every configuration a Linux host can produce:
#   linux         the Linux library the Python test suite loads through ctypes
#   linux-asan    the same with AddressSanitizer/UBSan for the C++ self-test
#   mingw-x86_64  drutacore.dll + druta-probe.exe for 64-bit Windows (XP x64 .. 11)
#   mingw-i686    the same for 32-bit Windows (XP x86, and WOW64 on 64-bit)
# Usage: native/build.sh [config ...]   (default: all three)
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
  elif command -v wine >/dev/null 2>&1; then
    # Wine has no NVIDIA driver: this only proves the binaries start and report.
    WINEDEBUG=-all wine "$build/drutacore-selftest.exe"
  fi
done
