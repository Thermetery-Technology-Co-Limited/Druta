#!/usr/bin/env bash
# Build and run every fuzzer for a bounded time (default 60 s each) with clang.
# Usage: run.sh [seconds] [libFuzzer flags...]; extra flags go to the fuzzer
# (e.g. -rss_limit_mb=2048). DRUTA_FUZZ_CORPUS overrides the corpus directory.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
seconds="${1:-60}"
corpus="${DRUTA_FUZZ_CORPUS:-$here/build/fuzz/corpus/rail_transport}"
cmake -S "$here" -B "$here/build/fuzz" -G Ninja -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DDRUTA_FUZZ=ON -DDRUTA_SANITIZE=ON >/dev/null
cmake --build "$here/build/fuzz"
mkdir -p "$corpus"
"$here/build/fuzz/rail_transport_fuzz" -max_total_time="$seconds" -max_len=1200 "${@:2}" "$corpus"
