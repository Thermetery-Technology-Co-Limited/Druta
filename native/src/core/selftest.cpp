// Druta - a monitor and tuner for NVIDIA GPUs.
// Copyright (C) 2026 Thermetery Technology Co Limited
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Dependency-free self-test of the pure core. The Python suite is the main
// test bed (it runs the same cases through the C ABI); this catches a broken
// cross-build before anything is packaged.
#include <cstdio>
#include <string>

#include "core/slot.h"
#include "drutacore/drutacore.h"

// ABI mask values must survive the signed-32-bit boundary on every compiler.
// In particular, an MSVC enum must not truncate the lock bits or sign-extend energy.
static_assert(DC_T_ENERGY_J == UINT64_C(0x80000000));
static_assert(DC_T_VF_LOCKED == UINT64_C(0x100000000));
static_assert(DC_T_VF_LOCK_MV == UINT64_C(0x200000000));
static_assert(DC_T_CLK_LOCK == UINT64_C(0x400000000));
static_assert((DC_T_ENERGY_J | DC_T_VF_LOCKED | DC_T_VF_LOCK_MV | DC_T_CLK_LOCK) == UINT64_C(0x780000000));

namespace {
int failures = 0;

void check(bool ok, const char* what) {
    if (!ok) {
        ++failures;
        std::printf("FAIL: %s\n", what);
    }
}
}  // namespace

int main() {
    using druta::format_slot;
    using druta::parse_slot;
    using druta::same_slot;

    check(format_slot(0, 1, 0, 0) == "0000:01:00.0", "format_slot canonical");
    check(format_slot(0x10, 0xab, 0x1f, 7) == "0010:ab:1f.7", "format_slot lower-case hex");

    auto slot = parse_slot("0000:2a:00.0");
    check(slot && slot->domain == 0 && slot->bus == 0x2a && slot->device == 0 && slot->function == 0,
          "parse_slot canonical");
    slot = parse_slot("00000000:01:00.0");
    check(slot && slot->bus == 1, "parse_slot NVML 8-digit domain");
    slot = parse_slot("  0000:01:00.0\n");
    check(slot && slot->bus == 1, "parse_slot strips whitespace");
    check(parse_slot("0000:01:00.f").has_value(), "parse_slot hex function digit");
    check(!parse_slot("").has_value(), "parse_slot empty");
    check(!parse_slot("000:01:00.0").has_value(), "parse_slot short domain");
    check(!parse_slot("000000000:01:00.0").has_value(), "parse_slot long domain");
    check(!parse_slot("0000:1:00.0").has_value(), "parse_slot short bus");
    check(!parse_slot("0000:01:00.00").has_value(), "parse_slot long function");
    check(!parse_slot("0000:01:00").has_value(), "parse_slot missing function");
    check(!parse_slot("0000-01-00.0").has_value(), "parse_slot wrong separators");
    check(!parse_slot("0000:01:00.0 x").has_value(), "parse_slot trailing text");

    check(same_slot("0000:01:00.0", "00000000:01:00.0"), "same_slot ignores domain spelling");
    check(same_slot("0001:01:00.0", "0000:01:00.0"), "same_slot ignores domain value");
    check(!same_slot("0000:01:00.0", "0000:02:00.0"), "same_slot bus differs");
    check(!same_slot("0000:01:00.0", "0000:01:01.0"), "same_slot device differs");
    check(same_slot("0000:01:00.0", "0000:01:00.1"), "same_slot ignores function");
    check(!same_slot("", "0000:01:00.0"), "same_slot unparsable");

    if (failures) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("drutacore self-test passed\n");
    return 0;
}
