/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "tools/live_curve_readback.h"
#include <cstdio>
#include <cstdlib>
#include <limits>

namespace {
using namespace druta::ui;
using namespace druta::ui::live_check;

void check(bool result, const char* message) {
    if (!result) {
        std::fprintf(stderr, "live curve readback: %s\n", message);
        std::exit(1);
    }
}
}

int main() {
    LiveData baseline{};
    baseline.curve_freq_div = 1;
    baseline.curve = {{4, 900.0, 1605.0, 15000}, {5, 906.25, 1605.0, 0}};

    auto restored = baseline;
    restored.curve[1].freq_mhz = 1590.0;
    std::swap(restored.curve[0], restored.curve[1]);
    check(same_curve(baseline, restored, {}, CurveMatch::requests),
          "Core Undo restored the configured rows despite a later evaluated clock change");
    check(!same_curve(baseline, restored), "Evaluated curve drift is still reported");

    restored.curve[0].delta_khz = 15000;
    check(!same_curve(baseline, restored, {}, CurveMatch::requests), "A changed raw delta must fail");
    restored = baseline;
    restored.curve[0].volt_mv += 6.25;
    check(!same_curve(baseline, restored, {}, CurveMatch::requests), "A changed voltage mapping must fail");
    restored = baseline;
    restored.curve[0].idx = 6;
    check(!same_curve(baseline, restored, {}, CurveMatch::requests), "A changed row index must fail");
    restored.curve[0].idx = restored.curve[1].idx;
    check(!same_curve(baseline, restored, {}, CurveMatch::requests), "A duplicate row must fail");
    restored = baseline;
    restored.curve.pop_back();
    check(!same_curve(baseline, restored, {}, CurveMatch::requests), "An incomplete curve must fail");
    restored = baseline;
    restored.curve_freq_div = 2;
    check(!same_curve(baseline, restored, {}, CurveMatch::requests), "A changed frequency divisor must fail");
    restored = baseline;
    restored.curve[0].freq_mhz = std::numeric_limits<double>::infinity();
    check(!same_curve(baseline, restored, {}, CurveMatch::requests), "An invalid frequency sample must fail");
    return 0;
}
