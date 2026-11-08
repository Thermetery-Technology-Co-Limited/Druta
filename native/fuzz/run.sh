#!/usr/bin/env bash
# Build and run every fuzzer for a bounded time (default 60 s each) with clang.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
seconds="${1:-60}"
cmake -S "$here" -B "$here/build/fuzz" -G Ninja -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DDRUTA_FUZZ=ON -DDRUTA_SANITIZE=ON >/dev/null
cmake --build "$here/build/fuzz"
mkdir -p "$here/build/fuzz/corpus/rail_transport"
"$here/build/fuzz/rail_transport_fuzz" -max_total_time="$seconds" -max_len=1200 "$here/build/fuzz/corpus/rail_transport"
