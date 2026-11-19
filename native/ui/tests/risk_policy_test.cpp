/* Druta - Copyright (C) 2026 Thermetery Technology Co Limited
 * SPDX-License-Identifier: GPL-3.0-or-later */
#include "risk_policy.h"
#include <cstdio>
#include <cstdlib>
using namespace druta::ui;
static unsigned checks = 0;
#define CHECK(expr) do { ++checks; if (!(expr)) { std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #expr); std::abort(); } } while (false)
int main() {
    const auto on = RiskFlag::active, off = RiskFlag::inactive, unknown = RiskFlag::unknown;
    const RiskBand bands[] = {RiskBand::green, RiskBand::yellow, RiskBand::amber,
                              RiskBand::red, RiskBand::red, RiskBand::crimson,
                              RiskBand::crimson, RiskBand::crimson};
    const unsigned scores[] = {0, 1, 2, 3, 3, 4, 5, 6};
    const char* names[] = {"GREEN", "YELLOW", "AMBER", "RED", "RED", "CRIMSON", "CRIMSON", "CRIMSON"};
    for (unsigned mask = 0; mask < 8; ++mask) {
        RiskGates gates{mask & 1 ? on : off, mask & 2 ? on : off, on,
                        mask & 4 ? on : off, on};
        auto r = assess_risk(gates);
        CHECK(r.score == scores[mask] && r.possible_score == scores[mask]);
        CHECK(r.band == bands[mask] && r.name == names[mask] && !r.incomplete);
    }
    auto green = assess_risk({off, off, on, off, on});
    auto yellow = assess_risk({on, off, on, off, on});
    auto amber = assess_risk({off, on, on, off, on});
    auto red = assess_risk({off, off, on, on, on});
    auto crimson = assess_risk({on, off, on, on, on});
    CHECK(green.tint.background.g > green.tint.background.r && green.tint.text.g > green.tint.background.g);
    CHECK(yellow.tint.border.g != amber.tint.border.g && yellow.tint.text.b != amber.tint.text.b);
    CHECK(red.tint.background.g == 16 && red.tint.border.r == 208 && red.tint.text.g == 120);
    CHECK(crimson.tint.background.b == 26 && crimson.tint.border.b == 60 && crimson.tint.text.b == 140);
    CHECK(crimson.reason.find("board-controller") < crimson.reason.find("XOC"));
    auto r = assess_risk({off, off, unknown, off, unknown});
    CHECK(r.band == RiskBand::green && !r.incomplete); // disabled mode cannot be active
    r = assess_risk({unknown, off, off, off, off});
    CHECK(r.band == RiskBand::unknown && r.score == 0 && r.possible_score == 1 && r.incomplete);
    CHECK(r.name == "UNKNOWN" && r.reason.find("unknown") != std::string::npos);
    r = assess_risk({on, on, unknown, on, unknown});
    CHECK(r.band == RiskBand::yellow && r.score == 1 && r.possible_score == 6 && r.incomplete);
    CHECK(r.reason.find("XOC") != std::string::npos && r.reason.find("unknown") != std::string::npos);
    r = assess_risk({off, on, off, on, off});
    CHECK(r.band == RiskBand::green && !r.incomplete); // inaccessible controller/rail
    r = assess_risk({off, unknown, on, off, on});
    CHECK(r.band == RiskBand::unknown && r.possible_score == 2 && r.rail_limits == unknown);
    r = assess_risk({off, off, on, unknown, on});
    CHECK(r.band == RiskBand::unknown && r.possible_score == 3 && r.i2c == unknown);
    const auto before = assess_risk({off, off, on, on, on}); // transient presence is not a gate input
    const auto after = assess_risk({off, off, on, on, on});
    CHECK(before.band == RiskBand::red && after.band == RiskBand::red);
    std::printf("risk_policy_test: %u checks passed\n", checks);
}
