#!/usr/bin/env bash
# Build and run every fuzzer for a bounded time (default 60 s each) with clang.
# Usage: run.sh [seconds] [libFuzzer flags...]; extra flags go to the fuzzer
# (e.g. -rss_limit_mb=2048). DRUTA_FUZZ_CORPUS overrides the corpus root directory.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
seconds="${1:-60}"
corpora="${DRUTA_FUZZ_CORPUS:-$here/build/fuzz/corpus}"
cmake -S "$here" -B "$here/build/fuzz" -G Ninja -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
  -DDRUTA_FUZZ=ON -DDRUTA_SANITIZE=ON >/dev/null
cmake --build "$here/build/fuzz"
# Each fuzzer keeps its own corpus directory under $corpora. The PERF decoder takes whole INFO and
# CONTROL packets (up to 0x7A8A + 0x4C98 bytes), so its inputs must be allowed to grow that large.
mkdir -p "$corpora/rail_transport" "$corpora/perf_decode"
"$here/build/fuzz/rail_transport_fuzz" -max_total_time="$seconds" -max_len=1200 "${@:2}" "$corpora/rail_transport"
"$here/build/fuzz/perf_decode_fuzz" -max_total_time="$seconds" -max_len=130000 -len_control=0 "${@:2}" \
  "$corpora/perf_decode"
